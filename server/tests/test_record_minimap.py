"""Tests for the server-side trajectory minimap overlay in the ov_web recorder."""
import os
import sys

# make `server` importable (tests live in server/tests, repo root is server/..)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from server import record


def make_recorder(tmp_path):
    root = tmp_path / "out"
    return record.OvWebRecorder("127.0.0.1", 9988, str(root), minimap=True)


def test_update_state_uses_full_path(tmp_path):
    rec = make_recorder(tmp_path)
    rec._update_state('{"path": [[0, 0, 0], [1, 2, 0], [3, 4, 0]], "opx": 9, "opy": 8}')
    assert rec.traj_points == [(0.0, 0.0), (1.0, 2.0), (3.0, 4.0)]
    assert rec.has_path is True
    # opx/opy updates latest_pos but does not override the authoritative path
    assert rec.latest_pos == (9.0, 8.0)


def test_update_state_fallback_accumulates(tmp_path):
    rec = make_recorder(tmp_path)
    rec._update_state('{"opx": 0, "opy": 0}')
    rec._update_state('{"opx": 1, "opy": 2}')
    rec._update_state('{"opx": 1, "opy": 2}')  # duplicate consecutive -> dedup
    assert rec.has_path is False
    assert rec.traj_points == [(0.0, 0.0), (1.0, 2.0)]
    assert rec.latest_pos == (1.0, 2.0)


def test_update_state_malformed_keeps_prior(tmp_path):
    rec = make_recorder(tmp_path)
    rec._update_state('{"opx": 1, "opy": 1}')
    rec._update_state("not-json{")
    assert rec.traj_points == [(1.0, 1.0)]


def test_render_minimap_empty_returns_none(tmp_path):
    rec = make_recorder(tmp_path)
    assert rec._render_minimap() is None


def test_render_minimap_disabled_returns_none(tmp_path):
    rec = record.OvWebRecorder("127.0.0.1", 9988, str(tmp_path / "out"), minimap=False)
    rec.traj_points = [(0.0, 0.0), (1.0, 1.0)]
    assert rec._render_minimap() is None


def test_render_minimap_populated_has_alpha(tmp_path):
    rec = make_recorder(tmp_path)
    rec._update_state('{"path": [[0, 0, 0], [1, 2, 0], [3, 4, 0]]}')
    ov = rec._render_minimap()
    assert ov is not None
    assert ov.mode == "RGBA"
    assert ov.size == (rec.minimap_size, rec.minimap_size)
    # the overlay must actually draw something (non-transparent pixels exist)
    assert ov.getchannel("A").getextrema()[1] > 0


def test_loopback_jpeg_gets_composited(tmp_path):
    from io import BytesIO
    from PIL import Image

    rec = make_recorder(tmp_path)
    rec._update_state('{"path": [[0, 0, 0], [1, 2, 0]]}')
    src = Image.new("RGB", (320, 240), (30, 30, 30))
    jpg = BytesIO()
    src.save(jpg, "JPEG")
    # manually mirror the JPEG branch's composite logic
    mm = rec._render_minimap()
    assert mm is not None
    im = Image.open(BytesIO(jpg.getvalue())).convert("RGB")
    before = im.tobytes()
    im.paste(mm, (im.width - mm.width - rec._border, im.height - mm.height - rec._border), mm)
    assert im.tobytes() != before


def test_run_surfaces_record_error(tmp_path):
    """A recording failure must be exposed, not silently swallowed.

    Regression: record.py lazily `import websockets` inside `_record()`; when the
    dep was missing the ImportError hit `_run`'s bare `except Exception: pass`,
    so every run silently captured 0 frames and no video showed in stats. This
    proves the failure now lands in `last_error` and `record_error.txt`.
    """
    rec = make_recorder(tmp_path)

    # Simulate the lazy-import-failure path by forcing `_record` to raise before
    # it ever reaches the (possibly-uninstalled) websockets import.
    import asyncio

    async def failing_record():
        raise RuntimeError("import websockets: No module named 'websockets'")

    rec._record = failing_record
    rec._run()
    assert "RuntimeError" in rec.last_error
    err_file = os.path.join(rec.outdir, "record_error.txt")
    assert os.path.isfile(err_file)
    assert "websockets" in open(err_file).read()
