"""Batch backtest engine: sequential queue on one board, result collection, stats.

A batch is a list of datasets run one after another on a single board. For each
item the engine:
  1. starts the full backtest chain (backtest.start_backtest)
  2. waits for completion (VIO process exits or bag playback finishes)
  3. collects result artifacts (ov_est.tum / state_estimate.txt / result bag
     metadata) from the board via SFTP into results/<batch_id>/<dataset>/
  4. renders stats: trajectory plot PNG, preview image, endpoint, duration

State is kept in memory (single batch at a time per board) and exposed via API.
"""
import datetime
import json
import os
import shutil
import threading
import time

from . import backtest, config, datasets, experiments, runno
from .boards import Ssh
from .record import OvWebRecorder

RESULTS_DIR = os.path.join(config.REPO_DIR, "results")

# how the engine detects "run finished": VIO node gone (exited after bag end)
_WAIT_POLL_S = 5
_MAX_RUN_S = 3600  # hard cap per dataset


def _item_offline_bag(experiment: str, baseline_default: bool) -> bool:
    """Named experiments carry their own offline_bag (set in the experiment
    modal, stored as sidecar meta); the baseline ("") takes the caller's
    sidebar checkbox value."""
    return experiments.get_offline_bag(experiment) if experiment else baseline_default


class BatchItem:
    def __init__(self, dataset: str, experiment: str = "", offline_bag: bool = True):
        self.dataset = dataset
        self.experiment = experiment or ""  # "" means baseline (no override)
        self.offline_bag = offline_bag
        self.status = "pending"  # pending | running | done | failed | skipped
        self.error = ""
        self.started_at = ""
        self.finished_at = ""
        self.result_dir = ""
        self.stats = {}
        self.experiment_keys = []
        self.recorder = None

    def to_dict(self):
        d = dict(self.__dict__)
        d.pop("recorder", None)
        return d


