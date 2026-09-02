"""test_platform web service entry: FastAPI app + static frontend + API."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "vendor_libs"))
sys.path.insert(0, _REPO)
os.chdir(_REPO)

from fastapi import FastAPI, HTTPException, Response, UploadFile, File, Form  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from server import backtest, batch, boards, config, datasets, experiments, yaml_edit
from server import auto_test, report  # noqa: E402

app = FastAPI(title="VIO test platform")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def no_cache_html(request, call_next):
    """HTML entry point must always revalidate so users never run stale JS
    against new routes (static assets carry explicit ?v= cache-busters)."""
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.endswith(".html"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ------------------------------------------------------------------ datasets
@app.get("/api/datasets")
def api_datasets(refresh: bool = False):
    return datasets.scan_datasets(refresh=refresh)


@app.delete("/api/datasets/{name:path}")
def api_dataset_del(name: str):
    try:
        return datasets.delete_dataset(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/parents/{parent:path}")
def api_parent_del(parent: str):
    try:
        return datasets.delete_parent_dir(parent)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/datasets/{name:path}/info")
def api_dataset_info(name: str):
    try:
        info = datasets.bag_info(name)
        info["default_topics"] = datasets.pick_default_topics(name)
        info["configs"] = yaml_edit.list_configs(name)
        return info
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/datasets/topic_counts")
def api_dataset_topic_counts(names: str = ""):
    """Batch endpoint: return {name: {topics: N, messages: M}} for the given names.

    Reads metadata.yaml for each bag (cached by mtime inside datasets.bag_info).
    Missing/unparseable datasets report zeros.
    """
    import concurrent.futures

    if not names:
        return {"counts": {}}
    name_list = [n for n in names.split(",") if n]
    out = {}

    def _count(name):
        try:
            info = datasets.bag_info(name)
            return name, {"topics": len(info.get("topics", [])), "messages": int(info.get("message_count", 0))}
        except Exception:  # noqa: BLE001
            return name, {"topics": 0, "messages": 0}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for name, n in ex.map(_count, name_list):
            out[name] = n
    return {"counts": out}


@app.get("/api/datasets/{name:path}/image/{topic:path}")
def api_image(name: str, topic: str, index: int = 0):
    try:
        jpeg, ts = datasets.image_frame_jpeg(name, "/" + topic.lstrip("/"), index)
        return Response(jpeg, media_type="image/jpeg",
                        headers={"X-Timestamp-Ns": str(ts), "Cache-Control": "public, max-age=86400"})
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.get("/api/datasets/{name:path}/thumbnail")
def api_thumbnail(name: str):
    try:
        jpeg = datasets.thumbnail_jpeg(name)
        if not jpeg:
            raise HTTPException(404, "no image topic in this bag")
        return Response(jpeg, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.get("/api/parents")
def api_parents():
    return datasets.list_parent_dirs()


@app.post("/api/datasets/{name:path}/frames/prepare")
def api_frames_prepare(name: str, topic: str):
    try:
        return datasets.start_extraction(name, "/" + topic.lstrip("/"))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/datasets/{name:path}/frames/{topic:path}/status")
def api_frames_status(name: str, topic: str):
    try:
        return datasets.extraction_status(name, "/" + topic.lstrip("/"))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/datasets/{name:path}/frames/{topic:path}/manifest")
def api_frames_manifest(name: str, topic: str):
    try:
        mf = datasets.frame_manifest(name, "/" + topic.lstrip("/"))
        if not mf:
            raise HTTPException(404, "manifest not ready; call POST .../frames/prepare first")
        return mf
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/datasets/{name:path}/frames/{topic:path}/{index}.jpg")
def api_frame_jpeg(name: str, topic: str, index: int):
    try:
        jpeg = datasets.get_cached_frame(name, "/" + topic.lstrip("/"), index)
        return Response(jpeg, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ video cache (MP4 for native <video> playback)
@app.post("/api/datasets/{name:path}/video/prepare")
def api_video_prepare(name: str, topic: str):
    try:
        return datasets.video_prepare(name, "/" + topic.lstrip("/"))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/datasets/{name:path}/video/{topic}/status")
def api_video_status(name: str, topic: str):
    try:
        return datasets.video_status(name, "/" + topic.lstrip("/"))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.post("/api/datasets/upload")
async def api_upload(
    parent: str = Form(...),
    name: str = Form(...),
    files: list[UploadFile] = File(...),
):
    """Persist uploaded files under DATA_ROOT/<parent>/<name>/.

    Each file's filename carries the relative path within the dataset folder
    (browsers preserve webkitRelativePath when the frontend sets it explicitly).
    After all files land, validate the tree contains ros2bag_vio/ and
    stereo_auto_gen/; return the result.
    """
    import os

    safe_parent = os.path.basename(parent.strip().strip("/")) or "uploaded"
    safe_name = os.path.basename(name.strip().strip("/")) or "dataset"
    written = []
    for f in files:
        rel = f.filename or ""
        # normalize: strip any leading slashes, keep internal subdirs
        rel = rel.lstrip("/")
        if not rel or ".." in rel.split("/"):
            raise HTTPException(400, f"unsafe filename: {rel}")
        data = await f.read()
        try:
            written.append(datasets.upload_file(safe_parent, safe_name, rel, data))
        except ValueError as e:
            raise HTTPException(400, str(e))
    # invalidate scan cache so the new dataset shows up immediately
    datasets._scan_cache["key"] = None
    return {"ok": True, "parent": safe_parent, "name": safe_name, "files": len(written), "validate": datasets.validate_uploaded(safe_parent, safe_name)}


@app.get("/api/datasets/{name:path}/series/{topic:path}")
def api_series(name: str, topic: str, max_points: int = 2000):
    try:
        return datasets.topic_series(name, "/" + topic.lstrip("/"), max_points=min(max_points, config.SERIES_MAX_POINTS))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


@app.get("/api/datasets/{name:path}/player/prepare")
def api_player_prepare(name: str):
    try:
        return datasets.player_prepare(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/datasets/{name:path}/player/slice")
def api_player_slice(name: str, t_ns: int, window_s: float = 4.0, images_only: bool = False):
    try:
        return datasets.player_slice(name, t_ns, window_s, images_only)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except RuntimeError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ config files
@app.get("/api/datasets/{name:path}/config")
def api_config_list(name: str):
    return yaml_edit.list_configs(name)


@app.get("/api/datasets/{name:path}/config/{fname}")
def api_config_read(name: str, fname: str):
    try:
        return yaml_edit.read_config(name, fname)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/datasets/{name:path}/config/{fname}/flat")
def api_config_flat(name: str, fname: str):
    """Return flat list of dotted keys + stringified values for navigation UI."""
    try:
        cfg = yaml_edit.read_config(name, fname)
        return {"keys": experiments.flatten_config(cfg["text"])}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


class ConfigBody(BaseModel):
    text: str


@app.put("/api/datasets/{name:path}/config/{fname}")
def api_config_write(name: str, fname: str, body: ConfigBody):
    try:
        return yaml_edit.write_config(name, fname, body.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


class ConfigPatchBody(BaseModel):
    overrides: dict


@app.patch("/api/datasets/{name:path}/config/{fname}")
def api_config_patch(name: str, fname: str, body: ConfigPatchBody):
    """In-place key overrides that keep comments/formatting (baseline editor)."""
    try:
        return yaml_edit.patch_config(name, fname, body.overrides)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


# ------------------------------------------------------------------ boards
class BoardBody(BaseModel):
    ip: str
    user: str | None = None
    password: str | None = None
    note: str = ""


@app.get("/api/boards")
def api_boards():
    # never expose passwords to the browser
    return [{k: v for k, v in b.items() if k != "password"} for b in boards.list_boards()]


@app.post("/api/boards")
def api_board_add(b: BoardBody):
    if not boards.is_valid_ip(b.ip):
        raise HTTPException(400, f"不是合法的 IP 地址：{b.ip}（例：192.168.1.10）")
    # A board must be a real X5 test board: refuse to add it unless root/root SSH
    # login (or the supplied creds) actually works. Otherwise we'd register a
    # non-test node that later hangs every poll/run.
    try:
        res = boards.test_board(b.ip, b.user, b.password)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"SSH 登录校验失败：{e}")
    if not res.get("ok"):
        raise HTTPException(400, f"SSH 登录失败，不是测试板子（root/root）：{res.get('detail')}")
    try:
        boards.add_board(b.ip, b.user, b.password, b.note)
        return {"ok": True, "ip": b.ip}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/boards/{ip}")
def api_board_del(ip: str):
    boards.remove_board(ip)
    return {"ok": True}


@app.post("/api/boards/{ip}/test")
def api_board_test(ip: str):
    if not boards.is_valid_ip(ip):
        raise HTTPException(400, f"不是合法的 IP 地址：{ip}（例：192.168.1.10）")
    return boards.test_board(ip)


@app.get("/api/boards/ping")
def api_boards_ping():
    """Ping all boards in parallel and return per-board latency in ms.

    Uses system `ping -c 1 -W 1 <ip>`. Returns null for unreachable / timeout,
    or a float (ms, 1 decimal) for success.
    """
    import subprocess
    import concurrent.futures

    def _ping(ip: str):
        try:
            r = subprocess.run(
                ["ping", "-c", "1", "-W", "1", ip],
                capture_output=True, text=True, timeout=2,
            )
            if r.returncode != 0:
                return None
            for line in r.stdout.splitlines():
                if "min/avg/max" in line or "rtt min" in line:
                    parts = line.split("=")[-1].strip().split()
                    if len(parts) >= 1:
                        avg = parts[0].split("/")
                        if len(avg) >= 2:
                            try:
                                return round(float(avg[1]), 1)
                            except ValueError:
                                return None
            return None
        except Exception:  # noqa: BLE001
            return None

    bs = boards.list_boards()
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for b, ms in zip(bs, ex.map(_ping, [b["ip"] for b in bs])):
            out[b["ip"]] = ms
    return {"latency": out}


@app.get("/api/boards/{ip}/ping")
def api_board_ping(ip: str):
    """Ping a single board and return latency in ms (null = unreachable)."""
    import subprocess
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "1", ip],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode != 0:
            return {"ip": ip, "ms": None}
        for line in r.stdout.splitlines():
            if "min/avg/max" in line or "rtt min" in line:
                parts = line.split("=")[-1].strip().split()
                if len(parts) >= 1:
                    avg = parts[0].split("/")
                    if len(avg) >= 2:
                        try:
                            return {"ip": ip, "ms": round(float(avg[1]), 1)}
                        except ValueError:
                            pass
        return {"ip": ip, "ms": None}
    except Exception as e:  # noqa: BLE001
        return {"ip": ip, "ms": None, "error": str(e)}


# ------------------------------------------------------------------ experiments
@app.get("/api/experiments")
def api_exp_list():
    return experiments.list_experiments()


@app.get("/api/experiments/{name}")
def api_exp_read(name: str):
    try:
        return {"name": name, "text": experiments.read_experiment(name),
                "offline_bag": experiments.get_offline_bag(name)}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/experiments/{name}/merged")
def api_exp_merged(name: str, dataset: str):
    """Live preview of the runtime config (dataset base + experiment fragment)
    for runs whose result snapshot hasn't been collected yet."""
    try:
        base = yaml_edit.read_config(dataset, "estimator_config.yaml")
        text = experiments.merge_config(base["text"], experiments.read_experiment(name))
        return {"name": name, "dataset": dataset, "text": text}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


