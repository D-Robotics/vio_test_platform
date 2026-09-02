"""Dataset scanning, bag metadata parsing and mcap message reading."""
import glob
import json
import os
import threading
import time
from dataclasses import dataclass, field

import yaml

from . import config

_lock = threading.Lock()
_cache = {}  # name -> {"mtime", "info"}
_scan_cache = {"key": None, "datasets": None}


# ---------------------------------------------------------------- scanning
def _dir_size(path: str) -> int:
    total = 0
    for p in glob.glob(os.path.join(path, "**", "*"), recursive=True):
        try:
            if os.path.isfile(p):
                total += os.path.getsize(p)
        except OSError:
            pass
    return total


def scan_datasets(refresh: bool = False):
    """Find dataset dirs (contain ros2bag_vio/ and/or stereo_auto_gen/) under DATA_ROOT."""
    key = config.DATA_ROOT
    if not refresh and _scan_cache["key"] == key and _scan_cache["datasets"] is not None:
        return _scan_cache["datasets"]

    root = config.DATA_ROOT
    out = []
    if os.path.isdir(root):
        base_depth = root.rstrip("/").count("/")
        for dirpath, dirnames, _ in os.walk(root):
            depth = dirpath.rstrip("/").count("/") - base_depth
            if depth >= config.SCAN_DEPTH:
                dirnames[:] = []
                continue
            # skip heavy/irrelevant trees
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("log", "build", "install", ".venv", "src")]
            if "ros2bag_vio" in dirnames or "stereo_auto_gen" in dirnames:
                rel = os.path.relpath(dirpath, root)
                out.append(
                    {
                        "name": rel,
                        "path": dirpath,
                        "has_bag": "ros2bag_vio" in dirnames,
                        "has_config": "stereo_auto_gen" in dirnames,
                    }
                )
                dirnames[:] = []  # do not descend into a dataset
    out.sort(key=lambda d: d["name"])
    with _lock:
        _scan_cache["key"] = key
        _scan_cache["datasets"] = out
    return out


def get_dataset(name: str):
    for d in scan_datasets():
        if d["name"] == name:
            return d
    raise FileNotFoundError(f"dataset not found: {name}")


def delete_dataset(name: str) -> dict:
    """Delete a dataset directory by name. Refuses paths outside DATA_ROOT."""
    import os as _os
    import shlex
    base = _os.path.join(config.DATA_ROOT, name)
    base_abs = _os.path.realpath(base)
    root_abs = _os.path.realpath(config.DATA_ROOT)
    # base_abs must be inside root_abs (and not equal to root)
    if base_abs == root_abs or not base_abs.startswith(root_abs.rstrip("/") + "/"):
        raise ValueError(f"refusing to delete path outside DATA_ROOT: {name}")
    if not _os.path.isdir(base_abs):
        raise FileNotFoundError(f"dataset not found: {name}")
    import shutil
    shutil.rmtree(base_abs)
    # invalidate scan cache so removal is reflected immediately
    _scan_cache["key"] = None
    return {"ok": True, "removed": base_abs}


def delete_parent_dir(parent: str) -> dict:
    """Delete a top-level parent directory under DATA_ROOT.

    `parent` is the top-level folder name (or "./" → refused, would erase
    the root). Refuses paths outside DATA_ROOT or that escape via ../.
    """
    import os as _os
    import shutil
    parent = (parent or "").strip().strip("/")
    if not parent or parent == "." or parent == "./":
        raise ValueError("refusing to delete DATA_ROOT (parent is empty or './')")
    if ".." in parent.split("/"):
        raise ValueError(f"refusing to delete path with '..': {parent}")
    base = _os.path.join(config.DATA_ROOT, parent)
    base_abs = _os.path.realpath(base)
    root_abs = _os.path.realpath(config.DATA_ROOT)
    if base_abs == root_abs or not base_abs.startswith(root_abs.rstrip("/") + "/"):
        raise ValueError(f"refusing to delete path outside DATA_ROOT: {parent}")
    if not _os.path.isdir(base_abs):
        raise FileNotFoundError(f"parent directory not found: {parent}")
    shutil.rmtree(base_abs)
    _scan_cache["key"] = None
    return {"ok": True, "removed": base_abs}


