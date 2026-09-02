"""Backtest run numbering: manually-<n> / daily-test-<n> / commit-<sha5>-<n>.

The trailing id is one global auto-incrementing counter, persisted to
state/backtest_seq.json so numbers keep climbing across server restarts.
"""
import json
import os
import threading

from . import config

_SEQ_FILE = os.path.join(config.STATE_DIR, "backtest_seq.json")
_lock = threading.Lock()
_state = None  # lazy {"seq": int}

TYPE_LABELS = {"manual": "手动", "daily": "定时", "commit": "commit"}


def _load():
    global _state
    if _state is None:
        _state = {"seq": 0}
        try:
            with open(_SEQ_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded.get("seq"), int):
                _state = loaded
        except (OSError, json.JSONDecodeError):
            pass


def next_run_no(kind: str, commit: str = "") -> str:
    """Allocate the next run number. kind: manual | daily | commit."""
    with _lock:
        _load()
        _state["seq"] += 1
        n = _state["seq"]
        tmp = _SEQ_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_state, f)
            os.replace(tmp, _SEQ_FILE)
        except OSError:
            pass
    if kind == "daily":
        return f"daily-test-{n}"
    if kind == "commit":
        return f"commit-{(commit or 'unknown')[:5]}-{n}"
    return f"manually-{n}"


def kind_of(source: str, commit: str = "") -> str:
    """Classify a result/task into manual | daily | commit.

    Anything carrying a commit sha (hourly fetch enqueue, manual commit
    enqueue, legacy 'auto' results) is a commit backtest; source 'daily' is
    the scheduled regression; everything else is a manual backtest.
    """
    if source == "daily":
        return "daily"
    if commit or source in ("hourly", "auto"):
        return "commit"
    return "manual"
