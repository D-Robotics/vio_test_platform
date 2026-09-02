"""stereo_auto_gen yaml config file list / read / validated write with backup."""
import os
import re
import shutil

import yaml

from . import datasets


def _config_dir(name: str) -> str:
    ds = datasets.get_dataset(name)
    return os.path.join(ds["path"], "stereo_auto_gen")


def list_configs(name: str) -> list:
    d = _config_dir(name)
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted(os.listdir(d)):
        p = os.path.join(d, f)
        if os.path.isfile(p) and (f.endswith(".yaml") or f.endswith(".yml")):
            out.append({"name": f, "size": os.path.getsize(p), "mtime": os.path.getmtime(p)})
    return out


def _safe_name(fname: str):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", fname):
        raise ValueError("invalid file name")
    return fname


def _strip_opencv_header(text: str) -> str:
    """OpenCV FileStorage yaml starts with '%YAML:1.0' which pyyaml cannot parse."""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].lstrip().startswith("%YAML"):
        return "".join(lines[1:])
    return text


def read_config(name: str, fname: str) -> dict:
    fname = _safe_name(fname)
    p = os.path.join(_config_dir(name), fname)
    if not os.path.isfile(p):
        raise FileNotFoundError(fname)
    with open(p, encoding="utf-8") as f:
        text = f.read()
    parsed = None
    error = ""
    try:
        parsed = yaml.safe_load(_strip_opencv_header(text))
    except yaml.YAMLError as e:
        error = str(e)
    return {"name": fname, "text": text, "parsed_ok": parsed is not None or not text.strip(), "parse_error": error}


_NUM_RE = re.compile(r"^-?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _opencv_quote(value: str, risky: bool) -> str:
    """OpenCV FileStorage swallows trailing comments into UNQUOTED string
    values (`key: FAST # opts` becomes value "FAST # opts"), which crashes
    enum parsing at VIO boot. Quote any non-numeric, non-bool value on a line
    that keeps a comment or whose value itself contains '#'."""
    v = value.strip()
    if not risky or not v or v[0] in "\"'":
        return value
    if _NUM_RE.match(v) or v.lower() in ("true", "false"):
        return value
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _split_trailing_comment(rest: str):
    """Split `value  # comment` into (value, comment|None), honoring quotes."""
    quote = None
    for idx, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (idx == 0 or rest[idx - 1] in " \t"):
            return rest[:idx].rstrip(), rest[idx:]
    return rest.rstrip(), None


def _find_key_line(lines: list, dotted: str):
    """Locate the line index of a (possibly nested) dotted key by walking
    indentation. Returns (index, indent) or (None, None)."""
    segs = dotted.split(".")
    lo = 0
    indent = 0  # expected indent of segments at the current depth
    for di, seg in enumerate(segs):
        found = None
        i = lo
        while i < len(lines):
            line = lines[i]
            if line.startswith("\t"):
                i += 1
                continue
            stripped = line.lstrip(" ")
            cur = len(line) - len(stripped)
            if not stripped or stripped.startswith("#"):
                i += 1
                continue
            if cur < indent:
                break  # left the parent block without a match
            if cur > indent:
                i += 1
                continue
            m = re.match(r"([^:#]+?):\s*(.*)$", stripped)
            if m and m.group(1).strip() == seg:
                found = i
                break
            i += 1
        if found is None:
            return None, None
        if di == len(segs) - 1:
            return found, indent
        # descend into this key's child block: first deeper line below it
        fline = lines[found]
        findent = len(fline) - len(fline.lstrip(" "))
        child_indent = None
        j = found + 1
        while j < len(lines):
            s2 = lines[j].strip()
            if not s2 or s2.startswith("#"):
                j += 1
                continue
            ci = len(lines[j]) - len(lines[j].lstrip(" "))
            if ci <= findent:
                break
            child_indent = ci
            break
        if child_indent is None:
            return None, None  # scalar value — cannot descend
        lo = found + 1
        indent = child_indent
    return None, None


def patch_config(name: str, fname: str, overrides: dict) -> dict:
    """Apply {dotted.key: value-string} onto the existing config, replacing
    only the touched lines so comments and formatting of the rest survive.
    Used for baseline editing from the UI (write-back to the dataset dir)."""
    fname = _safe_name(fname)
    if not overrides:
        raise ValueError("no overrides given")
    d = _config_dir(name)
    p = os.path.join(d, fname)
    if not os.path.isfile(p):
        raise FileNotFoundError(fname)
    with open(p, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)
    appended = []  # (dotted, value) for keys absent from the file
    for key, value in overrides.items():
        key = str(key).strip()
        if not key or key.startswith("."):
            raise ValueError(f"invalid key: {key!r}")
        value = str(value)
        idx, indent = _find_key_line(lines, key)
        if idx is None:
            if "." in key:
                raise ValueError(f"key {key} 不在当前配置里，请重新打开再改")
            appended.append((key, value))
            continue
        old = lines[idx]
        eol = "\n" if old.endswith("\n") else ""
        colon = old.index(":")
        _, comment = _split_trailing_comment(old[colon + 1:].rstrip("\n"))
        value = _opencv_quote(value, bool(comment) or "#" in value)
        new_line = " " * indent + key.split(".")[-1] + ": " + value
        if comment:
            new_line += " " + comment
        lines[idx] = new_line + eol
    for key, value in appended:
        sep = "" if (not lines or lines[-1].endswith("\n")) else "\n"
        lines.append(f"{sep}{key}: {value}\n")
    out = "".join(lines)
    try:
        yaml.safe_load(_strip_opencv_header(out))
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise ValueError(f"改后 YAML 解析失败{where}: {e}")
    shutil.copy2(p, p + ".bak")
    with open(p, "w", encoding="utf-8") as f:
        f.write(out)
    return {"name": fname, "changed": len(overrides), "backup": True}


def write_config(name: str, fname: str, text: str) -> dict:
    fname = _safe_name(fname)
    d = _config_dir(name)
    p = os.path.join(d, fname)
    # validate before touching disk (tolerate the OpenCV %YAML header line)
    try:
        yaml.safe_load(_strip_opencv_header(text))
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        raise ValueError(f"YAML parse error{where}: {e}")
    if os.path.isfile(p):
        shutil.copy2(p, p + ".bak")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return {"name": fname, "bytes": len(text), "backup": os.path.isfile(p + ".bak")}