# ---------------------------------------------------------------- bag info
@dataclass
class BagInfo:
    topics: list = field(default_factory=list)  # [{name,type,count}]
    start_ns: int = 0
    end_ns: int = 0
    message_count: int = 0
    storage: str = ""
    error: str = ""


def _find_bag_file(bag_dir: str) -> str:
    for ext in ("*.mcap", "*.db3"):
        hits = sorted(glob.glob(os.path.join(bag_dir, ext)))
        if hits:
            return hits[0]
    return ""


def bag_info(name: str) -> dict:
    """Parse metadata.yaml of the dataset's ros2bag_vio bag; cached by mtime."""
    ds = get_dataset(name)
    bag_dir = os.path.join(ds["path"], "ros2bag_vio")
    meta_path = os.path.join(bag_dir, "metadata.yaml")
    if not os.path.isfile(meta_path):
        return {"error": "no ros2bag_vio/metadata.yaml", "topics": [], "message_count": 0}

    mtime = os.path.getmtime(meta_path)
    with _lock:
        c = _cache.get(name)
        if c and c["mtime"] == mtime:
            return c["info"]

    info = {"topics": [], "start_ns": 0, "end_ns": 0, "message_count": 0, "storage": "", "size_bytes": _dir_size(bag_dir), "error": ""}
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = yaml.safe_load(f) or {}
        bd = (meta.get("rosbag2_bagfile_information") or {})
        info["storage"] = bd.get("storage_identifier", "")
        info["message_count"] = int(bd.get("message_count", 0))
        start = bd.get("starting_time", {}).get("nanoseconds_since_epoch", 0)
        dur = bd.get("duration", {}).get("nanoseconds", 0)
        info["start_ns"] = int(start)
        info["end_ns"] = int(start) + int(dur)
        topics = []
        for t in bd.get("topics_with_message_count", []):
            tm = t.get("topic_metadata", {}) or {}
            topics.append(
                {
                    "name": tm.get("name", ""),
                    "type": tm.get("type", ""),
                    "count": int(t.get("message_count", 0)),
                    "serialization": tm.get("serialization_format", ""),
                }
            )
        info["topics"] = topics
    except Exception as e:  # noqa: BLE001
        info["error"] = f"metadata parse failed: {e}"
    with _lock:
        _cache[name] = {"mtime": mtime, "info": info}
    return info


def pick_default_topics(name: str) -> dict:
    """Choose image + imu topics from bag metadata.

    The combined stereo topic is ALWAYS `/sub_image_combine_raw` on the rig; the
    VIO derives the CompressedImage variant (`raw` -> `jpeg` in ROS2Visualizer).
    So we return the canonical RAW name for `image` regardless of how the bag
    recorded it, and only flag `image_compressed` so the launch sets
    sub_from_compressed_image:=True for CompressedImage bags.
    """
    info = bag_info(name)
    img, img_compressed, imu = config.CANONICAL_STEREO_TOPIC, False, None
    for t in info.get("topics", []):
        ty = t["type"].split("/")[-1]
        if ty == "CompressedImage":
            img_compressed = True
        elif ty == "Image":
            pass  # canonical name already set
        elif ty == "Imu" and imu is None:
            imu = t["name"]
    return {"image": img, "image_compressed": img_compressed, "imu": imu}


def pick_frame_topic(name: str) -> "str | None":
    """Return an image topic name that ACTUALLY holds frames in this bag.

    `pick_default_topics` returns the canonical RAW name (/sub_image_combine_raw)
    which is what the VIO launch needs — but that topic does not exist in a
    CompressedImage-only bag, so it can't be used to *read* a frame for previews
    / thumbnails. This resolves the real topic (Image or CompressedImage) that is
    present in this specific bag, or None if there is none.
    """
    for t in bag_info(name).get("topics", []):
        ty = t["type"].split("/")[-1]
        if ty in ("Image", "CompressedImage"):
            return t["name"]
    return None


