#!/usr/bin/env bash
# Cross-build drobotics_vio for X5 (aarch64) inside the pc_tros docker image.
#
# Usage: bash build_x5_docker.sh <src_dir> [extras_dir]
#   <src_dir>    drobotics_vio source tree; colcon build/ install/ log/ land there
#                (the auto-test mirror passes its own checkout).
#   <extras_dir> optional dir with extra colcon packages (irobot_create_msgs,
#                trial_guard, ...) that drobotics_vio find_package()s but are
#                not in /opt/tros/humble. Each subdir becomes a sibling package
#                in the workspace.
#
# The aarch64 sysroot (.cache/x5_sysroot, pulled from a real X5 board by
# pull_sysroot.py) is bind-mounted over the image's native paths:
#   /opt/tros/humble, /usr/include, /usr/share/eigen3, /usr/lib/aarch64-linux-gnu
# so every absolute path baked into the board's cmake configs stays valid.
set -euo pipefail
SRC="$(cd "$1" && pwd)"
EXTRAS="${2:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SYSROOT="${X5_SYSROOT:-$HERE/.cache/x5_sysroot}"
IMAGE="${X5_BUILD_IMAGE:-pc_tros_solution_ubuntu22.04:v1.0.0}"

# sysroot candidates: explicit X5_SYSROOT (exact) else .cache/x5_sysroot else the
# machine's own native sysroot (/). An aarch64 dev host/board already has
# /opt/tros/humble, /usr/include, /usr/share/eigen3, /usr/lib/aarch64-linux-gnu at
# / — recognise that instead of forcing a pull. Only pull from the board when no
# candidate satisfies every bind-mount path below.
MARKERS="opt/tros/humble usr/include usr/share/eigen3 usr/lib/aarch64-linux-gnu"

_sysroot_ok() { # $1 = candidate dir; every marker must be a dir under it
  for p in $MARKERS; do
    [ -d "$1/$p" ] || return 1
  done
  return 0
}

if [ -z "${X5_SYSROOT:-}" ] && ! _sysroot_ok "$SYSROOT"; then
  if _sysroot_ok /; then
    SYSROOT=/
  fi
fi

for p in $MARKERS; do
  [ -e "$SYSROOT/$p" ] || {
    echo "sysroot missing: $SYSROOT/$p — 机器已原生带 aarch64 sysroot 则设 X5_SYSROOT=/；否则运行 python3 pull_sysroot.py <board_ip>" >&2
    exit 1; }
done

# Workspace layout in docker:
#   /ws/                       <- scratch dir for colcon (build/ install/ log/)
#   /ws/drobotics_vio          <- bind-mount of $SRC (rw; the package itself)
#   /ws/<extra>                <- bind-mount per extras subdir (ro)
# After the build, install/ is rsync'd back to $SRC/install/ so the deploy
# step finds it in the expected place.
WS="$(mktemp -d -p "$(dirname "$SRC")" .x5ws.XXXXXX)"
trap 'rm -rf "$WS" 2>/dev/null || sudo -n rm -rf "$WS" 2>/dev/null || true' EXIT

DOCKER_ARGS=(
  -v "$WS":/ws
  -w /ws
  -v "$SRC":/ws/drobotics_vio
  -v "$HERE/x5.toolchain.cmake":/x5.toolchain.cmake:ro
  -v "$SYSROOT/opt/tros/humble":/opt/tros/humble:ro
  -v "$SYSROOT/usr/include":/usr/include:ro
  -v "$SYSROOT/usr/share/eigen3":/usr/share/eigen3:ro
  -v "$SYSROOT/usr/lib/aarch64-linux-gnu":/usr/lib/aarch64-linux-gnu:ro
)
if [ -n "$EXTRAS" ]; then
  for d in "$EXTRAS"/*/; do
    name="$(basename "$d")"
    [ "$name" = "drobotics_vio" ] && continue
    DOCKER_ARGS+=( -v "$(cd "$d" && pwd)":/ws/$name:ro )
    echo "[build_x5_docker] extras: $name <- $d"
  done
fi

docker run --rm "${DOCKER_ARGS[@]}" "$IMAGE" bash -c '
  set -e
  source /opt/tros/humble/setup.bash
  # /ws is the workspace root; colcon discovers packages one level down.
  colcon build \
    --cmake-args -DCMAKE_TOOLCHAIN_FILE=/x5.toolchain.cmake -DCMAKE_BUILD_TYPE=Release
'

# Copy install/ back to $SRC/install/ so deploy finds it. Files are root-owned
# (docker ran as root) — use docker to chown them first, then rsync.
docker run --rm -v "$WS":/ws "$IMAGE" bash -c 'chown -R --reference=/ws /ws/install /ws/build /ws/log 2>/dev/null || chmod -R a+rwX /ws/install /ws/build /ws/log'
mkdir -p "$SRC/install"
cp -r --force "$WS/install/." "$SRC/install/"
echo "[build_x5_docker] install/ -> $SRC/install/"