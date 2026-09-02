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
@page { size: A4; margin: 12mm; }
body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       color: rgba(5,5,5,.88); margin: 0; padding: 16px 20px 36px;
       font-size: 13px; line-height: 1.4; background: #fff; }
h1 { font-size: 19px; margin: 2px 0 6px; }
.meta { color: rgba(5,5,5,.45); font-size: 12px; margin-bottom: 16px; }
table { border-collapse: collapse; width: 100%; margin: 2px 0 12px; }
th, td { border: 1px solid #e6e6e6; padding: 6px 10px; text-align: left;
         vertical-align: middle; }
th { background: #f7f7f7; font-weight: 600; color: rgba(5,5,5,.6);
     font-size: 12px; white-space: nowrap; }
td { font-size: 12.5px; }
td.no { width: 30px; text-align: right; color: #999; }
td.ds { word-break: break-word; min-width: 150px; }
td.exp { white-space: nowrap; }
td.num, td.mono { white-space: nowrap; font-family: "SFMono-Regular", Consolas,
                  Menlo, monospace; font-size: 12px; }
td.ctr { text-align: center; width: 96px; }
.st-done { color: #52c41a; font-weight: 600; white-space: nowrap; }
.st-failed { color: #ff4d4f; font-weight: 600; white-space: nowrap; }
.st-running, .st-pending { color: #faad14; white-space: nowrap; }
.drift { color: rgba(5,5,5,.5); font-size: 11px; margin-top: 2px; }
table.flat tr { page-break-inside: avoid; }
table.flat .err { color: #ff4d4f; font-size: 11px; max-width: 220px;
                  word-break: break-word; }
/* 缩略图固定框 + contain；轨迹图为竖长图，用略高的框兼顾可读性 */
img.thumb { object-fit: contain; width: 72px; height: 72px; display: block;
            border: 1px solid #e6e6e6; border-radius: 5px; background: #fff; }
img.thumb + img.thumb { margin-top: 4px; }
code { background: #f0f0f0; padding: 1px 5px; border-radius: 4px; font-size: 12px; }
/* 平铺布局：回测类型 → 编号 → 通栏表，无左侧缩进，全部展开 */
.rtype { margin: 12px 0 22px; }
.rtype-head { font-size: 15px; font-weight: 600; background: #e6f4ff;
              border: 1px solid #91caff; border-radius: 6px; padding: 7px 12px;
              margin-bottom: 10px; }
.run { margin: 0 0 4px; }
.run-head { font-size: 13px; font-weight: 600; color: rgba(5,5,5,.8);
            padding: 4px 2px 6px; }
.run-no { background: #f0f0f0; padding: 1px 7px; border-radius: 4px; }
.count { color: rgba(5,5,5,.45); font-weight: 400; font-size: 12px; }
"""


def _row_html(it: dict, idx: int) -> str:
    s = it["stats"]
    end = s.get("end")
    end_txt = (f"[{', '.join(str(x) for x in end)}]"
               if isinstance(end, (list, tuple)) else "-")
    drift = _drift(s)
    time_txt = (f"{s.get('vio_time_avg_ms', '-')} / {s.get('vio_time_max_ms', '-')}"
                if s.get("vio_time_avg_ms") is not None else "-")
    thumbs = []
    if it["preview"]:
        thumbs.append(f'<img class="thumb" src="{it["preview"]}">')
    if it["trajectory"]:
        thumbs.append(f'<img class="thumb" src="{it["trajectory"]}">')
    imgs = "\n".join(thumbs) if thumbs else "—"
    err = f'<div class="err">{_esc(it["error"])}</div>' if it["error"] else ""
    end_cell = _esc(end_txt)
    if drift != "-":
        end_cell += f'<div class="drift">漂移 {_esc(drift)} m</div>'
    return (f"<tr><td class=\"no\">{idx}</td>"
            f"<td class=\"ds\">{_esc(it['dataset'])}</td>"
            f"<td class=\"exp\"><b>{_esc(it['experiment'])}</b></td>"
            f"<td class=\"ctr\">{imgs}</td>"
            f"<td class=\"num\">{_esc(s.get('path_len_m', '-'))}</td>"
            f"<td class=\"mono\">{end_cell}</td>"
            f"<td class=\"num\">{time_txt}</td>"
            f"<td><span class=\"st-{_esc(it['status'])}\">{_esc(it['status'])}</span>{err}</td></tr>")


def _flat_table(items: list) -> str:
    rows = "\n".join(_row_html(it, i + 1) for i, it in enumerate(items))
    return f"""<table class="flat"><thead><tr>
      <th class="no">#</th><th>数据集</th><th>实验组</th>
      <th class="ctr">预览 / 轨迹</th><th class="num">路程(m)</th>
      <th title="终点坐标与零点的 2D 距离（假设 Z=0）">终点 · 漂移</th>
      <th class="num">耗时 avg/max(ms)</th><th>状态</th>
    </tr></thead><tbody>{rows}</tbody></table>"""


def build_html(items: list, title: str = "") -> str:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_done = sum(1 for it in items if it["status"] == "done")
    n_fail = sum(1 for it in items if it["status"] == "failed")
    parts = [f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{_esc(title or "VIO 回测报告")}</title><style>{_CSS}</style></head><body>
<h1>{_esc(title or "VIO 回测报告")}</h1>
<p class="meta">生成时间 {now} · 共 {len(items)} 条 · 成功 {n_done} · 失败 {n_fail}</p>"""]
    # 平铺布局：回测类型 → 编号 → 通栏表（实验组作为表格行），全部展开、无缩进
    tree: dict = {}
    for it in items:
        (tree.setdefault(it["type"], {})
             .setdefault(it["run_no"] or "(未编号)", [])
             .append(it))
    for t in sorted(tree, key=lambda x: _TYPE_ORDER.get(x, 9)):
        t_rs = tree[t]
        t_count = sum(len(v) for v in t_rs.values())
        label = f'{runno.TYPE_LABELS.get(t, t)}回测'
        parts.append(f'<section class="rtype">'
                     f'<div class="rtype-head">{_esc(label)} '
                     f'<span class="count">{t_count} 条</span></div>')
        for no in sorted(t_rs, key=_no_key):
            rs = t_rs[no]
            rs.sort(key=lambda it: (it["dataset"], it["experiment"]))
            commit = ""
            c = next((it for it in rs if it["commit_short"]), None)
            if c:
                commit = (f' <code>{_esc(c["commit_short"])}</code> '
                          f'<span class="count">{_esc(c["commit_msg"])}</span>')
            parts.append(f'<div class="run">'
                         f'<div class="run-head"><span class="run-no">{_esc(no)}</span> '
                         f'{commit}</div>')
            parts.append(_flat_table(rs))
            parts.append('</div>')
        parts.append('</section>')
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
