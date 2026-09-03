"""Regression tests for the empty-logs / empty-stats collection race.

The bug: `_wait_finish` used `seen_vio and vio == 0` as the "run is finished"
signal, which can fire a few seconds BEFORE the VIO flushes ov_est.tum. The
platform then SFTP'd an empty `current/output`, logged 0-byte logs, and built
stats with nothing — "logs are empty / stats table is empty".

The fix: once vio==0 is seen, `_wait_finish` confirms the trajectory actually
flushed (`_board_output_ready`) within a bounded grace before declaring done,
so a still-flushing run is not cut off and collected empty.

These run fully offline: `server.boards.Ssh` (used by _board_output_ready via a
local import) and `backtest.backtest_status` are replaced with fakes; the clock
is faked so the grace window's real-time deadline is reached instantly.
"""
from server import backtest, batch, boards


class _Clock:
    """Fake time module: sleep advances the clock so the grace deadline hits."""

    def __init__(self):
        self.now = 0.0

    def sleep(self, s):
        self.now += s

    def time(self):
        return self.now


class _ReadySsh:
    """Fake server.boards.Ssh for _board_output_ready (local `from .boards import Ssh`)."""

    def __init__(self, ready):
        self.ready = ready

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cmd, timeout=None):
        return {"rc": 0, "out": "READY\n" if self.ready else "NOT\n", "err": ""}


class _State:
    """Sequence-driven fake: pops the next value on each call (last repeats)."""

    def __init__(self, seq):
        self.seq = list(seq)

    def next(self):
        if len(self.seq) == 1:
            return self.seq[0]
        return self.seq.pop(0)


def test_board_output_ready_true_when_trajectory_flushed(monkeypatch):
    monkeypatch.setattr(boards, "Ssh", lambda ip, timeout=None: _ReadySsh(True))
    assert batch._board_output_ready("10.64.91.57") is True


def test_board_output_ready_false_when_not_flushed(monkeypatch):
    monkeypatch.setattr(boards, "Ssh", lambda ip, timeout=None: _ReadySsh(False))
    assert batch._board_output_ready("10.64.91.57") is False


def test_wait_finish_holds_done_until_trajectory_flushes(monkeypatch):
    """viol==0 must NOT immediately declare done when ov_est.tum isn't yet on the
    board: _wait_finish stays inside the output grace and only returns ok once the
    trajectory appears (the flush that the old code collected as empty)."""
    clk = _Clock()
    monkeypatch.setattr(batch.time, "sleep", clk.sleep)
    monkeypatch.setattr(batch.time, "time", clk.time)

    vio_states = _State([1, 0, 0])       # alive → gone → still gone (polled during grace)
    ready_states = _State([False, True])  # first vio==0 poll: not flushed; grace poll: flushed

    def fake_status(ip):
        return {"processes": {"vio": vio_states.next(), "bag_play": 0}, "crash": None}

    monkeypatch.setattr(backtest, "backtest_status", fake_status)
    monkeypatch.setattr(boards, "Ssh", lambda ip, timeout=None: _ReadySsh(ready_states.next()))

    ok, err = batch._wait_finish("10.64.91.57", expect_bag_play=False)
    assert ok is True
    assert err == ""


def test_wait_finish_returns_crash_error_when_vio_died(monkeypatch):
    clk = _Clock()
    monkeypatch.setattr(batch.time, "sleep", clk.sleep)
    monkeypatch.setattr(batch.time, "time", clk.time)

    def fake_status(ip):
        return {"processes": {"vio": 0, "bag_play": 0},
                "crash": {"cause": "bad config value"}}

    monkeypatch.setattr(backtest, "backtest_status", fake_status)
    monkeypatch.setattr(boards, "Ssh", lambda ip, timeout=None: _ReadySsh(False))

    ok, err = batch._wait_finish("10.64.91.57", expect_bag_play=False)
    assert ok is False
    assert "crash" in err or "config" in err or "vio.log" in err


def test_wait_finish_resumes_when_vio_comes_back_within_grace(monkeypatch):
    """A pgrep blip (vio reported 0, then alive again) must not be treated as an
    empty run: _wait_finish resumes normal waiting instead of collecting now."""
    clk = _Clock()
    monkeypatch.setattr(batch.time, "sleep", clk.sleep)
    monkeypatch.setattr(batch.time, "time", clk.time)

    # vio blips to 0 (output still not flushed), then comes back to 1 (resume),
    # then goes to 0 again once the trajectory has flushed → done.
    vio_states = _State([1, 0, 1, 0])
    ready_states = _State([False, True])  # not flushed on the blip, flushed later

    def fake_status(ip):
        return {"processes": {"vio": vio_states.next(), "bag_play": 0}, "crash": None}

    monkeypatch.setattr(backtest, "backtest_status", fake_status)
    monkeypatch.setattr(boards, "Ssh", lambda ip, timeout=None: _ReadySsh(ready_states.next()))

    ok, err = batch._wait_finish("10.64.91.57", expect_bag_play=False)
    assert ok is True
    assert err == ""


def test_wait_finish_accepts_genuinely_empty_run_after_grace(monkeypatch):
    """If the board has a real run with no VIO output at all, the grace expiry
    returns done (accept) rather than hanging a no-output case forever."""
    clk = _Clock()
    monkeypatch.setattr(batch.time, "sleep", clk.sleep)
    monkeypatch.setattr(batch.time, "time", clk.time)

    # VIO was seen alive once (so the "run started" signal is satisfied), then
    # goes away with the trajectory never flushing — the grace expires and the
    # run is accepted rather than hanging forever collecting nothing.
    vio_states = _State([1, 0])  # alive → gone (then stays gone)

    def fake_status(ip):
        return {"processes": {"vio": vio_states.next(), "bag_play": 0}, "crash": None}

    monkeypatch.setattr(backtest, "backtest_status", fake_status)
    monkeypatch.setattr(boards, "Ssh", lambda ip, timeout=None: _ReadySsh(False))

    ok, err = batch._wait_finish("10.64.91.57", expect_bag_play=False)
    assert ok is True
    assert err == ""