# ---------------------------------------------------------------- mcap reading
def _mcap_messages(bag_dir: str, topic: str, limit: int = 0, indices: "set[int] | None" = None):
    """Yield (index, timestamp, schema_name, decoded_dict_or_bytes) for a topic.

    Decoding requires the mcap-ros2-support decoder; on failure raw bytes are
    yielded with schema name so callers can degrade gracefully.
    """
    from mcap_ros2.decoder import DecoderFactory
    from mcap.reader import make_reader

    bag_file = _find_bag_file(bag_dir)
    if not bag_file or not bag_file.endswith(".mcap"):
        raise RuntimeError("only mcap bags are supported for message reading")

    idx = -1
    with open(bag_file, "rb") as f:
        reader = make_reader(f, decoder_factories=[DecoderFactory()])
        for schema, channel, message, decoded in reader.iter_decoded_messages(topics=[topic]):
            idx += 1
            if indices is not None and idx not in indices:
                continue
            yield idx, message.log_time, channel.message_encoding, schema.name if schema else "", decoded
            if limit and idx + 1 >= limit:
                return


def _time_index_all(name: str) -> dict:
    """{topic: [log_time_ns, ...]} for every topic in the bag, built in ONE mcap pass.

    Disk-cached at frame_cache/<ds>/time_index.json keyed by bag mtime, so the
    player's seek index is built once per bag (not once per server process) and
    re-opening the preview is instant. An in-memory layer on top avoids
    re-reading the JSON on every player tick.
    """
    import json

    from mcap.reader import make_reader

    mtime = _bag_mtime(name)
    if not mtime:
        return {}
    mem_key = f"tidx_all:{name}"
    with _lock:
        c = _cache.get(mem_key)
        if c and c["mtime"] == mtime:
            return c["info"]

    idx_path = os.path.join(config.FRAME_CACHE_DIR, name.replace("/", "__"), "time_index.json")
    try:
        if os.path.isfile(idx_path):
            with open(idx_path) as f:
                data = json.load(f)
            if data.get("bag_mtime") == mtime and isinstance(data.get("topics"), dict):
                with _lock:
                    _cache[mem_key] = {"mtime": mtime, "info": data["topics"]}
                return data["topics"]
    except Exception:  # noqa: BLE001 — fall through to rebuild
        pass

    ds = get_dataset(name)
    bag_dir = os.path.join(ds["path"], "ros2bag_vio")
    bag_file = _find_bag_file(bag_dir)
    if not bag_file or not bag_file.endswith(".mcap"):
        return {}
    topics = {}
    with open(bag_file, "rb") as f:
        reader = make_reader(f)
        for _schema, channel, message in reader.iter_messages():
            topics.setdefault(channel.topic, []).append(message.log_time)
    try:
        os.makedirs(os.path.dirname(idx_path), exist_ok=True)
        tmp = idx_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"bag_mtime": mtime, "topics": topics}, f)
        os.replace(tmp, idx_path)
    except OSError:
        pass
    with _lock:
        _cache[mem_key] = {"mtime": mtime, "info": topics}
    return topics


def _time_index(name: str, topic: str) -> list:
    """Sorted list of (log_time_ns) for a topic.

    Served from the bag-wide disk-cached index (_time_index_all); the player
    uses it to map bag time -> nearest message index per topic for seeking.
    """
    return _time_index_all(name).get(topic, [])


def _topic_type(name: str, topic: str) -> str:
    for t in bag_info(name).get("topics", []):
        if t["name"] == topic:
            return t["type"].split("/")[-1]
    return ""


def image_frame_jpeg(name: str, topic: str, index: int = 0):
    """Return (jpeg_bytes, ts_ns). CompressedImage -> passthrough; Image -> PIL convert.

    Prefers the on-disk frame cache (O(1)) over an mcap scan (O(index)).
    """
    ds = get_dataset(name)
    bag_dir = os.path.join(ds["path"], "ros2bag_vio")
    ty = _topic_type(name, topic)
    if ty not in ("CompressedImage", "Image"):
        raise RuntimeError(f"topic {topic} is not an image topic (type={ty})")

    cache_file = os.path.join(_frame_cache_dir(name, topic), f"{index:06d}.jpg")
    if os.path.isfile(cache_file):
        with open(cache_file, "rb") as f:
            jpeg = f.read()
        ts = 0
        times = _time_index(name, topic)
        if 0 <= index < len(times):
            ts = times[index]
        return jpeg, ts

    for idx, ts, _, _, decoded in _mcap_messages(bag_dir, topic, indices={index}):
        if idx != index:
            continue
        return _decoded_to_jpeg(decoded, ty), ts
    raise RuntimeError(f"frame {index} not found on {topic}")


