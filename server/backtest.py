"""Board-side backtest orchestration over SSH: NFS mount, launch chain, status, stop."""
import datetime
import os
import re

import yaml

from . import config, datasets, yaml_edit
from .boards import Ssh

# process groups tracked in status.
# NOTE: the vio pattern must match the actual node binary path only. A bare
# 'drobotics_vio_node' also matches the live-mode bag-play wrapper shell whose
# cmdline carries the literal text `pkill -INT -f drobotics_vio_node` — that
# wrapper outlives a crashed VIO and made the monitor report "vio alive".
_PROC_GROUPS = [
    ("static_tf", "static_transform_publisher"),
    ("ov_web", "ov_web_node"),
    ("vio", "lib/drobotics_vio/drobotics_vio_node"),
    ("bag_play", "ros2 bag play"),
]

# first ERROR/died markers in vio.log that mean the node crashed at boot
_CRASH_MARKERS = ["process has died", "VioManager(): invalid", "VioManager(): feature_detector_backend"]

# vio.log first: it is the primary artifact the user watches during a run
_LOGS = ["vio.log", "ov_web.log", "tf.log", "bag.log"]

# non-interactive SSH shells do not source .bashrc: prepend the ROS env here.
# The VIO install is the platform's own cross-built one (deployed to
# BOARD_INSTALL_DIR) — NOT the board's preinstalled /userdata/demo/install,
# which is too old (no ov_web).
_ROS_ENV = (
    "source /opt/tros/humble/setup.bash"
    f" && [ -f {config.BOARD_INSTALL_DIR}/setup.bash ] && source {config.BOARD_INSTALL_DIR}/setup.bash"
    " || true; "
)


def _board_log(p: str) -> str:
    """Log path inside the CURRENT run dir (via the current symlink)."""
    return f"{config.BOARD_CURRENT_LINK}/{p}"


# ------------------------------------------------------------------ env
def env_status() -> dict:
    import subprocess

    host = config.host_ip()
    nfs_exported = False
    export_detail = ""
    try:
        r = subprocess.run(["exportfs", "-v"], capture_output=True, text=True, timeout=10)
        nfs_exported = r.returncode == 0 and config.DATA_ROOT in (r.stdout or "")
        export_detail = (r.stdout or r.stderr or "").strip()
        if r.returncode != 0 and not r.stdout:
            export_detail = "exportfs not available or nfs-kernel-server not installed"
    except Exception as e:  # noqa: BLE001
        export_detail = str(e)
    # One-liner the operator can copy-paste. setup_nfs.sh reads DATA_ROOT from the
    # environment, so it must be set (not hardcoded to the %PATH default).
    setup_nfs = os.path.join(config.REPO_DIR, "setup_nfs.sh")
    setup_command = f"sudo DATA_ROOT={config.DATA_ROOT} bash {setup_nfs}"
    return {
        "host_ip": host,
        "data_root": config.DATA_ROOT,
        "nfs_exported": nfs_exported,
        "nfs_detail": export_detail,
        "setup_hint": f"run: {setup_command}" if not nfs_exported else "",
        "setup_command": setup_command if not nfs_exported else "",
        "board_mount": config.BOARD_MOUNT,
        "ov_web_port": config.OV_WEB_PORT,
    }


# ------------------------------------------------------------------ mount
def board_mounted_path(ip: str) -> "str | None":
    """Return a board path that already exposes the host DATA_ROOT (any NFS mount), else None.

    Boards may already mount the host filesystem (e.g. /mnt/nfs20 -> host:/home).
    If the mounted source covers DATA_ROOT, reuse it instead of mounting again.
    """
    with Ssh(ip, timeout=10) as s:
        r = s.run("mount | grep -E 'nfs' | awk '{print $1, $3}'")
    host = config.host_ip()
    for line in r["out"].splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        src, mnt = parts
        if not src.startswith(f"{host}:"):
            continue
        exported = src[len(host) + 1:] or "/"
        # exported dir must be a prefix of DATA_ROOT (or equal)
        if config.DATA_ROOT == exported or config.DATA_ROOT.startswith(exported.rstrip("/") + "/"):
            # translate DATA_ROOT into the board's mount namespace
            rel = config.DATA_ROOT[len(exported.rstrip("/")):].lstrip("/")
            return mnt if not rel else mnt.rstrip("/") + "/" + rel
    return None


