"""Automated commit-level backtesting.

A daemon thread runs two time-triggered loops:
  - hourly: `git fetch` from the configured GitHub remote, diff against the
    last-seen SHA, and enqueue one task per new commit × (dataset, experiment)
    from the auto config.
  - daily: at the configured HH:MM, run all pending tasks sequentially.
    Each task: git checkout → cross-build → SCP install/ to the board → reuse
    backtest.start_backtest → wait for finish → collect results into
    results/auto/<commit_short>/<experiment_or_baseline>/<dataset>/.

State (config + task queue + history) is persisted to auto_test_state.json.

Manual backtest also uses this: when the user picks a commit from the
dropdown, /api/auto/enqueue creates one task per (dataset, experiment) and
the scheduler runs it immediately (or the user can hit /api/auto/daily_run).
"""
import datetime
import json
import os
import re
import subprocess
import threading
import time
import types

from . import backtest, batch, config, experiments as _experiments, runno
from .boards import Ssh

# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------
_STATE_FILE = os.path.join(config.REPO_DIR, "auto_test_state.json")
_MIRROR_DIR = os.path.join(config.REPO_DIR, ".cache", "vio_mirror")
_RESULTS_ROOT = os.path.join(batch.RESULTS_DIR, "auto")
_BUILD_SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build_x5_docker.sh")
)
# extra workspace pkgs (irobot_create_msgs, trial_guard) that drobotics_vio
# depends on but are NOT in /opt/tros/humble — populated once into .cache.
_BUILD_EXTRAS = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".cache", "x5_extras")
)
_DEFAULT_BUILD_CMD = f"bash {_BUILD_SCRIPT} . {_BUILD_EXTRAS}"

# -----------------------------------------------------------------------------
# Defaults
# -----------------------------------------------------------------------------
_DEFAULT_CONFIG = {
    "enabled": False,
    "github_url": "https://github.com/D-Robotics/drobotics_vio.git",
    "branch": "master",
    "hourly_check": True,
    "daily_time": "02:00",
    "board_ip": "",
    "datasets": [],
    "experiments": [],
    "offline_bag": True,
    "build_enabled": True,
    "build_cmd": _DEFAULT_BUILD_CMD,
    "board_install_path": config.BOARD_INSTALL_DIR,
    "use_proxy": False,
    "launch_script_override": "",
}

# in-memory state
_state_lock = threading.Lock()
_state = {"config": dict(_DEFAULT_CONFIG), "tasks": [], "last_seen_sha": "",
           "last_hourly_check": "", "last_daily_run": "", "next_hourly_check": "",
           "next_daily_run": ""}
_scheduler_thread = None
_scheduler_stop = threading.Event()
# calendar-date lock for the once-per-day auto regression; reset to "" when a
# daily task is permanently deleted so today's scheduled run can fire again
_daily_gate_date = ""

# forward-declared outdir helper (kept local to avoid batch.BatchRun coupling)
def _task_outdir(commit: str, experiment: str, dataset: str) -> str:
    short = (commit or "unknown")[:10]
    exp_dir = experiment if experiment else "_baseline"
    safe_ds = dataset.replace("/", "__")
    d = os.path.join(_RESULTS_ROOT, short, exp_dir, safe_ds)
    os.makedirs(d, exist_ok=True)
    return d


# -----------------------------------------------------------------------------
# Persistence
# -----------------------------------------------------------------------------
def _load_state():
    global _state
    if os.path.isfile(_STATE_FILE):
        try:
            with open(_STATE_FILE, encoding="utf-8") as f:
                loaded = json.load(f)
            # merge config defaults so new keys appear in old state files
            cfg = dict(_DEFAULT_CONFIG)
            cfg.update(loaded.get("config", {}))
            # migrate: the board's preinstalled demo install is no longer the
            # run/deploy target (it lacks ov_web; we deploy our own build)
            if cfg.get("board_install_path") == "/userdata/demo/install":
                cfg["board_install_path"] = config.BOARD_INSTALL_DIR
            # migrate: build-tros-x5 doesn't exist on this host; the new
            # cross-build runs in docker with the board sysroot
            if cfg.get("build_cmd") == "build-tros-x5 drobotics_vio":
                cfg["build_cmd"] = _DEFAULT_BUILD_CMD
            _state = {
                "config": cfg,
                "tasks": loaded.get("tasks", []),
                "last_seen_sha": loaded.get("last_seen_sha", ""),
                "last_hourly_check": loaded.get("last_hourly_check", ""),
                "last_daily_run": loaded.get("last_daily_run", ""),
                "next_hourly_check": loaded.get("next_hourly_check", ""),
                "next_daily_run": loaded.get("next_daily_run", ""),
            }
        except (json.JSONDecodeError, OSError):
            pass
    # a task still marked "running" at boot means the server died mid-run:
    # nothing is actually executing now, so fail it instead of leaving it stuck
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for t in _state["tasks"]:
        if t.get("status") == "running":
            t["status"] = "failed"
            t["phase"] = ""
            t["error"] = "server restarted while task was running"
            t["finished_at"] = now