class ExpWriteBody(BaseModel):
    text: str
    offline_bag: bool | None = None  # per-experiment VIO直读 flag (sidecar meta)


@app.put("/api/experiments/{name}")
def api_exp_write(name: str, body: ExpWriteBody):
    try:
        return experiments.write_experiment(name, body.text, offline_bag=body.offline_bag)
    except ValueError as e:
        raise HTTPException(400, str(e))


class ExpFlagBody(BaseModel):
    offline_bag: bool


@app.patch("/api/experiments/{name}")
def api_exp_flag(name: str, body: ExpFlagBody):
    """Row-level VIO直读 toggle — updates only the sidecar meta."""
    try:
        return experiments.set_offline_bag(name, body.offline_bag)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/experiments/{name}")
def api_exp_del(name: str):
    try:
        return experiments.delete_experiment(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


# ------------------------------------------------------------------ env + backtest
@app.get("/api/env")
def api_env():
    return backtest.env_status()


@app.post("/api/boards/{ip}/mount")
def api_mount(ip: str):
    return backtest.mount_board(ip)


@app.post("/api/boards/{ip}/deploy")
def api_deploy(ip: str):
    """Deploy the platform's own cross-built VIO install to the board.

    Source: the newest install/ available — the auto-test mirror's build, or a
    manual cross-build in the platform's parent checkout (whichever is newer).
    Destination: config.BOARD_INSTALL_DIR on the board — the launch script
    sources exactly that, never /userdata/demo/install.
    """
    try:
        return auto_test.deploy_to_board(ip)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"deploy failed: {e}")


