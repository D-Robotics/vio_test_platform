"""``batch._prune_finished_items`` is the "归档" sweep: when the user launches
the next batch, finished (done) rows leave the task queue so it only holds
in-flight work. Collected results must NOT be touched — they stay in stats.
"""
from server import batch


def _mk_batch(item_dirs, bid=""):
    br = batch.BatchRun("192.168.1.15", ["ds"])
    br.id = bid or br.id
    br.items = []
    for i, rd in enumerate(item_dirs):
        it = batch.BatchItem(f"ds{i}")
        it.result_dir = rd
        br.items.append(it)
    return br


def test_prune_drops_done_keeps_inflight(monkeypatch):
    b = _mk_batch(["/r/a/b", "/r/c/d", "/r/pending"], bid="b1")
    b.items[0].status = "done"
    b.items[1].status = "failed"
    b.items[2].status = "pending"
    monkeypatch.setattr(batch, "_batches", {"b1": b})
    monkeypatch.setattr(batch, "_save_state", lambda br: None)
    monkeypatch.setattr(batch, "_forget_batch_dir", lambda bid: None)
    monkeypatch.setattr(batch, "_forget_batch_record", lambda bid: None)

    removed = batch._prune_finished_items()

    assert removed == 1
    assert [it.status for it in b.items] == ["failed", "pending"]
    assert "b1" in batch._batches  # non-drained → record kept


def test_prune_forgets_batch_that_drains_empty(monkeypatch):
    b = _mk_batch(["/r/a/b"], bid="b1")
    b.items[0].status = "done"
    monkeypatch.setattr(batch, "_batches", {"b1": b})
    monkeypatch.setattr(batch, "_save_state", lambda br: None)
    calls = []
    monkeypatch.setattr(batch, "_forget_batch_dir", lambda bid: calls.append("dir:" + bid))
    monkeypatch.setattr(batch, "_forget_batch_record", lambda bid: calls.append("rec:" + bid))

    removed = batch._prune_finished_items()

    assert removed == 1
    assert "b1" not in batch._batches
    assert calls == ["dir:b1", "rec:b1"]


def test_prune_keeps_collected_results_on_disk(monkeypatch, tmp_path):
    root = tmp_path / "results"
    b = _mk_batch(["/r/a/b"], bid="b1")
    b.items[0].status = "done"
    # a collected result lives under RESULTS_DIR/b1/_baseline/ds
    meta_dir = root / "b1" / "_baseline" / "ds"
    meta_dir.mkdir(parents=True)
    (meta_dir / "_meta.json").write_text("{}")
    monkeypatch.setattr(batch, "RESULTS_DIR", str(root))
    monkeypatch.setattr(batch, "_batches", {"b1": b})
    monkeypatch.setattr(batch, "_save_state", lambda br: None)

    batch._prune_finished_items()

    # the batch.json record is dropped (won't reload into the queue), but the
    # collected result dir stays — it remains visible in stats
    assert not (root / "b1" / "batch.json").exists()
    assert (meta_dir / "_meta.json").exists()


def test_discard_from_queue_keeps_result_on_disk(monkeypatch, tmp_path):
    # queue 删除 = 仅摘批次行；结果目录与自动任务都不得被删（留在统计）
    root = tmp_path / "results"
    b = _mk_batch(["/r/a/b"], bid="b1")
    b.items[0].status = "done"
    meta_dir = root / "b1" / "_baseline" / "ds"
    meta_dir.mkdir(parents=True)
    (meta_dir / "_meta.json").write_text("{}")
    monkeypatch.setattr(batch, "RESULTS_DIR", str(root))
    monkeypatch.setattr(batch, "_batches", {"b1": b})

    res = batch.discard_result_from_queue("/r/a/b")

    assert res["ok"] is True
    assert res["removed"] == 1
    assert "保留在统计" in res["detail"]
    assert "b1" not in batch._batches  # 批次排空 → 记录不再存活
    assert not (root / "b1" / "batch.json").exists()  # 避免重启后已完成项复活
    assert (meta_dir / "_meta.json").exists()      # 结果保留在统计
