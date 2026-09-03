"""Regression tests for board NFS mount + dataset-visibility fail-fast.

Two bugs caused runs to launch with "blank logs":
  1. mount_board computed `mounted = "MOUNTED" in check["out"]` — "MOUNTED" is a
     substring of "NOTMOUNTED", so a FAILED mount (mountpoint -q -> not a
     mountpoint) was treated as success, and build_launch_script built/launched a
     run against an empty /mnt/vio_datasets.
  2. No check that the board actually sees the dataset artifacts before launching,
     so a broken mount produced a silent empty run only a late config_path error.

These run fully offline: `backtest.Ssh` is replaced with a fake shell.
"""
from server import backtest, config


class _FakeSsh:
    """Replace backtest.Ssh; .run(cmd, timeout=None) dispatches on content."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _out(self, cmd):
        if "mount | grep -E 'nfs'" in cmd:      # board_mounted_path probe
            return {"out": "", "rc": 0, "err": ""}
        if "which mount.nfs" in cmd:            # nfs-client availability
            return {"out": "HAS_NFS\n", "rc": 0, "err": ""}
        if "mkdir" in cmd and "mount -t nfs" in cmd:  # the mount attempt
            return {"out": "", "rc": 32,
                    "err": f"mount.nfs: access denied by server while mounting {config.host_ip()}:{config.DATA_ROOT}\n"}
        if "mountpoint -q" in cmd:              # the success check
            return {"out": "NOTMOUNTED\n", "rc": 0, "err": ""}
        if "MISS::" in cmd and "[ -f" in cmd:   # _board_dataset_ready checks
            return {"out": "MISS::stereo_auto_gen/estimator_config.yaml\n", "rc": 0, "err": ""}
        return {"out": "", "rc": 0, "err": ""}

    def run(self, cmd, timeout=None):
        return self._out(cmd)


def test_mount_board_returns_failure_when_mountpoint_check_is_NOTMOUNTED(monkeypatch):
    """A failed mount yields mountpoint -> NOTMOUNTED; it must NOT be read as success."""
    monkeypatch.setattr(backtest, "Ssh", _FakeSsh)
    m = backtest.mount_board("10.64.91.57")
    assert m["ok"] is False
    assert m["board_path"] is None
    assert "access denied" in m["detail"].lower()


def test_build_launch_script_fails_fast_when_data_root_not_exported(monkeypatch):
    monkeypatch.setattr(backtest, "Ssh", _FakeSsh)
    monkeypatch.setattr(
        backtest.datasets, "get_dataset",
        lambda name: {"name": name, "path": "/x", "has_bag": True, "has_config": True},
    )
    r = backtest.build_launch_script("10.64.91.57", "ysdata/some_dataset", offline_bag=True)
    assert r.get("ok") is False
    assert r.get("run_dir") is None
    assert "data root not mounted on board" in r.get("detail", "")
    assert "setup_nfs.sh" in r.get("detail", "") or "--data-root" in r.get("detail", "")


def test_board_dataset_ready_reports_missing_artifacts(monkeypatch):
    monkeypatch.setattr(backtest, "Ssh", _FakeSsh)
    ok, why = backtest._board_dataset_ready("10.64.91.57", "/mnt/vio_datasets/ysdata/some_dataset")
    assert ok is False
    assert "estimator_config.yaml" in why


def test_board_dataset_ready_true_when_all_artifacts_present(monkeypatch):
    class Clean(_FakeSsh):
        def _out(self, cmd):
            if "[ -f" in cmd and "MISS::" in cmd:
                return {"out": "", "rc": 0, "err": ""}
            return super()._out(cmd)

    monkeypatch.setattr(backtest, "Ssh", Clean)
    ok, why = backtest._board_dataset_ready("10.64.91.57", "/mnt/vio_datasets/ysdata/some_dataset")
    assert ok is True
    assert why == ""


def test_env_status_emits_actionable_setup_command(monkeypatch):
    """When DATA_ROOT isn't NFS-exported, the operator must get a copy-pasteable
    command that sets DATA_ROOT and calls the absolute setup_nfs.sh path."""
    import subprocess as _sp

    # not exported -> actionable command
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _sp.CompletedProcess([], 0, stdout="", stderr=""))
    e = backtest.env_status()
    assert e["nfs_exported"] is False
    assert e["setup_command"].startswith("sudo DATA_ROOT=")
    assert f"DATA_ROOT={config.DATA_ROOT}" in e["setup_command"]
    assert e["setup_command"].endswith("setup_nfs.sh")
    assert e["setup_hint"].startswith("run: ")

    # exported -> no hint/command
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _sp.CompletedProcess([], 0, stdout=config.DATA_ROOT, stderr=""))
    e2 = backtest.env_status()
    assert e2["nfs_exported"] is True
    assert e2["setup_command"] == ""
    assert e2["setup_hint"] == ""
