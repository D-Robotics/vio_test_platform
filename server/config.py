"""Global configuration for the test platform."""
import os
import socket

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Port for the web service (start.sh PORT env overrides)
PORT = int(os.environ.get("PORT", "1234"))

# Root directory that contains dataset dirs (each dataset = a dir holding
# ros2bag_vio/ and/or stereo_auto_gen/). Override with DATA_ROOT env.
DATA_ROOT = os.environ.get(
    "DATA_ROOT",
    "/home/hobot/work/cc_ws/tros_ws",
)

# Board registry + run records live inside the project
STATE_DIR = os.path.join(REPO_DIR, "state")
BOARDS_FILE = os.path.join(STATE_DIR, "boards.json")

# Lazy-extracted JPEG frame cache: STATE_DIR/frame_cache/<ds>/<topic>/<idx>.jpg
FRAME_CACHE_DIR = os.path.join(STATE_DIR, "frame_cache")

# Mount point used on the boards for the NFS-mounted DATA_ROOT
BOARD_MOUNT = "/mnt/vio_datasets"

# Default SSH credential for the dev boards (board entry can override)
DEFAULT_SSH_USER = "root"
DEFAULT_SSH_PASS = "root"

# Persistent base dir on the boards for everything the platform creates:
# the self-built VIO install + one dir per backtest run (launch.sh, merged
# config, all logs, VIO output). /tmp on X5 is tmpfs — contents vanish on
# reboot — so nothing important lives there.
BOARD_BASE = "/userdata/vio_backtest"

# Where the platform's own cross-built drobotics_vio install is deployed
BOARD_INSTALL_DIR = f"{BOARD_BASE}/install"

# Per-run dirs: BOARD_RUNS_DIR/<yyyymmdd_hhmmss>__<experiment|baseline>__<dataset>/
BOARD_RUNS_DIR = f"{BOARD_BASE}/runs"

# Symlink to the most recent run dir; status polling / script readback /
# result collection all follow it, so they always see the current run.
BOARD_CURRENT_LINK = f"{BOARD_BASE}/current"

# ov_web visualization port on the boards
OV_WEB_PORT = 9988

# scan depth when looking for dataset dirs under DATA_ROOT
SCAN_DEPTH = 3

# max messages streamed per series request
SERIES_MAX_POINTS = 2000
IMAGE_SAMPLE_COUNT = 8

# ---------------------------------------------------------------------------
# Stereo topic convention (single source of truth — do NOT invent another name).
# The rig's combined stereo image topic is ALWAYS the RAW name:
#   /sub_image_combine_raw
# THE VIO derives the CompressedImage variant in ROS2Visualizer by replacing the
# trailing token (raw -> jpeg), i.e. it NEVER wants a "_jpeg" topic passed in as
# topic_camera_stereo — doing so makes it derive "_jjpeg" and deserialization
# crashes (FastCDR exit -6). So the topic NAME is fixed; the only thing that may
# vary is whether the bag's image is CompressedImage, which the launch expresses
# via sub_from_compressed_image:=True/False. Authoritative source:
# ov_msckf/launch/subscribe_common.py (`topic_camera_stereo` default).
# ---------------------------------------------------------------------------
CANONICAL_STEREO_TOPIC = "/sub_image_combine_raw"


def host_ip(for_ip: str = None) -> str:
    """Primary non-loopback IPv4 of this host (used for NFS export address).

    If `for_ip` (typically a board IP) is given, return the source IP that
    would be used to reach it — multi-homed hosts (LAN + WiFi + docker
    bridges) must not hand the board an address on an unreachable subnet.
    """
    target = for_ip or "8.8.8.8"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target, 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"
