"""Tests for auto_test.pull_mirror (manual 拉取代码) and clone-error classification."""
from server import auto_test


def test_pull_clones_when_mirror_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_test, "_MIRROR_DIR", str(tmp_path / "mirror"))
    monkeypatch.setattr(auto_test, "_ensure_mirror", lambda use_proxy: True)
    ok, detail = auto_test.pull_mirror(None)
    assert ok is True
    assert detail == ""


def test_pull_surfaces_clone_error(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_test, "_MIRROR_DIR", str(tmp_path / "mirror"))
    monkeypatch.setattr(auto_test, "_ensure_mirror", lambda use_proxy: False)
    monkeypatch.setattr(auto_test, "_mirror_error", "镜像仓库克隆失败，本地没有代码。")
    ok, detail = auto_test.pull_mirror(None)
    assert ok is False
    assert detail == "镜像仓库克隆失败，本地没有代码。"


def test_pull_fetches_when_present(tmp_path, monkeypatch):
    (tmp_path / "mirror" / ".git").mkdir(parents=True)
    monkeypatch.setattr(auto_test, "_MIRROR_DIR", str(tmp_path / "mirror"))
    monkeypatch.setattr(auto_test, "get_config", lambda: {"branch": "develop", "use_proxy": False})
    calls = []

    def fake_git(args, timeout=120, net=False, use_proxy=None):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(auto_test, "_git", fake_git)
    ok, detail = auto_test.pull_mirror(None)
    assert ok is True
    assert "develop" in detail
    assert calls[0][0] == "fetch"


def test_pull_classifies_403(tmp_path, monkeypatch):
    (tmp_path / "mirror" / ".git").mkdir(parents=True)
    monkeypatch.setattr(auto_test, "_MIRROR_DIR", str(tmp_path / "mirror"))
    monkeypatch.setattr(auto_test, "get_config", lambda: {"branch": "develop", "use_proxy": True})

    def fake_git(args, timeout=120, net=False, use_proxy=None):
        err = ("remote: Write access to repository not granted.\n"
               "fatal: unable to access 'https://github.com/D-Robotics/drobotics_vio.git/': "
               "The requested URL returned error: 403")
        return 128, "", err

    monkeypatch.setattr(auto_test, "_git", fake_git)
    ok, detail = auto_test.pull_mirror(True)
    assert ok is False
    assert "403" in detail
    assert "代理" in detail  # used_proxy=True → hint to check the proxy


def test_pull_classifies_network_when_no_proxy(tmp_path, monkeypatch):
    (tmp_path / "mirror" / ".git").mkdir(parents=True)
    monkeypatch.setattr(auto_test, "_MIRROR_DIR", str(tmp_path / "mirror"))
    monkeypatch.setattr(auto_test, "get_config", lambda: {"branch": "develop", "use_proxy": False})

    def fake_git(args, timeout=120, net=False, use_proxy=None):
        return 128, "", "fatal: unable to access 'https://github.com/...': Could not resolve host"

    monkeypatch.setattr(auto_test, "_git", fake_git)
    ok, detail = auto_test.pull_mirror(False)
    assert ok is False
    assert "代理" in detail  # used_proxy=False → hint to enable proxychains4
