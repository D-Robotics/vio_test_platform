"""A commit-test task that fails before result collection (build / deploy /
backtest-start) must still land in 统计 as a failed row.

It writes a status=failed _meta.json into the run's result dir; without it the
run only shows in the task queue and never in /api/results.
"""
import json
import os

from server import auto_test, batch


def _task(**over):
    t = {
        "id": "task-x", "status": "running", "phase": "building",
        "commit": "a" * 40, "commit_short": "a" * 10,
        "commit_date": "2026-01-01T00:00:00", "commit_author": "dev",
        "commit_msg": "msg", "dataset": "ysdata/ds1", "experiment": "",
        "offline_bag": True, "source": "daily", "queued_at": "2026-01-01T00:00:00",
        "started_at": "2026-01-01T00:00:01", "finished_at": "", "result_dir": "",
        "error": "", "build_log_tail": "", "run_no": "daily-test-1",
        "board_ip": "192.168.1.15",
    }
    t.update(over)
    return t


def test_record_failed_result_writes_meta_and_lists(tmp_path, monkeypatch):
    task = _task()
    monkeypatch.setattr(auto_test, "_state", {"tasks": [dict(task)]})
    roots = os.path.join(str(tmp_path), "results", "auto")
    monkeypatch.setattr(auto_test, "_RESULTS_ROOT", roots)
    monkeypatch.setattr(batch, "RESULTS_DIR", os.path.dirname(roots))

    auto_test._record_failed_result(task, "deploy: deploy exception: Auth failed")

    assert task["status"] == "failed"
    assert task["error"] == "deploy: deploy exception: Auth failed"

    meta_p = os.path.join(roots, "a" * 10, "_baseline", "ysdata__ds1", "_meta.json")
    assert os.path.isfile(meta_p)
    meta = json.load(open(meta_p, encoding="utf-8"))
    assert meta["status"] == "failed"
    assert meta["error"] == "deploy: deploy exception: Auth failed"
    assert meta["dataset"] == "ysdata/ds1"
    assert meta["type"] == "daily"        # source=daily -> 定时回测 group
    assert meta.get("result_dir", "") == ""  # the dir path itself IS the result

    # also surfaced by /api/results (batch.list_results scans for _meta.json)
    rows = batch.list_results()
    hits = [r for r in rows if r["status"] == "failed" and r["error"]]
    assert any(r["dataset"] == "ysdata/ds1" and r["error"].startswith("deploy:")
               for r in hits)


def test_record_failed_result_marks_failed_even_if_dir_write_fails(tmp_path, monkeypatch):
    """The task must be marked failed even when the placeholder write raises
    (e.g. an unwritable results root) — meta writing is best-effort."""
    import os as _os
    task = _task()
    monkeypatch.setattr(auto_test, "_state", {"tasks": [dict(task)]})
    monkeypatch.setattr(batch, "RESULTS_DIR", str(tmp_path) + "/results")

    def boom(*a, **k):
        raise OSError("no space")

    monkeypatch.setattr(auto_test, "_task_outdir", boom)
    auto_test._record_failed_result(task, "build: something broke")
    assert task["status"] == "failed"