def _attempt_mount(host: str, mnt: str, ip: str) -> dict:
    """One attempt at NFS-mounting the host DATA_ROOT on the board at mnt."""
    with Ssh(ip) as s:
        # board needs nfs client
        r = s.run("which mount.nfs >/dev/null 2>&1 || dpkg -s nfs-common >/dev/null 2>&1 && echo HAS_NFS || echo NO_NFS")
        if "NO_NFS" in r["out"] and "HAS_NFS" not in r["out"]:
            try:
                s.run("apt-get install -y nfs-common", timeout=180)
            except Exception:  # noqa: BLE001
                pass
        cmd = f"mkdir -p {mnt} && mount -t nfs -o nolock,proto=tcp {host}:{config.DATA_ROOT} {mnt}"
        r = s.run(cmd, timeout=60)
        check = s.run(f"mountpoint -q {mnt} && echo MOUNTED || echo NOTMOUNTED")
        # split() so a failed mount ("NOTMOUNTED") does NOT match — "MOUNTED" is a
        # substring of "NOTMOUNTED", so `"MOUNTED" in out` was always True on failure.
        mounted = "MOUNTED" in check["out"].split()
        return {"ok": mounted, "already": False, "board_path": mnt if mounted else None,
                "detail": (r["out"] + r["err"]).strip() or (f"mounted {host}:{config.DATA_ROOT} -> {mnt}" if mounted else "mount failed")}


def _auto_export_host() -> tuple:
    """Provision the host NFS export for DATA_ROOT via passwordless sudo (never prompt).

    Returns (ok, detail). Only uses ``sudo -n`` so a server thread never blocks on a
    password prompt: if the platform user lacks passwordless sudo it returns False
    and the operator must run ``sudo bash setup_nfs.sh`` (or start via run.sh).
    setup_nfs.sh reads DATA_ROOT from the environment, so it is passed through sudo.
    """
    import subprocess

    setup_nfs = os.path.join(config.REPO_DIR, "setup_nfs.sh")
    if not os.path.isfile(setup_nfs):
        return False, "setup_nfs.sh not found"
    try:
        if subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=10).returncode != 0:
            return False, "no passwordless sudo"
        r = subprocess.run(["sudo", "-n", f"DATA_ROOT={config.DATA_ROOT}", "bash", setup_nfs],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or "").strip()[-120:]
        return True, "exported"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def mount_board(ip: str) -> dict:
    host = config.host_ip(for_ip=ip)
    mnt = config.BOARD_MOUNT
    existing = board_mounted_path(ip)
    if existing:
        return {"ok": True, "already": True, "board_path": existing, "detail": f"reusing existing NFS mount: {existing}"}
    result = _attempt_mount(host, mnt, ip)
    # Self-heal: an "access denied" mount means DATA_ROOT isn't NFS-exported on the
    # host. Auto-provision it (passwordless sudo) and retry once, so a backtest does
    # not require the operator to set up NFS by hand first.
    if not result["ok"] and "access denied" in result["detail"].lower():
        ok, detail = _auto_export_host()
        if ok:
            result = _attempt_mount(host, mnt, ip)
            if not result["ok"]:
                result["detail"] = f"{result['detail']} (auto-export ran: {detail})"
        else:
            result["detail"] = f"{result['detail']} — auto NFS export failed: {detail}; run `sudo bash setup_nfs.sh` or start via run.sh"
    return result


def _board_dataset_ready(ip: str, mnt_ds: str) -> "tuple[bool, str]":
    """True iff the board actually sees the dataset artifacts at mnt_ds.

    The NFS mount can silently fail (DATA_ROOT not exported on the host) or drop
    after a board reboot; launch.sh then yields a run with blank logs and only a
    late 'config_path does not exist'. Verify the two files the launch chain
    depends on BEFORE building the script so a bad mount surfaces as a clear
    actionable error instead of a silent empty run.
    """
    required = ("stereo_auto_gen/estimator_config.yaml", "ros2bag_vio/metadata.yaml")
    checks = "; ".join(f"[ -f '{mnt_ds}/{p}' ] || echo 'MISS::{p}'" for p in required)
    with Ssh(ip, timeout=15) as s:
        r = s.run(checks, timeout=20)
    missing = [ln.split("::", 1)[1] for ln in r["out"].splitlines() if ln.strip().startswith("MISS::")]
    if missing:
        return False, "missing " + ", ".join(missing)
    return True, ""


