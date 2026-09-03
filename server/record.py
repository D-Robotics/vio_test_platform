"""Record the ov_web visualization WebSocket stream to an MP4 video.

Connects to ws://<board_ip>:9988/ws, receives binary frames ([0]=JPEG,
[1]=JSON per ov_web/include/ov_web/Protocol.h), keeps the JPEG frames on
disk, then concatenates them into an MP4 with ffmpeg (board has ffmpeg; we
run it over SSH — or locally on the host if ffmpeg exists there).
"""
import asyncio
import glob
import io
import json
import os
import subprocess
import sys
import threading
import time

from PIL import Image, ImageDraw


class OvWebRecorder:
    """Records one backtest run's ov_web stream into <outdir>/frames/ + video.mp4."""

    def __init__(self, ip: str, port: int, outdir: str, max_seconds: int = 900, minimap: bool = True):
        self.ip = ip
        self.port = port
        self.outdir = outdir
        self.max_seconds = max_seconds
        self.frames_dir = os.path.join(outdir, "frames")
        self.video_path = os.path.join(outdir, "video.mp4")
        self.frame_count = 0
        self.json_last = ""
        self._stop = threading.Event()
        self._thread = None
        # Trajectory minimap overlay (server-side). True = composite a top-down map
        # of the odom-frame path into a corner of each frame; False = legacy raw JPEG.
        self.minimap = minimap
        self.minimap_size = 180
        self.traj_points = []  # [(x, y), ...] odom-frame XY trajectory
        self.has_path = False  # True once a full `path` array was received
        self.latest_pos = (0.0, 0.0)  # current robot position (odom XY)
        self._border = 12  # corner margin when pasting the minimap

    # ---------------------------------------------------------- lifecycle
    def start_async(self):
        os.makedirs(self.frames_dir, exist_ok=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---------------------------------------------------------- trajectory minimap
    def _update_state(self, raw: str):
        """Parse one ov_web JSON state message into a 2D odom-frame trajectory."""
        try:
            st = json.loads(raw)
        except Exception:  # noqa: BLE001 - malformed frame; keep prior trajectory
            return
        path = st.get("path")
        # Full `path` array is authoritative (already in odom frame, downsample <=500).
        if isinstance(path, list) and len(path) >= 2 and isinstance(path[0], (list, tuple)):
            self.traj_points = [(float(p[0]), float(p[1])) for p in path]
            self.has_path = True
        if isinstance(path, list) and path:
            self.latest_pos = (float(path[-1][0]), float(path[-1][1]))
        if "opx" in st and "opy" in st:
            self.latest_pos = (float(st["opx"]), float(st["opy"]))
            if not self.has_path:
                # Fallback: accumulate from odom positions, dedup consecutive jitter.
                last = self.traj_points[-1] if self.traj_points else None
                if last is None or abs(last[0] - self.latest_pos[0]) > 1e-6 or abs(last[1] - self.latest_pos[1]) > 1e-6:
                    self.traj_points.append(self.latest_pos)
                if len(self.traj_points) > 2000:
                    self.traj_points = self.traj_points[-2000:]

    def _render_minimap(self) -> "Image.Image | None":
        """Render a top-down trajectory map as an RGBA overlay (fit-all, y flipped)."""
        pts = self.traj_points
        if len(pts) < 1 or not self.minimap:
            return None
        S = self.minimap_size
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        span_x = max(x1 - x0, 1e-6)
        span_y = max(y1 - y0, 1e-6)
        pad = 0.25 * max(span_x, span_y)
        x0 -= pad
        x1 += pad
        y0 -= pad
        y1 += pad
        span_x = max(x1 - x0, 1e-6)
        span_y = max(y1 - y0, 1e-6)
        m = 10  # inner margin
        sc = (S - 2 * m) / max(span_x, span_y)

        def to_px(x, y):
            u = m + (x - x0) * sc
            v = S - m - (y - y0) * sc  # y-axis flipped (odom X fwd, Y left)
            return (u, v)

        ov = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        d.rounded_rectangle([0, 0, S - 1, S - 1], radius=6, fill=(20, 20, 20, 150),
                            outline=(0, 255, 0, 160), width=1)
        for k in range(1, 4):  # light 4-cell grid
            d.line([(int(S * k / 4.0), 0), (int(S * k / 4.0), S - 1)], fill=(60, 60, 60, 60), width=1)
            d.line([(0, int(S * k / 4.0)), (S - 1, int(S * k / 4.0))], fill=(60, 60, 60, 60), width=1)
        if len(pts) >= 2:
            d.line([to_px(*p) for p in pts], fill=(0, 255, 0, 255), width=2)
        cx, cy = to_px(*self.latest_pos)
        r = 4
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 200, 255, 255),
                  outline=(255, 255, 255, 255), width=1)
        sx, sy = to_px(*pts[0])
        d.ellipse([sx - 3, sy - 3, sx + 3, sy + 3], outline=(255, 255, 0, 255), width=2)
        return ov

    def stop_and_mux(self, fps: int = 15) -> "str | None":
        """Stop recording and mux frames into video.mp4; returns path or None."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self.frame_count == 0:
            return None
        # ffmpeg on the host if available, else on the board via SSH
        try:
            return self._mux_local(fps)
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None

    # ---------------------------------------------------------- internals
    def _run(self):
        try:
            asyncio.run(self._record())
        except Exception as e:  # noqa: BLE001 - recording must never break the batch
            # Never let recording break the batch, but DON'T hide the failure:
            # the lazy `import websockets` inside _record() used to die here and
            # silently yield 0 frames -> no video in stats. Surface it next to
            # the run and on stderr so a missing dep / bad connection is visible.
            self.last_error = f"{type(e).__name__}: {e}"
            print(f"[record] {self.last_error}", file=sys.stderr, flush=True)
            try:
                os.makedirs(self.outdir, exist_ok=True)
                with open(os.path.join(self.outdir, "record_error.txt"), "w") as f:
                    f.write(self.last_error)
            except Exception:  # noqa: BLE001
                pass

    async def _record(self):
        import websockets

        deadline = time.time() + self.max_seconds
        uri = f"ws://{self.ip}:{self.port}/ws"
        # Outer reconnect loop: ov_web may not be up yet when the batch starts,
        # and the WS can drop mid-run (ov_web restart / network hiccup). Without
        # reconnect the recording silently stops early → video covers only the
        # first seconds of the run.
        while not self._stop.is_set() and time.time() < deadline:
            try:
                ws = await websockets.connect(uri, max_size=None, open_timeout=5)
            except Exception:  # noqa: BLE001 - not up yet; retry until deadline
                await asyncio.sleep(3)
                continue
            try:
                while not self._stop.is_set() and time.time() < deadline:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    except (asyncio.TimeoutError, TimeoutError):
                        continue
                    if not isinstance(msg, (bytes, bytearray)) or len(msg) < 2:
                        continue
                    if msg[0] == 0:  # JPEG
                        raw = bytes(msg[1:])
                        buf_out = raw
                        mm = self._render_minimap()
                        if mm is not None:
                            im = Image.open(io.BytesIO(raw)).convert("RGB")
                            im.paste(mm, (im.width - mm.width - self._border,
                                          im.height - mm.height - self._border), mm)
                            out = io.BytesIO()
                            im.save(out, "JPEG", quality=90)
                            buf_out = out.getvalue()
                        with open(os.path.join(self.frames_dir, f"f{self.frame_count:06d}.jpg"), "wb") as f:
                            f.write(buf_out)
                        self.frame_count += 1
                    elif msg[0] == 1:  # JSON state
                        try:
                            self.json_last = bytes(msg[1:]).decode("utf-8", "replace")
                            self._update_state(self.json_last)
                        except Exception:  # noqa: BLE001
                            pass
            except Exception:  # noqa: BLE001 - connection dropped → reconnect
                pass
            finally:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass

    def _mux_local(self, fps: int) -> str:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-framerate", str(fps),
            "-i", os.path.join(self.frames_dir, "f%06d.jpg"),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23",
            self.video_path,
        ]
        subprocess.run(cmd, check=True, timeout=300)
        # frames are inside the video now; drop them to save disk
        import shutil

        shutil.rmtree(self.frames_dir, ignore_errors=True)
        return self.video_path