class BacktestBody(BaseModel):
    ip: str
    dataset: str
    image_topic: str | None = None
    extra_args: str = ""


@app.post("/api/backtest/start")
def api_bt_start(b: BacktestBody):
    try:
        return backtest.start_backtest(b.ip, b.dataset, b.image_topic, b.extra_args)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


class StopBody(BaseModel):
    ip: str
    umount: bool = False


@app.post("/api/backtest/stop")
def api_bt_stop(b: StopBody):
    return backtest.stop_backtest(b.ip, b.umount)


@app.get("/api/backtest/status")
def api_bt_status(ip: str):
    try:
        return backtest.backtest_status(ip)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"board unreachable: {e}")


@app.get("/api/backtest/launch_script")
def api_bt_launch_script(ip: str):
    """Return the current run's launch.sh from the board (or 404)."""
    try:
        text = backtest.read_launch_script(ip)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"board unreachable: {e}")
    return {"text": text, "path": f"{config.BOARD_CURRENT_LINK}/launch.sh"}


class PreviewScriptBody(BaseModel):
    ip: str
    dataset: str
    experiment: str = ""
    offline_bag: bool = True
    verbosity: str = "INFO"  # PRINT_* level: ALL/DEBUG/INFO/WARNING/ERROR/SILENT
    vio_log_level: str = "warn"  # ROS log level of the vio node


