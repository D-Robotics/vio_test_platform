"""Pull an aarch64 sysroot from the X5 board for cross-building drobotics_vio.

Streams a tar of the needed trees over one SSH exec (fast on LAN), writes it
to .cache/x5_sysroot.tar.gz, then EXTRACTS it into .cache/x5_sysroot/ so the
next build_x5_docker.sh finds /opt/tros/humble etc. Run from test_platform/:
  python3 pull_sysroot.py [board_ip]
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor_libs"))

from server.boards import Ssh  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, ".cache", "x5_sysroot.tar.gz")
EXTRACT_DIR = os.path.join(HERE, ".cache", "x5_sysroot")

TAR_ARGS = [
    "tar -czf - --hard-dereference -C /",
    "opt/tros/humble",
    "usr/include/eigen3 usr/include/boost usr/include/opencv4",
    "usr/include/glog usr/include/gflags",
    "usr/share/eigen3",
    "--wildcards",
    "'usr/lib/aarch64-linux-gnu/libopencv_*'",
    "'usr/lib/aarch64-linux-gnu/libboost_*'",
    "'usr/lib/aarch64-linux-gnu/libglog*'",
    "'usr/lib/aarch64-linux-gnu/libgflags*'",
    "'usr/lib/aarch64-linux-gnu/libceres*'",
    "'usr/lib/aarch64-linux-gnu/cmake'",
    "'usr/lib/aarch64-linux-gnu/pkgconfig'",
]


def main():
    ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.15"
    cmd = " ".join(TAR_ARGS)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with Ssh(ip, timeout=15) as s:
        stdin, stdout, stderr = s._cli.exec_command(cmd, timeout=1800)
        n = 0
        with open(OUT, "wb") as f:
            while True:
                chunk = stdout.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                n += len(chunk)
                print(f"\r{n / 1e6:.0f} MB", end="", flush=True)
        rc = stdout.channel.recv_exit_status()
        err = stderr.read().decode("utf-8", "replace")
    print(f"\nrc={rc} size={n / 1e6:.1f} MB -> {OUT}")
    if err.strip():
        print("stderr:", err[:500])
    if rc != 0:
        print("pull failed; sysroot not extracted", file=sys.stderr)
        sys.exit(1)
    # build_x5_docker.sh checks for the extracted dir (opt/tros/humble), not the
    # tarball — extract here so the documented hint actually leaves a usable sysroot.
    os.makedirs(EXTRACT_DIR, exist_ok=True)
    r = subprocess.run(["tar", "-xzf", OUT, "-C", EXTRACT_DIR],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("extract failed:", r.stderr[:300], file=sys.stderr)
        sys.exit(1)
    print(f"extracted -> {EXTRACT_DIR}")


if __name__ == "__main__":
    main()
