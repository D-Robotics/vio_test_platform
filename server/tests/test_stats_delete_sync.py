"""When a result is deleted in the 统计 (stats) view, its auto-queue task must
also be removed, so the 自动任务 list and stats stay consistent.

``batch._delete_matching_auto_tasks`` is the glue: it finds queue tasks whose
result_dir matches the deleted path and drops them from the queue (without
re-deleting the already-moved dir).
"""
import os

from server import auto_test, batch


def test_delete_matching_auto_tasks_removes_query_task(monkeypatch):
    calls = []

    def fake_list_tasks(limit=200):
        return [
            {"id": "daily_1", "result_dir": "/r/a/b", "source": "daily"},
            {"id": "daily_2", "result_dir": "/r/c/d", "source": "daily"},
            {"id": "cyclic_3", "result_dir": "/r/e/f/../b", "source": "auto"},
            {"id": "nopath", "result_dir": "", "source": "daily"},
        ]

    def fake_delete_task(task_id, remove_result=True):
        calls.append((task_id, remove_result))
        return {"ok": True}

    monkeypatch.setattr(auto_test, "list_tasks", fake_list_tasks)
    monkeypatch.setattr(auto_test, "delete_task", fake_delete_task)

    removed = batch._delete_matching_auto_tasks("/r/a/b")

    # "daily_1" matches exactly; "cyclic_3" matches after realpath normalization;
    # the realpath-normalized compare runs realpath() on the candidate, so
    # "/r/e/f/../b" resolves to "/r/e/b" != "/r/a/b" → not matched here.
    assert removed == 1
    assert calls == [("daily_1", False)]


def test_no_match_yields_zero(monkeypatch):
    monkeypatch.setattr(auto_test, "list_tasks", lambda limit=200: [
        {"id": "daily_1", "result_dir": "/r/a/b", "source": "daily"},
    ])

    def boom(*a, **k):
        raise AssertionError("should not delete on no match")

    monkeypatch.setattr(auto_test, "delete_task", boom)
    assert batch._delete_matching_auto_tasks("/r/other") == 0


def test_realpath_match_trailing_separator(monkeypatch):
    calls = []

    def fake_list_tasks(limit=200):
        return [{"id": "daily_x", "result_dir": "/r/data/ds/", "source": "daily"}]

    def fake_delete_task(task_id, remove_result=True):
        calls.append(task_id)
        return {"ok": True}

    monkeypatch.setattr(auto_test, "list_tasks", fake_list_tasks)
    monkeypatch.setattr(auto_test, "delete_task", fake_delete_task)

    # result_dir with a trailing slash must still match a canonical path
    batch._delete_matching_auto_tasks("/r/data/ds")
    assert calls == ["daily_x"]
    # ensure os.realpath is what normalises the trailing slash
    assert os.path.realpath("/r/data/ds/") == os.path.realpath("/r/data/ds")
