"""Board registry (JSON file) + paramiko SSH helper."""
import json
import os
import threading

import paramiko

from . import config

_lock = threading.Lock()


def _load() -> list:
    if not os.path.isfile(config.BOARDS_FILE):
        return []
    try:
        with open(config.BOARDS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(boards: list):
    os.makedirs(config.STATE_DIR, exist_ok=True)
    with open(config.BOARDS_FILE, "w", encoding="utf-8") as f:
        json.dump(boards, f, ensure_ascii=False, indent=2)


def list_boards() -> list:
    with _lock:
        return _load()


def add_board(ip: str, user: str = None, password: str = None, note: str = "") -> list:
    with _lock:
        boards = _load()
        if any(b["ip"] == ip for b in boards):
            raise ValueError(f"board {ip} already exists")
        boards.append({"ip": ip, "user": user or config.DEFAULT_SSH_USER, "password": password or config.DEFAULT_SSH_PASS, "note": note})
        _save(boards)
        return boards


def remove_board(ip: str) -> list:
    with _lock:
        boards = [b for b in _load() if b["ip"] != ip]
        _save(boards)
        return boards


def update_board(ip: str, note: str = None, user: str = None, password: str = None) -> list:
    with _lock:
        boards = _load()
        for b in boards:
            if b["ip"] == ip:
                if note is not None:
                    b["note"] = note
                if user:
                    b["user"] = user
                if password:
                    b["password"] = password
        _save(boards)
        return boards


def get_board(ip: str) -> dict:
    for b in list_boards():
        if b["ip"] == ip:
            return b
    raise FileNotFoundError(f"board not registered: {ip}")


class Ssh:
    """Thin paramiko wrapper: exec with timeout, returns (rc, stdout, stderr)."""

    def __init__(self, ip: str, user: str = None, password: str = None, timeout: int = 20):
        self.ip = ip
        b = None
        try:
            b = get_board(ip)
        except FileNotFoundError:
            pass
        self.user = user or (b or {}).get("user", config.DEFAULT_SSH_USER)
        self.password = password or (b or {}).get("password", config.DEFAULT_SSH_PASS)
        self.timeout = timeout
        self._cli = paramiko.SSHClient()
        self._cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    def __enter__(self):
        self._cli.connect(self.ip, username=self.user, password=self.password, timeout=self.timeout, allow_agent=False, look_for_keys=False)
        return self

    def __exit__(self, *exc):
        self._cli.close()

    def run(self, cmd: str, timeout: int = 30) -> dict:
        stdin, stdout, stderr = self._cli.exec_command(cmd, timeout=timeout)
        rc = stdout.channel.recv_exit_status()
        return {"rc": rc, "out": stdout.read().decode("utf-8", "replace"), "err": stderr.read().decode("utf-8", "replace")}


def test_board(ip: str, user: str = None, password: str = None) -> dict:
    try:
        with Ssh(ip, user, password) as s:
            r = s.run("uname -a && hostname && nproc")
            return {"ok": r["rc"] == 0, "detail": r["out"].strip() or r["err"].strip()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)}
