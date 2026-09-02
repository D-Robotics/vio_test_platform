"""Record the ov_web visualization WebSocket stream to an MP4 video.

Connects to ws://<board_ip>:9988/ws, receives binary frames ([0]=JPEG,
[1]=JSON per ov_web/include/ov_web/Protocol.h), keeps the JPEG frames on
disk, then concatenates them into an MP4 with ffmpeg (board has ffmpeg; we
run it over SSH — or locally on the host if ffmpeg exists there).
"""
import asyncio
import glob
import os
import subprocess
import threading
import time


class OvWebRecorder:
    """Records one backtest run's ov_web stream into <outdir>/frames/ + video.mp4."""

    def __init__(self, ip: str, port: int, outdir: str, max_seconds: int = 900):
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

    # ---------------------------------------------------------- lifecycle
    def start_async(self):
        os.makedirs(self.frames_dir, exist_ok=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

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
        except Exception:  # noqa: BLE001 - recording must never break the batch
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
                        idx = self.frame_count
                        with open(os.path.join(self.frames_dir, f"f{idx:06d}.jpg"), "wb") as f:
                            f.write(bytes(msg[1:]))
                        self.frame_count += 1
                    elif msg[0] == 1:  # JSON state
                        try:
                            self.json_last = bytes(msg[1:]).decode("utf-8", "replace")
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
