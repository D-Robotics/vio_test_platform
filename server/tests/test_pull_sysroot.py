"""pull_sysroot.py must build a robust tar command.

The old command used ``tar --wildcards`` to glob lib families during CREATION.
GNU tar's ``--wildcards`` only matches against archive contents, not the
filesystem walk, so it stats the literal ``usr/lib/aarch64-linux-gnu/libceres*``
path, finds nothing, and aborts the whole pull with
``tar: ... libceres*: Cannot stat`` even when the board does have ceres.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import pull_sysroot


def test_tar_cmd_pulls_full_trees_guarded():
    cmd = pull_sysroot._tar_cmd()
    assert "--wildcards" not in cmd
    assert "cd / &&" in cmd
    # each bind-mount tree is pulled in FULL (guarded so an absent tree is skipped,
    # not aborted) — a curated glob of headers/libs keeps missing system dev
    # packages (python3, openssl, tinyxml2, ...) that break the cross-build.
    for d in pull_sysroot.SYSROOT_TREES:
        assert f"'{d}'" in cmd
    assert "-T -" in cmd
    # guarded: only dirs that actually exist are emitted
    assert '[ -d "$x" ]' in cmd


def test_tar_cmd_pipeline_succeeds_with_absent_lib(tmp_path):
    """A missing lib family (libceres*) must be skipped, not abort the pull."""
    # Build a fake board filesystem rooted at tmp_path.
    lib = tmp_path / "usr" / "lib" / "aarch64-linux-gnu"
    (lib / "cmake").mkdir(parents=True)
    (tmp_path / "opt" / "tros" / "humble").mkdir(parents=True)
    (tmp_path / "usr" / "include" / "eigen3").mkdir(parents=True)
    # present libs (one real + one symlinked), libceres deliberately absent
    (lib / "libglog.so.0").write_text("x")
    (lib / "libglog.so").symlink_to("libglog.so.0")
    (tmp_path / "opt" / "tros" / "humble" / "setup.bash").write_text("x")
    (tmp_path / "usr" / "include" / "eigen3" / "Eigen").write_text("x")

    # Render the same pipeline but rooted at tmp_path (cd / -> here).
    cmd = pull_sysroot._tar_cmd().replace("cd / &&", f"cd {tmp_path} &&")
    r = subprocess.run(["sh", "-c", cmd], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()
    assert b"libceres*: Cannot stat" not in r.stderr
    # the present lib family and the guarded dirs made it into the archive
    members = subprocess.run(["tar", "-tzf", "-"], input=r.stdout,
                             capture_output=True).stdout.decode()
    assert "usr/lib/aarch64-linux-gnu/libglog.so" in members
    assert "opt/tros/humble/setup.bash" in members
    assert "usr/include/eigen3/Eigen" in members
