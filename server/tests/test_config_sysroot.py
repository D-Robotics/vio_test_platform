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