@app.post("/api/backtest/preview_script")
def api_bt_preview_script(b: PreviewScriptBody):
    """Build the default launch.sh for the current selection without running it.

    Side effect: ships the merged estimator_config.yaml to the board if an
    experiment is selected, so the script's config_path reference is valid.
    """
    try:
        built = backtest.build_launch_script(
            b.ip, b.dataset, experiment=b.experiment or None, offline_bag=b.offline_bag,
            verbosity=b.verbosity, vio_log_level=b.vio_log_level)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not built.get("ok"):
        raise HTTPException(400, built.get("detail", "build failed"))
    return {
        "text": built["script"],
        "config_path": built["config_path"],
        "experiment_keys": built["experiment_keys"],
        "board_dataset_path": built["board_dataset_path"],
        "run_dir": built["run_dir"],
    }


@app.get("/api/backtest/default_template")
def api_bt_default_template():
    """Dataset-free launch.sh template (core launch logic + {{token}} placeholders).

    Opens the launch-script editor without requiring a selected dataset — the
    run path fills the placeholders per task. No board/dataset access needed.
    """
    return {"text": backtest.default_launch_template()}


# ------------------------------------------------------------------ batch backtest
class BatchBody(BaseModel):
    ip: str
    datasets: list[str]
    experiments: list[str] = []  # empty → baseline only (dataset's own config)
    offline_bag: bool = True  # if True, VIO reads bag directly; if False, use ros2 bag play
    launch_script_override: str = ""  # optional custom launch.sh text (single-dataset batch only)
    branch: str = ""  # non-empty → fetch/build/deploy origin/<branch> before running
    verbosity: str = "INFO"  # PRINT_* level: ALL/DEBUG/INFO/WARNING/ERROR/SILENT
    vio_log_level: str = "warn"  # ROS log level of the vio node