# ------------------------------------------------------------------ static TF from camchain
def _rot_to_quat(R):
    """Hand-rolled 3x3 rotation -> (x, y, z, w) quaternion (no scipy)."""
    import math

    tr = R[0][0] + R[1][1] + R[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        w = 0.25 * s
        x = (R[2][1] - R[1][2]) / s
        y = (R[0][2] - R[2][0]) / s
        z = (R[1][0] - R[0][1]) / s
    elif R[0][0] > R[1][1] and R[0][0] > R[2][2]:
        s = math.sqrt(1.0 + R[0][0] - R[1][1] - R[2][2]) * 2
        w = (R[2][1] - R[1][2]) / s
        x = 0.25 * s
        y = (R[0][1] + R[1][0]) / s
        z = (R[0][2] + R[2][0]) / s
    elif R[1][1] > R[2][2]:
        s = math.sqrt(1.0 + R[1][1] - R[0][0] - R[2][2]) * 2
        w = (R[0][2] - R[2][0]) / s
        x = (R[0][1] + R[1][0]) / s
        y = 0.25 * s
        z = (R[1][2] + R[2][1]) / s
    else:
        s = math.sqrt(1.0 + R[2][2] - R[0][0] - R[1][1]) * 2
        w = (R[1][0] - R[0][1]) / s
        x = (R[0][2] + R[2][0]) / s
        y = (R[1][2] + R[2][1]) / s
        z = 0.25 * s
    return x, y, z, w


def static_tf_commands(name: str) -> "list[str] | None":
    """Two static_transform_publisher commands using extrinsics from the camchain yaml."""
    import os as _os

    try:
        files = [c["name"] for c in yaml_edit.list_configs(name)]
        cam = next((f for f in files if "camchain" in f), None)
        if not cam:
            return None
        p = _os.path.join(datasets.get_dataset(name)["path"], "stereo_auto_gen", cam)
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        T = data.get("T_imu_cam0")
        if not T or len(T) != 4:
            return None
        tx, ty, tz = (float(v) for v in (T[0][3], T[1][3], T[2][3]))
        x, y, z, w = _rot_to_quat([row[:3] for row in T])
        cam_imu = f"ros2 run tf2_ros static_transform_publisher {tx} {ty} {tz} {x} {y} {z} {w} camera_link imu_link"
        base_cam = "ros2 run tf2_ros static_transform_publisher 0 0 0 0 0 0 1 base_link camera_link"
        return [base_cam, cam_imu]
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ backtest lifecycle
def _render_template(tpl: str, ctx: dict) -> str:
    """Fill {{token}} placeholders in a launch.sh template from per-run values.

    Used for the auto-backtest template so one saved script can be reused across
    tasks/datasets without pinning the run_dir/config_path it was authored
    against. Unknown tokens are left as-is.
    """
    out = tpl
    for k, v in ctx.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def _launch_parts(*, image_topic, compressed, config_path, run_dir, mnt_ds,
                  offline_bag, verbosity, vio_log_level, extra_args) -> list:
    """Assemble the subscribe.launch.py arg list in one pure, testable place.

    Guard: the stereo image topic NAME is fixed by the rig convention
    (config.CANONICAL_STEREO_TOPIC). If a caller passes anything else we warn
    loudly rather than silently emitting it — the VIO derives the compressed
    sub-topic by rewriting the trailing token, so a non-canonical name produces
    a *_jjpeg mismatch and a FastCDR crash (exit -6), and we've been burned by
    that. The only legitimate variation is `compressed`:
        compressed=True  -> sub_from_compressed_image:=True
        compressed=False -> no flag (VIO default is False)
    """
    import sys as _sys
    resolved = image_topic or config.CANONICAL_STEREO_TOPIC
    if resolved != config.CANONICAL_STEREO_TOPIC:
        print(
            f"[WARN] non-canonical topic_camera_stereo '{resolved}' "
            f"!= '{config.CANONICAL_STEREO_TOPIC}'; check rig/launch",
            file=_sys.stderr,
            flush=True,
        )
    parts = [f"topic_camera_stereo:={resolved}"]
    if compressed:
        parts.append("sub_from_compressed_image:=True")
    parts.append("use_local_imu:=False")
    parts.append("use_sim_time:=True")
    # subscribe.launch.py: auto_gen_config defaults to True and is mutually
    # exclusive with config_path. We're passing an explicit config_path, so
    # auto_gen_config must be False.
    parts.append("auto_gen_config:=False")
    parts.append(f"config_path:={config_path}")
    parts.append(f"save_path:={run_dir}/output")
    if offline_bag:
        # VIO reads the bag directly via its BagReader; live subscriptions are
        # skipped, but a lightweight clock-only `ros2 bag play` is still added
        # below so sim-time /clock exists (throttled timing logs depend on it).
        parts.append(f"offline_bag_path:={mnt_ds}/ros2bag_vio")
    parts.append("vio_visual:=false")
    # 日志级别：verbosity=算法 PRINT_* 输出级别，vio_log_level=ROS 日志级别。
    # 与 launch 默认值一致时行为不变；放在 extra_args 前以便用户手动覆盖
    if verbosity:
        parts.append(f"verbosity:={verbosity}")
    if vio_log_level:
        parts.append(f"vio_log_level:={vio_log_level}")
    if extra_args.strip():
        parts.append(extra_args.strip())
    return parts


def default_launch_template() -> str:
    """Dataset-free launch.sh template: CORE launch logic only, with the per-run /
    per-dataset values expressed as {{token}} placeholders that start_backtest
    fills in at run time. The editor can therefore be opened and edited WITHOUT
    selecting a dataset — the dataset is just a placeholder.

    Tokens the run-time renderer knows: run_dir, config_path, save_path, dataset,
    image_topic, board_dataset, offline_bag, experiment, board_base, runs_dir,
    install_dir, current, _ros_env, sub_from_compressed_image, imu_topic,
    verbosity, vio_log_level.
    """
    env = ("source /opt/tros/humble/setup.bash && "
           "[ -f {{install_dir}}/setup.bash ] && source {{install_dir}}/setup.bash || true; ")
    env_sh = env.replace("{{install_dir}}", config.BOARD_INSTALL_DIR)
    lines = [
        "#!/bin/bash",
        "# Dataset-free template (generated by test_platform server/backtest.py)",
        "# dataset: {{dataset}}    experiment: {{experiment}}    offline_bag: {{offline_bag}}",
        "# run_dir: {{run_dir}}",
        "set -u",
        "mkdir -p {{run_dir}}/output",
        "ln -sfn {{run_dir}} {{current}}",
        # cleanup leftovers from any prior run
        "pkill -f 'drobotics_vio_node' ; pkill -f 'ov_web_node' ; pkill -f 'ros2 bag play' ; pkill -f static_transform_publisher ; true",
        # static TFs (per-run extrinsics; generic identity fallback shown here)
        "nohup bash -c '" + env_sh + " ros2 run tf2_ros static_transform_publisher 0 0 0 0 0 0 1 base_link camera_link' > {{run_dir}}/tf.log 2>&1 &",
        "nohup bash -c '" + env_sh + " ros2 run tf2_ros static_transform_publisher 0 0 0 0 0 0 1 camera_link imu_link' >> {{run_dir}}/tf.log 2>&1 &",
        # ov_web visualization websocket (launch file lives under share/<pkg>/ov_web/launch/)
        "nohup bash -c '" + env_sh + " ros2 launch " + config.BOARD_INSTALL_DIR + "/drobotics_vio/share/drobotics_vio/ov_web/launch/ov_web.launch.py' > {{run_dir}}/ov_web.log 2>&1 &",
        # VIO
        "nohup bash -c '" + env_sh + " ros2 launch drobotics_vio subscribe.launch.py "
        "topic_camera_stereo:={{image_topic}} "
        "sub_from_compressed_image:={{sub_from_compressed_image}} "
        "use_local_imu:=False use_sim_time:=True auto_gen_config:=False "
        "config_path:={{config_path}} "
        "save_path:={{save_path}} "
        "offline_bag_path:={{board_dataset}}/ros2bag_vio "
        "vio_visual:=false verbosity:={{verbosity}} vio_log_level:={{vio_log_level}}' "
        "> {{run_dir}}/vio.log 2>&1 &",
    ]
    return "\n".join(lines) + "\n"


def build_launch_script(ip: str, dataset: str, image_topic: str = None, extra_args: str = "",
                        experiment: str = None, offline_bag: bool = True,
                        verbosity: str = "INFO", vio_log_level: str = "warn") -> dict:
    """Build the launch.sh text + side artifacts for one (dataset, experiment) pair.

    Every run gets its own persistent dir on the board:
      <BOARD_RUNS_DIR>/<yyyymmdd_hhmmss>__<experiment|baseline>__<dataset>/
        launch.sh  estimator_config.yaml  vio.log  ov_web.log  tf.log  bag.log
        output/    (VIO save_path)
    and <BOARD_BASE>/current is re-pointed at it, so status polling and result
    collection always follow the latest run. Nothing is overwritten between runs.

    Side effect: if `experiment` is set, deep-merges its fragment onto the dataset's
    base estimator_config.yaml and SFTPs the merged file into the run dir (so
    config_path:=<run_dir>/... is valid whether the caller runs the script now
    or later).

    Does NOT execute launch.sh — the caller ships the (possibly user-edited) script
    text and runs `bash <run_dir>/launch.sh`.
    """
    ds = datasets.get_dataset(dataset)
    # board-visible path of the dataset (existing NFS mount preferred, else /mnt/vio_datasets)
    root_on_board = board_mounted_path(ip)
    mount_detail = ""
    if root_on_board is None:
        m = mount_board(ip)
        mount_detail = m.get("detail", "")
        root_on_board = m.get("board_path")
    if not root_on_board:
        hint = ""
        if "access denied" in mount_detail.lower():
            hint = (" — DATA_ROOT is not NFS-exported on the host; run "
                    "`sudo bash setup_nfs.sh` (with DATA_ROOT set) or start via "
                    "`run.sh --data-root=<dir>`, then 挂载数据 / retry")
        return {"ok": False, "detail": f"data root not mounted on board: {mount_detail or '(run 挂载数据 first)'}{hint}"}
    mnt_ds = f"{root_on_board}/{dataset}"
    ok, why = _board_dataset_ready(ip, mnt_ds)
    if not ok:
        return {"ok": False,
                "detail": f"dataset not visible on board at {mnt_ds}: {why} — ensure DATA_ROOT is NFS-exported "
                          f"(sudo bash setup_nfs.sh) and mounted on the board before running"}
    topics = datasets.pick_default_topics(dataset)
    image_topic = image_topic or topics.get("image")
    # `sub_from_compressed_image` must reflect the BAG's image encoding, not the
    # topic name: the topic is always `/sub_image_combine_raw`, so a "jpeg in
    # topic" check would always be False even for CompressedImage bags.
    compressed = bool(topics.get("image_compressed"))

    if not ds.get("has_bag") or not ds.get("has_config"):
        return {"ok": False, "detail": "dataset lacks ros2bag_vio/ or stereo_auto_gen/"}

    # per-run dir on the board (persistent partition; /tmp is tmpfs and would
    # lose logs/results on reboot)
    run_id = "{}__{}__{}".format(
        datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
        experiment or "baseline",
        dataset.replace("/", "__"),
    )
    run_dir = f"{config.BOARD_RUNS_DIR}/{run_id}"

    # config_path defaults to the dataset's own estimator_config.yaml. If an
    # experiment is selected, deep-merge its yaml fragment onto the base and
    # SFTP the merged file into the run dir; config_path points there.
    base_cfg_path = f"{mnt_ds}/stereo_auto_gen/estimator_config.yaml"
    config_path = base_cfg_path
    exp_keys = []
    if experiment:
        from . import experiments as _exp
        try:
            exp_text = _exp.read_experiment(experiment)
            host_base = os.path.join(datasets.get_dataset(dataset)["path"], "stereo_auto_gen", "estimator_config.yaml")
            with open(host_base, encoding="utf-8") as fp:
                base_text = fp.read()
            merged = _exp.merge_config(base_text, exp_text)
            exp_keys = _exp._flatten_keys(yaml.safe_load(exp_text) or {})
        except FileNotFoundError as e:
            return {"ok": False, "detail": f"experiment not found: {e}"}
        except ValueError as e:
            return {"ok": False, "detail": f"experiment merge error: {e}"}
        board_cfg = f"{run_dir}/estimator_config.yaml"
        try:
            with Ssh(ip) as s:
                s.run(f"mkdir -p {run_dir}")
                cli = s._cli
                sftp = cli.open_sftp()
                try:
                    with sftp.open(board_cfg, "w") as f:
                        f.write(merged)
                    # the merged config references sibling yamls (camchain /
                    # imu model) via paths relative to its own directory —
                    # ship them into the run dir alongside it
                    host_gen = os.path.dirname(host_base)
                    for fn in os.listdir(host_gen):
                        if fn.endswith((".yaml", ".yml")) and fn != "estimator_config.yaml":
                            sftp.put(os.path.join(host_gen, fn), f"{run_dir}/{fn}")
                finally:
                    sftp.close()
            config_path = board_cfg
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"failed to ship merged config to board: {e}"}

    # config may reference imu/image topics itself (auto-gen); topic overrides optional
    parts = _launch_parts(
        image_topic=image_topic,
        compressed=compressed,
        config_path=config_path,
        run_dir=run_dir,
        mnt_ds=mnt_ds,
        offline_bag=offline_bag,
        verbosity=verbosity,
        vio_log_level=vio_log_level,
        extra_args=extra_args,
    )
    vio_launch = "ros2 launch drobotics_vio subscribe.launch.py " + " ".join(parts)

    tf_cmds = static_tf_commands(dataset) or [
        "ros2 run tf2_ros static_transform_publisher 0 0 0 0 0 0 1 base_link camera_link",
        "ros2 run tf2_ros static_transform_publisher 0 0 0 0 0 0 1 camera_link imu_link",
    ]

    tf_log = f"{run_dir}/tf.log"
    ov_web_log = f"{run_dir}/ov_web.log"
    vio_log = f"{run_dir}/vio.log"
    bag_log = f"{run_dir}/bag.log"
    lines = [
        "#!/bin/bash",
        "# Auto-generated by test_platform server/backtest.py",
        f"# dataset: {dataset}    experiment: {experiment or '(baseline)'}    offline_bag: {offline_bag}",
        f"# run_dir: {run_dir}",
        "set -u",
        f"mkdir -p {run_dir}/output",
        # point the stable `current` symlink at this run (status/collect follow it)
        f"ln -sfn {run_dir} {config.BOARD_CURRENT_LINK}",
        # 1) cleanup leftovers
        "pkill -f 'drobotics_vio_node' ; pkill -f 'ov_web_node' ; pkill -f 'ros2 bag play' ; pkill -f static_transform_publisher ; true",
        # 2) static TFs (camchain extrinsics)
        f"nohup bash -c '{_ROS_ENV} {tf_cmds[0]}' > {tf_log} 2>&1 &",
        f"nohup bash -c '{_ROS_ENV} {tf_cmds[1]}' >> {tf_log} 2>&1 &",
        # 3) ov_web (visualization websocket, port 9988)
        # NOTE: ov_web's launch file is installed at
        # share/drobotics_vio/ov_web/launch/ov_web.launch.py, NOT the
        # conventional share/drobotics_vio/launch/ — so pass the absolute
        # path (ros2 launch <pkg> <file> only searches <share>/<pkg>/launch/).
        f"nohup bash -c '{_ROS_ENV} ros2 launch {config.BOARD_INSTALL_DIR}/drobotics_vio/share/drobotics_vio/ov_web/launch/ov_web.launch.py' > {ov_web_log} 2>&1 &",
        # 4) VIO
        f"nohup bash -c '{_ROS_ENV} {vio_launch}' > {vio_log} 2>&1 &",
    ]
    if not offline_bag:
        # 5) bag play — only needed when VIO is in live-subscription mode.
        # When ros2 bag play exits (bag exhausted), give VIO a grace period to
        # drain queued messages, then SIGINT it so it shuts down via the
        # standard rclcpp path (SIGINT → on_shutdown hook → request_shutdown →
        # spin exits → finalize_shutdown). VIO itself can't know the bag is
        # done in this mode — it's passively subscribed.
        lines.append(
            f"nohup bash -c '{_ROS_ENV} ros2 bag play {mnt_ds}/ros2bag_vio --clock; "
            f"rc=$?; sleep 5; pkill -INT -f drobotics_vio_node; exit $rc' > {bag_log} 2>&1 &"
        )
    else:
        # offline mode: VIO's BagReader reads the bag directly and self-exits at
        # EOF, but with use_sim_time:=True and no /clock source the node clock
        # is frozen — RCLCPP_*_THROTTLE ([TIME] per-frame timing lines) never
        # fires. Play back ONLY the imu topic with --clock to drive /clock at
        # minimal board cost (VIO skips live subscriptions in this mode, so the
        # published messages are ignored; only /clock matters). Falls back to a
        # full play when the bag has no imu topic.
        imu_topic = topics.get("imu")
        clock_play = f"ros2 bag play {mnt_ds}/ros2bag_vio --clock"
        if imu_topic:
            clock_play += f" --topics {imu_topic}"
        lines.append(
            f"nohup bash -c '{_ROS_ENV} {clock_play}' > {bag_log} 2>&1 &"
        )
    launch_sh = "\n".join(lines) + "\n"

    return {
        "ok": True,
        "detail": "built",
        "script": launch_sh,
        "run_dir": run_dir,
        "config_path": config_path,
        "experiment_keys": exp_keys,
        "board_dataset_path": mnt_ds,
        "commands": {"vio": vio_launch, "tf": tf_cmds},
        "offline_bag": offline_bag,
        "image_topic": image_topic or config.CANONICAL_STEREO_TOPIC,
        "sub_from_compressed_image": str(compressed).lower(),
        "imu_topic": topics.get("imu") or "",
        "ov_web_url": f"http://{ip}:{config.OV_WEB_PORT}/",
    }


