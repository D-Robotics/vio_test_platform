"""``batch.delete_result`` is idempotent and confined to the results root.

A stale finished row can reference a result_dir that is already gone (e.g. its
parent was removed by a stats-view delete, or collection never landed). Deleting
such a path must NOT raise — it should just clean up empty parents and report
it was already removed, so the queue row clears instead of erroring.
"""
import pytest

from server import batch


def test_delete_result_already_gone_is_ok(monkeypatch, tmp_path):
    root = tmp_path / "results"
    root.mkdir()
    monkeypatch.setattr(batch, "RESULTS_DIR", str(root))
    monkeypatch.setattr(batch, "_delete_matching_auto_tasks", lambda p: 0)
    # dir was already removed; the queue still surfaces this stale result_dir
    res = batch.delete_result(str(root / "20260901_151753" / "_baseline" / "ds"))
    assert res["ok"] is True
    assert "already removed" in res["detail"]


def test_delete_result_removes_existing_dir_and_parents(monkeypatch, tmp_path):
    root = tmp_path / "results"
    d = root / "batch" / "_baseline" / "ds"
    d.mkdir(parents=True)
    (d / "_meta.json").write_text("{}")
    monkeypatch.setattr(batch, "RESULTS_DIR", str(root))
    monkeypatch.setattr(batch, "_delete_matching_auto_tasks", lambda p: 0)
    res = batch.delete_result(str(d))
    assert res["ok"] is True
    assert "deleted" in res["detail"]
    # the dataset folder and its now-empty _baseline parent are pruned
    assert not d.exists()
    assert not (root / "batch" / "_baseline").exists()


def test_delete_result_rejects_path_outside_root(monkeypatch, tmp_path):
    root = tmp_path / "results"
    root.mkdir()
    monkeypatch.setattr(batch, "RESULTS_DIR", str(root))
    with pytest.raises(ValueError):
        batch.delete_result(str(tmp_path / "elsewhere" / "ds"))


def _mk_batch(item_dirs, bid=""):
    br = batch.BatchRun("192.168.1.15", ["ds"])
    br.id = bid or br.id
    br.items = []
    for i, rd in enumerate(item_dirs):
        it = batch.BatchItem(f"ds{i}")
        it.status = "done"
        it.result_dir = rd
        br.items.append(it)
    return br


def test_delete_also_prunes_manual_batch_item(monkeypatch):
    b1 = _mk_batch(["/r/a/b", "/r/other"], bid="batch_1")
    b2 = _mk_batch(["/r/keep"], bid="batch_2")
    monkeypatch.setattr(batch, "_batches", {"batch_1": b1, "batch_2": b2})
    monkeypatch.setattr(batch, "_save_state", lambda br: None)
    monkeypatch.setattr(batch, "_forget_batch_dir", lambda bid: None)
    removed = batch._delete_matching_batch_items("/r/a/b")
    assert removed == 1
    # the matching row is dropped from batch_1; the rest survive
    assert [it.result_dir for it in b1.items] == ["/r/other"]
    assert len(b2.items) == 1


def test_delete_forgets_batch_that_drains_to_empty(monkeypatch):
    b1 = _mk_batch(["/r/a/b"], bid="batch_1")
    monkeypatch.setattr(batch, "_batches", {"batch_1": b1})
    forgotten = []
    monkeypatch.setattr(batch, "_save_state", lambda br: None)
    monkeypatch.setattr(batch, "_forget_batch_dir", lambda bid: forgotten.append(bid))
    removed = batch._delete_matching_batch_items("/r/a/b")
    assert removed == 1
    assert "batch_1" not in batch._batches
    assert forgotten == ["batch_1"]