def image_frame_count(name: str, topic: str) -> int:
    for t in bag_info(name).get("topics", []):
        if t["name"] == topic:
            return t["count"]
    return 0


def thumbnail_jpeg(name: str, max_w: int = 160) -> bytes:
    """Random frame thumbnail (small JPEG). Picks a random image topic + frame.

    Returns empty bytes if the bag has no image topic.
    Result is cached on disk (keyed by bag mtime): picking a random frame costs
    an O(N) mcap scan, and the dataset list renders one thumbnail per card.
    """
    import random

    from io import BytesIO

    from PIL import Image

    topic = pick_frame_topic(name)
    if not topic:
        return b""
    count = image_frame_count(name, topic)
    if count <= 0:
        return b""
    thumb_path = os.path.join(_frame_cache_dir(name, topic), "thumb.jpg")
    try:
        if os.path.isfile(thumb_path) and os.path.getmtime(thumb_path) >= _bag_mtime(name):
            with open(thumb_path, "rb") as f:
                return f.read()
    except OSError:
        pass
    index = random.randint(0, count - 1)
    jpeg, _ts = image_frame_jpeg(name, topic, index)
    # downscale to max_w for fast thumbnail
    out = jpeg
    try:
        im = Image.open(BytesIO(jpeg))
        ratio = max_w / im.width
        if ratio < 1.0:
            im = im.resize((max_w, max(1, int(im.height * ratio))))
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=70)
        out = buf.getvalue()
    except Exception:  # noqa: BLE001
        pass  # fallback to the original frame bytes
    try:
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        tmp = thumb_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(out)
        os.replace(tmp, thumb_path)
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- upload
REQUIRED_SUBDIRS = ("ros2bag_vio", "stereo_auto_gen")


def upload_file(parent: str, name: str, rel_path: str, data: bytes) -> dict:
    """Write one uploaded file under DATA_ROOT/<parent>/<name>/<rel_path>.

    rel_path is the relative path within the dataset folder (e.g.
    "ros2bag_vio/metadata.yaml"). Path traversal (.. or absolute) is rejected.
    """
    import os
    import shlex

    safe_parent = os.path.basename(parent.strip().strip("/")) or "uploaded"
    safe_name = os.path.basename(name.strip().strip("/")) or "dataset"
    # reject obvious traversal attempts
    if rel_path != os.path.normpath(rel_path).lstrip("./") or ".." in rel_path.split("/"):
        raise ValueError(f"unsafe relative path: {rel_path}")
    target_dir = os.path.join(config.DATA_ROOT, safe_parent, safe_name, os.path.dirname(rel_path))
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, os.path.basename(rel_path))
    with open(target, "wb") as f:
        f.write(data)
    return {"path": os.path.relpath(target, config.DATA_ROOT), "bytes": len(data)}


def validate_uploaded(parent: str, name: str) -> dict:
    """Verify the uploaded tree contains both required subdirs."""
    import os

    root = os.path.join(config.DATA_ROOT, parent, name)
    if not os.path.isdir(root):
        return {"ok": False, "error": f"not a directory: {parent}/{name}"}
    missing = [d for d in REQUIRED_SUBDIRS if not os.path.isdir(os.path.join(root, d))]
    return {"ok": not missing, "missing": missing, "path": os.path.relpath(root, config.DATA_ROOT)}


def list_parent_dirs() -> list[str]:
    """Distinct top-level directory names that contain dataset folders."""
    import os

    out = set()
    root = config.DATA_ROOT
    if os.path.isdir(root):
        for dirpath, dirnames, _ in os.walk(root):
            depth = dirpath.rstrip("/").count("/") - root.rstrip("/").count("/")
            if depth >= config.SCAN_DEPTH:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("log", "build", "install", ".venv", "src")]
            if "ros2bag_vio" in dirnames or "stereo_auto_gen" in dirnames:
                rel = os.path.relpath(dirpath, root)
                parent = rel.split("/", 1)[0] if "/" in rel else "./"
                out.add(parent)
    return sorted(out)