def start_backtest(ip: str, dataset: str, image_topic: str = None, extra_args: str = "",
                   experiment: str = None, offline_bag: bool = True,
                   launch_script_override: "str | None" = None,
                   verbosity: str = "INFO", vio_log_level: str = "warn",
                   follow_run_dir_marker: bool = True) -> dict:
    """Build + ship + run launch.sh on the board.

    `launch_script_override` replaces the script body. For a one-off (manual
    batch) override the text is used verbatim and any `# run_dir:` header marker
    is honored. For a reusable auto template pass `follow_run_dir_marker=False`
    and/or use `{{token}}` placeholders so the script is rendered against THIS
    run's freshly-built dirs instead of the run it was authored on.
    """
    built = build_launch_script(ip, dataset, image_topic=image_topic, extra_args=extra_args,
                                experiment=experiment, offline_bag=offline_bag,
                                verbosity=verbosity, vio_log_level=vio_log_level)
    if not built.get("ok"):
        return built
    run_dir = built["run_dir"]
    launch_sh = built["script"]
    if launch_script_override:
        launch_sh = launch_script_override
        if "{{" in launch_sh:
            launch_sh = _render_template(launch_sh, {
                "run_dir": run_dir,
                "config_path": built["config_path"],
                "save_path": f"{run_dir}/output",
                "dataset": dataset,
                "image_topic": built.get("image_topic") or image_topic or "",
                "board_dataset": built["board_dataset_path"],
                "offline_bag": str(offline_bag).lower(),
                "experiment": experiment or "",
                "board_base": config.BOARD_BASE,
                "runs_dir": config.BOARD_RUNS_DIR,
                "install_dir": config.BOARD_INSTALL_DIR,
                "current": config.BOARD_CURRENT_LINK,
                "sub_from_compressed_image": built.get("sub_from_compressed_image") or "false",
                "imu_topic": built.get("imu_topic") or "",
                "_ros_env": _ROS_ENV,
                "verbosity": verbosity or "",
                "vio_log_level": vio_log_level or "",
            })
        if follow_run_dir_marker:
            m = re.search(r"^# run_dir: (\S+)$", launch_sh, re.M)
            if m:
                run_dir = m.group(1)
        else:
            # reusable auto template: never pin to a stale authored run_dir —
            # every task builds its own fresh dir (status/current/collect follow
            # the symlink created in the script).
            launch_sh = re.sub(r"^# run_dir: \S+$", "", launch_sh, flags=re.M)
    launch_path = f"{run_dir}/launch.sh"
    with Ssh(ip) as s:
        s.run(f"mkdir -p {run_dir}")
        cli = s._cli
        sftp = cli.open_sftp()
        try:
            with sftp.open(launch_path, "w") as f:
                f.write(launch_sh)
            sftp.chmod(launch_path, 0o755)
        finally:
            sftp.close()
        s.run(f"bash {launch_path}")
    return {
        "ok": True,
        "detail": "launched",
        "board_dataset_path": built["board_dataset_path"],
        "experiment": experiment,
        "experiment_keys": built["experiment_keys"],
        "config_path": built["config_path"],
        "commands": built["commands"],
        "launch_script": launch_sh,
        "launch_script_path": launch_path,
        "run_dir": run_dir,
        "ov_web_url": built["ov_web_url"],
        "offline_bag": offline_bag,
    }


