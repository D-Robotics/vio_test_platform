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
