"""Test report generation from selected result dirs.

write_report() renders a standalone HTML document (metrics + trajectory /
preview images inlined as base64) and, for format="pdf", prints it to PDF
with headless Chromium via playwright. Reports land in results/_reports/ and
are served back through the /results static mount.
"""
import base64
import datetime
import html as _html
import os

from . import batch, runno

REPORTS_DIR = os.path.join(batch.RESULTS_DIR, "_reports")

_TYPE_ORDER = {"manual": 0, "daily": 1, "commit": 2}


def _b64_img(path: str) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return ""
    mime = "image/png" if path.endswith(".png") else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _esc(v) -> str:
    return _html.escape(str(v if v is not None else ""))


def _drift(stats: dict) -> str:
    """漂移 = 终点坐标与零点的 2D 距离（假设 Z=0）"""
    end = stats.get("end")
    if isinstance(end, (list, tuple)) and len(end) >= 2:
        try:
            return f"{(float(end[0]) ** 2 + float(end[1]) ** 2) ** 0.5:.2f}"
        except (TypeError, ValueError):
            pass
    return "-"


def _load_items(paths: list) -> list:
    items = []
    for p in paths:
        meta = batch.read_result_stats(p)  # raises FileNotFoundError / ValueError
        stats = meta.get("stats", {}) or {}
        rtype = meta.get("type") or runno.kind_of(meta.get("source", ""),
                                                  meta.get("commit", ""))
        ds = meta.get("dataset", "")
        parent = ds.split("/", 1)[0] if "/" in ds else "(根目录)"
        items.append({
            "dir": p,
            "run_no": meta.get("run_no", ""),
            "type": rtype,
            "dataset": ds,
            "parent": parent,
            "experiment": meta.get("experiment", "") or "基线",
            "status": meta.get("status", ""),
            "error": meta.get("error", ""),
            "board": meta.get("board", ""),
            "started_at": meta.get("started_at", ""),
            "finished_at": meta.get("finished_at", ""),
            "commit_short": meta.get("commit_short", "") or (meta.get("commit", "") or "")[:10],
            "commit_msg": meta.get("commit_msg", ""),
            "commit_author": meta.get("commit_author", ""),
            "commit_date": meta.get("commit_date", ""),
            "stats": stats,
            "trajectory": _b64_img(os.path.join(p, "trajectory.png")),
            "preview": _b64_img(os.path.join(p, "preview.jpg")),
        })
    items.sort(key=lambda it: (_TYPE_ORDER.get(it["type"], 9),
                               _no_key(it["run_no"]), it["parent"],
                               it["dataset"], it["experiment"]))
    return items


def _no_key(run_no: str) -> int:
    digits = "".join(c for c in str(run_no) if c.isdigit())
    return int(digits) if digits else 0


_CSS = """
body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       color: rgba(5,5,5,.88); margin: 24px; font-size: 13px; }
h1 { font-size: 20px; }
.meta { color: rgba(5,5,5,.45); font-size: 12px; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 14px; }
th, td { border: 1px solid #f0f0f0; padding: 5px 8px; text-align: left; }
th { background: #fafafa; }
.st-done { color: #52c41a; } .st-failed { color: #ff4d4f; }
.st-running, .st-pending { color: #faad14; }
table.flat td { vertical-align: middle; }
table.flat img { max-height: 60px; max-width: 90px; border: 1px solid #f0f0f0;
                 border-radius: 4px; }
table.flat .err { color: #ff4d4f; font-size: 11px; max-width: 240px;
                  word-break: break-all; }
table.flat tr { page-break-inside: avoid; }
code { background: #fafafa; padding: 1px 5px; border-radius: 4px; }
/* collapsible groups: type → run number → flat table */
details { margin: 4px 0; }
details > summary {
  cursor: pointer; user-select: none; list-style: none;
  padding: 6px 10px; border-radius: 6px; background: #fafafa;
  border: 1px solid #f0f0f0; font-weight: 600;
}
details > summary::before { content: "▾ "; color: rgba(5,5,5,.45); }
details:not([open]) > summary::before { content: "▸ "; }
details > summary::-webkit-details-marker { display: none; }
details > summary:hover { background: rgba(5,5,5,.04); }
details.lv0 > summary { background: #e6f4ff; border-color: #91caff; font-size: 15px; }
details.lv1 > summary { font-size: 14px; }
details > .group-body { margin: 6px 0 6px 20px; }
.count { color: rgba(5,5,5,.45); font-weight: 400; font-size: 12px; }
"""


