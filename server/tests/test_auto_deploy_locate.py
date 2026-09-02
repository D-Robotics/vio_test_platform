"""Tests for auto_test._locate_install (deploy source selection) and default branch."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from server import auto_test


def test_default_branch_is_develop():
    assert auto_test._DEFAULT_CONFIG["branch"] == "develop"


def _mk_install(base, name, mtime):
    p = os.path.join(base, name, "install")
    os.makedirs(p, exist_ok=True)
    os.utime(p, (mtime, mtime))
    return p


def test_locate_prefers_newer_manual_build(tmp_path, monkeypatch):
    old_mirror = auto_test._MIRROR_DIR
    old_manual = auto_test._manual_vio_checkout
    try:
        monkeypatch.setattr(auto_test, "_MIRROR_DIR", str(tmp_path / "mirror"))
        monkeypatch.setattr(auto_test, "_manual_vio_checkout", lambda: str(tmp_path / "manual"))
        _mk_install(tmp_path, "mirror", 1000)   # stale mirror build
        man = _mk_install(tmp_path, "manual", 2000)  # fresher manual build
        d, label, _ = auto_test._locate_install()
        assert d == man
        assert label == "本地手动构建"
    finally:
        monkeypatch.setattr(auto_test, "_MIRROR_DIR", old_mirror)
        monkeypatch.setattr(auto_test, "_manual_vio_checkout", old_manual)


def test_locate_falls_back_to_mirror(tmp_path, monkeypatch):
    old_mirror = auto_test._MIRROR_DIR
    old_manual = auto_test._manual_vio_checkout
    try:
        monkeypatch.setattr(auto_test, "_MIRROR_DIR", str(tmp_path / "mirror"))
        monkeypatch.setattr(auto_test, "_manual_vio_checkout", lambda: str(tmp_path / "manual"))
        mir = _mk_install(tmp_path, "mirror", 1000)
        d, label, _ = auto_test._locate_install()
        assert d == mir
        assert label == "镜像仓库"
    finally:
        monkeypatch.setattr(auto_test, "_MIRROR_DIR", old_mirror)
        monkeypatch.setattr(auto_test, "_manual_vio_checkout", old_manual)


def test_locate_none_when_no_build(tmp_path, monkeypatch):
    old_mirror = auto_test._MIRROR_DIR
    old_manual = auto_test._manual_vio_checkout
    try:
        monkeypatch.setattr(auto_test, "_MIRROR_DIR", str(tmp_path / "mirror"))
        monkeypatch.setattr(auto_test, "_manual_vio_checkout", lambda: str(tmp_path / "manual"))
        d, label, sha = auto_test._locate_install()
        assert d is None and label == "" and sha == ""
    finally:
        monkeypatch.setattr(auto_test, "_MIRROR_DIR", old_mirror)
        monkeypatch.setattr(auto_test, "_manual_vio_checkout", old_manual)
