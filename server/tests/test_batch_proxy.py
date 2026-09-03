"""Manual backtest must be able to pull GitHub code through proxychains4.

The manual batch build reuses the auto mirror (auto_test._git / _ensure_mirror),
which by default follows the global auto ``use_proxy``. A batch can pin its own
choice: ``batch.use_proxy = True`` forces the proxychains4 wrapper for the
clone/fetch; ``None`` inherits the auto config (so enabling auto proxy doesn't
get overridden by an untouched manual batch).
"""
import subprocess

from server import auto_test, batch


def test_batchrun_use_proxy_roundtrip():
    b = batch.BatchRun("192.168.1.15", ["ds1"], branch="main", use_proxy=True)
    assert b.use_proxy is True
    assert batch.BatchRun.from_dict(b.to_dict()).use_proxy is True
    # default: None = inherit the auto config
    b2 = batch.BatchRun("192.168.1.15", ["ds1"])
    assert b2.use_proxy is None
    assert batch.BatchRun.from_dict(b2.to_dict()).use_proxy is None


def test_git_proxychains_follows_forced_flag(monkeypatch, tmp_path):
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)  # satisfy the "is a repo" guard
    monkeypatch.setattr(auto_test, "_MIRROR_DIR", str(mirror))
    # global config says NO proxy…
    monkeypatch.setattr(auto_test, "get_config", lambda: {"use_proxy": False})
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(auto_test.subprocess, "run", fake_run)

    # …but the batch forces proxy → wrapped
    rc, out, err = auto_test._git(["rev-parse", "HEAD"], net=True, use_proxy=True)
    assert calls[-1] == ["proxychains4", "git", "-C", str(mirror), "rev-parse", "HEAD"]
    # explicit off → no wrapper
    auto_test._git(["rev-parse", "HEAD"], net=True, use_proxy=False)
    assert calls[-1] == ["git", "-C", str(mirror), "rev-parse", "HEAD"]
    # None + config False → no wrapper
    auto_test._git(["rev-parse", "HEAD"], net=True, use_proxy=None)
    assert calls[-1] == ["git", "-C", str(mirror), "rev-parse", "HEAD"]


def test_git_proxychains_follows_global_when_None(monkeypatch, tmp_path):
    mirror = tmp_path / "mirror"
    (mirror / ".git").mkdir(parents=True)
    monkeypatch.setattr(auto_test, "_MIRROR_DIR", str(mirror))
    # global config ON, batch flag None → inherit → wrapped
    monkeypatch.setattr(auto_test, "get_config", lambda: {"use_proxy": True})
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(auto_test.subprocess, "run", fake_run)
    auto_test._git(["fetch", "origin"], net=True, use_proxy=None)
    assert calls[-1][0] == "proxychains4"
    # a batch that forbids proxy can still opt out even with global ON
    auto_test._git(["fetch", "origin"], net=True, use_proxy=False)
    assert calls[-1][0] == "git"


def test_batch_build_forwards_use_proxy_to_fetch(monkeypatch):
    b = batch.BatchRun("192.168.1.15", ["ds1"], branch="main", use_proxy=True)
    git_calls = []

    def fake_ensure_mirror(use_proxy=None):
        return None

    def fake_git(args, timeout=120, net=False, use_proxy=None):
        git_calls.append((list(args), net, use_proxy))
        if args and args[0] == "fetch":
            return 0, "", ""
        return 0, "aabbccd", ""

    def fake_run_build(sha, board_ip=None):
        return True, "built"

    def fake_deploy_install(ip):
        return True, "deployed"

    monkeypatch.setattr(auto_test, "_ensure_mirror", fake_ensure_mirror)
    monkeypatch.setattr(auto_test, "_git", fake_git)
    monkeypatch.setattr(auto_test, "_run_build", fake_run_build)
    monkeypatch.setattr(auto_test, "_deploy_install", fake_deploy_install)

    assert batch._batch_build_and_deploy(b) is True
    fetch = next((c for c in git_calls if c[0] and c[0][0] == "fetch"), None)
    assert fetch is not None
    assert fetch[1] is True   # net
    assert fetch[2] is True   # use_proxy forwarded