def topic_series(name: str, topic: str, max_points: int = config.SERIES_MAX_POINTS) -> dict:
    """Downsampled series for Imu / Odometry / TFMessage / NavSatFix / generic."""
    ds = get_dataset(name)
    bag_dir = os.path.join(ds["path"], "ros2bag_vio")
    ty = _topic_type(name, topic)

    total = image_frame_count(name, topic) if ty in ("Image", "CompressedImage") else 0
    for t in bag_info(name).get("topics", []):
        if t["name"] == topic:
            total = t["count"]
    if total == 0:
        return {"type": ty, "error": "no messages", "points": []}

    step = max(1, total // max_points)
    rows = []
    for idx, ts, _, _, decoded in _mcap_messages(bag_dir, topic, limit=total):
        if idx % step:
            continue
        rows.append(_row_for(ty, ts, decoded))
        if len(rows) >= max_points:
            break
    return {"type": ty, "count": total, "step": step, "points": rows}


def _row_for(ty: str, ts: int, decoded) -> dict:
    t = ts / 1e9
    if ty == "Imu":
        a = decoded.linear_acceleration
        g = decoded.angular_velocity
        return {"t": t, "ax": a.x, "ay": a.y, "az": a.z, "gx": g.x, "gy": g.y, "gz": g.z}
    if ty == "Odometry":
        p = decoded.pose.pose.position
        q = decoded.pose.pose.orientation
        return {"t": t, "x": p.x, "y": p.y, "z": p.z, "qw": q.w, "qx": q.x, "qy": q.y, "qz": q.z}
    if ty == "NavSatFix":
        return {"t": t, "lat": decoded.latitude, "lon": decoded.longitude, "alt": decoded.altitude, "status": int(decoded.status.status)}
    if ty == "TFMessage":
        first = decoded.transforms[0] if decoded.transforms else None
        if first is None:
            return {"t": t}
        p, q = first.transform.translation, first.transform.rotation
        return {
            "t": t,
            "frame": first.header.frame_id,
            "child": first.child_frame_id,
            "x": p.x,
            "y": p.y,
            "z": p.z,
            "qw": q.w,
            "qx": q.x,
            "qy": q.y,
            "qz": q.z,
        }
    # generic: compact repr of the first-level attributes
    try:
        d = {k: (list(v) if isinstance(v, (list, tuple)) and len(v) < 16 else str(type(v))) for k, v in vars(decoded).items()}
    except TypeError:
        d = {"repr": str(decoded)[:128]}
    return {"t": t, "fields": d}


# ---------------------------------------------------------------- player
def player_prepare(name: str) -> dict:
    """Timeline summary for the player: per-topic type + sampled timestamps.

    For image topics we ship the full timestamps array so the player can
    binary-search the nearest frame index client-side and hit the cached
    JPEG endpoint directly (no per-tick server round-trip).
    """
    info = bag_info(name)
    topics = []
    for t in info.get("topics", []):
        ty = t["type"].split("/")[-1]
        entry = {"name": t["name"], "type": ty, "count": t["count"]}
        if ty in ("Image", "CompressedImage"):
            times = _time_index(name, t["name"])
            if times:
                entry["full_times"] = times  # full ns array for binary search
                entry["t0"] = times[0]
                entry["t1"] = times[-1]
        elif ty in ("Imu", "Odometry", "TFMessage", "NavSatFix"):
            times = _time_index(name, t["name"])
            if times:
                step = max(1, len(times) // 200)
                entry["times"] = [times[i] for i in range(0, len(times), step)]
                entry["t0"] = times[0]
                entry["t1"] = times[-1]
        topics.append(entry)
    return {
        "start_ns": info.get("start_ns", 0),
        "end_ns": info.get("end_ns", 0),
        "topics": topics,
    }


def _nearest_index(times: list, t_ns: int) -> int:
    """Binary search: index of the last timestamp <= t_ns."""
    import bisect

    i = bisect.bisect_right(times, t_ns) - 1
    return max(0, i)


def player_slice(name: str, t_ns: int, window_s: float = 4.0, images_only: bool = False) -> dict:
    """All topic data around bag time t_ns for one player tick.

    - image topics: nearest frame index at/before t_ns (as JPEG reference)
    - series topics: points within [t_ns - window_s, t_ns] (downsampled)
    - images_only=True: return only image frame indices (pure binary search on
      the cached time index, zero mcap decoding - used by the player's tick)
    """
    ds = get_dataset(name)
    bag_dir = os.path.join(ds["path"], "ros2bag_vio")
    out = {"t_ns": t_ns, "topics": {}}
    lo = t_ns - int(window_s * 1e9)
    for t in bag_info(name).get("topics", []):
        ty = t["type"].split("/")[-1]
        topic = t["name"]
        if ty in ("Image", "CompressedImage"):
            times = _time_index(name, topic)
            if not times:
                continue
            out["topics"][topic] = {"type": ty, "frame": _nearest_index(times, t_ns), "count": len(times)}
        elif images_only:
            continue
        elif ty in ("Imu", "Odometry", "TFMessage", "NavSatFix"):
            times = _time_index(name, topic)
            if not times:
                continue
            import bisect

            i0 = max(0, bisect.bisect_left(times, lo))
            i1 = _nearest_index(times, t_ns)
            rows = []
            # downsample the window to ~400 points
            span = max(1, i1 - i0)
            step = max(1, span // 400)
            idxs = {i for i in range(i0, i1 + 1, step)}
            if i1 >= i0:
                idxs.add(i1)
            for idx, ts, _enc, _schema, decoded in _mcap_messages(bag_dir, topic, indices=idxs):
                rows.append(_row_for(ty, ts, decoded))
            out["topics"][topic] = {"type": ty, "count": len(times), "points": rows}
    return out


# ---------------------------------------------------------------- frame cache
"""Pre-extract image frames as JPEGs for O(1) seek during playback.

The mcap reader streams from the start, so the previous per-frame fetch was
O(N) per seek — dragging the slider meant each step waited hundreds of ms.
This module lazily writes every image message to disk and serves a static URL
so the browser can load a frame in <10ms after the cache is warm.
"""

_extraction_jobs = {}  # (name, topic) -> {"done": int, "total": int, "thread": Thread, "error": str}
_extraction_lock = threading.Lock()


def _frame_cache_dir(name: str, topic: str) -> str:
    safe_name = name.replace("/", "__")
    safe_topic = topic.strip("/").replace("/", "_")
    return os.path.join(config.FRAME_CACHE_DIR, safe_name, safe_topic)


def _bag_mtime(name: str) -> float:
    ds = get_dataset(name)
    bag_dir = os.path.join(ds["path"], "ros2bag_vio")
    bag_file = _find_bag_file(bag_dir)
    return os.path.getmtime(bag_file) if bag_file else 0.0


def frame_manifest(name: str, topic: str) -> dict:
    """Read cached manifest {count, times: [ns...], bag_mtime} or {} if absent."""
    import json

    mf = os.path.join(_frame_cache_dir(name, topic), "manifest.json")
    if not os.path.isfile(mf):
        return {}
    try:
        with open(mf) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def get_cached_frame(name: str, topic: str, index: int) -> bytes:
    """Return JPEG bytes for frame index. Extracts on demand if missing."""
    cache_dir = _frame_cache_dir(name, topic)
    cache_file = os.path.join(cache_dir, f"{index:06d}.jpg")
    if os.path.isfile(cache_file):
        with open(cache_file, "rb") as f:
            return f.read()
    # miss: extract this frame from the mcap (slow path) and cache it
    jpeg, _ts = image_frame_jpeg(name, topic, index)
    os.makedirs(cache_dir, exist_ok=True)
    tmp = cache_file + ".tmp"
    with open(tmp, "wb") as f:
        f.write(jpeg)
    os.replace(tmp, cache_file)
    return jpeg


def _extract_all(name: str, topic: str) -> None:
    """Background worker: extract all image frames to disk + manifest.

    Single pass over the mcap (O(N)): each message is written as encountered.
    The previous implementation called image_frame_jpeg() per frame, which
    re-iterated the bag from message 0 every time — O(N²), unusable beyond a
    few hundred frames.
    """
    import json

    try:
        count = image_frame_count(name, topic)
        if count <= 0:
            return
        cache_dir = _frame_cache_dir(name, topic)
        os.makedirs(cache_dir, exist_ok=True)
        times = _time_index(name, topic)
        ds = get_dataset(name)
        bag_dir = os.path.join(ds["path"], "ros2bag_vio")
        ty = _topic_type(name, topic)
        cancelled = False
        for idx, _ts, _enc, _schema, decoded in _mcap_messages(bag_dir, topic):
            if idx >= count:
                break
            with _extraction_lock:
                job = _extraction_jobs.get((name, topic))
                if job and job.get("cancel"):
                    cancelled = True
                    break
                if job:
                    job["done"] = idx + 1
            cache_file = os.path.join(cache_dir, f"{idx:06d}.jpg")
            if not os.path.isfile(cache_file):
                try:
                    jpeg = _decoded_to_jpeg(decoded, ty)
                    tmp = cache_file + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(jpeg)
                    os.replace(tmp, cache_file)
                except Exception:  # noqa: BLE001
                    pass  # skip bad frames rather than aborting
        if cancelled:
            return
        # write manifest
        mf_path = os.path.join(cache_dir, "manifest.json")
        tmp = mf_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"count": count, "times": times, "bag_mtime": _bag_mtime(name)}, f)
        os.replace(tmp, mf_path)
        with _extraction_lock:
            _extraction_jobs[(name, topic)]["done"] = count
    except Exception as e:  # noqa: BLE001
        with _extraction_lock:
            _extraction_jobs[(name, topic)]["error"] = str(e)


def _decoded_to_jpeg(decoded, ty: str) -> bytes:
    """JPEG bytes for one decoded image message (CompressedImage passthrough)."""
    from io import BytesIO

    from PIL import Image

    if ty == "CompressedImage":
        return bytes(decoded.data)
    enc = decoded.encoding
    img = Image.frombytes("L", (decoded.width, decoded.height), bytes(decoded.data), "raw", enc if enc in ("mono8", "8UC1") else "L")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def start_extraction(name: str, topic: str) -> dict:
    """Kick off background extraction if not already running / cached."""
    import threading

    cache_dir = _frame_cache_dir(name, topic)
    mf = frame_manifest(name, topic)
    if mf and mf.get("bag_mtime") == _bag_mtime(name):
        return {"running": False, "done": mf.get("count", 0), "total": mf.get("count", 0), "ready": True}
    with _extraction_lock:
        job = _extraction_jobs.get((name, topic))
        if job and job.get("thread") and job["thread"].is_alive():
            return {"running": True, "done": job.get("done", 0), "total": job.get("total", 0), "ready": False}
        count = image_frame_count(name, topic)
        new_job = {"done": 0, "total": count, "error": "", "cancel": False}
        t = threading.Thread(target=_extract_all, args=(name, topic), daemon=True)
        new_job["thread"] = t
        _extraction_jobs[(name, topic)] = new_job
    t.start()
    return {"running": True, "done": 0, "total": count, "ready": False}


def extraction_status(name: str, topic: str) -> dict:
    with _extraction_lock:
        job = _extraction_jobs.get((name, topic))
        if not job:
            mf = frame_manifest(name, topic)
            if mf and mf.get("bag_mtime") == _bag_mtime(name):
                return {"running": False, "done": mf.get("count", 0), "total": mf.get("count", 0), "ready": True}
            return {"running": False, "done": 0, "total": 0, "ready": False}
        return {
            "running": job["thread"].is_alive() if job.get("thread") else False,
            "done": job.get("done", 0),
            "total": job.get("total", 0),
            "ready": False,
            "error": job.get("error", ""),
        }


# ---------------------------------------------------------------- video cache
"""Pre-generate an MP4 per (dataset, image_topic) so the data preview modal can
use a native <video> element (hardware decode, smooth Range seek, loop) instead
of per-frame <img> HTTP round-trips.

Pipeline: frame extraction (JPEGs on disk) → ffmpeg image2 demuxer → H.264 MP4
with +faststart for progressive download. State persists as video.meta.json so
survival across server restarts is free.
"""
_video_jobs = {}  # (name, topic) -> {"state": "generating"|"ready"|"error", ...}
_video_lock = threading.Lock()


def _video_cache_paths(name: str, topic: str) -> "tuple[str, str, str]":
    cache_dir = _frame_cache_dir(name, topic)
    return cache_dir, os.path.join(cache_dir, "video.mp4"), os.path.join(cache_dir, "video.meta.json")


def video_status(name: str, topic: str) -> dict:
    """state: ready | generating | error | absent."""
    _meta_path = os.path.join(_frame_cache_dir(name, topic), "video.meta.json")
    if os.path.isfile(_meta_path):
        try:
            with open(_meta_path) as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    with _video_lock:
        job = _video_jobs.get((name, topic))
        if job:
            return dict(job)
    return {"state": "absent"}


def video_path(name: str, topic: str) -> "str | None":
    mp4 = _video_cache_paths(name, topic)[1]
    return mp4 if os.path.isfile(mp4) else None


def video_prepare(name: str, topic: str) -> dict:
    """Kick off background MP4 generation. Idempotent.

    Returns the current status. The worker:
      1. ensures JPEG extraction is done (waits if needed)
      2. runs ffmpeg image2 demuxer → libx264 +faststart
      3. writes video.meta.json with state=ready
    """
    cache_dir, mp4_path, meta_path = _video_cache_paths(name, topic)
    # fast path: already ready
    if os.path.isfile(mp4_path) and os.path.isfile(meta_path):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            return meta
        except Exception:  # noqa: BLE001
            pass
    with _video_lock:
        existing = _video_jobs.get((name, topic))
        if existing and existing.get("state") == "generating":
            return dict(existing)
        _video_jobs[(name, topic)] = {"state": "generating"}

    t = threading.Thread(target=_video_worker, args=(name, topic), daemon=True)
    t.start()
    return {"state": "generating"}


def _video_worker(name: str, topic: str) -> None:
    cache_dir, mp4_path, meta_path = _video_cache_paths(name, topic)
    try:
        import subprocess

        # 1. ensure JPEG extraction is complete (frames must exist on disk)
        mf = frame_manifest(name, topic)
        if not mf or mf.get("bag_mtime") != _bag_mtime(name):
            start_extraction(name, topic)
            # wait for extraction to finish (timeout 10 min)
            deadline = time.time() + 600
            while time.time() < deadline:
                st = extraction_status(name, topic)
                if st.get("ready") or st.get("error"):
                    break
                if not st.get("running"):
                    break
                time.sleep(1)
            mf = frame_manifest(name, topic)

        count = (mf or {}).get("count", 0)
        times = (mf or {}).get("times", [])
        if count <= 0:
            _set_video_job(name, topic, {"state": "error", "error": "no frames extracted"})
            return

        # 2. compute fps from real timestamps (capped 1..60)
        if len(times) >= 2:
            duration_s = max(0.001, (times[-1] - times[0]) / 1e9)
            fps = max(1, min(60, round(count / duration_s)))
        else:
            duration_s = count / 30.0
            fps = 30

        os.makedirs(cache_dir, exist_ok=True)
        # 3. ffmpeg image2 → libx264 +faststart (progressive download)
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", os.path.join(cache_dir, "%06d.jpg"),
            "-c:v", "libx264", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-loglevel", "error",
            mp4_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            err = (r.stderr or "").strip()[-300:]
            _set_video_job(name, topic, {"state": "error", "error": f"ffmpeg: {err}"})
            return

        meta = {
            "state": "ready",
            "mp4": mp4_path,
            "duration_s": round(duration_s, 3),
            "frame_count": count,
            "fps": fps,
        }
        with open(meta_path, "w") as f:
            json.dump(meta, f)
        _set_video_job(name, topic, meta)
    except Exception as e:  # noqa: BLE001
        _set_video_job(name, topic, {"state": "error", "error": str(e)})


def _set_video_job(name: str, topic: str, val: dict) -> None:
    with _video_lock:
        _video_jobs[(name, topic)] = val
