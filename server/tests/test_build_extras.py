"""A fresh build host must self-heal its empty extras dir.

The cross-build find_package(... REQUIRED)s irobot_create_msgs + trial_guard,
which are NOT in /opt/tros/humble and must live in .cache/x5_extras. When that
dir is empty the build_x5_docker.sh glob `"$EXTRAS"/*/` leaves a literal '*/'
(no match) and the subsequent `cd` aborts the build. The platform prepares the
extras by cloning the two repos when absent.
"""
import subprocess

from server import auto_test


def _blank_extras(monkeypatch, tmp_path, subdirs=()):
    monkeypatch.setattr(auto_test, "_BUILD_EXTRAS", str(tmp_path))
    for s in subdirs:
        (tmp_path / s).mkdir()
    # no proxy, no global config surprises
    monkeypatch.setattr(auto_test, "get_config", lambda: {"use_proxy": False})


def test_ensure_extras_clones_missing(monkeypatch, tmp_path):
    _blank_extras(monkeypatch, tmp_path)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(auto_test.subprocess, "run", fake_run)

    ok, detail = auto_test._ensure_extras()
    assert ok is True
    # both repos were cloned (git clone <url> <dest>)
    cl = [c for c in calls if c[0] == "git" and c[1] == "clone"]
    assert len(cl) == len(auto_test._BUILD_EXTRAS_REPOS)
    # each clone targets a dir inside _BUILD_EXTRAS
    import os
    for c in cl:
        assert c[-1].startswith(str(tmp_path))


def test_ensure_extras_skips_when_present(monkeypatch, tmp_path):
    _blank_extras(monkeypatch, tmp_path, subdirs=tuple(auto_test._BUILD_EXTRAS_REPOS))
    calls = []

    monkeypatch.setattr(auto_test.subprocess, "run",
                        lambda *a, **k: calls.append(a))
    ok, detail = auto_test._ensure_extras()
    assert ok is True
    assert calls == []  # nothing cloned


def test_ensure_extras_removes_hollow_on_failure(monkeypatch, tmp_path):
    _blank_extras(monkeypatch, tmp_path)
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="bad")

    monkeypatch.setattr(auto_test.subprocess, "run", fake_run)
    ok, detail = auto_test._ensure_extras()
    assert ok is False
    assert "拉取失败" in detail
    # no hollow dirs left behind
    import os
    assert os.listdir(str(tmp_path)) == []


def test_ensure_extras_forces_credential_helper_off(monkeypatch, tmp_path):
    # no token configured -> host credential.helper must be forced off so a
    # host 'gh'/store credential never injects a token lacking access (403)
    _blank_extras(monkeypatch, tmp_path)
    envs = []

    def fake_run(cmd, **kw):
        envs.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(auto_test.subprocess, "run", fake_run)
    auto_test._ensure_extras()
    for env in envs:
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert env["GIT_CONFIG_VALUE_0"] == ""
        assert "GIT_ASKPASS" not in env  # no token -> no askpass


def test_ensure_extras_uses_token_when_present(monkeypatch, tmp_path):
    _blank_extras(monkeypatch, tmp_path)
    monkeypatch.setattr(auto_test, "get_config",
                        lambda: {"use_proxy": False, "github_token": "secret-token"})
    envs = []

    def fake_run(cmd, **kw):
        envs.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(auto_test.subprocess, "run", fake_run)
    # _git_auth_env writes a temp askpass file — stub it out
    monkeypatch.setattr(
        auto_test, "_git_auth_env",
        lambda token, username="x-access-token": (
            {"GIT_ASKPASS": "/tmp/askpass", "GIT_ASKPASS_BY_TOKEN": token,
             "GIT_CONFIG_COUNT": "1",
             "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": ""},
            None))
    auto_test._ensure_extras()
    for env in envs:
        assert env["GIT_ASKPASS"] == "/tmp/askpass"  # token drives auth
        assert env["GIT_CONFIG_VALUE_0"] == ""