def _save_state():
    tmp = _STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _STATE_FILE)
    except OSError:
        pass


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------
def get_config() -> dict:
    with _state_lock:
        return dict(_state["config"])


def update_config(partial: dict) -> dict:
    with _state_lock:
        cfg = _state["config"]
        for k, v in partial.items():
            if k in cfg and v is not None:
                cfg[k] = v
        _save_state()
        return dict(cfg)


def list_tasks(status_filter: str = "", limit: int = 200) -> list:
    with _state_lock:
        tasks = list(_state["tasks"])
    if status_filter:
        tasks = [t for t in tasks if t["status"] == status_filter]
    # most recent first
    tasks.sort(key=lambda t: t.get("queued_at", ""), reverse=True)
    if limit > 0:
        tasks = tasks[:limit]
    out = []
    for t in tasks:
        d = dict(t)
        d.setdefault("phase", "")
        out.append(d)
    return out


def get_scheduler_status() -> dict:
    with _state_lock:
        return {
            "scheduler_alive": _scheduler_thread is not None and _scheduler_thread.is_alive(),
            "config": dict(_state["config"]),
            "last_seen_sha": _state["last_seen_sha"],
            "last_hourly_check": _state["last_hourly_check"],
            "last_daily_run": _state["last_daily_run"],
            "next_hourly_check": _state["next_hourly_check"],
            "next_daily_run": _state["next_daily_run"],
            "pending": sum(1 for t in _state["tasks"] if t["status"] == "pending"),
            "running": sum(1 for t in _state["tasks"] if t["status"] == "running"),
            "done": sum(1 for t in _state["tasks"] if t["status"] == "done"),
            "failed": sum(1 for t in _state["tasks"] if t["status"] == "failed"),
        }