def stop_backtest(ip: str, umount: bool = False) -> dict:
    with Ssh(ip) as s:
        out = []
        for pat in ("drobotics_vio_node", "ov_web_node", "ros2 bag play", "static_transform_publisher"):
            r = s.run(f"pkill -f '{pat}' ; true")
            out.append(f"{pat}: killed")
        if umount:
            s.run(f"umount {config.BOARD_MOUNT} ; true")
            out.append("umount requested")
        return {"ok": True, "detail": "; ".join(out)}


def backtest_status(ip: str) -> dict:
    """One SSH round trip for process liveness + log tails.

    The previous implementation ran 8 separate exec_command calls (4 pgrep +
    4 tail), each a full round trip (~0.1-0.3s on LAN); the frontend polls
    this every 3s, so the latency stacked up. Here a single remote shell
    prints a machine-parseable blob we split host-side.
    """
    pat_map = "; ".join(
        f"echo \"P:{key}:$(pgrep -f '[{pat[0]}]{pat[1:]}' | wc -l)\""
        for key, pat in _PROC_GROUPS
    )
    log_map = "; ".join(
        f"echo \"L:{name}:BEGIN\"; tail -n 30 {_board_log(name)} 2>/dev/null"
        for name in _LOGS
    )
    with Ssh(ip) as s:
        r = s.run(pat_map + "; " + log_map, timeout=30)
    procs = {}
    logs = {}
    cur_log = None
    for line in r["out"].splitlines():
        if line.startswith("P:"):
            _, key, cnt = line.split(":", 2)
            try:
                procs[key] = int(cnt.strip())
            except ValueError:
                procs[key] = 0
            cur_log = None
        elif line.startswith("L:") and line.endswith(":BEGIN"):
            cur_log = line[2:-6]
            logs[cur_log] = []
        elif cur_log is not None:
            logs[cur_log].append(line)
    logs = {k: "\n".join(v) for k, v in logs.items()}
    return {"ip": ip, "processes": procs, "logs": logs, "mounted": board_mounted_path(ip) is not None,
            "ov_web_url": f"http://{ip}:{config.OV_WEB_PORT}/", "crash": _detect_crash(logs, procs)}