def _row_html(it: dict, idx: int) -> str:
    s = it["stats"]
    end = s.get("end")
    end_txt = (f"[{', '.join(str(x) for x in end)}]"
               if isinstance(end, (list, tuple)) else "-")
    time_txt = (f"{s.get('vio_time_avg_ms', '-')} / {s.get('vio_time_max_ms', '-')}"
                if s.get("vio_time_avg_ms") is not None else "-")
    prev = f'<img src="{it["preview"]}">' if it["preview"] else "—"
    traj = f'<img src="{it["trajectory"]}">' if it["trajectory"] else "-"
    err = f'<div class="err">{_esc(it["error"])}</div>' if it["error"] else ""
    return (f"<tr><td>{idx}</td><td>{_esc(it['dataset'])}</td>"
            f"<td><b>{_esc(it['experiment'])}</b></td>"
            f"<td>{prev}</td><td>{_esc(s.get('path_len_m', '-'))}</td>"
            f"<td>{_esc(end_txt)}</td><td>{_drift(s)}</td><td>{traj}</td>"
            f"<td>{time_txt}</td>"
            f"<td class=\"st-{_esc(it['status'])}\">{_esc(it['status'])}{err}</td></tr>")


def _flat_table(items: list) -> str:
    rows = "\n".join(_row_html(it, i + 1) for i, it in enumerate(items))
    return f"""<table class="flat"><thead><tr>
      <th>#</th><th>数据集</th><th>实验组</th><th>预览</th><th>路程(m)</th>
      <th>终点</th><th title="终点坐标与零点的 2D 距离（假设 Z=0）">漂移(m)</th>
      <th>轨迹图</th><th>耗时 avg/max(ms)</th><th>状态</th>
    </tr></thead><tbody>{rows}</tbody></table>"""


def _group(label: str, count: int, inner: str, level: int, extra_html: str = "") -> str:
    """One collapsible group; `open` keeps HTML expanded by default and makes
    the PDF print fully expanded — collapse is an interactive HTML-only act."""
    return (f'<details class="lv{level}" open><summary>{_esc(label)}{extra_html} '
            f'<span class="count">{count} 条</span></summary>'
            f'<div class="group-body">{inner}</div></details>')


def build_html(items: list, title: str = "") -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_done = sum(1 for it in items if it["status"] == "done")
    n_fail = sum(1 for it in items if it["status"] == "failed")
    parts = [f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{_esc(title or "VIO 回测报告")}</title><style>{_CSS}</style></head><body>
<h1>{_esc(title or "VIO 回测报告")}</h1>
<p class="meta">生成时间 {now} · 共 {len(items)} 条 · 成功 {n_done} · 失败 {n_fail}</p>"""]
    # 回测类型 → 编号 → 平铺表格（实验组作为表格行，不再单独折叠）
    tree: dict = {}
    for it in items:
        (tree.setdefault(it["type"], {})
             .setdefault(it["run_no"] or "(未编号)", [])
             .append(it))
    for t in sorted(tree, key=lambda x: _TYPE_ORDER.get(x, 9)):
        t_inner, t_count = [], 0
        for no in sorted(tree[t], key=_no_key):
            rs = tree[t][no]
            rs.sort(key=lambda it: (it["dataset"], it["experiment"]))
            t_count += len(rs)
            commit = ""
            c = next((it for it in rs if it["commit_short"]), None)
            if c:
                commit = (f' <span class="count"><code>{_esc(c["commit_short"])}</code> '
                          f'{_esc(c["commit_msg"])}</span>')
            t_inner.append(_group(no, len(rs), _flat_table(rs), 1, extra_html=commit))
        label = f'{runno.TYPE_LABELS.get(t, t)}回测'
        parts.append(_group(label, t_count, "\n".join(t_inner), 0))
    parts.append("</body></html>")
    return "\n".join(parts)


def _html_to_pdf(html_text: str, out_pdf: str) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html_text, wait_until="load")
            page.pdf(path=out_pdf, format="A4", print_background=True,
                     margin={"top": "15mm", "bottom": "15mm",
                             "left": "12mm", "right": "12mm"})
        finally:
            browser.close()


def write_report(paths: list, fmt: str = "html", title: str = "") -> dict:
    """Render selected result dirs into a report file. Returns {"url", "path"}."""
    if not paths:
        raise ValueError("empty selection")
    if len(paths) > 200:
        raise ValueError("too many results selected (max 200)")
    fmt = (fmt or "html").lower()
    if fmt not in ("html", "pdf"):
        raise ValueError(f"unsupported format: {fmt}")
    items = _load_items(paths)
    html_text = build_html(items, title)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if fmt == "html":
        out = os.path.join(REPORTS_DIR, f"report_{stamp}.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(html_text)
    else:
        out = os.path.join(REPORTS_DIR, f"report_{stamp}.pdf")
        _html_to_pdf(html_text, out)
    rel = os.path.relpath(out, batch.RESULTS_DIR).replace(os.sep, "/")
    return {"url": f"/results/{rel}", "path": out, "count": len(items), "format": fmt}
