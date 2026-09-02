"""Tests for github_token plumbing: masking in public_config and git askpass auth."""
import os
import shutil
import types

from server import auto_test


def test_public_config_masks_token(monkeypatch):
    monkeypatch.setattr(auto_test, "_state", {"config": {"github_token": "XYZ", "branch": "develop"}})
    pc = auto_test.public_config()
    assert pc["github_token"] == ""
    assert pc["github_token_set"] is True
    assert pc["branch"] == "develop"


def test_public_config_reports_unset(monkeypatch):
    monkeypatch.setattr(auto_test, "_state", {"config": {"github_token": "", "branch": "develop"}})
    pc = auto_test.public_config()
    assert pc["github_token"] == ""
    assert pc["github_token_set"] is False


def test_update_config_returns_masked(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_test, "_state", {"config": {"github_token": "", "branch": "develop"}})
    monkeypatch.setattr(auto_test, "_STATE_FILE", str(tmp_path / "state.json"))
    out = auto_test.update_config({"github_token": "QWE"})
    assert out["github_token"] == ""
    assert out["github_token_set"] is True


def test_git_auth_env_writes_token_to_file_only(tmp_path):
    env, tmp = auto_test._git_auth_env("SECRETTOKEN")
    try:
        assert env["GIT_ASKPASS"] and os.path.exists(env["GIT_ASKPASS"])
        assert env["VIO_GIT_TOKEN_FILE"] and os.path.exists(env["VIO_GIT_TOKEN_FILE"])
        with open(env["VIO_GIT_TOKEN_FILE"], encoding="utf-8") as f:
            assert f.read() == "SECRETTOKEN"
        # the token must not be an env value of the child process itself
        assert "SECRETTOKEN" not in str(env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_git_injects_auth_env_for_net(tmp_path, monkeypatch):
    (tmp_path / "mirror" / ".git").mkdir(parents=True)
    monkeypatch.setattr(auto_test, "_MIRROR_DIR", str(tmp_path / "mirror"))
    monkeypatch.setattr(auto_test, "get_config",
                        lambda: {"github_token": "TOKEN", "use_proxy": False, "branch": "develop"})
    captured = {}

    def fake_run(cmd, capture_output=True, text=True, timeout=None, env=None):
        captured["env"] = env
        r = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return r

    monkeypatch.setattr(auto_test.subprocess, "run", fake_run)
    rc, _, _ = auto_test._git(["fetch", "origin", "develop"], net=True)
    assert rc == 0
    assert captured["env"]["GIT_ASKPASS"]
    assert captured["env"]["VIO_GIT_TOKEN_FILE"]
    # the github secret must NOT be an env value; it lives only in the askpass file
    assert captured["env"]["VIO_GIT_USER"] != "TOKEN"
    assert "TOKEN" != captured["env"].get("VIO_GIT_TOKEN_FILE")


def test_git_no_auth_when_no_token(tmp_path, monkeypatch):
    (tmp_path / "mirror" / ".git").mkdir(parents=True)
    monkeypatch.setattr(auto_test, "_MIRROR_DIR", str(tmp_path / "mirror"))
    monkeypatch.setattr(auto_test, "get_config",
                        lambda: {"github_token": "", "use_proxy": False, "branch": "develop"})
    captured = {}

    def fake_run(cmd, capture_output=True, text=True, timeout=None, env=None):
        captured["env"] = env
        r = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return r

    monkeypatch.setattr(auto_test.subprocess, "run", fake_run)
    auto_test._git(["fetch", "origin", "develop"], net=True)
    assert not captured["env"] or "GIT_ASKPASS" not in captured["env"]
