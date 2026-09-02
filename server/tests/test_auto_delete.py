"""``auto_test.delete_task`` by default only drops the queue entry — the
collected result must stay in 统计. Only an explicit remove_result=True
hard-deletes the result dir (that's what a stats delete is for)."""
import os

from server import auto_test


def test_delete_task_default_keeps_result_dir(monkeypatch):
    rd = tempfile = __import__("tempfile").mkdtemp(prefix="at_keep_")
    monkeypatch.setattr(auto_test, "_state", {"tasks": [
        {"id": "t1", "result_dir": rd, "source": "auto"},
    ]})
    monkeypatch.setattr(auto_test, "_save_state", lambda: None)

    res = auto_test.delete_task("t1")

    assert res["ok"] is True
    assert auto_test._state["tasks"] == []  # 队列记录已摘
    assert os.path.isdir(rd)                 # 结果保留在统计
    os.rmdir(rd)


def test_delete_task_explicit_remove_result_true_removes_dir(monkeypatch):
    rd = __import__("tempfile").mkdtemp(prefix="at_rm_")
    monkeypatch.setattr(auto_test, "_state", {"tasks": [
        {"id": "t2", "result_dir": rd, "source": "auto"},
    ]})
    monkeypatch.setattr(auto_test, "_save_state", lambda: None)

    auto_test.delete_task("t2", remove_result=True)

    assert not os.path.isdir(rd)


def test_uncollected_auto_task_not_surfaced_in_stats(monkeypatch, tmp_path):
    """统计页只放收集出的自动结果；未跑完/失败的自动任务不应出现在统计里。

    这修复了「在任务列表删一条未完成的任务，统计里的占位也跟着消失」的困惑——
    因为占位行只是从队列镜像出来的，背后没有结果文件。占位的归属是任务列表。
    """
    from server import batch
    root = tmp_path / "results"
    batch.RESULTS_DIR = str(root)
    auto_test.RESULTS_DIR = str(root)
    # 已收集（磁盘上有 _meta.json）→ 应出现在统计
    rdA = root / "auto" / "f974b64d5c" / "exp1" / "ds__A"
    rdA.mkdir(parents=True)
    (rdA / "_meta.json").write_text("{}")
    # 未收集（failed，无结果目录）→ 不应出现在统计
    auto_test._state = {"tasks": [
        {"id": "a1", "status": "done", "source": "auto", "result_dir": str(rdA),
         "commit_short": "f974b64d5c", "commit": "x" * 40, "experiment": "exp1",
         "dataset": "ds/A", "queued_at": "2026-01-01T00:00:00", "finished_at": "2026-01-01T01:00:00",
         "board_ip": "1.1.1.1", "run_no": "", "error": ""},
        {"id": "b1", "status": "failed", "source": "auto", "result_dir": "",
         "commit_short": "abc1234567", "commit": "y" * 40, "experiment": "",
         "dataset": "ds/B", "queued_at": "2026-01-02T00:00:00", "finished_at": "2026-01-02T01:00:00",
         "board_ip": "1.1.1.1", "run_no": "", "error": "boom"},
    ]}
    auto_test._save_state = lambda: None

    rows = batch.list_results()

    # 只有收集出的 auto 结果；失败/未收集的不作为占位出现
    assert len(rows) == 1
    assert rows[0].get("source") == "auto"
    assert not rows[0].get("live")
    assert rows[0].get("dataset") == "ds/A"