# -----------------------------------------------------------------------------
# Git / mirror
# -----------------------------------------------------------------------------
def _ensure_mirror(use_proxy: "bool | None" = None) -> None:
    """Clone the GitHub remote if not already cloned. Idempotent.

    Critical: if the clone fails (network, auth, etc.), we MUST NOT leave a
    hollow _MIRROR_DIR — subsequent `git -C <mirror>` calls would walk up the
    filesystem and silently operate on whatever parent repo they find (e.g.
    the drobotics_vio checkout the test_platform itself lives in), which
    corrupts last_seen_sha and lets "fetch succeeded" lie to the user.

    use_proxy: None → follow the auto config; True/False → force/forbid the
    proxychains4 wrapper (a manual batch can pin its own proxy choice).
    """
    cfg = get_config()
    if os.path.isdir(os.path.join(_MIRROR_DIR, ".git")):
        return
    os.makedirs(os.path.dirname(_MIRROR_DIR), exist_ok=True)
    url = cfg.get("github_url", "")
    if not url:
        return
    cmd = ["git", "clone", url, _MIRROR_DIR]
    if (cfg.get("use_proxy", False) if use_proxy is None else use_proxy):
        cmd = ["proxychains4"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        r = None
    # if the clone did not produce a .git dir, remove the hollow remnant so
    # the next call retries instead of inheriting a broken state
    if not os.path.isdir(os.path.join(_MIRROR_DIR, ".git")):
        try:
            import shutil
            shutil.rmtree(_MIRROR_DIR, ignore_errors=True)
        except OSError:
            pass


def _git(args: list, timeout: int = 120, net: bool = False,
         use_proxy: "bool | None" = None) -> tuple:
    """Run `git -C <mirror> <args>`; returns (rc, stdout, stderr).

    net=True marks a network operation (fetch); when the auto config has
    use_proxy enabled the call is wrapped in proxychains4 so GitHub is
    reachable through the local proxy. use_proxy overrides that decision
    (None = follow the auto config), so a manual batch can pin its own.

    Returns rc=128 with a clear error if the mirror is not a git repo —
    otherwise git would walk up the directory tree and silently target the
    parent repo.
    """
    if not os.path.isdir(os.path.join(_MIRROR_DIR, ".git")):
        return 128, "", f"mirror is not a git repo: {_MIRROR_DIR} (clone may have failed — check network / use_proxy)"
    cmd = ["git", "-C", _MIRROR_DIR] + args
    if net and (get_config().get("use_proxy", False) if use_proxy is None else use_proxy):
        cmd = ["proxychains4"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"


def mirror_head_sha() -> str:
    """Current checked-out commit of the mirror ("" if unavailable)."""
    rc, out, _ = _git(["rev-parse", "HEAD"])
    return out.strip() if rc == 0 else ""


def fetch_new_commits() -> dict:
    """Fetch origin and list new commits since last_seen_sha. Enqueue tasks.

    Returns {fetched, new_commits, enqueued, error}.

    First-sync policy: if last_seen_sha is empty (fresh state / never synced),
    we DO NOT backfill. We just record the current origin HEAD as the baseline
    and return zero commits. Rationale: the mirror tracks the upstream GitHub
    repo which has the full OpenVINS history — enqueuing all of them would
    burn hours of board time on commits that are not what the user is trying
    to regression-test.
    """
    _ensure_mirror()
    cfg = get_config()
    branch = cfg.get("branch", "master")
    rc, out, err = _git(["fetch", "origin", branch, "--tags", "--prune"], timeout=300, net=True)
    if rc != 0:
        return {"fetched": False, "new_commits": [], "enqueued": 0,
                "error": f"git fetch failed: {err.strip() or out.strip()}"}
    with _state_lock:
        last = _state["last_seen_sha"]
    if not last:
        # first sync: just pin the baseline at current origin HEAD
        rc3, out3, _ = _git(["rev-parse", f"origin/{branch}"])
        if rc3 != 0:
            return {"fetched": True, "new_commits": [], "enqueued": 0,
                    "error": "git rev-parse failed"}
        with _state_lock:
            _state["last_seen_sha"] = out3.strip()
            _state["last_hourly_check"] = datetime.datetime.now().isoformat(timespec="seconds")
            _save_state()
        return {"fetched": True, "new_commits": [], "enqueued": 0,
                "error": "", "note": "first sync: pinned baseline at origin HEAD, no backfill"}
    rc2, out2, _ = _git(["log", f"{last}..origin/{branch}",
                          "--format=%H|%ci|%an|%s"])
    if rc2 != 0:
        return {"fetched": True, "new_commits": [], "enqueued": 0,
                "error": "git log failed"}
    new_commits = []
    for line in out2.splitlines():
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        sha, date, author, msg = parts
        new_commits.append({"sha": sha, "date": date, "author": author, "msg": msg})
    # chronological order: oldest first (so oldest commit runs first)
    new_commits.reverse()
    enqueued = 0
    if new_commits:
        enqueued = _enqueue_commits(new_commits, source="hourly")
        # advance last_seen_sha to the newest commit
        with _state_lock:
            _state["last_seen_sha"] = new_commits[-1]["sha"]
            _state["last_hourly_check"] = datetime.datetime.now().isoformat(timespec="seconds")
            _save_state()
    else:
        with _state_lock:
            _state["last_hourly_check"] = datetime.datetime.now().isoformat(timespec="seconds")
            _save_state()
    return {"fetched": True, "new_commits": new_commits, "enqueued": enqueued, "error": ""}


def _enqueue_commits(commits: list, source: str = "hourly",
                     datasets: "list | None" = None, experiments: "list | None" = None,
                     offline_bag: "bool | None" = None, board_ip: "str | None" = None) -> int:
    """For each commit × each (dataset, experiment), create a task.

    Defaults come from the auto config; callers may pass explicit overrides
    (manual enqueue) without mutating the persisted config.

    source "daily" tags the task id with the date so the scheduled regression
    always enqueues a fresh set even when the commit was already tested by the
    hourly fetch path.
    """
    cfg = get_config()
    datasets = datasets if datasets is not None else (cfg.get("datasets", []) or [""])
    experiments = experiments if experiments is not None else (cfg.get("experiments", []) or [""])
    # explicit [] from the API means the same as the cfg fallback's empty list:
    # baseline-only (one task per dataset with experiment="")
    experiments = experiments or [""]
    baseline_offline = offline_bag if offline_bag is not None else cfg.get("offline_bag", True)
    board_ip = board_ip if board_ip is not None else cfg.get("board_ip", "")
    if not datasets:
        return 0
    kind = "daily" if source == "daily" else "commit"
    tid_prefix = (f"daily_{datetime.date.today().strftime('%Y%m%d')}__"
                  if source == "daily" else "")
    enqueued = 0
    with _state_lock:
        existing_ids = {t["id"] for t in _state["tasks"]}
        now = datetime.datetime.now().isoformat(timespec="seconds")
        for c in commits:
            sha = c["sha"]
            for ds in datasets:
                for exp in experiments:
                    exp = exp or ""
                    tid = f"{tid_prefix}{sha[:10]}__{ds.replace('/', '__')}__{exp or 'baseline'}"
                    if tid in existing_ids:
                        continue
                    _state["tasks"].append({
                        "id": tid,
                        "run_no": runno.next_run_no(kind, sha),
                        "commit": sha,
                        "commit_short": sha[:10],
                        "commit_date": c.get("date", ""),
                        "commit_author": c.get("author", ""),
                        "commit_msg": c.get("msg", ""),
                        "dataset": ds,
                        "experiment": exp,
                        # named experiments carry their own flag (experiment modal
                        # sidecar); baseline takes the caller/auto-config default
                        "offline_bag": _experiments.get_offline_bag(exp) if exp else baseline_offline,
                        "source": source,
                        "status": "pending",
                        "phase": "",
                        "queued_at": now,
                        "started_at": "",
                        "finished_at": "",
                        "result_dir": "",
                        "error": "",
                        "build_log_tail": "",
                        "board_ip": board_ip,
                    })
                    existing_ids.add(tid)
                    enqueued += 1
        _save_state()
    return enqueued


def _enqueue_daily() -> int:
    """Scheduled regression: enqueue the full dataset × experiment matrix on
    the current origin HEAD, tagged source="daily" (numbered daily-test-N)."""
    _ensure_mirror()
    cfg = get_config()
    branch = cfg.get("branch", "master")
    rc, out, _ = _git(["log", f"origin/{branch}", "-n", "1", "--format=%H|%ci|%an|%s"])
    if rc != 0 or not out.strip():
        return 0
    sha, date, author, msg = out.strip().split("|", 3)
    return _enqueue_commits([{"sha": sha, "date": date, "author": author, "msg": msg}],
                            source="daily")


_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def enqueue_manual(commit: str, datasets: list, experiments: list,
                  offline_bag: bool, board_ip: str = "") -> dict:
    """Manual enqueue: one commit × given datasets × experiments.

    Pulls commit metadata from the mirror (or fetches first if unknown).
    """
    commit = (commit or "").strip()
    if not _COMMIT_RE.match(commit):
        return {"ok": False, "detail": f"invalid commit sha: {commit!r}"}
    _ensure_mirror()
    # if commit is not yet in mirror, fetch first
    rc, out, _ = _git(["log", commit, "-n", "1", "--format=%H|%ci|%an|%s"])
    if rc != 0:
        # try fetching
        cfg = get_config()
        _git(["fetch", "origin", cfg.get("branch", "master"), "--tags", "--prune"], timeout=300, net=True)
        rc, out, _ = _git(["log", commit, "-n", "1", "--format=%H|%ci|%an|%s"])
        if rc != 0:
            return {"ok": False, "detail": f"commit {commit} not found in mirror"}
    parts = out.strip().split("|", 3)
    if len(parts) != 4:
        return {"ok": False, "detail": "could not parse commit metadata"}
    sha, date, author, msg = parts
    commits = [{"sha": sha, "date": date, "author": author, "msg": msg}]
    n = _enqueue_commits(commits, source="manual", datasets=datasets,
                         experiments=experiments, offline_bag=offline_bag,
                         board_ip=board_ip or None)
    if n == 0:
        return {"ok": True, "enqueued": 0, "commit": sha, "commit_short": sha[:10],
                "detail": "no new task enqueued (already in queue)"}
    # kick the scheduler to run pending tasks immediately
    _kick_scheduler()
    return {"ok": True, "enqueued": n, "commit": sha, "commit_short": sha[:10]}


def list_known_commits(limit: int = 100, branch: str = "") -> list:
    """Return recent commits from the mirror for the dropdown.

    branch="": use the config default branch (auto回测基线). A concrete branch
    (from #bt-branch) lists that branch's history so the manual commit dropdown
    follows the selected code branch.
    """
    _ensure_mirror()
    ref = f"origin/{branch or get_config().get('branch', 'master')}"
    rc, out, _ = _git(["log", ref, "-n", str(limit),
                       "--format=%H|%h|%ci|%an|%s"])
    if rc != 0:
        return []
    commits = []
    for line in out.splitlines():
        parts = line.split("|", 4)
        if len(parts) != 5:
            continue
        sha, short, date, author, msg = parts
        commits.append({"sha": sha, "short": short, "date": date, "author": author, "msg": msg})
    return commits


def list_remote_branches(fetch: bool = False) -> list:
    """Return remote branch names from the mirror (origin/<name>, no HEAD symref).

    fetch=True refreshes origin first (network op, honours use_proxy); the
    default reads the cached refs so the UI stays fast.
    """
    _ensure_mirror()
    if fetch:
        _git(["fetch", "origin", "--prune"], timeout=300, net=True)
    rc, out, _ = _git(["for-each-ref",
                       "--format=%(refname:short)",
                       "refs/remotes/origin/"])
    if rc != 0:
        return []
    branches = []
    for line in out.splitlines():
        name = line.strip()
        if not name or name == "origin/HEAD":
            continue
        if name.startswith("origin/"):
            name = name[len("origin/"):]
        branches.append(name)
    return sorted(branches)


# -----------------------------------------------------------------------------
# Task execution
# -----------------------------------------------------------------------------
def _run_build(commit: str) -> tuple:
    """Checkout + build. Returns (ok, log_tail)."""
    cfg = get_config()
    rc, _, err = _git(["checkout", "-f", commit])
    if rc != 0:
        return False, f"git checkout failed: {err}"
    if not cfg.get("build_enabled", True):
        return True, "build skipped (build_enabled=false)"
    build_cmd = cfg.get("build_cmd", "")
    if not build_cmd:
        return True, "build skipped (no build_cmd)"
    try:
        r = subprocess.run(build_cmd, shell=True, cwd=_MIRROR_DIR,
                           capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return False, "build timeout"
    except Exception as e:  # noqa: BLE001
        return False, f"build exception: {e}"
    tail = (r.stdout + "\n" + r.stderr)[-2000:]
    if r.returncode != 0:
        return False, f"build rc={r.returncode}\n{tail}"
    return True, tail


def _deploy_install(ip: str) -> tuple:
    """Tar install/ on host, SFTP to board, extract into board_install_path.

    Returns (ok, detail).
    """
    cfg = get_config()
    install_dir = os.path.join(_MIRROR_DIR, "install")
    if not os.path.isdir(install_dir):
        return False, f"no install/ dir at {install_dir} (did build run?)"
    board_path = cfg.get("board_install_path", config.BOARD_INSTALL_DIR)
    staging = f"{config.BOARD_BASE}/_deploy"  # NOT /tmp (tmpfs, lost on reboot)
    tar_path = os.path.join(_MIRROR_DIR, ".install.tar.gz")
    try:
        r = subprocess.run(
            ["tar", "-czf", tar_path, "-C", _MIRROR_DIR, "install"],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            return False, f"tar failed: {r.stderr}"
    except Exception as e:  # noqa: BLE001
        return False, f"tar exception: {e}"
    try:
        with Ssh(ip) as s:
            cli = s._cli
            sftp = cli.open_sftp()
            try:
                s.run(f"mkdir -p {staging}")
                sftp.put(tar_path, f"{staging}/install.tar.gz")
            finally:
                sftp.close()
            # extract on board
            r = s.run(f"mkdir -p {board_path} && tar -xzf {staging}/install.tar.gz -C {staging} && "
                      f"cp -a {staging}/install/. {board_path}/ && rm -rf {staging}",
                      timeout=180)
            if r["rc"] != 0:
                return False, f"extract failed: {r['err'] or r['out']}"
            # version stamp — ensure_deployed compares it against the mirror HEAD
            # and skips re-deploying identical code
            sha = mirror_head_sha()
            if sha:
                s.run(f"echo {sha} > {board_path}/.platform_stamp", timeout=15)
    except Exception as e:  # noqa: BLE001
        return False, f"deploy exception: {e}"
    return True, f"deployed to {board_path}"


def deploy_to_board(ip: str) -> dict:
    """Public wrapper: deploy the mirror's built install/ to the board."""
    install_dir = os.path.join(_MIRROR_DIR, "install")
    if not os.path.isdir(install_dir):
        return {"ok": False,
                "detail": "镜像仓库还没有构建产物（install/ 不存在）。\n请先在「自动」面板跑一次构建，或手动交叉编译后放入。"}
    ok, detail = _deploy_install(ip)
    return {"ok": ok, "detail": detail, "board_path": get_config().get("board_install_path")}


def ensure_deployed(ip: str) -> tuple:
    """Deploy the mirror's built install/ to the board unless already current.

    Called before running a backtest so users never have to click 部署 manually.
    The board carries a `.platform_stamp` (commit sha) written by
    `_deploy_install`; when it matches the mirror HEAD the transfer is skipped.
    Returns (ok, detail).
    """
    install_dir = os.path.join(_MIRROR_DIR, "install")
    if not os.path.isdir(install_dir):
        return False, ("镜像仓库还没有构建产物（install/ 不存在），无法自动部署。"
                       "请先在「自动」面板跑一次构建，或用指定分支启动批次触发构建+部署。")
    board_path = get_config().get("board_install_path", config.BOARD_INSTALL_DIR)
    sha = mirror_head_sha()
    if sha:
        try:
            with Ssh(ip, timeout=10) as s:
                r = s.run(f"cat {board_path}/.platform_stamp 2>/dev/null", timeout=15)
            if r["rc"] == 0 and r["out"].strip() == sha:
                return True, f"板端已是最新（commit {sha[:10]}），跳过部署"
        except Exception:  # noqa: BLE001
            pass  # stamp unreadable → deploy anyway
    return _deploy_install(ip)


def _run_one_task(task: dict) -> None:
    """Execute one task's full chain: checkout/build → deploy → backtest → collect."""
    ip = task.get("board_ip") or get_config().get("board_ip", "")
    if not ip:
        _mark_failed(task, "no board_ip configured")
        return
    # 1) build
    _update_task(task["id"], phase="building")
    task["phase"] = "building"
    ok, log_tail = _run_build(task["commit"])
    _update_task(task["id"], build_log_tail=log_tail[-1000:])
    if not ok:
        _mark_failed(task, f"build: {log_tail[:500]}")
        return
    # 2) deploy
    if get_config().get("build_enabled", True):
        _update_task(task["id"], phase="deploying")
        task["phase"] = "deploying"
        ok, detail = _deploy_install(ip)
        if not ok:
            _mark_failed(task, f"deploy: {detail}")
            return
    # 3) backtest — apply the user's auto launch template if one is set (reused
    # across tasks: placeholders are rendered per-run; run_dir is never pinned
    # to the run the template was authored on).
    override = (get_config().get("launch_script_override") or "").strip() or None
    _update_task(task["id"], phase="testing")
    task["phase"] = "testing"
    try:
        r = backtest.start_backtest(ip, task["dataset"], experiment=task["experiment"] or None,
                                    offline_bag=task["offline_bag"],
                                    launch_script_override=override,
                                    follow_run_dir_marker=False)
    except Exception as e:  # noqa: BLE001
        _mark_failed(task, f"start_backtest exception: {e}")
        return
    if not r.get("ok"):
        _mark_failed(task, f"start_backtest: {r.get('detail', '')}")
        return
    # 4) wait for VIO to exit
    done, err = batch._wait_finish(ip, expect_bag_play=not task.get("offline_bag", True))
    # 5) stop + collect
    try:
        backtest.stop_backtest(ip)
    except Exception:  # noqa: BLE001
        pass
    outdir = _task_outdir(task["commit"], task["experiment"], task["dataset"])
    try:
        batch.collect_results_to_dir(ip, outdir, experiment=task["experiment"] or None,
                                     dataset=task["dataset"])
    except Exception as e:  # noqa: BLE001
        outdir = ""
    # 6) stats + meta
    _finish_task(task, done and not err, err or "", outdir)
    if outdir:
        try:
            shim = types.SimpleNamespace(result_dir=outdir, dataset=task["dataset"])
            task["stats"] = batch._make_stats(shim)
        except Exception:  # noqa: BLE001 — stats are best-effort, never fail the task
            task["stats"] = {}
        _write_meta(task, outdir)


def _mark_failed(task: dict, err: str):
    with _state_lock:
        for t in _state["tasks"]:
            if t["id"] == task["id"]:
                t["status"] = "failed"
                t["phase"] = ""
                t["error"] = err
                t["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
                break
        _save_state()


def _finish_task(task: dict, ok: bool, err: str, outdir: str):
    with _state_lock:
        for t in _state["tasks"]:
            if t["id"] == task["id"]:
                t["status"] = "done" if ok else "failed"
                t["phase"] = ""
                t["error"] = err
                t["result_dir"] = outdir
                t["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
                # keep the caller's copy in sync — _write_meta(task) reads it
                task.update(t)
                break
        _save_state()


def _update_task(task_id: str, **fields):
    with _state_lock:
        for t in _state["tasks"]:
            if t["id"] == task_id:
                t.update(fields)
        _save_state()


def _write_meta(task: dict, outdir: str):
    try:
        with open(os.path.join(outdir, "_meta.json"), "w", encoding="utf-8") as f:
            json.dump({
                "status": task["status"], "error": task["error"],
                "queued_at": task["queued_at"],
                "started_at": task["started_at"],
                "finished_at": task["finished_at"],
                "commit": task["commit"], "commit_short": task["commit_short"],
                "commit_date": task["commit_date"], "commit_author": task["commit_author"],
                "commit_msg": task["commit_msg"],
                "dataset": task["dataset"], "experiment": task["experiment"],
                "stats": task.get("stats", {}),
                "board": task["board_ip"], "source": task["source"],
                "run_no": task.get("run_no", ""),
                "type": runno.kind_of(task.get("source", ""), task.get("commit", "")),
            }, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


# -----------------------------------------------------------------------------
# Scheduler
# -----------------------------------------------------------------------------
_kick_event = threading.Event()


def _kick_scheduler():
    _kick_event.set()


def _scheduler_loop():
    """30s tick: hourly fetch + daily run + manual kicks."""
    global _daily_gate_date
    last_hourly_min = -1
    while not _scheduler_stop.is_set():
        cfg = get_config()
        now = datetime.datetime.now()
        # ----- hourly check (at minute 0 of every hour) -----
        # NOTE: gated on cfg.enabled — otherwise the server boot fetch runs
        # even when the user has auto-test disabled, which clutters the queue
        # with tasks they never asked for.
        if cfg.get("enabled") and cfg.get("hourly_check") and now.minute != last_hourly_min:
            if now.minute == 0 or last_hourly_min == -1:
                # on first boot always do one fetch; thereafter only at :00
                if last_hourly_min == -1 or now.minute == 0:
                    try:
                        fetch_new_commits()
                    except Exception as e:  # noqa: BLE001
                        with _state_lock:
                            _state["last_hourly_check"] = f"error: {e}"
                            _save_state()
                    next_h = (now.replace(minute=0, second=0, microsecond=0)
                               + datetime.timedelta(hours=1))
                    with _state_lock:
                        _state["next_hourly_check"] = next_h.isoformat(timespec="minutes")
                        _save_state()
                last_hourly_min = now.minute
        # ----- daily run at HH:MM -----
        daily_time = cfg.get("daily_time", "02:00")
        try:
            hh, mm = daily_time.split(":")
            target_h, target_m = int(hh), int(mm)
        except (ValueError, AttributeError):
            target_h, target_m = 2, 0
        if (now.hour == target_h and now.minute >= target_m
                and now.date().isoformat() != _daily_gate_date
                and cfg.get("enabled", False)):
            _daily_gate_date = now.date().isoformat()
            _enqueue_daily()
            _run_all_pending()
            with _state_lock:
                _state["last_daily_run"] = now.isoformat(timespec="seconds")
                # next daily = tomorrow at HH:MM
                tomorrow = now.date() + datetime.timedelta(days=1)
                _state["next_daily_run"] = datetime.datetime.combine(
                    tomorrow, datetime.time(target_h, target_m)).isoformat(timespec="minutes")
                _save_state()
        # ----- manual kick (enqueue or run-now) -----
        if _kick_event.is_set():
            _kick_event.clear()
            _run_all_pending()
        # ----- pick up pending tasks one at a time if config enabled -----
        # (so manual enqueue runs even outside daily window when enabled=true)
        if cfg.get("enabled", False):
            _run_one_pending_if_idle()
        _scheduler_stop.wait(30.0)


def _run_one_pending_if_idle():
    """If no task is running, pick the oldest pending and run it."""
    with _state_lock:
        if any(t["status"] == "running" for t in _state["tasks"]):
            return
        task = None
        for t in _state["tasks"]:
            if t["status"] == "pending":
                task = dict(t)
                # mark running in place
                t["status"] = "running"
                t["started_at"] = datetime.datetime.now().isoformat(timespec="seconds")
                break
        _save_state()
    if not task:
        return
    try:
        _run_one_task(task)
    except Exception as e:  # noqa: BLE001
        _mark_failed(task, f"scheduler exception: {e}")


def _run_all_pending():
    """Run all pending tasks sequentially. Used by daily + manual run-now button."""
    while True:
        with _state_lock:
            if any(t["status"] == "running" for t in _state["tasks"]):
                # something is already running; let the main loop continue it
                return
            task = None
            for t in _state["tasks"]:
                if t["status"] == "pending":
                    task = dict(t)
                    t["status"] = "running"
                    t["started_at"] = datetime.datetime.now().isoformat(timespec="seconds")
                    break
            _save_state()
        if not task:
            return
        try:
            _run_one_task(task)
        except Exception as e:  # noqa: BLE001
            _mark_failed(task, f"scheduler exception: {e}")


def trigger_hourly_check() -> dict:
    """Synchronous fetch + enqueue (does not run tasks)."""
    try:
        return fetch_new_commits()
    except Exception as e:  # noqa: BLE001
        return {"fetched": False, "new_commits": [], "enqueued": 0, "error": str(e)}


def trigger_daily_run() -> dict:
    """Kick the scheduler to run all pending tasks immediately."""
    _kick_scheduler()
    return {"ok": True, "detail": "kick sent; scheduler will run pending tasks sequentially"}


def start_scheduler():
    """Start the background scheduler thread. Idempotent."""
    global _scheduler_thread
    _load_state()
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="auto_scheduler")
    _scheduler_thread.start()


def stop_scheduler():
    _scheduler_stop.set()


def reset_daily_gate():
    """Clear the once-per-day lock so the daily regression can fire again today.

    Called when a daily-sourced task is permanently deleted from the queue:
    the next scheduler tick re-runs the daily gate, which re-enqueues the
    (now-freed) daily task on the configured branch at the scheduled time.
    """
    global _daily_gate_date
    _daily_gate_date = ""


def delete_task(task_id: str, remove_result: bool = False) -> dict:
    """Remove a task from the auto queue (hard delete, no cache).

    Deleting from the 自动任务 list should only drop the queue entry — the
    collected result stays in 统计 (that is what a stats delete is for). So by
    default the result dir is left untouched; pass remove_result=True to ALSO
    hard-delete it. If the removed task was a daily-sourced regression, the
    daily gate is re-armed so today's scheduled run can trigger again.
    """
    import shutil
    removed = None
    with _state_lock:
        for i, t in enumerate(_state["tasks"]):
            if t.get("id") == task_id:
                removed = _state["tasks"].pop(i)
                break
        _save_state()
    if removed is None:
        return {"ok": False, "detail": f"task not found: {task_id}"}
    rd = removed.get("result_dir") or ""
    if remove_result and rd and os.path.isdir(rd):
        try:
            shutil.rmtree(rd)
        except OSError:
            pass
    if removed.get("source") == "daily":
        reset_daily_gate()
    return {"ok": True, "detail": f"deleted task {removed.get('id')}"}
