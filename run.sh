#!/usr/bin/env bash
# Self-provisioning launcher for the test_platform web service.
#
#   bash run.sh                     # ensure dirs + deps (py + ffmpeg + NFS), then start on :1234
#   bash run.sh --hiden             # start in the background (nohup+disown): closing the
#                                   #   terminal does NOT stop the service; log in /tmp/test_platform_<PORT>.log
#   bash run.sh --check             # report missing deps/dirs, exit non-zero if any (no install, no start)
#   bash run.sh --install           # install all missing deps, then exit (no start)
#   bash run.sh --data-root=/d --port=1235   # override dataset root / port on the CLI
#
# Overrides (CLI args win over env):
#   --hiden          background run (alias --hidden); survives console close
#   --port=N         service port (default 1234)
#   --data-root=/p   dataset root exported to boards via NFS (default below)
#   --python=PY      interpreter to use (default python3)
#   PORT, DATA_ROOT, PYTHON, SKIP_DEPS are the equivalent env vars; SKIP_DEPS=1
#   check-but-don't-install python deps (warn and proceed).
#
# Required system deps are auto-installed via apt when missing: ffmpeg (mp4
# recording) and nfs-kernel-server + a DATA_ROOT export (boards mount the
# datasets). If that needs a password, the script prompts interactively.
set -e

die() { printf '[test_platform][ERROR] %s\n' "$*" >&2; exit 1; }

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# CLI args override env (e.g. `bash run.sh --data-root=/d --port=1235`).
for arg in "$@"; do
  case "$arg" in
    --port=*)        PORT="${arg#--port=}" ;;
    --data-root=*)   DATA_ROOT="${arg#--data-root=}" ;;
    --python=*)      PYTHON="${arg#--python=}" ;;
    --hiden|--hidden) HIDEN=1 ;;        # 后台运行：关终端不掉服务
    --check|--install) ;;               # handled below
    *) die "unknown argument: $arg (expected --hiden, --port=, --data-root=, --python=, --check, --install)" ;;
  esac
done

export PORT="${PORT:-1234}"
export DATA_ROOT="${DATA_ROOT:-/home/hobot/work/cc_ws/tros_ws}"
# Canonicalize so /etc/exports (setup_nfs.sh) and the board mount share ONE path.
# A `..`/relative form otherwise makes the NFS server deny the mount ("access
# denied"), because it matches the client mount against the export name literally.
export DATA_ROOT="$(realpath -m -- "${DATA_ROOT}" 2>/dev/null || printf '%s' "$DATA_ROOT")"
PY="${PYTHON:-python3}"

# ------------------------------------------------------------------ interpreter
"$PY" --version >/dev/null 2>&1 || die "no usable interpreter: $PY (set PYTHON=...)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "python >= 3.10 required (found $("$PY" --version 2>&1))"

# ------------------------------------------------------------------ dirs (required at import: main.py mounts /results + /frame_cache via StaticFiles)
mkdir -p "$DIR/results" "$DIR/state"

