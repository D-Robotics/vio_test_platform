"""``auto_test.list_tasks`` always exposes a ``phase`` field so the frontend can
tell building/deploying/testing apart while a task is running. Legacy tasks
persisted before the field existed default to ""."""
import copy

from server import auto_test


def _tasks():
    return [
        {"id": "t1", "queued_at": "2026-01-01T00:00:00", "status": "pending"},
        {"id": "t2", "queued_at": "2026-01-02T00:00:00", "status": "running", "phase": "building"},
        {"id": "t3", "queued_at": "2026-01-03T00:00:00", "status": "running", "phase": "testing"},
    ]


def test_list_tasks_defaults_missing_phase_to_empty(monkeypatch):
    monkeypatch.setattr(auto_test, "_state", {"tasks": _tasks()})
    ts = {t["id"]: t for t in auto_test.list_tasks(limit=0)}
    # legacy task without a phase key must still report ""
    assert ts["t1"]["phase"] == ""
    # running tasks carry their phase through
    assert ts["t2"]["phase"] == "building"
    assert ts["t3"]["phase"] == "testing"


def test_list_tasks_does_not_mutate_state(monkeypatch):
    st = {"tasks": _tasks()}
    monkeypatch.setattr(auto_test, "_state", st)
    before = copy.deepcopy(st["tasks"])
    auto_test.list_tasks(limit=0)
    assert st["tasks"] == before