class BatchRun:
    def __init__(self, ip: str, datasets_list: list, experiments_list: list = None,
                 offline_bag: bool = True, launch_script_override: "str | None" = None,
                 branch: str = "", verbosity: str = "INFO", vio_log_level: str = "warn",
                 use_proxy: "bool | None" = None):
        # None = 跟随 auto 配置的 use_proxy（同一 mirror）；True = 强制本批次走 proxychains4
        self.use_proxy = use_proxy
        self.id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_no = runno.next_run_no("manual")
        self.ip = ip
        # cartesian product: for each experiment, run every dataset
        experiments_list = experiments_list or [""]
        self.items = [BatchItem(d, e, offline_bag=_item_offline_bag(e, offline_bag))
                      for e in experiments_list for d in datasets_list]
        self.experiments = list(experiments_list)
        self.offline_bag = offline_bag
        # branch: non-empty → checkout origin/<branch> in the mirror, cross-build
        # and deploy to the board before the first item runs (same pipeline as
        # auto backtest). Empty → auto-deploy the mirror's already-built install/
        # before the first item (skipped when the board stamp matches).
        self.branch = branch or ""
        self.build_status = ""   # "", fetching/building/deploying, done, failed
        self.build_log = ""
        self.commit_short = ""
        # Custom script applies only when a single dataset is in the batch —
        # the script has hardcoded dataset paths that don't translate across
        # multiple datasets. For multi-dataset batches we fall back to the
        # auto-generated per-item script.
        self.launch_script_override = launch_script_override if len(datasets_list) == 1 else None
        # 日志级别（启动脚本 verbosity:= / vio_log_level:=），空串=用 launch 默认值
        self.verbosity = verbosity or ""
        self.vio_log_level = vio_log_level or ""
        self.status = "running"  # running | finished
        self.created_at = datetime.datetime.now().isoformat(timespec="seconds")
        self.thread = None

    def to_dict(self):
        return {
            "id": self.id,
            "run_no": self.run_no,
            "ip": self.ip,
            "status": self.status,
            "experiments": self.experiments,
            "offline_bag": self.offline_bag,
            "launch_script_override": self.launch_script_override,
            "branch": self.branch,
            "verbosity": self.verbosity,
            "vio_log_level": self.vio_log_level,
            "use_proxy": self.use_proxy,
            "build_status": self.build_status,
            "build_log": self.build_log[-500:] if self.build_log else "",
            "commit_short": self.commit_short,
            "created_at": self.created_at,
            "items": [i.to_dict() for i in self.items],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BatchRun":
        """Rebuild a record from batch.json without __init__ side effects
        (no new id, no run_no consumption)."""
        b = cls.__new__(cls)
        b.id = d["id"]
        b.run_no = d.get("run_no", "")
        b.ip = d.get("ip", "")
        b.items = []
        for it in d.get("items", []):
            bi = BatchItem(it.get("dataset", ""), it.get("experiment", ""),
                           it.get("offline_bag", True))
            bi.status = it.get("status", "pending")
            bi.error = it.get("error", "")
            bi.started_at = it.get("started_at", "")
            bi.finished_at = it.get("finished_at", "")
            bi.result_dir = it.get("result_dir", "")
            bi.stats = it.get("stats", {}) or {}
            bi.experiment_keys = it.get("experiment_keys", []) or []
            b.items.append(bi)
        b.experiments = d.get("experiments", [])
        b.offline_bag = d.get("offline_bag", True)
        b.branch = d.get("branch", "")
        b.build_status = d.get("build_status", "")
        b.build_log = d.get("build_log", "")
        b.commit_short = d.get("commit_short", "")
        b.launch_script_override = d.get("launch_script_override")
        b.verbosity = d.get("verbosity", "")
        b.vio_log_level = d.get("vio_log_level", "")
        b.use_proxy = d.get("use_proxy")
        b.status = d.get("status", "finished")
        b.created_at = d.get("created_at", "")
        b.thread = None
        return b


_batches: dict[str, BatchRun] = {}
_lock = threading.Lock()


def _save_state(batch: "BatchRun"):
    """Persist the batch record under its results dir so the task queue
    survives server restarts. Best-effort: a write failure must never break
    a running backtest."""
    d = os.path.join(RESULTS_DIR, batch.id)
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "batch.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(batch.to_dict(), f, ensure_ascii=False)
        os.replace(tmp, os.path.join(d, "batch.json"))
    except OSError:
        pass


def _load_persisted():
    """Rebuild in-memory batch records from results/*/batch.json at startup."""
    if not os.path.isdir(RESULTS_DIR):
        return
    for batch_id in sorted(os.listdir(RESULTS_DIR)):
        p = os.path.join(RESULTS_DIR, batch_id, "batch.json")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            b = BatchRun.from_dict(d)
        except (OSError, ValueError, KeyError):
            continue
        if b.status == "running":
            # the engine thread died with the server; settle the leftovers
            b.status = "interrupted"
            now = datetime.datetime.now().isoformat(timespec="seconds")
            for it in b.items:
                if it.status == "running":
                    it.status = "failed"
                    it.error = "服务器重启，任务中断"
                    it.finished_at = it.finished_at or now
                elif it.status == "pending":
                    it.status = "skipped"
                    it.error = "服务器重启，未执行"
        _batches[b.id] = b


_load_persisted()


def _prune_finished_items() -> int:
    """Drop finished (done) rows from every batch so the task queue only holds
    in-flight work. Collected results are NOT touched — they stay in stats
    (results/ on disk). Runs when the user launches the next batch, so the
    queue is clean on the next run. Returns the number of rows pruned."""
    forget = []
    removed = 0
    with _lock:
        for b in list(_batches.values()):
            keep = [it for it in b.items if it.status != "done"]
            if len(keep) == len(b.items):
                continue
            removed += len(b.items) - len(keep)
            b.items = keep
            if keep:
                _save_state(b)
            else:
                del _batches[b.id]
                forget.append(b.id)
    for bid in forget:
        _forget_batch_dir(bid)    # keep dir when it holds collected results
        _forget_batch_record(bid)  # drop batch.json so it won't reload
    return removed


def start_batch(ip: str, datasets_list: list, experiments_list: list = None,
                offline_bag: bool = True,
                launch_script_override: "str | None" = None,
                branch: str = "", verbosity: str = "INFO",
                vio_log_level: str = "warn", use_proxy: "bool | None" = None) -> dict:
    if not datasets_list:
        raise ValueError("empty dataset list")
    if not experiments_list:
        experiments_list = [""]
    with _lock:
        for b in _batches.values():
            if b.ip == ip and b.status == "running":
                raise ValueError(f"a batch is already running on {ip} (id {b.id})")
    _prune_finished_items()  # next run clears the queue of finished rows
    batch = BatchRun(ip, datasets_list, experiments_list, offline_bag=offline_bag,
                     launch_script_override=launch_script_override, branch=branch,
                     verbosity=verbosity, vio_log_level=vio_log_level, use_proxy=use_proxy)
    batch.thread = threading.Thread(target=_run_batch, args=(batch,), daemon=True)
    with _lock:
        _batches[batch.id] = batch
    _save_state(batch)
    batch.thread.start()
    return batch.to_dict()


def get_batch(batch_id: str) -> dict:
    with _lock:
        b = _batches.get(batch_id)
        if not b:
            raise FileNotFoundError(f"batch not found: {batch_id}")
        return b.to_dict()


def list_batches() -> list:
    with _lock:
        return [b.to_dict() for b in _batches.values()]


def stop_batch(batch_id: str) -> dict:
    """Stop the whole batch: kill board processes; running items marked failed."""
    with _lock:
        b = _batches.get(batch_id)
    if not b:
        raise FileNotFoundError(batch_id)
    if b.status == "running":
        for it in b.items:
            if it.status in ("pending", "running"):
                it.status = "failed" if it.status == "running" else "skipped"
                if it.status == "failed":
                    it.error = "batch stopped by user"
        b.status = "finished"
        backtest.stop_backtest(b.ip)
        _save_state(b)
    return b.to_dict()


def _forget_batch_dir(batch_id: str):
    """Remove results/<batch_id>/ only if it holds no collected result (no
    _meta.json anywhere) — i.e. nothing but the batch record / partial
    recordings of a run that never finished. Collected results are never
    touched here."""
    d = os.path.join(RESULTS_DIR, batch_id)
    if not os.path.isdir(d):
        return
    for root, _dirs, files in os.walk(d):
        if "_meta.json" in files:
            return  # has collected results — keep the dir
    shutil.rmtree(d, ignore_errors=True)


def _forget_batch_record(batch_id: str):
    """Remove the batch record file (results/<id>/batch.json) so a drained
    batch doesn't reload into the task queue on server restart. Collected
    result dirs are left untouched — they stay in stats."""
    try:
        os.remove(os.path.join(RESULTS_DIR, batch_id, "batch.json"))
    except OSError:
        pass


def discard_batch_item(batch_id: str, experiment: str, dataset: str) -> dict:
    """Discard a not-yet-collected item (pending/failed/skipped) from a batch
    that is not running, so its live row leaves the stats view. A collected
    (done) item is rejected here — use delete_result on its result dir. When
    the last item is discarded the whole batch record (batch.json) is
    forgotten and its results dir removed if nothing was collected."""
    exp = experiment or ""
    forget_dir = False
    removed = 0
    with _lock:
        b = _batches.get(batch_id)
        if not b:
            raise FileNotFoundError(f"batch not found: {batch_id}")
        if b.status == "running":
            raise ValueError("批次运行中，不能移除在途项；请先停止批次")
        keep = []
        for it in b.items:
            if it.dataset == dataset and (it.experiment or "") == exp and it.status != "done":
                removed += 1
            else:
                keep.append(it)
        if not removed:
            raise FileNotFoundError("没有可移除的在途/失败项（已完成的结果请用行内「删除」）")
        b.items = keep
        if keep:
            _save_state(b)
        else:
            del _batches[batch_id]
            forget_dir = True
    if forget_dir:
        _forget_batch_dir(batch_id)
    return {"ok": True, "removed": removed, "remaining": len(keep)}


def append_to_batch(batch_id: str, datasets_list: list,
                    experiments_list: list = None,
                    offline_bag: bool = True,
                    verbosity: str = "", vio_log_level: str = "",
                    use_proxy: "bool | None" = None) -> dict:
    """Append items to a running batch (added items run after current queue).

    If the batch is already finished, do nothing — caller should start a new
    batch instead. Returns the updated batch.to_dict().
    """
    if not datasets_list:
        raise ValueError("empty dataset list")
    experiments_list = experiments_list or [""]
    with _lock:
        b = _batches.get(batch_id)
        if not b:
            raise FileNotFoundError(f"batch not found: {batch_id}")
        if b.status != "running":
            raise ValueError(f"batch {batch_id} is not running (status={b.status})")
        new_items = [BatchItem(d, e, offline_bag=_item_offline_bag(e, offline_bag))
                     for e in experiments_list for d in datasets_list]
        b.items.extend(new_items)
        # 追加项沿用批次当时的启动参数；若调用方显式给了新值则覆盖后续项
        if verbosity:
            b.verbosity = verbosity
        if vio_log_level:
            b.vio_log_level = vio_log_level
        if use_proxy is not None:
            b.use_proxy = use_proxy
        _save_state(b)
        return b.to_dict()


def _batch_build_and_deploy(batch: "BatchRun") -> bool:
    """Fetch origin/<branch> in the mirror, cross-build, deploy to the board.

    Mirrors the auto-backtest pipeline so a manual batch can pin an exact code
    version instead of using whatever is installed on the board.
    """
    from . import auto_test  # lazy: auto_test imports batch (results collection)
    try:
        batch.build_status = "fetching"
        auto_test._ensure_mirror(use_proxy=batch.use_proxy)
        rc, out, err = auto_test._git(
            ["fetch", "origin", batch.branch, "--tags", "--prune"], timeout=300,
            net=True, use_proxy=batch.use_proxy)
        if rc != 0:
            batch.build_log = f"git fetch failed: {err.strip() or out.strip()}"
            return False
        rc, out, err = auto_test._git(["rev-parse", f"origin/{batch.branch}"])
        if rc != 0:
            batch.build_log = f"git rev-parse failed: {err.strip()}"
            return False
        sha = out.strip()
        batch.commit_short = sha[:10]
        batch.build_status = "building"
        ok, log = auto_test._run_build(sha)
        batch.build_log = log
        if not ok:
            return False
        batch.build_status = "deploying"
        ok, detail = auto_test._deploy_install(batch.ip)
        batch.build_log = (batch.build_log + "\n" + detail)[-4000:]
        if not ok:
            return False
        batch.build_status = "done"
        return True
    except Exception as e:  # noqa: BLE001
        batch.build_log = f"build exception: {e}"
        return False


def _batch_ensure_deployed(batch: "BatchRun") -> bool:
    """无分支批次：启动前把镜像仓库已构建的 install/ 自动部署到板子。

    板端已有相同版本（.platform_stamp 匹配）时跳过传输。用户无需再手点「部署」。
    """
    from . import auto_test  # lazy: auto_test imports batch (results collection)
    try:
        batch.build_status = "deploying"
        ok, detail = auto_test.ensure_deployed(batch.ip)
        batch.build_log = detail
        batch.build_status = "done" if ok else "failed"
        if ok:
            batch.commit_short = auto_test.mirror_head_sha()[:10]
        return ok
    except Exception as e:  # noqa: BLE001
        batch.build_status = "failed"
        batch.build_log = f"deploy exception: {e}"
        return False


def _run_batch(batch: BatchRun):
    if batch.branch:
        if not _batch_build_and_deploy(batch):
            batch.build_status = "failed"
            with _lock:
                for it in batch.items:
                    if it.status == "pending":
                        it.status = "failed"
                        it.error = f"branch {batch.branch} build/deploy failed: {batch.build_log[-300:]}"
                        it.finished_at = datetime.datetime.now().isoformat(timespec="seconds")
                batch.status = "finished"
            _save_state(batch)
            return
        _save_state(batch)  # build/deploy done, commit pinned
    else:
        if not _batch_ensure_deployed(batch):
            with _lock:
                for it in batch.items:
                    if it.status == "pending":
                        it.status = "failed"
                        it.error = f"启动前自动部署失败: {batch.build_log[-300:]}"
                        it.finished_at = datetime.datetime.now().isoformat(timespec="seconds")
                batch.status = "finished"
            _save_state(batch)
            return
        _save_state(batch)
    # index-based loop under the lock: append_to_batch() may extend items while
    # we run; checking the end condition under the same lock closes the race
    # where an appended item lands just after the iterator saw the old end.
    idx = 0
    while True:
        with _lock:
            if batch.status != "running" or idx >= len(batch.items):
                break
            item = batch.items[idx]
            idx += 1
            if item.status != "pending":
                continue
            item.status = "running"
            item.started_at = datetime.datetime.now().isoformat(timespec="seconds")
        _save_state(batch)
        try:
            r = backtest.start_backtest(batch.ip, item.dataset, experiment=item.experiment or None,
                                        offline_bag=item.offline_bag,
                                        launch_script_override=batch.launch_script_override,
                                        verbosity=batch.verbosity,
                                        vio_log_level=batch.vio_log_level)
            if not r.get("ok"):
                item.status = "failed"
                item.error = r.get("detail", "launch failed")
                _save_state(batch)
                continue
            item.experiment_keys = r.get("experiment_keys", [])
            # record the ov_web visualization stream for the whole run
            item.recorder = OvWebRecorder(batch.ip, config.OV_WEB_PORT, _item_outdir(batch, item), max_seconds=_MAX_RUN_S)
            item.recorder.start_async()
            done, err = _wait_finish(batch.ip, expect_bag_play=not item.offline_bag)
            item.finished_at = datetime.datetime.now().isoformat(timespec="seconds")
            # stop_batch may have marked this item failed/skipped while we were
            # waiting — don't resurrect it to done
            if item.status == "running":
                if err:
                    item.status = "failed"
                    item.error = err
                else:
                    item.status = "done"
            # stop the ov_web recording FIRST (while the board chain is still
            # alive and streaming), then kill the board processes
            video = item.recorder.stop_and_mux() if item.recorder else None
            video_frames = item.recorder.frame_count if item.recorder else 0
            backtest.stop_backtest(batch.ip)
            # collect artifacts regardless of status (tum may still exist)
            item.result_dir = _collect_results(batch, item)
            if item.result_dir:
                item.stats = _make_stats(item, batch)
            elif video and os.path.isfile(video):
                # keep stats even if board collection failed
                item.result_dir = _item_outdir(batch, item)
                item.stats = _make_stats(item, batch)
            if video and os.path.isfile(video):
                item.stats["video_mp4"] = "video.mp4"
                item.stats["video_frames"] = video_frames
            _write_meta(item, batch)
            _save_state(batch)
        except Exception as e:  # noqa: BLE001
            item.status = "failed"
            item.error = str(e)
            item.finished_at = datetime.datetime.now().isoformat(timespec="seconds")
            try:
                backtest.stop_backtest(batch.ip)
            except Exception:  # noqa: BLE001
                pass
            _save_state(batch)
    with _lock:
        batch.status = "finished"
    _save_state(batch)


def _wait_finish(ip: str, expect_bag_play: bool = True):
    """Wait until the VIO run ends on the board. Returns (ok, err).

    Completion semantics:
      - offline_bag mode (expect_bag_play=False): no `ros2 bag play` exists;
        the VIO node exits by itself at bag end -> vio==0 (after having been
        seen alive) is the completion signal.
      - live mode (expect_bag_play=True): bag player exiting is the signal,
        with a short grace for VIO to flush; VIO exiting early also ends it.

    Boot-safety guards:
      - vio==0 before VIO was ever seen is NOT "finished" (launch takes a
        while; without this guard we'd collect an empty result instantly)
      - if VIO never appears within the boot grace, fail fast instead of
        waiting the full _MAX_RUN_S
    """
    _VIO_BOOT_GRACE_S = 180
    deadline = time.time() + _MAX_RUN_S
    boot_deadline = time.time() + _VIO_BOOT_GRACE_S
    seen_vio = False
    seen_bag = False
    # give the chain time to boot before checking liveness
    time.sleep(20)
    while time.time() < deadline:
        try:
            st = backtest.backtest_status(ip)
        except Exception:  # noqa: BLE001
            time.sleep(_WAIT_POLL_S)
            continue
        vio = st["processes"].get("vio", 0)
        bag_play = st["processes"].get("bag_play", 0)
        if vio > 0:
            seen_vio = True
        if bag_play > 0:
            seen_bag = True
        crash = st.get("crash")
        if crash and vio == 0:
            # node died at boot (bad config value etc.) — fail fast with the
            # PRINT_ERROR cause instead of hanging until the boot grace
            return False, f"VIO crashed at startup: {crash.get('cause') or crash.get('died') or 'see vio.log'}"
        if seen_vio and vio == 0:
            # VIO exited after having run -> bag consumed (offline) or stopped
            if crash:
                return False, f"VIO crashed during run: {crash.get('cause') or crash.get('died') or 'see vio.log'}"
            return True, ""
        if not seen_vio and time.time() > boot_deadline:
            return False, f"VIO process never appeared within {_VIO_BOOT_GRACE_S}s of launch"
        if expect_bag_play and seen_bag and bag_play == 0 and vio > 0:
            # bag done but VIO still alive: give it a short grace to flush, then stop
            time.sleep(10)
            try:
                st2 = backtest.backtest_status(ip)
                if st2["processes"].get("bag_play", 0) == 0:
                    return True, ""
            except Exception:  # noqa: BLE001
                return True, ""
        time.sleep(_WAIT_POLL_S)
    return False, "timeout waiting for run to finish"


# ------------------------------------------------------------------ collection
def _sftp_get_tree(sftp, remote_dir: str, local_dir: str) -> None:
    """Recursively fetch remote_dir into local_dir (overwrites in place)."""
    os.makedirs(local_dir, exist_ok=True)
    try:
        entries = sftp.listdir_attr(remote_dir)
    except OSError:
        return
    for e in entries:
        r = os.path.join(remote_dir, e.filename)
        l = os.path.join(local_dir, e.filename)
        if e.st_mode is not None and not (e.st_mode & 0o170000 == 0o040000):
            try:
                sftp.get(r, l)
            except OSError:
                pass
        else:
            _sftp_get_tree(sftp, r, l)


def _item_outdir(batch: 'BatchRun', item: BatchItem) -> str:
    safe_ds = item.dataset.replace("/", "__")
    exp_dir = item.experiment if item.experiment else "_baseline"
    d = os.path.join(RESULTS_DIR, batch.id, exp_dir, safe_ds)
    os.makedirs(d, exist_ok=True)
    return d


def _collect_results(batch: BatchRun, item: BatchItem) -> str:
    """SFTP result artifacts of the latest board run into results/<batch>/<dataset>/.

    Delegates to the shared collect_results_to_dir; kept for the BatchRun path
    so item.stats gets the fetched_logs side-effect.
    """
    local = _item_outdir(batch, item)
    fetched_logs = collect_results_to_dir(batch.ip, local, experiment=item.experiment,
                                          dataset=item.dataset)
    item.stats["logs"] = fetched_logs
    return local


def collect_results_to_dir(ip: str, local: str, experiment: "str | None" = None,
                           dataset: str = "") -> list:
    """SFTP result artifacts of the latest board run into `local`.

    Pulls from the current run dir (<BOARD_BASE>/current → runs/<ts>__<exp>__<ds>/):
      - the whole output/ tree (VIO save_path: trajectory/ov_est.tum,
        state/state_estimate.txt, logs/vio_algo.log, ...)
      - the four board-side logs (vio/ov_web/tf/bag)
      - the merged estimator_config.yaml (if an experiment was applied)
      - launch.sh — the actual script that ran on the board

    For baseline runs (no merged config on the board) snapshots the dataset's
    estimator_config.yaml when `dataset` is given, so per-run config detail
    reflects what this run actually used.

    Returns the list of fetched log file names (vio.log/ov_web.log/...).
    """
    os.makedirs(local, exist_ok=True)
    fetched_logs = []
    with Ssh(ip) as s:
        cli = s._cli
        sftp = cli.open_sftp()
        try:
            _sftp_get_tree(sftp, f"{config.BOARD_CURRENT_LINK}/output",
                           os.path.join(local, "output"))
            # board-side logs (they live in the run dir now)
            for name in backtest._LOGS:
                remote = f"{config.BOARD_CURRENT_LINK}/{name}"
                try:
                    sftp.get(remote, os.path.join(local, name))
                    fetched_logs.append(name)
                except OSError:
                    pass
            # merged config (if experiment applied)
            cfg_dst = os.path.join(local, "estimator_config.yaml")
            try:
                sftp.get(f"{config.BOARD_CURRENT_LINK}/estimator_config.yaml", cfg_dst)
            except OSError:
                pass
            # baseline runs have no merged config on the board (launch uses the
            # dataset config directly); paramiko leaves a 0-byte local file when
            # the remote is missing — drop it so has_config reflects reality
            if os.path.isfile(cfg_dst) and os.path.getsize(cfg_dst) == 0:
                os.remove(cfg_dst)
            # launch.sh — the actual script that ran on the board
            try:
                sftp.get(f"{config.BOARD_CURRENT_LINK}/launch.sh",
                         os.path.join(local, "launch.sh"))
            except OSError:
                pass
        finally:
            sftp.close()
    # experiment fragment (the diff) — saved from host side
    if experiment:
        try:
            from . import experiments as _exp
            text = _exp.read_experiment(experiment)
            with open(os.path.join(local, "experiment.yaml"), "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:  # noqa: BLE001
            pass
    if dataset and not os.path.isfile(os.path.join(local, "estimator_config.yaml")):
        try:
            import shutil
            src = os.path.join(datasets.get_dataset(dataset)["path"],
                               "stereo_auto_gen", "estimator_config.yaml")
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(local, "estimator_config.yaml"))
        except Exception:  # noqa: BLE001 — snapshot is best-effort
            pass
    return fetched_logs


# ------------------------------------------------------------------ stats
# evo_traj CLI breaks on this host: /usr/lib/python3/dist-packages carries a
# matplotlib-3.6.3 nspkg.pth that hijacks `mpl_toolkits.__path__`, so evo gets
# the 3.6.3 mplot3d against the user's 3.11 matplotlib and crashes on
# `matplotlib.tri.triangulation` (removed in 3.10). The wrapper re-prepends
# the user's mpl_toolkits before evo imports anything.
_EVO_WRAPPER = (
    "import sys, os;"
    "p = os.path.expanduser('~/.local/lib/python%d.%d/site-packages/mpl_toolkits'"
    " % sys.version_info[:2]);"
    "import mpl_toolkits;"
    "p in mpl_toolkits.__path__ or mpl_toolkits.__path__.insert(0, p);"
    "sys.argv = ['evo_traj'] + sys.argv[1:];"
    "from evo.cli.entry_points import traj; sys.exit(traj())"
)


def _evo_traj_plot(tum_path: str, out_png: str, title: str) -> bool:
    """Render trajectory PNG via evo_traj. Returns True on success."""
    import subprocess
    try:
        if os.path.isfile(out_png):
            os.remove(out_png)
        r = subprocess.run(
            ["python3", "-c", _EVO_WRAPPER, "tum", tum_path,
             "--plot_mode", "xy", "-v", "--no_warnings",
             "--save_plot", out_png],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "MPLBACKEND": "Agg", "QT_QPA_PLATFORM": "offscreen"},
        )
        if r.returncode != 0:
            return False
        # evo exports one PNG per figure: <stem>_trajectories/_xyz/_rpy/_speeds.
        # Keep the xy trajectories view as our trajectory.png, drop the rest.
        stem, _ = os.path.splitext(out_png)
        produced = stem + "_trajectories.png"
        if not os.path.isfile(produced):
            return False
        for suffix in ("_xyz", "_rpy", "_speeds"):
            p = stem + suffix + ".png"
            if os.path.isfile(p):
                os.remove(p)
        os.replace(produced, out_png)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    except Exception:  # noqa: BLE001
        return False