@app.post("/api/batch/start")
def api_batch_start(b: BatchBody):
    try:
        return batch.start_batch(b.ip, b.datasets, experiments_list=b.experiments,
                                 offline_bag=b.offline_bag,
                                 launch_script_override=b.launch_script_override or None,
                                 branch=b.branch or "",
                                 verbosity=b.verbosity, vio_log_level=b.vio_log_level)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/batch")
def api_batch_list():
    return batch.list_batches()


@app.get("/api/batch/{batch_id}")
def api_batch_get(batch_id: str):
    try:
        return batch.get_batch(batch_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.post("/api/batch/{batch_id}/stop")
def api_batch_stop(batch_id: str):
    try:
        return batch.stop_batch(batch_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


class DiscardItemBody(BaseModel):
    experiment: str = ""
    dataset: str


@app.delete("/api/batch/{batch_id}/item")
def api_batch_item_discard(batch_id: str, b: DiscardItemBody):
    try:
        return batch.discard_batch_item(batch_id, b.experiment, b.dataset)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


class AppendBody(BaseModel):
    datasets: list[str]
    experiments: list[str] = []
    offline_bag: bool = True
    verbosity: str = "INFO"
    vio_log_level: str = "warn"


@app.post("/api/batch/{batch_id}/append")
def api_batch_append(batch_id: str, b: AppendBody):
    try:
        return batch.append_to_batch(batch_id, b.datasets,
                                     experiments_list=b.experiments,
                                     offline_bag=b.offline_bag,
                                     verbosity=b.verbosity, vio_log_level=b.vio_log_level)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/results")
def api_results():
    return batch.list_results()


@app.get("/api/results/stats")
def api_result_stats(path: str):
    """Read _meta.json from a result dir (path must be under RESULTS_DIR)."""
    try:
        return batch.read_result_stats(path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/results/file")
def api_result_file(path: str, name: str):
    """Read a text artifact (e.g. estimator_config.yaml) from a result dir."""
    try:
        return batch.read_result_file(path, name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


class ResultDeleteBody(BaseModel):
    path: str


@app.delete("/api/results")
def api_result_delete(b: ResultDeleteBody):
    """Move a result dir to results_deleted_YYYYMMDD/ (recoverable)."""
    try:
        return batch.delete_result(b.path)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/batch/queue")
def api_batch_queue_delete(b: ResultDeleteBody):
    """Remove a finished row from the manual 回测 queue only; the collected
    result is left in 统计 for later viewing/deletion."""
    return batch.discard_result_from_queue(b.path)


@app.get("/api/boards/{ip}/ov_web_ready")
def api_ov_web_ready(ip: str):
    """Probe whether ov_web's HTTP server accepts requests yet.

    The viz iframe hits ERR_CONNECTION_REFUSED while the board chain is still
    coming up; the UI polls this and only points the iframe at ov_web once
    ready, so no manual refresh is needed.
    """
    import urllib.request
    try:
        req = urllib.request.Request(
            f"http://{ip}:{config.OV_WEB_PORT}/", method="HEAD")
        with urllib.request.urlopen(req, timeout=2) as r:
            return {"ready": r.status < 500}
    except Exception:  # noqa: BLE001 - any failure means "not ready"
        return {"ready": False}


class ReportBody(BaseModel):
    paths: list[str]
    format: str = "html"  # html | pdf
    title: str = ""


@app.post("/api/stats/report")
def api_stats_report(b: ReportBody):
    """Render the selected result dirs into a standalone HTML / PDF report."""
    try:
        return report.write_report(b.paths, b.format, b.title)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001  (e.g. playwright/chromium missing)
        raise HTTPException(500, f"report generation failed: {e}")


# ------------------------------------------------------------------ auto test
@app.get("/api/auto/status")
def api_auto_status():
    return auto_test.get_scheduler_status()


@app.get("/api/auto/config")
def api_auto_config_get():
    return auto_test.get_config()


class AutoConfigBody(BaseModel):
    enabled: bool | None = None
    github_url: str | None = None
    branch: str | None = None
    hourly_check: bool | None = None
    daily_time: str | None = None
    board_ip: str | None = None
    datasets: list[str] | None = None
    experiments: list[str] | None = None
    offline_bag: bool | None = None
    build_enabled: bool | None = None
    build_cmd: str | None = None
    board_install_path: str | None = None
    launch_script_override: str | None = None


@app.put("/api/auto/config")
def api_auto_config_put(b: AutoConfigBody):
    return auto_test.update_config(b.model_dump(exclude_none=True))


@app.get("/api/auto/tasks")
def api_auto_tasks(status: str = "", limit: int = 200):
    return auto_test.list_tasks(status_filter=status, limit=limit)


@app.delete("/api/auto/tasks/{task_id}")
def api_auto_task_delete(task_id: str):
    """Remove an auto task from the queue only; the collected result stays in
    统计 for later viewing/deletion. Daily-sourced tasks re-arm today's run."""
    return auto_test.delete_task(task_id)


@app.post("/api/auto/hourly_check")
def api_auto_hourly():
    return auto_test.trigger_hourly_check()


@app.post("/api/auto/daily_run")
def api_auto_daily():
    return auto_test.trigger_daily_run()


class AutoEnqueueBody(BaseModel):
    commit: str
    datasets: list[str]
    experiments: list[str] = []
    offline_bag: bool = True
    board_ip: str = ""


@app.post("/api/auto/enqueue")
def api_auto_enqueue(b: AutoEnqueueBody):
    try:
        return auto_test.enqueue_manual(b.commit, b.datasets, b.experiments,
                                       b.offline_bag, b.board_ip)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/auto/commits")
def api_auto_commits(limit: int = 100, branch: str = ""):
    return auto_test.list_known_commits(limit=limit, branch=branch)


@app.get("/api/auto/branches")
def api_auto_branches(fetch: bool = False):
    return {"branches": auto_test.list_remote_branches(fetch=fetch),
            "error": auto_test.mirror_error()}


class AutoPullBody(BaseModel):
    use_proxy: bool | None = None


@app.post("/api/auto/pull")
def api_auto_pull(b: AutoPullBody):
    """Manual "拉取代码": clone if absent, else fetch the default branch.

    Lets a user resolve a fresh-host clone failure from the UI (with the
    proxy toggle) instead of being stuck on a silent-empty dropdown.
    """
    ok, detail = auto_test.pull_mirror(b.use_proxy)
    return {"ok": ok, "detail": detail, "error": auto_test.mirror_error()}


# ------------------------------------------------------------------ static frontend
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
os.makedirs(config.FRAME_CACHE_DIR, exist_ok=True)


class _NoCacheStatic(StaticFiles):
    """Serve web assets with no-cache so the browser never keeps a stale app.js.

    The frontend is versioned via ?v=N query strings; without this a browser can
    keep an old cached app.js/index.html across deploys and show stale UI (e.g.
    an empty branch dropdown even though the API returns branches).
    """

    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers.setdefault("Cache-Control", "no-cache")
        return r


# start the auto-test scheduler (hourly fetch + daily run + manual kicks)
auto_test.start_scheduler()
# clone the mirror (synchronous guarantee) + fetch the default branch so the
# code is present the moment the service answers; manual/auto backtests should
# never wait for a first-use pull
auto_test.bootstrap_mirror()
app.mount("/results", StaticFiles(directory=batch.RESULTS_DIR, html=False), name="results")
# Static mount for frame cache: serves video.mp4 + JPEGs with full HTTP Range
# support so the native <video> element can seek smoothly without per-frame
# Python route overhead.
app.mount("/frame_cache", StaticFiles(directory=config.FRAME_CACHE_DIR, html=False), name="frame_cache")
app.mount("/", _NoCacheStatic(directory=WEB_DIR, html=True), name="web")


if __name__ == "__main__":
    import uvicorn

    print(f"[test_platform] http://0.0.0.0:{config.PORT}  DATA_ROOT={config.DATA_ROOT}")
    uvicorn.run(app, host="0.0.0.0", port=config.PORT, log_level="warning")