def _detect_crash(logs: dict, procs: dict) -> "dict | None":
    """Surface a VIO boot crash instead of silently showing 'running'.

    The tail of vio.log carries ros2 launch's 'process has died' line plus the
    PRINT_ERROR cause right above it. Report both so the UI can banner the
    exact bad key (e.g. an enum value OpenCV FileStorage mis-parsed).
    """
    vio_log = logs.get("vio.log", "")
    if not vio_log or not any(m in vio_log for m in _CRASH_MARKERS):
        return None
    lines = [ln for ln in vio_log.splitlines() if ln.strip()]
    errors = []
    died = ""
    for ln in lines:
        if "process has died" in ln:
            died = ln.strip()
        elif "[ERROR]" in ln:
            # strip ANSI color codes for readable display
            errors.append(re.sub(r"\x1b\[[0-9;]*m", "", ln).strip())
    # prefer the real cause line (names the bad key) over follow-up "- FAST"
    # style listing lines; fall back to the first error seen.
    cause = next((e for e in errors if "VioManager()" in e), errors[0] if errors else "")
    return {"cause": cause[-400:], "died": died[-400:], "vio_alive": procs.get("vio", 0) > 0}


def read_launch_script(ip: str) -> str:
    """Fetch the current run's launch.sh from the board. Raises FileNotFoundError if absent."""
    path = f"{config.BOARD_CURRENT_LINK}/launch.sh"
    with Ssh(ip, timeout=10) as s:
        cli = s._cli
        sftp = cli.open_sftp()
        try:
            try:
                with sftp.open(path, "r") as f:
                    return f.read().decode("utf-8", errors="replace")
            except IOError as e:
                raise FileNotFoundError(f"launch.sh not on board: {e}")
        finally:
            sftp.close()
