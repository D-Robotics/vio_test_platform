"""A native aarch64 build host needs no cross sysroot.

build_x5_docker.sh builds natively (no docker image, no sysroot bind) when
uname -m is aarch64/arm64 AND /opt/tros/humble is present. ensure_sysroot must
then short-circuit instead of demanding a board pull or a valid board_ip.
"""
from types import SimpleNamespace

from server import auto_test, config


def test_native_aarch64_host_true(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setattr(config.os, "uname", lambda: SimpleNamespace(machine="aarch64"))
    monkeypatch.setattr(config.os.path, "isdir", lambda p: p == "/opt/tros/humble")
    assert config.native_aarch64_host() is True


def test_native_aarch64_host_false_on_x86(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setattr(config.os, "uname", lambda: SimpleNamespace(machine="x86_64"))
    monkeypatch.setattr(config.os.path, "isdir", lambda p: True)
    assert config.native_aarch64_host() is False


def test_native_aarch64_host_false_without_ros(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setattr(config.os, "uname", lambda: SimpleNamespace(machine="aarch64"))
    monkeypatch.setattr(config.os.path, "isdir", lambda p: False)
    assert config.native_aarch64_host() is False


def test_native_aarch64_host_false_non_linux(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "darwin")
    assert config.native_aarch64_host() is False


def test_ensure_sysroot_shortcircuits_on_native_host(monkeypatch):
    monkeypatch.setattr(config, "native_aarch64_host", lambda: True)
    # must not attempt any board pull / ip validation on a native host
    monkeypatch.setattr(auto_test, "_pull_sysroot",
                        lambda ip: (_ for _ in ()).throw(AssertionError("board pull tried")))
    ok, detail = auto_test.ensure_sysroot("")
    assert ok is True
    assert "aarch64" in detail


def _mk_sysroot(root, with_content=True):
    (root / "opt" / "tros" / "humble").mkdir(parents=True)
    (root / "usr" / "include").mkdir(parents=True)
    (root / "usr" / "share" / "eigen3").mkdir(parents=True)
    (root / "usr" / "lib" / "aarch64-linux-gnu").mkdir(parents=True)
    if with_content:
        (root / "usr" / "include" / "python3.10").mkdir(parents=True)
        (root / "usr" / "lib" / "aarch64-linux-gnu" / "libpython3.10.so").write_text("")
        (root / "usr" / "include" / "openssl").mkdir(parents=True)
        (root / "usr" / "include" / "openssl" / "ssl.h").write_text("")
    return root


def test_sysroot_ready_false_when_crossbuild_content_missing(tmp_path, monkeypatch):
    # mount dirs present but no python dev / openssl -> NOT ready (must re-pull)
    root = _mk_sysroot(tmp_path, with_content=False)
    monkeypatch.setattr(config, "_sysroot_candidates", lambda: [str(root)])
    assert config.sysroot_ready() is False


def test_sysroot_ready_true_when_crossbuild_content_present(tmp_path, monkeypatch):
    root = _mk_sysroot(tmp_path, with_content=True)
    monkeypatch.setattr(config, "_sysroot_candidates", lambda: [str(root)])
    assert config.sysroot_ready() is True


def test_sysroot_dir_returns_complete_candidate(tmp_path, monkeypatch):
    incomplete = _mk_sysroot(tmp_path / "old", with_content=False)
    complete = _mk_sysroot(tmp_path / "new", with_content=True)
    monkeypatch.setattr(config, "_sysroot_candidates", lambda: [str(incomplete), str(complete)])
    assert config.sysroot_dir() == str(complete)
