"""Experiment yaml fragments: small overrides layered on top of a dataset's
base estimator_config.yaml at backtest time.

An experiment is a yaml file under test_platform/experiments/ containing only
the keys the user wants to override (e.g. ``msckf_chi2_margin: 0.001``). At
backtest start the chosen fragment is deep-merged onto the dataset's base
config and the merged yaml is shipped to the board as the runtime config_path.
"""
import os
import re

import yaml

from . import config

EXP_DIR = os.path.join(config.REPO_DIR, "experiments")

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _ensure_dir():
    os.makedirs(EXP_DIR, exist_ok=True)


def _validate_name(name: str) -> str:
    if not name or not _SAFE_NAME.match(name):
        raise ValueError(f"invalid experiment name: {name!r} (use letters, digits, _ or -)")
    return name


def _meta_path(name: str) -> str:
    return os.path.join(EXP_DIR, name + ".meta.json")


def get_offline_bag(name: str) -> bool:
    """Per-experiment offline-bag flag (VIO reads the bag directly vs live
    ros2 bag play). Stored in a <name>.meta.json sidecar so the yaml fragment
    stays a pure estimator-config override. Default True."""
    import json

    try:
        with open(_meta_path(name), encoding="utf-8") as f:
            return bool(json.load(f).get("offline_bag", True))
    except Exception:  # noqa: BLE001 — missing/corrupt sidecar → default
        return True


def set_offline_bag(name: str, flag: bool) -> dict:
    """Update only the offline-bag sidecar flag (row-level toggle in the UI)."""
    import json

    _validate_name(name)
    if not any(os.path.isfile(os.path.join(EXP_DIR, name + ext)) for ext in (".yaml", ".yml")):
        raise FileNotFoundError(f"experiment not found: {name}")
    with open(_meta_path(name), "w", encoding="utf-8") as fp:
        json.dump({"offline_bag": bool(flag)}, fp)
    return {"name": name, "offline_bag": get_offline_bag(name)}


def list_experiments() -> list:
    """Return [{name, keys, text_preview, offline_bag}]."""
    _ensure_dir()
    out = []
    for f in sorted(os.listdir(EXP_DIR)):
        if not f.endswith(".yaml") and not f.endswith(".yml"):
            continue
        name = f[: -5] if f.endswith(".yaml") else f[: -4]
        path = os.path.join(EXP_DIR, f)
        try:
            with open(path, encoding="utf-8") as fp:
                text = fp.read()
            data = yaml.safe_load(text) or {}
            keys = sorted(_flatten_keys(data)) if isinstance(data, dict) else []
        except Exception:  # noqa: BLE001
            keys, text = [], ""
        out.append({"name": name, "keys": keys, "text_preview": text[:400],
                    "offline_bag": get_offline_bag(name)})
    return out


def read_experiment(name: str) -> str:
    _validate_name(name)
    for ext in (".yaml", ".yml"):
        p = os.path.join(EXP_DIR, name + ext)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fp:
                return fp.read()
    raise FileNotFoundError(f"experiment not found: {name}")


def write_experiment(name: str, text: str, offline_bag: "bool | None" = None) -> dict:
    _validate_name(name)
    # validate yaml before saving
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"yaml parse error: {e}")
    _ensure_dir()
    p = os.path.join(EXP_DIR, name + ".yaml")
    with open(p, "w", encoding="utf-8") as fp:
        fp.write(text)
    if offline_bag is not None:
        set_offline_bag(name, offline_bag)
    return {"name": name, "path": p, "offline_bag": get_offline_bag(name)}


def delete_experiment(name: str) -> dict:
    _validate_name(name)
    for ext in (".yaml", ".yml"):
        p = os.path.join(EXP_DIR, name + ext)
        if os.path.isfile(p):
            os.remove(p)
            if os.path.isfile(_meta_path(name)):
                os.remove(_meta_path(name))
            return {"name": name, "removed": p}
    raise FileNotFoundError(f"experiment not found: {name}")


def merge_config(base_text: str, exp_text: str) -> str:
    """Deep-merge exp onto base (exp overrides). Returns merged yaml text."""
    base = yaml.safe_load(_strip_yaml_directives(base_text)) or {}
    exp = yaml.safe_load(_strip_yaml_directives(exp_text)) or {}
    if not isinstance(base, dict):
        raise ValueError("base config is not a yaml mapping")
    if not isinstance(exp, dict):
        raise ValueError("experiment is not a yaml mapping")
    merged = _deep_merge(base, exp)
    # default_flow_style=None: scalar-only collections dump in flow style
    # ([ 0.0, 0.0 ]) while nested maps stay block — matches the hand-written
    # configs and, critically, OpenCV FileStorage's limited YAML parser which
    # rejects non-indented block sequences ("Incorrect indentation").
    out = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, width=1000,
                         default_flow_style=None)
    # OpenCV FileStorage refuses yaml without the %YAML directive ("Input file
    # is invalid"). safe_load/safe_dump drop directives, so re-add the header
    # when the source config carried one.
    if any(ln.lstrip().startswith("%YAML") for ln in (base_text or "").splitlines()):
        out = "%YAML:1.0\n" + out
    return out


def _strip_yaml_directives(text: str) -> str:
    """Drop `%YAML:...`, `%YAML 1.0`, etc. — PyYAML rejects the colon form."""
    return "\n".join(
        ln for ln in (text or "").splitlines() if not ln.lstrip().startswith("%")
    )


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _flatten_keys(d: dict, prefix: str = "") -> list:
    """Return dotted key names, e.g. 'a.b.c' for nested dicts."""
    keys = []
    for k, v in d.items():
        full = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            keys.extend(_flatten_keys(v, full))
        else:
            keys.append(full)
    return keys


def _value_to_str(v) -> str:
    """Render a yaml scalar/list/dict value as a single-line string for display."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float, str)):
        return str(v)
    return yaml.safe_dump(v, default_flow_style=True, allow_unicode=True).strip()


def flatten_config(text: str) -> list:
    """Parse yaml, return [{key, value}] with dotted keys and stringified values."""
    # drop `%YAML:` and other directives — some files use `%YAML:1.0` which
    # PyYAML rejects (colon after tag). The directives carry no value for the
    # flat-key view, so strip them before parsing.
    cleaned = "\n".join(
        ln for ln in (text or "").splitlines() if not ln.lstrip().startswith("%")
    )
    data = yaml.safe_load(cleaned) or {}
    if not isinstance(data, dict):
        return []
    out = []

    def walk(d, prefix=""):
        for k, v in d.items():
            full = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                walk(v, full)
            else:
                out.append({"key": full, "value": _value_to_str(v)})

    walk(data)
    return out