def _matplotlib_plot(xs, ys, out_png: str, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5, 4), dpi=100)
    ax.plot(xs, ys, "-", lw=1.2, color="#f80")
    ax.plot(xs[0], ys[0], "o", color="#4ec", label="start")
    ax.plot(xs[-1], ys[-1], "o", color="#e55", label="end")
    ax.set_aspect("equal", adjustable="datalim")
    ax.set_xlabel("x [m]")
    ax.set_ylabel("y [m]")
    ax.set_title(f"{title} (xy)")
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)


def _find_tum(d: str) -> "str | None":
    for cand in (os.path.join(d, "output", "trajectory", "ov_est.tum"),
                 os.path.join(d, "ov_est.tum")):  # legacy flat layout
        if os.path.isfile(cand):
            return cand
    return None


def _parse_vio_timing(d: str) -> dict:
    """Extract VIO per-frame timing from vio.log [TIME] lines.

    Line shape (RCLCPP wrapper prefix varies):
      [TIME]: 54.4ms total, min: 13.7ms, max: 164.3ms, avg(excl zupt): 60.2ms. ...
    Returns vio_time_avg_ms (mean of the per-window avg) and vio_time_max_ms
    (worst max seen), or {} when the log has no timing lines.
    """
    import re
    log_p = os.path.join(d, "vio.log")
    if not os.path.isfile(log_p):
        return {}
    pat = re.compile(r"\[TIME\]: [\d.]+ms total, min: ([\d.]+)ms, max: ([\d.]+)ms,"
                     r" avg\(excl zupt\): ([\d.]+)ms")
    avgs, maxs = [], []
    try:
        with open(log_p, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = pat.search(line)
                if m:
                    maxs.append(float(m.group(2)))
                    avgs.append(float(m.group(3)))
    except OSError:
        return {}
    if not avgs:
        return {}
    return {"vio_time_avg_ms": round(sum(avgs) / len(avgs), 1),
            "vio_time_max_ms": round(max(maxs), 1)}


def _make_stats(item: BatchItem, batch: 'BatchRun' = None) -> dict:
    """Parse collected artifacts into stats + render plots."""
    d = item.result_dir
    tum = _find_tum(d) or os.path.join(d, "ov_est.tum")
    stats = {}
    stats.update(_parse_vio_timing(d))
    if os.path.isfile(tum):
        rows = []
        with open(tum, encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 8:
                    try:
                        rows.append([float(x) for x in parts[:8]])
                    except ValueError:
                        continue
        stats["poses"] = len(rows)
        if rows:
            ts = [r[0] for r in rows]
            # ov_est.tum layout: standard TUM = ts tx ty tz qx qy qz qw
            xs = [r[1] for r in rows]
            ys = [r[2] for r in rows]
            zs = [r[3] for r in rows]
            stats.update(
                {
                    "duration_s": round(ts[-1] - ts[0], 2) if len(ts) > 1 else 0.0,
                    "end": [round(xs[-1], 3), round(ys[-1], 3), round(zs[-1], 3)],
                    "start": [round(xs[0], 3), round(ys[0], 3), round(zs[0], 3)],
                    "path_len_m": round(sum(((xs[i] - xs[i - 1]) ** 2 + (ys[i] - ys[i - 1]) ** 2) ** 0.5 for i in range(1, len(xs))), 2),
                }
            )
            # trajectory plot — prefer evo_traj, fall back to matplotlib
            if not _evo_traj_plot(tum, os.path.join(d, "trajectory.png"), item.dataset):
                _matplotlib_plot(xs, ys, os.path.join(d, "trajectory.png"), item.dataset)
            stats["trajectory_png"] = "trajectory.png"
    # preview image: first frame of the bag (host-side, no board needed).
    # Use the topic that ACTUALLY holds frames (pick_frame_topic), not the
    # canonical RAW launch topic, which doesn't exist in a CompressedImage-only
    # bag and would make image_frame_jpeg raise → no preview/thumbnail.
    try:
        topic = datasets.pick_frame_topic(item.dataset)
        if topic:
            jpeg, _ = datasets.image_frame_jpeg(item.dataset, topic, 0)
            with open(os.path.join(d, "preview.jpg"), "wb") as f:
                f.write(jpeg)
            stats["preview_jpg"] = "preview.jpg"
    except Exception:  # noqa: BLE001
        pass
    return stats




def _write_meta(item: BatchItem, batch: "BatchRun"):
    if not item.result_dir:
        return
    try:
        with open(os.path.join(item.result_dir, "_meta.json"), "w", encoding="utf-8") as f:
            json.dump(
                {"status": item.status, "error": item.error, "started_at": item.started_at, "finished_at": item.finished_at,
                 "stats": item.stats, "board": batch.ip, "dataset": item.dataset, "experiment": item.experiment,
                 "experiment_keys": item.experiment_keys,
                 "run_no": batch.run_no, "type": "manual", "source": "manual",
                 "branch": batch.branch, "commit": batch.commit_short,
                 "commit_short": batch.commit_short,
                 "batch_id": batch.id},
                f, ensure_ascii=False, indent=1,
            )
    except OSError:
        pass

# ------------------------------------------------------------------ results listing (persisted)
def read_result_stats(path: str) -> dict:
    """Read _meta.json from a result dir. Refuses paths outside RESULTS_DIR."""
    abs_p = os.path.realpath(path)
    if not abs_p.startswith(os.path.realpath(RESULTS_DIR) + os.sep):
        raise FileNotFoundError("path not under results dir")
    meta = os.path.join(abs_p, "_meta.json")
    if not os.path.isfile(meta):
        raise FileNotFoundError(f"_meta.json not found: {meta}")
    import json as _json
    with open(meta, encoding="utf-8") as f:
        return _json.load(f)


def read_result_file(path: str, name: str) -> dict:
    """Read a text artifact from a result dir (path must be under RESULTS_DIR).

    `name` must be a plain file name (no path separators). For
    estimator_config.yaml on baseline runs the result dir has no merged copy
    (the board launched with the dataset config directly), so fall back to the
    dataset's stereo_auto_gen/estimator_config.yaml under DATA_ROOT.
    """
    abs_p = os.path.realpath(path)
    if not abs_p.startswith(os.path.realpath(RESULTS_DIR) + os.sep):
        raise ValueError("path not under results dir")
    if not os.path.isdir(abs_p):
        raise FileNotFoundError(f"result dir not found: {path}")
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"bad file name: {name!r}")
    fp = os.path.join(abs_p, name)
    if name == "estimator_config.yaml" and (
            not os.path.isfile(fp) or os.path.getsize(fp) == 0):
        ds = ""
        meta_p = os.path.join(abs_p, "_meta.json")
        if os.path.isfile(meta_p):
            try:
                with open(meta_p, encoding="utf-8") as f:
                    ds = (json.load(f).get("dataset") or "").strip()
            except (OSError, json.JSONDecodeError):
                ds = ""
        if not ds:
            ds = os.path.basename(abs_p).replace("__", "/")
        cand = os.path.realpath(os.path.join(
            config.DATA_ROOT, ds, "stereo_auto_gen", "estimator_config.yaml"))
        if cand.startswith(os.path.realpath(config.DATA_ROOT) + os.sep) \
                and os.path.isfile(cand):
            fp = cand
    if not os.path.isfile(fp):
        raise FileNotFoundError(f"file not found: {name}")
    if os.path.getsize(fp) > 4 * 1024 * 1024:
        raise ValueError(f"file too large: {name}")
    with open(fp, encoding="utf-8", errors="replace") as f:
        return {"name": name, "text": f.read(), "path": fp}


def delete_result(path: str) -> dict:
    """Permanently delete a result dir and its artifacts (no recycle/trash).

    Deletion is immediate and unrecoverable. Also drops any auto-queue task that
    led to this result so the stats view and the auto task list stay consistent.
    """
    import shutil
    abs_p = os.path.realpath(path)
    root = os.path.realpath(RESULTS_DIR)
    if not abs_p.startswith(root + os.sep):
        raise ValueError("path not under results dir")
    existed = os.path.isdir(abs_p)
    if existed:
        shutil.rmtree(abs_p)
    # 清理删除后残留的父目录（batch/exp 层），rmdir 只删空目录，安全。
    # 目录本身已不存在（结果可能经统计页删过、或收集时未落盘）也照常清理，
    # 这样队列/统计里残留的 finished 行会随父目录清理一并消失，删除幂等。
    parent = os.path.dirname(abs_p)
    while parent.startswith(root + os.sep):
        try:
            os.rmdir(parent)
        except OSError:
            break
        parent = os.path.dirname(parent)
    _delete_matching_auto_tasks(abs_p)
    _delete_matching_batch_items(abs_p)
    detail = f"deleted {abs_p}" if existed else f"already removed: {abs_p}"
    return {"ok": True, "detail": detail}


def _delete_matching_auto_tasks(result_dir: str) -> int:
    """Remove auto-queue tasks whose result dir was just deleted in the stats view.

    The 统计 page and the 自动任务 list are two windows into the same run; when a
    result is deleted in stats, its queue entry must vanish too, or the task
    list keeps showing a dead task. result_dir already moved to trash, so we
    delete the queue record WITHOUT re-deleting the (now gone) dir.
    """
    from . import auto_test  # lazy: auto_test imports batch at module level
    removed = 0
    try:
        tasks = auto_test.list_tasks(limit=0)
    except Exception:  # noqa: BLE001
        return 0
    want = os.path.realpath(result_dir)
    for t in tasks:
        rd = t.get("result_dir") or ""
        if rd and os.path.realpath(rd) == want:
            # remove_result=False: the dir is already in the trash; only the
            # queue record is dropped, and the daily gate re-arms if applicable
            auto_test.delete_task(t["id"], remove_result=False)
            removed += 1
    return removed


def _delete_matching_batch_items(result_dir: str) -> int:
    """Remove manual-batch items whose result dir was just deleted.

    Mirrors _delete_matching_auto_tasks for the manual 回测 queue: deleting a
    result (from stats OR the queue) must also drop the matching item from its
    batch record, or the queue keeps showing a finished row whose result_dir no
    longer exists. When a batch drains to zero items it is forgotten and its
    (now empty) results dir pruned.
    """
    want = os.path.realpath(result_dir)
    forget = []
    removed = 0
    with _lock:
        for b in list(_batches.values()):
            keep = [it for it in b.items
                    if not (it.result_dir and os.path.realpath(it.result_dir) == want)]
            if len(keep) == len(b.items):
                continue
            removed += len(b.items) - len(keep)
            b.items = keep
            if keep:
                _save_state(b)
            else:
                del _batches[b.id]
                forget.append(b.id)
    for bid in forget:
        _forget_batch_dir(bid)
        _forget_batch_record(bid)
    return removed


def discard_result_from_queue(result_dir: str) -> dict:
    """Remove a finished row from the manual 回测 queue WITHOUT deleting the
    collected result — it stays in stats for later viewing/deletion.

    This is what the queue's 删除 button does. It is intentionally NOT the same
    as a stats delete: the on-disk result dir and the auto-task record are left
    untouched; only the batch item (and, if the batch drains empty, its batch
    record file) is dropped. Returns how many queue rows were pruned.
    """
    want = os.path.realpath(result_dir)
    removed = _delete_matching_batch_items(want)
    kind = "队列无该行" if not removed else f"从队列移除 {removed} 项"
    return {"ok": True, "removed": removed, "detail": f"{kind}（结果保留在统计）"}


def list_results() -> list:
    """All collected results on disk (survives server restarts), plus live
    rows for in-flight batch items so 编号 shows up as soon as a backtest
    starts, not only after collection.

    Layout: results/<batch_id>/<experiment_or_baseline>/<dataset>/
    For backward compat, also walks the legacy 2-level layout
    (results/<batch_id>/<dataset>/).
    """
    out = []
    if os.path.isdir(RESULTS_DIR):
        for batch_id in sorted(os.listdir(RESULTS_DIR), reverse=True):
            bdir = os.path.join(RESULTS_DIR, batch_id)
            if not os.path.isdir(bdir):
                continue
            # auto layout: results/auto/<commit_short>/<exp>/<dataset>/
            # (one extra level vs manual batch layout)
            if batch_id == "auto":
                for commit_short in sorted(os.listdir(bdir), reverse=True):
                    cdir = os.path.join(bdir, commit_short)
                    if not os.path.isdir(cdir):
                        continue
                    for exp_name in sorted(os.listdir(cdir)):
                        edir = os.path.join(cdir, exp_name)
                        if not os.path.isdir(edir):
                            continue
                        for ds_folder in sorted(os.listdir(edir)):
                            d = os.path.join(edir, ds_folder)
                            if not os.path.isdir(d):
                                continue
                            # 无 _meta.json = 尚未收集（录制器开场就建目录）
                            if not os.path.isfile(os.path.join(d, "_meta.json")):
                                continue
                            _append_result(out, "auto", exp_name, ds_folder, d,
                                           source="auto", commit_short=commit_short)
                continue
            for name in sorted(os.listdir(bdir)):
                edir = os.path.join(bdir, name)
                if not os.path.isdir(edir):
                    continue
                # detect legacy 2-level layout: edir directly contains _meta.json
                if os.path.isfile(os.path.join(edir, "_meta.json")):
                    _append_result(out, batch_id, "", name, edir, source="manual")
                    continue
                # new 3-level layout: results/<batch>/<exp>/<dataset>/
                exp_name = name
                for ds_folder in sorted(os.listdir(edir)):
                    d = os.path.join(edir, ds_folder)
                    if not os.path.isdir(d):
                        continue
                    # 无 _meta.json = 尚未收集（录制器开场就建 frames/ 目录）——
                    # 由 _append_live_items 出在途行，这里跳过
                    if not os.path.isfile(os.path.join(d, "_meta.json")):
                        continue
                    _append_result(out, batch_id, exp_name, ds_folder, d, source="manual")
    _append_live_items(out)
    return out


def _append_live_items(out: list):
    """Merge in-memory MANUAL batch items whose result dir hasn't been collected
    yet (pending / running / failed-without-dir) so stats shows the 编号 group
    the moment a backtest starts. Deduped against the disk scan: collected =
    the result dir has _meta.json (recorder pre-creates the dir at run start).

    Auto tasks are NOT merged here — stats only shows collected auto results;
    uncollected auto work belongs to the 自动任务 list, and surfacing it here
    made a queue delete look like it wiped the stats row."""
    with _lock:
        batches = list(_batches.values())
    for b in batches:
        commit_id = b.commit_short
        rtype = runno.kind_of("manual", commit_id)
        for it in b.items:
            if it.status not in ("pending", "running", "failed"):
                continue
            safe_ds = it.dataset.replace("/", "__")
            exp_dir = it.experiment if it.experiment else "_baseline"
            d = os.path.join(RESULTS_DIR, b.id, exp_dir, safe_ds)
            if os.path.isfile(os.path.join(d, "_meta.json")):
                continue  # already collected → covered by the disk scan
            out.append(
                {
                    "live": True,
                    "batch_id": b.id,
                    "batch_status": b.status,
                    "experiment": it.experiment,
                    "dataset": it.dataset,
                    "dir": "",
                    "source": "manual",
                    "type": rtype,
                    "run_no": b.run_no,
                    "status": it.status,
                    "error": it.error,
                    "started_at": it.started_at,
                    "finished_at": it.finished_at,
                    "commit": "",
                    "commit_short": commit_id,
                    "has_tum": False,
                    "has_trajectory": False,
                    "has_preview": False,
                    "has_video": False,
                    "has_vio_log": False,
                    "has_ov_web_log": False,
                    "has_tf_log": False,
                    "has_bag_log": False,
                    "has_experiment": bool(it.experiment),
                    "has_config": False,
                    "experiment_diff": [],
                    "experiment_keys": it.experiment_keys,
                    "stats": it.stats or {},
                }
            )
    # auto tasks: pending/running/failed are owned by the 自动任务 tab and are
    # deliberately NOT surfaced here as placeholder "results" — stats shows only
    # collected auto results (the disk scan above). Surfacing them made a queue
    # delete look like it also wiped the stats entry, since there was no result
    # behind the row; the auto task list is where uncollected work lives.


def _append_result(out: list, batch_id: str, exp_name: str, ds_folder: str, d: str,
                   source: str = "", commit_short: str = ""):
    meta_p = os.path.join(d, "_meta.json")
    meta = {}
    if os.path.isfile(meta_p):
        try:
            with open(meta_p, encoding="utf-8") as f:
                meta = json.load(f)
        except json.JSONDecodeError:
            pass
    # legacy 2-level path: ds_folder is actually the dataset folder name,
    # exp_name is "" (we passed it as such). New 3-level: exp_name is the
    # experiment name (or "_baseline"), ds_folder is the dataset folder name.
    if exp_name == "" and ds_folder != "_baseline":
        # legacy path: only dataset known, experiment unknown → baseline
        dataset = ds_folder.replace("__", "/")
        experiment = ""
    else:
        experiment = "" if exp_name == "_baseline" else exp_name
        dataset = ds_folder.replace("__", "/")
    has = {f: os.path.isfile(os.path.join(d, f)) for f in
           ("trajectory.png", "preview.jpg", "video.mp4",
            "vio.log", "ov_web.log", "tf.log", "bag.log",
            "experiment.yaml")}
    # 0-byte merged config = baseline run (collect drops it now; older results
    # kept the empty file) — report it as absent so the UI shows the baseline
    # marker and the config popup falls back to the dataset config
    cfg_p = os.path.join(d, "estimator_config.yaml")
    has["estimator_config.yaml"] = os.path.isfile(cfg_p) and os.path.getsize(cfg_p) > 0
    has["ov_est.tum"] = _find_tum(d) is not None
    commit_id = commit_short or meta.get("commit_short", "") or meta.get("commit", "")
    rtype = meta.get("type") or runno.kind_of(source or meta.get("source", ""), commit_id)
    meta.setdefault("run_no", "")
    meta.setdefault("type", rtype)
    # experiment diff detail: the fragment IS the override list — flatten to
    # dotted key: value lines so the UI can show 改动明细 without opening files.
    exp_diff = []
    if has["experiment.yaml"]:
        try:
            from . import experiments as _exp
            with open(os.path.join(d, "experiment.yaml"), encoding="utf-8") as f:
                exp_diff = _exp.flatten_config(f.read())
        except Exception:  # noqa: BLE001
            exp_diff = []
    out.append(
        {
            "batch_id": batch_id,
            "experiment": experiment,
            "dataset": dataset,
            "dir": d,
            "source": source or meta.get("source", "manual"),
            "commit": meta.get("commit", ""),
            "commit_short": commit_short or meta.get("commit_short", ""),
            "commit_msg": meta.get("commit_msg", ""),
            "commit_author": meta.get("commit_author", ""),
            "commit_date": meta.get("commit_date", ""),
            "has_tum": has["ov_est.tum"],
            "has_trajectory": has["trajectory.png"],
            "has_preview": has["preview.jpg"],
            "has_video": has["video.mp4"],
            "has_vio_log": has["vio.log"],
            "has_ov_web_log": has["ov_web.log"],
            "has_tf_log": has["tf.log"],
            "has_bag_log": has["bag.log"],
            "has_experiment": has["experiment.yaml"],
            "has_config": has["estimator_config.yaml"],
            "experiment_diff": exp_diff,
            **meta,
        }
    )