# ------------------------------------------------------------------ helpers
import_ok() { "$PY" -c "import sys; sys.path.insert(0, '$DIR/vendor_libs'); import $1" >/dev/null 2>&1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Re-running the launcher must not fail with "address already in use". Stop any
# process already listening on $PORT before we bind. We key off the LISTENING
# SOCKET (via ss) rather than a command-line pattern, so we never match this
# script's own shell or an unrelated server that merely mentions a matching arg.
stop_existing() {
  local port_pids pid
  port_pids=$(ss -ltnp 2>/dev/null \
    | grep -E "[:.]${PORT}[[:space:]]" \
    | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
  for pid in $port_pids; do
    echo "[test_platform] stopping existing pid $pid on :$PORT"
    kill "$pid" 2>/dev/null || true
  done
  if [ -n "$port_pids" ]; then
    sleep 1
    # second pass: anything still holding the port gets SIGKILL (e.g. a hung proc)
    port_pids=$(ss -ltnp 2>/dev/null \
      | grep -E "[:.]${PORT}[[:space:]]" \
      | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
    for pid in $port_pids; do
      [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true
    done
    sleep 1
  fi
}

# Run a privileged command: as root directly; else passwordless sudo; else, on a
# TTY, `sudo` (prompts for the password); else fail with a clear message.
su_run() {
  if [ "$(id -u)" = "0" ]; then "$@"; return $?; fi
  if sudo -n true 2>/dev/null; then sudo "$@"; return $?; fi
  if [ -t 0 ]; then sudo "$@"; return $?; fi
  printf '[test_platform][ERROR] needs sudo (no TTY to prompt for password): %s\n' "$*" >&2
  return 1
}

apt_install() {
  echo "  [sudo] apt-get install $1 ..."
  su_run apt-get update || die "apt-get update failed"
  su_run apt-get install -y "$@" || die "apt-get install $* failed"
}

# Is DATA_ROOT (or one of its ancestors) already exported in /etc/exports?
nfs_export_covers() {
  [ -r /etc/exports ] || return 1
  local p="$DATA_ROOT"
  while [ -n "$p" ] && [ "$p" != "/" ]; do
    if awk -v pat="^[[:space:]]*${p}([[:space:]]|$)" '{ if ($0 ~ pat) exit 0 }' /etc/exports; then
      return 0
    fi
    p="${p%/*}"
  done
  return 1
}

ensure_pip() {
  "$PY" -m pip --version >/dev/null 2>&1 && return 0
  "$PY" -m ensurepip --upgrade >/dev/null 2>&1 && return 0
  if can_user_apt; then
    apt_install python3-pip && return 0
  fi
  return 1
}
can_user_apt() { [ "$(id -u)" = "0" ] || sudo -n true 2>/dev/null || [ -t 0 ]; }

# ------------------------------------------------------------------ dependency check
check_deps() {
  echo "[test_platform] python deps (interpreter: $("$PY" --version 2>&1), target: vendor_libs/)"
  local missing=""
  for m in fastapi uvicorn paramiko mcap mcap_ros2 yaml PIL numpy matplotlib multipart; do
    if import_ok "$m"; then echo "  ok    $m"; else echo "  MISS  $m"; missing="$missing $m"; fi
  done
  echo "[test_platform] required system deps"
  if have ffmpeg; then echo "  ok    ffmpeg"; else echo "  MISS  ffmpeg (mp4 recording)"; missing="$missing ffmpeg"; fi
  if have exportfs; then echo "  ok    exportfs"; else echo "  MISS  exportfs (NFS server: boards mount DATA_ROOT)"; missing="$missing nfs-kernel-server"; fi
  if nfs_export_covers; then echo "  ok    NFS export covers $DATA_ROOT"; else echo "  MISS  NFS export for $DATA_ROOT"; missing="$missing nfs_export"; fi
  echo "[test_platform] dirs"
  [ -d "$DIR/results" ] && echo "  ok    results/" || { echo "  MISS  results/"; missing="$missing results_dir"; }
  [ -d "$DIR/web" ]     && echo "  ok    web/"     || { echo "  MISS  web/"; missing="$missing web_dir"; }
  echo "[test_platform] env"
  echo "  data   DATA_ROOT=$DATA_ROOT"
  echo "  port   PORT=$PORT"
  [ -n "$missing" ] && { echo "  MISSING:$missing"; return 1; }
  return 0
}

# ------------------------------------------------------------------ installers
install_deps() {  # python deps -> vendor_libs (pip --target, matches project convention)
  if [ "${SKIP_DEPS}" = "1" ]; then
    echo "[test_platform] SKIP_DEPS=1 -> skipping python install (may fail at runtime)"
    return 0
  fi
  ensure_pip || die "no pip available (try: python3 -m ensurepip, or apt-get install python3-pip)"
  echo "[test_platform] installing python deps into vendor_libs/ ..."
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet --target "$DIR/vendor_libs" \
       -r "$DIR/requirements.txt" matplotlib
  echo "[test_platform] python deps done."
}

install_sys() {  # required system packages (ffmpeg + NFS server/export)
  if ! have ffmpeg; then
    echo "[test_platform] installing ffmpeg ..."
    apt_install ffmpeg
  fi
  if ! have exportfs || ! nfs_export_covers; then
    echo "[test_platform] installing/exporting NFS for $DATA_ROOT ..."
    # sudo strips env, so pass DATA_ROOT through explicitly (setup_nfs.sh validates it).
    if [ "$(id -u)" = "0" ]; then
      DATA_ROOT="$DATA_ROOT" bash "$DIR/setup_nfs.sh"
    elif sudo -n true 2>/dev/null; then
      sudo -n "DATA_ROOT=$DATA_ROOT" bash "$DIR/setup_nfs.sh"
    else
      su_run bash "$DIR/setup_nfs.sh" || die "NFS setup failed; run: sudo DATA_ROOT=$DATA_ROOT bash $DIR/setup_nfs.sh"
    fi
  fi
  echo "[test_platform] system deps done."
}

# ------------------------------------------------------------------ args
MODE="start"
case "${1:-}" in
  --check)   MODE="check" ;;
  --install) MODE="install" ;;
esac

if [ "$MODE" = "check" ]; then
  check_deps || { echo "[test_platform] deps missing (see above)."; exit 1; }
  echo "[test_platform] check ok."
  exit 0
fi

if ! check_deps; then
  echo "[test_platform] missing deps -> installing ..."
  install_deps
  install_sys
  echo "[test_platform] re-checking ..."
  if ! check_deps; then
    die "deps still missing after install; fix the items marked MISS above and re-run"
  fi
else
  echo "[test_platform] deps already satisfied."
fi

if [ "$MODE" = "install" ]; then
  echo "[test_platform] install complete (not starting). run 'bash run.sh' to start."
  exit 0
fi

stop_existing
if [ "${HIDEN:-0}" = "1" ]; then
  # 后台运行：nohup 忽略 SIGHUP + disown 脱离 shell 任务表，关终端不掉服务。
  LOG="/tmp/test_platform_${PORT}.log"
  echo "[test_platform] starting (background)  http://0.0.0.0:$PORT  DATA_ROOT=$DATA_ROOT"
  nohup "$PY" server/main.py >"$LOG" 2>&1 &
  BGPID=$!
  disown
  echo "[test_platform] pid=$BGPID  log=$LOG  （关闭终端不影响服务；重跑本脚本即优雅重启/停止）"
else
  echo "[test_platform] starting  http://0.0.0.0:$PORT  DATA_ROOT=$DATA_ROOT"
  exec "$PY" server/main.py
fi
