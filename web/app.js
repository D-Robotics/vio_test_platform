/* VIO test platform frontend (vanilla JS, no build step) */
"use strict";

const $ = (s, p = document) => p.querySelector(s);
const $$ = (s, p = document) => [...p.querySelectorAll(s)];

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* keep */ }
    throw new Error(msg);
  }
  return r.json();
}

function fmtBytes(n) {
  if (n > 1e9) return (n / 1e9).toFixed(2) + " GB";
  if (n > 1e6) return (n / 1e6).toFixed(1) + " MB";
  if (n > 1e3) return (n / 1e3).toFixed(0) + " KB";
  return n + " B";
}

function isValidIPv4(s) {
  const p = String(s).trim().split(".");
  if (p.length !== 4) return false;
  return p.every((o) => /^\d{1,3}$/.test(o) && Number(o) <= 255);
}

/* ------------------------------- tabs ------------------------------- */
$$(".tab-btn").forEach((b) =>
  b.addEventListener("click", () => {
    $$(".tab-btn").forEach((x) => x.classList.remove("active"));
    $$(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("#tab-" + b.dataset.tab).classList.add("active");
    if (b.dataset.tab === "boards") loadBoards();
    if (b.dataset.tab === "backtest") { loadBtSelectors(); pollBacktest(true); pollBatch(true); loadAutoCommits(); }
    if (b.dataset.tab === "stats") loadStats();
  })
);

/* ------------------------- sub-tabs (preview/config, manual/auto) ------------------------- */
$$(".sub-tab-btn").forEach((b) =>
  b.addEventListener("click", () => {
    // scope to the nearest enclosing tab/pane container so sub-tabs in
    // different tabs don't deactivate each other
    const scope = b.closest("section.tab, .pane.right, .modal-card") || document;
    scope.querySelectorAll(".sub-tab-btn").forEach((x) => x.classList.remove("active"));
    scope.querySelectorAll(".sub-pane").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    const pane = scope.querySelector("#sub-pane-" + b.dataset.sub);
    if (pane) pane.classList.add("active");
    if (b.dataset.sub === "auto") { loadAutoConfig(); loadAutoTasks(); loadAutoStatus(); }
    if (b.dataset.sub === "exp-config") loadCfgExperiments();
    if (b.dataset.sub === "manual") loadAutoCommits();
  })
);

/* =============================== 数据集 =============================== */
let dsCache = [];

function groupDatasets(list) {
  // group by parent directory (path before first '/'); top-level entries form
  // their own group keyed by "./".
  const groups = {};  // parent -> [ds, ...]
  for (const d of list) {
    const slash = d.name.indexOf("/");
    const parent = slash > 0 ? d.name.slice(0, slash) : "./";
    (groups[parent] = groups[parent] || []).push(d);
  }
  // root ("./") entries last so the listed folders come first
  return Object.entries(groups).sort(([a], [b]) => (a === "./" ? 1 : b === "./" ? -1 : a.localeCompare(b)));
}

async function loadDatasets(refresh = false) {
  dsCache = await api(`/api/datasets${refresh ? "?refresh=1" : ""}`);
  if (refresh) TOPIC_COUNT_CACHE.clear();
  const ul = $("#ds-list");
  ul.innerHTML = "";
  for (const [parent, items] of groupDatasets(dsCache)) {
    ul.appendChild(makeGroupLi(parent, items));
  }
  const cnt = $("#ds-count");
  if (cnt) cnt.textContent = String(dsCache.length);
  loadLazyThumbs();
  // prune any auto-pane picks that no longer exist
  const names = new Set(dsCache.map((d) => d.name));
  for (const n of Array.from(AUTO_DS_PICK)) if (!names.has(n)) AUTO_DS_PICK.delete(n);
  // refresh the auto-pane dataset list if it has been rendered
  if ($("#auto-ds-list") && $("#auto-ds-list").children.length) renderAutoDsList();
}

function makeGroupLi(parent, items) {
  const li = document.createElement("li");
  li.className = "ds-group";
  const isRoot = parent === "./";
  const parentVal = isRoot ? "" : parent;
  li.innerHTML = `
    <div class="ds-group-head">
      <span class="twist">▸</span>
      <span class="ds-group-name">${escapeHtml(parent)}</span>
      <span class="ds-meta">(${items.length})</span>
      <button class="ds-del-btn" title="删除该父目录" ${isRoot ? "disabled" : ""}>✕</button>
    </div>
    <ul class="ds-sublist collapsed"></ul>`;
  const sub = li.querySelector(".ds-sublist");
  for (const d of items) sub.appendChild(makeDsLi(d));
  li.querySelector(".ds-group-head").addEventListener("click", () => {
    sub.classList.toggle("collapsed");
    li.querySelector(".twist").textContent = sub.classList.contains("collapsed") ? "▸" : "▾";
    if (!sub.classList.contains("collapsed")) loadLazyThumbs(sub);
  });
  li.querySelector(".ds-del-btn").addEventListener("click", async (e) => {
    e.stopPropagation();
    if (isRoot) return;
    if (!confirm(`删除父目录 ${parent} 及其下所有数据集？此操作不可撤销。`)) return;
    try {
      const url = `/api/parents/${parent.split("/").map(encodeURIComponent).join("/")}`;
      const r = await fetch(url, { method: "DELETE" });
      if (!r.ok) {
        let msg = r.statusText;
        try { msg = (await r.json()).detail || msg; } catch (_) { /* keep */ }
        throw new Error(msg);
      }
      await loadDatasets(true);
    } catch (err) { popupAlert(`删除失败：${err.message}`); }
  });
  return li;
}

function makeDsLi(d) {
  const li = document.createElement("li");
  li.className = "ds-item";
  // only request a thumbnail when the bag actually has frames
  const thumb = d.has_bag
    ? `<img class="ds-thumb lazy" data-src="/api/datasets/${encodeURIComponent(d.name)}/thumbnail" alt="">`
    : `<span class="ds-thumb-placeholder">no img</span>`;
  const leaf = d.name.split("/").pop();
  li.innerHTML = `
    ${thumb}
    <div class="ds-text">
      <span class="ds-name" title="${escapeHtml(d.name)}">${escapeHtml(leaf)}</span>
      <span class="ds-meta">
        <span class="badge ${d.has_bag ? "on" : ""}">bag</span>
        <span class="badge ${d.has_config ? "on" : ""}">config</span>
      </span>
    </div>
    <button class="ds-del-btn" title="删除该数据集">✕</button>`;
  li.addEventListener("click", (e) => {
    if (e.target.classList.contains("ds-del-btn")) return;
    $$(".ds-list .ds-item").forEach((x) => x.classList.remove("active"));
    li.classList.add("active");
    showDataset(d.name);
  });
  li.querySelector(".ds-del-btn").addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!confirm(`删除数据集 ${d.name}？此操作不可撤销。`)) return;
    try {
      const url = `/api/datasets/${d.name.split("/").map(encodeURIComponent).join("/")}`;
      const r = await fetch(url, { method: "DELETE" });
      if (!r.ok) {
        let msg = r.statusText;
        try { msg = (await r.json()).detail || msg; } catch (_) { /* keep */ }
        throw new Error(msg);
      }
      await loadDatasets(true);
    } catch (err) { popupAlert(`删除失败：${err.message}`); }
  });
  return li;
}

function loadLazyThumbs(root = document) {
  // lazy-load thumbnails when they scroll into view (or right away for the list)
  const imgs = $$(".ds-thumb.lazy", root);
  const io = new IntersectionObserver((ents) => {
    for (const e of ents) {
      if (e.isIntersecting) {
        const img = e.target;
        if (img.dataset.src) { img.src = img.dataset.src; delete img.dataset.src; }
        io.unobserve(img);
      }
    }
  }, { rootMargin: "200px" });
  imgs.forEach((img) => io.observe(img));
}

function fillSelect(sel, items) {
  const cur = sel.value;
  sel.innerHTML = "";
  for (const it of items) {
    const o = document.createElement("option");
    o.value = it;
    o.textContent = it;
    sel.appendChild(o);
  }
  if (cur && items.includes(cur)) sel.value = cur;
}

$("#btn-refresh-ds").addEventListener("click", () => loadDatasets(true));

/* =============================== 添加数据 =============================== */
const UPLOAD = { files: [], valid: false };

$("#btn-add-ds").addEventListener("click", async () => {
  UPLOAD.files = [];
  UPLOAD.valid = false;
  $("#upload-name").value = "";
  $("#upload-folder").value = "";
  $("#upload-status").textContent = "";
  setUploadCheck("ros2bag_vio", false);
  setUploadCheck("stereo_auto_gen", false);
  // prefill parent datalist with existing parents
  try {
    const ps = await api("/api/parents");
    fillSelect($("#parent-list"), ps.map((p) => p === "./" ? "" : p));
    // the <datalist> needs <option> elements; fillSelect puts values as text too
    // but for empty parent we want to suggest leaving blank for "./"
  } catch (e) { /* ignore */ }
  $("#upload-modal").classList.remove("hidden");
});

$("#upload-close").addEventListener("click", () => $("#upload-modal").classList.add("hidden"));

$("#upload-folder").addEventListener("change", async () => {
  const input = $("#upload-folder");
  const files = [...input.files];
  UPLOAD.files = files;
  UPLOAD.valid = false;
  if (!files.length) { $("#upload-status").textContent = "未选择文件"; return; }
  // derive dataset name from first file's webkitRelativePath[0]
  const first = files[0].webkitRelativePath || files[0].name;
  const parts = first.split("/");
  const folderName = parts.length > 1 ? parts[0] : "uploaded_dataset";
  if (!$("#upload-name").value) $("#upload-name").value = folderName;
  // check folder contains both required subdirs
  const subdirs = new Set();
  for (const f of files) {
    const rp = f.webkitRelativePath || f.name;
    const seg = rp.split("/");
    if (seg.length >= 2) subdirs.add(seg[1]);
  }
  const hasBag = subdirs.has("ros2bag_vio");
  // accept either stereo_auto_gen (canonical) or stereo_auto_config (alt spelling)
  const hasCfg = subdirs.has("stereo_auto_gen") || subdirs.has("stereo_auto_config");
  setUploadCheck("ros2bag_vio", hasBag);
  setUploadCheck("stereo_auto_gen", hasCfg);
  UPLOAD.valid = hasBag && hasCfg;
  const totalMB = (files.reduce((s, f) => s + f.size, 0) / 1e6).toFixed(1);
  $("#upload-status").textContent = `${files.length} 文件 / ${totalMB} MB`;
  if (!UPLOAD.valid) {
    const missing = [];
    if (!hasBag) missing.push("ros2bag_vio");
    if (!hasCfg) missing.push("stereo_auto_gen");
    popupAlert(`数据集结构有误：缺少必需子目录 ${missing.join("、")}。请确认所选文件夹根目录下同时存在 ros2bag_vio/（bag 数据）和 stereo_auto_gen/（标定配置），然后重新选择。`);
  }
});

function setUploadCheck(name, ok) {
  const row = $(`#upload-check .check-row[data-check="${name}"]`);
  if (!row) return;
  row.textContent = (ok ? "✓ " : "⊘ ") + name + "/";
  row.classList.toggle("ok", ok);
}

$("#upload-submit").addEventListener("click", async () => {
  if (!UPLOAD.files.length) { popupAlert("请先选择数据集文件夹"); return; }
  if (!UPLOAD.valid) {
    popupAlert("数据集结构有误：缺少 ros2bag_vio 或 stereo_auto_gen，无法上传。请重新选择一个合法的数据集文件夹。");
    return;
  }
  const parent = $("#upload-parent").value.trim() || "./";
  const name = $("#upload-name").value.trim();
  if (!name) { popupAlert("请填写数据集名称"); return; }
  const btn = $("#upload-submit");
  btn.disabled = true; btn.textContent = "上传中…";
  $("#upload-status").textContent = "0 / " + UPLOAD.files.length;
  // build FormData with relative paths preserved in filename
  const fd = new FormData();
  fd.append("parent", parent);
  fd.append("name", name);
  let done = 0;
  for (const f of UPLOAD.files) {
    const rel = f.webkitRelativePath || f.name;
    // strip the top-level folder name from the path so files land directly
    // under ros2bag_vio/... and stereo_auto_gen/...
    const slash = rel.indexOf("/");
    const stripped = slash > 0 ? rel.slice(slash + 1) : rel;
    fd.append("files", f, stripped);
    done++;
    if (done % 50 === 0 || done === UPLOAD.files.length) {
      $("#upload-status").textContent = `${done} / ${UPLOAD.files.length}`;
      await new Promise((r) => setTimeout(r, 0)); // let UI paint
    }
  }
  try {
    const r = await fetch("/api/datasets/upload", { method: "POST", body: fd });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || r.statusText);
    const v = j.validate || {};
    $("#upload-status").textContent = `✓ 上传 ${j.files} 文件 → ${j.parent}/${j.name}`;
    if (v.ok) {
      await loadDatasets(true);
      setTimeout(() => $("#upload-modal").classList.add("hidden"), 1200);
    } else {
      popupAlert(`服务端二次校验失败：缺少 ${(v.missing || []).join("、")}。请检查上传的文件夹是否完整。`);
    }
  } catch (e) {
    popupAlert("上传失败：" + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "上传";
  }
});

function popupAlert(msg) {
  // lightweight in-page alert (avoids blocking native alert())
  let el = $("#popup-alert");
  if (!el) {
    el = document.createElement("div");
    el.id = "popup-alert";
    el.className = "popup-alert";
    el.innerHTML = `<div class="popup-card">
      <div class="popup-msg"></div>
      <div class="popup-foot"><button class="primary popup-ok">知道了</button></div>
    </div>`;
    document.body.appendChild(el);
    el.querySelector(".popup-ok").addEventListener("click", () => el.classList.add("hidden"));
  }
  el.querySelector(".popup-msg").textContent = msg;
  el.classList.remove("hidden");
}

// 轻量顶部提示：自动消失，不阻塞操作。用于构建/部署进行中与完成的提醒。
function showToast(msg, ms = 5000) {
  let el = $("#build-toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "build-toast";
    el.className = "build-toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  requestAnimationFrame(() => el.classList.add("show"));
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), ms);
}

const PLAYER = {
  name: null, prep: null, t: 0, t0: 0, t1: 0, playing: false, speed: 1,
  timer: null, lastTick: 0, visible: new Set(), fetching: false,
  root: "#sub-pane-preview",  // selector (or element) of the DOM root for player controls
};

function _playerRoot() {
  const r = PLAYER.root;
  return r instanceof Element ? r : $(r) || document;
}

async function showDataset(name, rootSel = "#sub-pane-preview") {
  PLAYER.root = rootSel;
  const box = (rootSel instanceof Element ? rootSel : $(rootSel));
  if (!box) return;
  box.innerHTML = '<p class="hint">加载播放器（构建时间索引，首次约 1-2 秒）…</p>';
  // load configs for the right pane's config editor in parallel (dataset-tab only)
  if (rootSel === "#sub-pane-preview") loadCfgFilesFor(name);
  try {
    const [info, prep] = await Promise.all([
      api(`/api/datasets/${encodeURIComponent(name)}/info`),
      api(`/api/datasets/${encodeURIComponent(name)}/player/prepare`),
    ]);
    PLAYER.name = name;
    PLAYER.prep = prep;
    PLAYER.t0 = prep.start_ns;
    PLAYER.t1 = prep.end_ns;
    PLAYER.t = prep.start_ns;
    PLAYER.playing = false;
    PLAYER.visible = new Set(prep.topics.filter((t) => t.times || t.full_times).map((t) => t.name));

    const durS = ((prep.end_ns - prep.start_ns) / 1e9).toFixed(1);
    const playable = prep.topics.filter((t) => t.times || t.full_times);
    // build topic -> message count map from bag info (per-topic count)
    const cntMap = new Map((info.topics || []).map((t) => [t.name, t.count || 0]));
    const fmtCnt = (n) => n ? n.toLocaleString() + " 条" : "0 条";
    let chips = playable.map((t) => `<span class="chip on" data-topic="${t.name}" title="${t.name} · ${t.type} · ${fmtCnt(cntMap.get(t.name) || 0)}">${t.name.split("/").pop()} (${t.type}, ${fmtCnt(cntMap.get(t.name) || 0)})</span>`).join("");
    let wins = playable
      .map((t) => {
        const isImg = t.type === "Image" || t.type === "CompressedImage";
        const cnt = cntMap.get(t.name) || 0;
        return `<div class="pwin${isImg ? " image-win" : ""}" data-win="${escapeHtml(t.name)}"><h4>${escapeHtml(t.name)}</h4><div class="pwin-meta">${escapeHtml(t.type)} · ${fmtCnt(cnt)}</div><div class="pbody" data-body="${escapeHtml(t.name)}"></div></div>`;
      })
      .join("");

    PLAYER.durMs = Math.max(1, Math.round((prep.end_ns - prep.start_ns) / 1e6));
    box.innerHTML = `
      <h2 style="margin:0 0 4px">${escapeHtml(name)}</h2>
      <div class="hint">存储 ${info.storage || "?"} · 消息 ${info.message_count} · 时长 ${durS}s · 大小 ${fmtBytes(info.size_bytes || 0)}${info.error ? " · ⚠ " + info.error : ""}</div>
      <div class="player">
        <div class="player-ctrl">
          <button id="pl-play">▶</button>
          <select id="pl-speed"><option value="0.25">0.25x</option><option value="0.5">0.5x</option><option value="1" selected>1x</option><option value="2">2x</option><option value="4">4x</option></select>
          <input type="range" id="pl-seek" min="0" max="${PLAYER.durMs}" step="10" value="0">
          <span class="t-lab" id="pl-time">0.0 / ${durS}s</span>
          <div class="player-chips">${chips}</div>
        </div>
        <div class="player-wins">${wins || '<p class="hint">本 bag 无可播放的 topic（image/imu/odom/tf/gps）</p>'}</div>
      </div>`;

    $("#pl-play", _playerRoot()).addEventListener("click", togglePlay);
    $("#pl-speed", _playerRoot()).addEventListener("change", (e) => (PLAYER.speed = parseFloat(e.target.value)));
    const seek = $("#pl-seek", _playerRoot());
    // pointer-held state: while the user holds the thumb, playback-driven
    // seek.value writes must stop or they fight the drag
    let holding = false;
    seek.addEventListener("pointerdown", () => { holding = true; PLAYER.holding = true; });
    window.addEventListener("pointerup", () => { holding = false; PLAYER.holding = false; });
    seek.addEventListener("input", () => {
      PLAYER.t = PLAYER.t0 + parseInt(seek.value, 10) * 1e6; // slider is ms offset
      const lab = $("#pl-time", _playerRoot());
      if (lab) {
        const cur = ((PLAYER.t - PLAYER.t0) / 1e9).toFixed(1);
        const tot = ((PLAYER.t1 - PLAYER.t0) / 1e9).toFixed(1);
        lab.textContent = `${cur} / ${tot}s`;
      }
      // coalesce rapid drag events to one rAF so we don't pile up decode work
      if (PLAYER._dragRaf) return;
      PLAYER._dragRaf = requestAnimationFrame(() => {
        PLAYER._dragRaf = null;
        updateImagesOnly();
        updateSeriesOnly();
      });
    });
    $$(".chip", box).forEach((c) =>
      c.addEventListener("click", () => {
        const t = c.dataset.topic;
        const root = _playerRoot();
        if (PLAYER.visible.has(t)) { PLAYER.visible.delete(t); c.classList.remove("on"); $(`[data-win="${t}"]`, root).classList.add("hidden-win"); }
        else { PLAYER.visible.add(t); c.classList.add("on"); $(`[data-win="${t}"]`, root).classList.remove("hidden-win"); updateTick(true); }
      })
    );
    // first paint immediately; preload series in parallel (not blocking)
    updateTick(true);
    // kick off background JPEG extraction for each image topic so dragging
    // the slider loads frames from disk cache instead of decoding mcap live
    const nameEnc0 = encodeURIComponent(name);
    for (const t of playable) {
      if (t.type !== "Image" && t.type !== "CompressedImage") continue;
      fetch(`/api/datasets/${nameEnc0}/frames/prepare?topic=${encodeURIComponent(t.name)}`, { method: "POST" }).catch(() => {});
    }
    await Promise.all(playable.map(async (t) => {
      if (t.type === "Image" || t.type === "CompressedImage") return;
      try {
        t.series = await api(`/api/datasets/${encodeURIComponent(name)}/series/${encodeURIComponent(t.name)}?max_points=2000`);
      } catch (e) { t.series = { points: [] }; }
      if (PLAYER.name === name) updateTick(true); // repaint once data arrives
    }));
  } catch (e) {
    box.innerHTML = `<p class="hint" style="color:var(--bad)">${escapeHtml(e.message)}</p>`;
  }
}

function togglePlay() {
  PLAYER.playing = !PLAYER.playing;
  $("#pl-play", _playerRoot()).textContent = PLAYER.playing ? "⏸" : "▶";
  if (PLAYER.playing) {
    // if at the end, restart from the beginning so the user's click actually
    // plays something instead of immediately re-triggering the end-of-stream
    // toggle (which made the button appear to "twitch" ⏸↔▶)
    if (PLAYER.t1 > PLAYER.t0 && PLAYER.t >= PLAYER.t1) {
      PLAYER.t = PLAYER.t0;
    }
    PLAYER.lastTick = performance.now();
    if (PLAYER.timer) clearInterval(PLAYER.timer);
    PLAYER.timer = setInterval(playTick, 100);
  } else {
    if (PLAYER.timer) clearInterval(PLAYER.timer);
    PLAYER.timer = null;
  }
}

function playTick() {
  if (!PLAYER.playing) return; // defensive: a stale timer fired after pause
  if (PLAYER.holding) { PLAYER.lastTick = performance.now(); return; }
  const now = performance.now();
  const dt = ((now - PLAYER.lastTick) / 1000) * PLAYER.speed * 1e9;
  PLAYER.lastTick = now;
  PLAYER.t += dt;
  let ended = false;
  if (PLAYER.t >= PLAYER.t1) {
    PLAYER.t = PLAYER.t1;
    ended = true;
  }
  const seek = $("#pl-seek", _playerRoot());
  if (seek) seek.value = Math.round((PLAYER.t - PLAYER.t0) / 1e6);
  updateTick();
  if (ended) togglePlay();
}

function nearestFrameIndex(times, tNs) {
  // binary search: last index with times[i] <= tNs
  let lo = 0, hi = times.length - 1, ans = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= tNs) { ans = mid; lo = mid + 1; }
    else hi = mid - 1;
  }
  return ans;
}

function _loadFrame(body, nameEnc, topic, idx) {
  body.dataset.curIdx = String(idx);
  body.dataset.loading = "1";
  const url = `/api/datasets/${nameEnc}/frames/${encodeURIComponent(topic)}/${idx}.jpg`;
  const img = body.querySelector("img.frame");
  if (!img) { delete body.dataset.loading; return; }
  // pre-decode off-DOM, then swap — avoids flashing half-loaded frames and
  // bounds main-thread decode work to one frame at a time during drag.
  const pre = new Image();
  pre.onload = () => {
    img.src = url;
    delete body.dataset.loading;
    // if a newer index piled up while we were busy, kick it now
    if (body.dataset.pendingIdx) {
      const next = parseInt(body.dataset.pendingIdx, 10);
      delete body.dataset.pendingIdx;
      _loadFrame(body, nameEnc, topic, next);
    }
  };
  pre.onerror = () => { delete body.dataset.loading; };
  pre.src = url;
}

function updateImagesOnly() {
  if (!PLAYER.name || !PLAYER.prep) return;
  const root = _playerRoot();
  const nameEnc = encodeURIComponent(PLAYER.name);
  for (const tp of PLAYER.prep.topics) {
    if (tp.type !== "Image" && tp.type !== "CompressedImage") continue;
    if (!PLAYER.visible.has(tp.name)) continue;
    const body = $(`[data-body="${tp.name}"]`, root);
    if (!body) continue;
    const times = tp.full_times;
    if (!times || !times.length) continue;
    const idx = nearestFrameIndex(times, PLAYER.t);
    // label always updates synchronously so the user sees the frame ID move
    // even while the actual image is mid-decode.
    let lab = body.querySelector(".hint");
    if (!lab) {
      body.innerHTML = `<img class="frame"><div class="hint" style="margin-top:4px"></div>`;
      lab = body.querySelector(".hint");
    }
    lab.textContent = `帧 ${idx + 1}/${times.length}`;
    // skip if already showing this index, or queue if a decode is in flight
    if (body.dataset.curIdx === String(idx)) continue;
    if (body.dataset.loading === "1") {
      body.dataset.pendingIdx = String(idx); // latest wins; older queued idx is dropped
      continue;
    }
    _loadFrame(body, nameEnc, tp.name, idx);
  }
}

function updateSeriesOnly() {
  if (!PLAYER.name || !PLAYER.prep) return;
  const root = _playerRoot();
  const winS = 8;
  const tSec = PLAYER.t / 1e9;
  for (const tp of PLAYER.prep.topics) {
    if (tp.type === "Image" || tp.type === "CompressedImage") continue;
    if (!tp.series || !tp.series.points) continue;
    if (!PLAYER.visible.has(tp.name)) continue;
    const body = $(`[data-body="${tp.name}"]`, root);
    if (!body) continue;
    const pts = tp.series.points.filter((p) => p.t > tSec - winS && p.t <= tSec + winS);
    renderSeries({ type: tp.type, count: tp.series.count, points: pts, cursorT: tSec }, body, true);
  }
}

async function updateTick() {
  if (!PLAYER.name || !PLAYER.prep) return;
  const lab = $("#pl-time", _playerRoot());
  const cur = ((PLAYER.t - PLAYER.t0) / 1e9).toFixed(1);
  const tot = ((PLAYER.t1 - PLAYER.t0) / 1e9).toFixed(1);
  if (lab) lab.textContent = `${cur} / ${tot}s`;
  updateImagesOnly();
  updateSeriesOnly();
}

// exposed for inline onclick
window.previewTopic = async function (nameEnc, topicEnc, ty) {
  const name = decodeURIComponent(nameEnc), topic = decodeURIComponent(topicEnc);
  const box = $("#ds-preview");
  box.innerHTML = '<p class="hint">读取数据中…</p>';
  try {
    if (ty === "CompressedImage" || ty === "Image") {
      const info = await api(`/api/datasets/${encodeURIComponent(name)}/info`);
      const t = (info.topics || []).find((x) => x.name === topic);
      const count = t ? t.count : 0;
      const n = Math.min(8, count || 8);
      const idxs = [];
      for (let i = 0; i < n; i++) idxs.push(Math.round((i * (count - 1)) / Math.max(1, n - 1)));
      let html = `<div class="preview-sec"><h3>图像预览 · ${topic}（${count} 帧）</h3><div class="frames">`;
      for (const i of idxs) {
        const url = `/api/datasets/${encodeURIComponent(name)}/image/${encodeURIComponent(topic)}?index=${i}`;
        html += `<img src="${url}" title="frame ${i}" onclick="showBig('${url}')">`;
      }
      html += `</div><div style="margin-top:10px"><img id="img-big" style="display:none"></div></div>`;
      box.innerHTML = html;
    } else {
      const s = await api(`/api/datasets/${encodeURIComponent(name)}/series/${encodeURIComponent(topic)}`);
      box.innerHTML = `<div class="preview-sec"><h3>${escapeHtml(ty)} · ${escapeHtml(topic)}（共 ${s.count} 条，抽样 ${s.points.length}）</h3><div id="series-box" class="chart"></div></div>`;
      renderSeries(s, $("#series-box"));
    }
  } catch (e) {
    box.innerHTML = `<p class="hint" style="color:var(--bad)">${escapeHtml(e.message)}</p>`;
  }
};

window.showBig = function (url) {
  const img = $("#img-big");
  img.src = url;
  img.style.display = "block";
};

function renderSeries(s, box, compact = false) {
  const pts = s.points;
  if (!pts.length) {
    // keep any existing chart, just show a hint line
    if (!box.querySelector(".nopts")) {
      const h = document.createElement("p");
      h.className = "nopts hint";
      h.textContent = "无数据点";
      box.appendChild(h);
    }
    return;
  }
  const nopts = box.querySelector(".nopts");
  if (nopts) nopts.remove();

  if (s.type === "Imu") {
    drawImu(box, pts, compact, s.cursorT);
  } else if (s.type === "Odometry" || s.type === "TFMessage") {
    if (box._uplot) { box._uplot.destroy(); box._uplot = null; }
    box.innerHTML = "";
    const c = document.createElement("canvas");
    c.className = "path";
    box.appendChild(c);
    drawPath(c, pts.map((p) => [p.x || 0, p.y || 0]), `${s.type} 轨迹 (x-y)`);
    const pre = document.createElement("pre");
    pre.style.cssText = "font:12px Consolas,monospace;color:var(--muted);max-height:160px;overflow:auto";
    pre.textContent = pts.slice(0, 20).map((p) => `t=${p.t.toFixed(3)} x=${(p.x ?? 0).toFixed(3)} y=${(p.y ?? 0).toFixed(3)} z=${(p.z ?? 0).toFixed(3)}${p.child ? "  " + p.frame + "→" + p.child : ""}`).join("\n");
    box.appendChild(pre);
  } else if (s.type === "NavSatFix") {
    if (box._uplot) { box._uplot.destroy(); box._uplot = null; }
    box.innerHTML = "";
    const c = document.createElement("canvas");
    c.className = "path";
    box.appendChild(c);
    drawPath(c, pts.map((p) => [p.lon, p.lat]), "GPS 经纬度散点", true);
  } else {
    if (box._uplot) { box._uplot.destroy(); box._uplot = null; }
    box.innerHTML = "";
    const pre = document.createElement("pre");
    pre.style.cssText = "font:12px Consolas,monospace;max-height:300px;overflow:auto";
    pre.textContent = JSON.stringify(pts.slice(0, 12), null, 1);
    box.appendChild(pre);
  }
}

function drawImu(box, pts, compact, cursorT) {
  // Hand-rolled canvas IMU chart: 6 curves, dual y-axis (acc left / gyro right).
  // Replaces uPlot which had mode:2 paired-data rendering issues locally.
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(360, box.clientWidth || 620);
  const h = compact ? 280 : 320;
  let c = box.querySelector("canvas.imu");
  if (!c) {
    box.innerHTML = "";
    c = document.createElement("canvas");
    c.className = "imu";
    box.appendChild(c);
  }
  c.style.width = w + "px";
  c.style.height = h + "px";
  c.width = w * dpr;
  c.height = h * dpr;
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.fillStyle = "#ffffff";
  g.fillRect(0, 0, w, h);

  const ACC_KEYS = ["ax", "ay", "az"];
  const GYRO_KEYS = ["gx", "gy", "gz"];
  const ACC_COLOR = { ax: "#e07b00", ay: "#0d9e80", az: "#a24de0" };
  const GYRO_COLOR = { gx: "#d98e04", gy: "#2e9fd6", gz: "#d6599e" };

  const ts = pts.map((p) => p.t);
  const t0 = ts.length ? ts[0] : 0;
  const t1 = ts.length ? ts[ts.length - 1] : 1;
  const tspan = Math.max(1e-6, t1 - t0);

  // y-ranges (auto-scale with small padding)
  let aMin = Infinity, aMax = -Infinity, gMin = Infinity, gMax = -Infinity;
  for (const p of pts) {
    for (const k of ACC_KEYS) { const v = p[k]; if (v < aMin) aMin = v; if (v > aMax) aMax = v; }
    for (const k of GYRO_KEYS) { const v = p[k]; if (v < gMin) gMin = v; if (v > gMax) gMax = v; }
  }
  if (!isFinite(aMin)) { aMin = -1; aMax = 1; }
  if (!isFinite(gMin)) { gMin = -1; gMax = 1; }
  const padA = (aMax - aMin) * 0.1 || 0.5;
  const padG = (gMax - gMin) * 0.1 || 0.05;
  aMin -= padA; aMax += padA;
  gMin -= padG; gMax += padG;

  const padL = 44, padR = 44, padT = 18, padB = 24;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const sx = (t) => padL + ((t - t0) / tspan) * plotW;
  const syA = (v) => padT + (1 - (v - aMin) / (aMax - aMin)) * plotH;
  const syG = (v) => padT + (1 - (v - gMin) / (gMax - gMin)) * plotH;

  // grid + axes
  g.strokeStyle = "#d6dae0";
  g.lineWidth = 1;
  g.fillStyle = "#6b7280";
  g.font = "10px Consolas, monospace";
  g.textAlign = "right";
  g.textBaseline = "middle";
  const gridN = 4;
  for (let i = 0; i <= gridN; i++) {
    const y = padT + (i / gridN) * plotH;
    g.strokeStyle = "#e7eaef";
    g.beginPath();
    g.moveTo(padL, y);
    g.lineTo(w - padR, y);
    g.stroke();
    // left axis (acc)
    const aVal = aMax - (i / gridN) * (aMax - aMin);
    g.fillStyle = ACC_COLOR.az;
    g.fillText(aVal.toFixed(1), padL - 4, y);
    // right axis (gyro)
    const gVal = gMax - (i / gridN) * (gMax - gMin);
    g.fillStyle = GYRO_COLOR.gx;
    g.textAlign = "left";
    g.fillText(gVal.toFixed(2), w - padR + 4, y);
    g.textAlign = "right";
  }
  // x-axis ticks
  g.textAlign = "center";
  g.textBaseline = "top";
  g.fillStyle = "#6b7280";
  const xN = 4;
  for (let i = 0; i <= xN; i++) {
    const tt = t0 + (i / xN) * tspan;
    g.fillText(tt.toFixed(1) + "s", sx(tt), h - padB + 4);
  }

  // draw a curve
  const drawCurve = (key, color, sy) => {
    g.strokeStyle = color;
    g.lineWidth = 1.3;
    g.beginPath();
    for (let i = 0; i < pts.length; i++) {
      const x = sx(ts[i]);
      const y = sy(pts[i][key]);
      if (i === 0) g.moveTo(x, y);
      else g.lineTo(x, y);
    }
    g.stroke();
  };
  for (const k of ACC_KEYS) drawCurve(k, ACC_COLOR[k], syA);
  // dashed for gyro
  g.setLineDash([4, 3]);
  for (const k of GYRO_KEYS) drawCurve(k, GYRO_COLOR[k], syG);
  g.setLineDash([]);

  // legend
  g.textAlign = "left";
  g.textBaseline = "middle";
  g.font = "11px Consolas, monospace";
  let lx = padL + 4;
  const ly = padT + 8;
  for (const k of ACC_KEYS) {
    g.strokeStyle = ACC_COLOR[k];
    g.beginPath();
    g.moveTo(lx, ly);
    g.lineTo(lx + 14, ly);
    g.stroke();
    g.fillStyle = "#4b5563";
    g.fillText(k, lx + 18, ly);
    lx += 50;
  }
  for (const k of GYRO_KEYS) {
    g.strokeStyle = GYRO_COLOR[k];
    g.setLineDash([4, 3]);
    g.beginPath();
    g.moveTo(lx, ly);
    g.lineTo(lx + 14, ly);
    g.stroke();
    g.setLineDash([]);
    g.fillStyle = "#4b5563";
    g.fillText(k, lx + 18, ly);
    lx += 50;
  }

  // playhead line at cursorT (seconds): vertical bar so user can see sync with image frame
  if (cursorT != null && cursorT >= t0 && cursorT <= t1) {
    const cx = sx(cursorT);
    g.strokeStyle = "#d6483f";
    g.lineWidth = 1.2;
    g.setLineDash([2, 3]);
    g.beginPath();
    g.moveTo(cx, padT);
    g.lineTo(cx, padT + plotH);
    g.stroke();
    g.setLineDash([]);
    g.fillStyle = "#d6483f";
    g.textAlign = "center";
    g.textBaseline = "top";
    g.font = "10px Consolas, monospace";
    g.fillText(cursorT.toFixed(2) + "s", cx, padT + plotH - 14);
  }
}

function drawPath(c, xy, title, scatter = false) {
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth || 800, h = 320;
  c.width = w * dpr; c.height = h * dpr;
  const g = c.getContext("2d");
  g.scale(dpr, dpr);
  g.fillStyle = "#ffffff"; g.fillRect(0, 0, w, h);
  const xs = xy.map((p) => p[0]), ys = xy.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = 40;
  const sx = (v) => pad + ((v - x0) / Math.max(1e-9, x1 - x0)) * (w - 2 * pad);
  const sy = (v) => h - pad - ((v - y0) / Math.max(1e-9, y1 - y0)) * (h - 2 * pad);
  g.strokeStyle = "#9aa1ab"; g.lineWidth = 1;
  g.beginPath(); g.moveTo(pad, 0); g.lineTo(pad, h); g.moveTo(0, h - pad); g.lineTo(w, h - pad); g.stroke();
  g.fillStyle = "#6b7280"; g.font = "11px Consolas";
  g.fillText(title, pad + 6, 16);
  g.fillText(x0.toFixed(4), pad, h - pad + 14);
  g.fillText(x1.toFixed(4), w - 70, h - pad + 14);
  g.fillText(y1.toFixed(4), 4, pad - 4);
  g.fillText(y0.toFixed(4), 4, h - pad);
  if (scatter) {
    g.fillStyle = "#e07b00";
    for (const p of xy) { g.beginPath(); g.arc(sx(p[0]), sy(p[1]), 2, 0, 7); g.fill(); }
  } else {
    g.strokeStyle = "#e07b00"; g.lineWidth = 1.5;
    g.beginPath();
    xy.forEach((p, i) => (i ? g.lineTo(sx(p[0]), sy(p[1])) : g.moveTo(sx(p[0]), sy(p[1]))));
    g.stroke();
    if (xy.length) { g.fillStyle = "#0d9e80"; g.beginPath(); g.arc(sx(xs[0]), sy(ys[0]), 4, 0, 7); g.fill(); }
  }
}

/* =============================== 配置编辑 =============================== */
// The config editor lives in the right pane; the active dataset is taken
// from PLAYER.name (set by showDataset) rather than a separate dropdown.
$("#cfg-file").addEventListener("change", loadCfgFile);
$("#cfg-load").addEventListener("click", loadCfgFile);
$("#cfg-validate").addEventListener("click", () => saveCfg(false));
$("#cfg-save").addEventListener("click", () => saveCfg(true));

async function loadCfgFilesFor(name) {
  if (!name) return;
  try {
    const files = await api(`/api/datasets/${encodeURIComponent(name)}/config`);
    fillSelect($("#cfg-file"), files.map((f) => f.name));
    if (files.length) loadCfgFile();
  } catch (e) { setCfgStatus(e.message, true); }
}

async function loadCfgFile() {
  const name = PLAYER.name, fname = $("#cfg-file").value;
  if (!name || !fname) return;
  try {
    const r = await api(`/api/datasets/${encodeURIComponent(name)}/config/${encodeURIComponent(fname)}`);
    $("#cfg-text").value = r.text;
    setCfgStatus(r.parsed_ok ? `已读取 ${fname}（${r.text.length} 字节）` : `读取成功但当前内容不是合法 YAML：${r.parse_error}`, !r.parsed_ok);
  } catch (e) { setCfgStatus(e.message, true); }
}

async function saveCfg(write) {
  const name = PLAYER.name, fname = $("#cfg-file").value;
  if (!name || !fname) return;
  const text = $("#cfg-text").value;
  try {
    const r = await fetch(`/api/datasets/${encodeURIComponent(name)}/config/${encodeURIComponent(fname)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || r.statusText);
    setCfgStatus(write ? `已保存（${j.bytes} 字节，备份 ${fname}.bak）` : "YAML 校验通过");
  } catch (e) { setCfgStatus((write ? "保存失败：" : "校验失败：") + e.message, true); }
}

function setCfgStatus(msg, bad = false) {
  const s = $("#cfg-status");
  s.textContent = msg;
  s.style.color = bad ? "var(--bad)" : "var(--ok)";
}

/* =============================== 板子管理 =============================== */
async function loadBoards() {
  try {
    const bs = await api("/api/boards");
    const tb = $("#bd-table tbody");
    tb.innerHTML = "";
    for (const b of bs) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><input type="checkbox" class="bd-sel" data-ip="${escapeHtml(b.ip)}"></td>
        <td>${escapeHtml(b.ip)}</td><td>${escapeHtml(b.note || "")}</td>
        <td><span class="ping" id="bd-ping-${b.ip.replace(/\./g, "_")}">测量中…</span></td>
        <td><span class="status" id="bd-st-${b.ip.replace(/\./g, "_")}">未测试</span></td>
        <td class="btn-row"><button class="small" data-test="${escapeHtml(b.ip)}">测试连接</button>
        <button class="small danger" data-del="${escapeHtml(b.ip)}">删除</button></td>`;
      tb.appendChild(tr);
    }
    $$("[data-test]", tb).forEach((btn) => btn.addEventListener("click", async () => {
      const st = document.getElementById("bd-st-" + btn.dataset.test.replace(/\./g, "_"));
      st.textContent = "测试中…";
      const r = await api(`/api/boards/${btn.dataset.test}/test`, { method: "POST" });
      st.textContent = r.ok ? "✓ " + r.detail.split("\n")[0] : "✗ " + r.detail;
      st.style.color = r.ok ? "var(--ok)" : "var(--bad)";
    }));
    $$("[data-del]", tb).forEach((btn) => btn.addEventListener("click", async () => {
      await api(`/api/boards/${btn.dataset.del}`, { method: "DELETE" });
      loadBoards();
    }));
    $("#bd-sel-all").checked = false;
    updateBdSelCount();
    fillSelect($("#bt-board"), bs.map((b) => b.ip));
    refreshBoardPing(bs);
    if (window._bdPingTimer) clearInterval(window._bdPingTimer);
    window._bdPingTimer = setInterval(() => {
      const t = document.querySelector(".tab-btn.active");
      if (t && t.dataset.tab === "boards" && !document.hidden && !window._bdPingBusy) {
        window._bdPingBusy = true;
        Promise.resolve(refreshBoardPing()).catch(() => {}).finally(() => { window._bdPingBusy = false; });
      }
    }, 30000);
  } catch (e) { console.error(e); }
}

function colorForPing(ms) {
  if (ms === null || ms === undefined) return { color: "var(--bad)", label: "不通" };
  if (ms < 10) return { color: "var(--ok)", label: `${ms} ms` };
  if (ms < 50) return { color: "#f0ad4e", label: `${ms} ms` };
  return { color: "var(--bad)", label: `${ms} ms` };
}

async function refreshBoardPing(boards) {
  const tb = $("#bd-table tbody");
  if (!tb) return;
  let res;
  try { res = await api("/api/boards/ping"); }
  catch (e) {
    for (const b of boards || await api("/api/boards")) {
      const span = document.getElementById("bd-ping-" + b.ip.replace(/\./g, "_"));
      if (span) { span.textContent = "ping 失败"; span.style.color = "var(--bad)"; }
    }
    return;
  }
  const latency = res.latency || {};
  for (const b of boards || await api("/api/boards")) {
    const span = document.getElementById("bd-ping-" + b.ip.replace(/\./g, "_"));
    if (!span) continue;
    const ms = latency[b.ip];
    const { color, label } = colorForPing(ms);
    span.textContent = label;
    span.style.color = color;
  }
}

function updateBdSelCount() {
  const n = $("#bd-table tbody").querySelectorAll("input.bd-sel:checked").length;
  const btn = $("#bd-del-sel");
  if (btn) {
    btn.disabled = n === 0;
    btn.textContent = `批量删除${n > 0 ? ` (${n})` : ""}`;
  }
  const all = $("#bd-sel-all");
  const total = $("#bd-table tbody").querySelectorAll("input.bd-sel").length;
  if (all) all.checked = total > 0 && n === total;
}

$("#bd-sel-all")?.addEventListener("change", (e) => {
  $("#bd-table tbody").querySelectorAll("input.bd-sel").forEach((cb) => { cb.checked = e.target.checked; });
  updateBdSelCount();
});

$("#bd-table tbody")?.addEventListener("change", (e) => {
  if (e.target.classList.contains("bd-sel")) updateBdSelCount();
});

$("#bd-del-sel")?.addEventListener("click", async () => {
  const ips = Array.from($("#bd-table tbody").querySelectorAll("input.bd-sel:checked")).map((cb) => cb.dataset.ip);
  if (!ips.length) return;
  if (!confirm(`确认删除 ${ips.length} 个板子？\n${ips.join(", ")}`)) return;
  for (const ip of ips) {
    try { await api(`/api/boards/${ip}`, { method: "DELETE" }); }
    catch (e) { popupAlert(`删除 ${ip} 失败: ${e.message}`); break; }
  }
  loadBoards();
});

$("#bd-add").addEventListener("click", async () => {
  const ip = $("#bd-ip").value.trim();
  if (!ip) return;
  if (!isValidIPv4(ip)) return popupAlert(`不是合法的 IPv4 地址：${ip}（例：192.168.1.10）`);
  const btn = $("#bd-add");
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-sm"></span>正在添加中…';
  showToast(`正在添加板子 ${ip}：校验 SSH 登录，请稍候…`, 6000);
  try {
    await api("/api/boards", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ip, note: $("#bd-note").value.trim() }) });
    $("#bd-ip").value = ""; $("#bd-note").value = "";
    loadBoards();
    showToast(`板子 ${ip} 添加成功 ✓`, 4000);
  } catch (e) {
    popupAlert(`添加板子 ${ip} 失败：${e.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
});

/* =============================== 回测 =============================== */
let btTimer = null;

async function loadBtSelectors() {
  if (!dsCache.length) await loadDatasets();
  else renderBtDsList();
  const bs = await api("/api/boards");
  fillSelect($("#bt-board"), bs.map((b) => b.ip));
  loadExperiments();
}

// picked experiment set: empty string "" = baseline (no override);
// other strings = experiment fragment names. Multi-select cartesian product.
const EXP_PICK = new Set([""]);  // baseline checked by default

async function loadExperiments() {
  let list = [];
  try { list = await api("/api/experiments"); } catch (e) { /* ignore */ }
  EXP_LIST_CACHE = list;
  // total includes baseline (+1) since baseline is a selectable run mode
  $("#bt-exp-total").textContent = String(list.length + 1);
  renderExpPicker();
  // also refresh the auto-pane picker so both surfaces see the same experiments
  if ($("#auto-exp-rows")) {
    AUTO_EXP_LIST_CACHE = list;
    for (const n of Array.from(AUTO_EXP_PICK)) {
      if (n === "") continue;
      if (!list.some((e) => e.name === n)) AUTO_EXP_PICK.delete(n);
    }
    $("#auto-exp-total").textContent = String(list.length + 1);
    renderAutoExpPicker();
  }
}

let EXP_LIST_CACHE = [];

// 点击行空白处 = 切换勾选；名字（打开编辑）、删除钮、直读勾选等控件除外
function wireExpRowToggle(row) {
  row.addEventListener("click", (e) => {
    if (e.target.closest("input, .name.clickable, .exp-del-btn, .exp-offline-check")) return;
    const cb = row.querySelector("input");
    cb.checked = !cb.checked;
    cb.dispatchEvent(new Event("change"));
  });
}

// 行内「直读」勾选（实验行）：只更新 sidecar meta，即时生效
function wireExpOfflineToggle(row, name) {
  const cb = row.querySelector(".exp-offline-check input");
  if (!cb) return;
  cb.addEventListener("change", async () => {
    const want = cb.checked;
    try {
      await api(`/api/experiments/${encodeURIComponent(name)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ offline_bag: want }),
      });
      for (const cache of [EXP_LIST_CACHE, AUTO_EXP_LIST_CACHE]) {
        const it = (cache || []).find((x) => x.name === name);
        if (it) it.offline_bag = want;
      }
      renderExpPicker();
      renderAutoExpPicker();
    } catch (e) {
      cb.checked = !want;
      popupAlert(`直读开关更新失败: ${e.message}`);
    }
  });
}

const OFFLINE_CHECK_TITLE = "VIO 直接读 bag（离线）；不勾 = ros2 bag play 实时回放";

function renderExpPicker() {
  const host = $("#bt-exp-rows");
  host.innerHTML = "";
  // baseline row (special) — checkbox toggles selection; clicking the name
  // pops up the selected dataset's effective config (what the baseline runs with)
  const baseRow = document.createElement("div");
  baseRow.className = "exp-pick-row baseline";
  baseRow.innerHTML = `<input type="checkbox" ${EXP_PICK.has("") ? "checked" : ""}><span class="name clickable" title="像实验一样逐 key 编辑基线参数（保存写回数据集目录）">基线（不改参数，用数据集原配置）</span><label class="exp-offline-check" title="基线组：${OFFLINE_CHECK_TITLE}"><input type="checkbox" ${BT_BASELINE_OFFLINE ? "checked" : ""}>直读</label>`;
  baseRow.querySelector("input").addEventListener("change", (e) => {
    if (e.target.checked) EXP_PICK.add(""); else EXP_PICK.delete("");
    updateExpPickerCount();
  });
  baseRow.querySelector(".exp-offline-check input").addEventListener("change", (e) => {
    BT_BASELINE_OFFLINE = e.target.checked;
  });
  baseRow.querySelector(".name").addEventListener("click", (e) => {
    e.stopPropagation();
    e.preventDefault();
    const sel = getBtSelected();
    if (!sel.length) return popupAlert("先在左侧勾选数据集，基线用各数据集自带的配置");
    openExpModal("", { baselineDs: sel[0], baselineTotal: sel.length });
  });
  wireExpRowToggle(baseRow);
  host.appendChild(baseRow);
  for (const ex of EXP_LIST_CACHE) {
    const row = document.createElement("div");
    row.className = "exp-pick-row";
    const checked = EXP_PICK.has(ex.name) ? "checked" : "";
    row.innerHTML = `<input type="checkbox" ${checked}><span class="name clickable">${escapeHtml(ex.name)}</span><label class="exp-offline-check" title="${OFFLINE_CHECK_TITLE}"><input type="checkbox" ${ex.offline_bag === false ? "" : "checked"}>直读</label><span class="keys">${escapeHtml((ex.keys || []).join(", "))}</span><button class="exp-del-btn" title="删除该实验">✕</button>`;
    // clicking the name opens the editor for this experiment
    row.querySelector(".name").addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      openExpModalFor(ex.name);
    });
    wireExpOfflineToggle(row, ex.name);
    row.querySelector("input").addEventListener("change", (e) => {
      if (e.target.checked) EXP_PICK.add(ex.name); else EXP_PICK.delete(ex.name);
      updateExpPickerCount();
    });
    row.querySelector(".exp-del-btn").addEventListener("click", async (e) => {
      e.stopPropagation();
      e.preventDefault();
      if (!confirm(`删除实验 ${ex.name}？`)) return;
      try {
        await api(`/api/experiments/${encodeURIComponent(ex.name)}`, { method: "DELETE" });
        EXP_PICK.delete(ex.name);
        await loadExperiments();
      } catch (err) { popupAlert(`删除失败: ${err.message}`); }
    });
    wireExpRowToggle(row);
    host.appendChild(row);
  }
  updateExpPickerCount();
}

async function openExpModalFor(name) {
  await openExpModal(name);
}

function updateExpPickerCount() {
  $("#bt-exp-count").textContent = String(EXP_PICK.size);
  updateBtExpToggleLabel();
}

function updateBtExpToggleLabel() {
  const btn = document.querySelector("#bt-exp-pick-toggle");
  if (btn) btn.textContent = EXP_PICK.size > 0 ? "清空" : "全选";
}
$("#bt-exp-pick-toggle").addEventListener("click", () => {
  if (EXP_PICK.size > 0) {
    EXP_PICK.clear();
  } else {
    for (const ex of EXP_LIST_CACHE) EXP_PICK.add(ex.name);
  }
  renderExpPicker();
  updateBtExpToggleLabel();
});
$("#bt-exp-edit").addEventListener("click", () => {
  openExpModal("", { forceNew: true });
});

/* =============================== 实验编辑 =============================== */
// Full-list editor: every flat key rendered as a row with checkbox + override
// input. State:
//   EXP_STATE = { keys: [{key,value}], toggled: {key: overrideValue}, name, filter }
const EXP_STATE = { keys: [], toggled: {}, name: "", filter: "", baselineDs: "" };

// 基线组「VIO 直读」行内勾选状态：手动=会话级；自动=随自动任务配置持久化
let BT_BASELINE_OFFLINE = true;
let AUTO_BASELINE_OFFLINE = true;

// The same modal serves two modes:
//  - experiment mode: checked keys form a fragment saved as an experiment
//  - baseline mode (baselineDs set): checked keys are edits written straight
//    back into the dataset's stereo_auto_gen/estimator_config.yaml
async function openExpModal(forName = "", { forceNew = false, baselineDs = "", baselineTotal = 1 } = {}) {
  const isBase = !!baselineDs;
  EXP_STATE.baselineDs = isBase ? baselineDs : "";
  const sel = $("#exp-base-ds");
  sel.innerHTML = "";
  for (const d of dsCache) {
    if (!d.has_config) continue;
    const o = document.createElement("option");
    o.value = d.name; o.textContent = d.name;
    sel.appendChild(o);
  }
  const curExp = isBase ? "" : (forName || (forceNew ? "" : (() => {
    // fallback: if exactly one experiment (non-baseline) is picked, edit it
    const nonBaseline = Array.from(EXP_PICK).filter((x) => x);
    return nonBaseline.length === 1 ? nonBaseline[0] : "";
  })()));
  EXP_STATE.toggled = {};
  EXP_STATE.name = curExp || "";
  EXP_STATE.filter = "";
  $("#exp-filter").value = "";
  const nameInput = $("#exp-name-input");
  if (nameInput) {
    nameInput.value = curExp || "";
    nameInput.disabled = !!curExp;  // lock when editing existing
  }
  // mode-dependent chrome: baseline hides experiment-only controls
  // (VIO 直读 toggle lives on the picker rows, not in this modal)
  $("#exp-title").textContent = isBase
    ? `基线配置 — ${baselineDs}（直接改值，保存写回数据集）`
    : (curExp ? `实验管理 — ${curExp}` : "实验管理（新建）");
  $(".modal-foot-field").classList.toggle("hidden", isBase);
  $("#exp-force-save").closest("label").classList.toggle("hidden", isBase);
  $("#exp-del").classList.toggle("hidden", isBase);
  $("#exp-save").textContent = isBase ? "保存（写回数据集）" : "保存";
  $("#exp-col-base").textContent = isBase ? "当前值" : "基线值";
  $("#exp-col-ovr").textContent = isBase ? "改为" : "覆盖值";
  $("#exp-preview-title").textContent = isBase
    ? "改动预览（保存直接写回数据集配置，注释保留）"
    : "实验片段预览（deep-merge 到基线）";
  $("#exp-force-save").checked = false;
  if (isBase) {
    sel.value = baselineDs;
    sel.disabled = true;  // baseline is this dataset's own config — lock source
  } else {
    sel.disabled = false;
    if (sel.options.length) sel.value = sel.options[0].value;
  }
  await populateExpFileSelect();
  if (isBase) $("#exp-base-file").disabled = true;
  await loadExpBase();
  // if editing an existing experiment, parse its yaml to pre-populate toggled
  if (curExp) {
    try {
      const r = await api(`/api/experiments/${encodeURIComponent(curExp)}`);
      const parsed = parseSimpleYaml(r.text);
      for (const [k, v] of Object.entries(parsed)) {
        EXP_STATE.toggled[k] = v;
      }
      renderExpList();
    } catch (e) { /* ignore */ }
  }
  $("#exp-status").textContent = isBase && baselineTotal > 1
    ? `共选中 ${baselineTotal} 个数据集；基线配置是每个数据集各自的，当前编辑 ${baselineDs}`
    : "";
  $("#exp-modal").classList.remove("hidden");
}

// Best-effort flat yaml parser: handles `a.b.c: value` and `key: value`.
// Nested mappings are not supported (experiments are flat fragments by design).
function parseSimpleYaml(text) {
  const out = {};
  if (!text) return out;
  for (const line of text.split(/\r?\n/)) {
    const raw = line.trim();
    if (!raw || raw.startsWith("#")) continue;
    const idx = raw.indexOf(":");
    if (idx < 0) continue;
    const key = raw.slice(0, idx).trim();
    let val = raw.slice(idx + 1).trim();
    if (!key || key in out) continue;
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1);
    }
    out[key] = val;
  }
  return out;
}

async function populateExpFileSelect() {
  const ds = $("#exp-base-ds").value;
  const fsel = $("#exp-base-file");
  fsel.innerHTML = "";
  if (!ds) { fsel.disabled = true; return; }
  fsel.disabled = false;
  try {
    const info = await api(`/api/datasets/${encodeURIComponent(ds)}/info`);
    const cfgs = info.configs || [];
    const est = cfgs.find((c) => c.name === "estimator_config.yaml") || cfgs.find((c) => c.name.includes("estimator"));
    for (const c of cfgs) {
      const o = document.createElement("option");
      o.value = c.name; o.textContent = c.name;
      fsel.appendChild(o);
    }
    if (est) fsel.value = est.name;
  } catch (e) { /* ignore */ }
}

async function loadExpBase() {
  const ds = $("#exp-base-ds").value;
  const fname = $("#exp-base-file").value;
  if (!ds || !fname) { EXP_STATE.keys = []; renderExpList(); return; }
  try {
    const r = await api(`/api/datasets/${encodeURIComponent(ds)}/config/${encodeURIComponent(fname)}/flat`);
    EXP_STATE.keys = r.keys || [];
    renderExpList();
  } catch (e) {
    EXP_STATE.keys = [];
    $("#exp-status").textContent = `加载失败: ${e.message}`;
    renderExpList();
  }
}

function renderExpList() {
  const host = $("#exp-list");
  host.innerHTML = "";
  const f = EXP_STATE.filter.trim().toLowerCase();
  let shown = 0;
  EXP_STATE.keys.forEach((k, i) => {
    if (f && !k.key.toLowerCase().includes(f)) return;
    shown++;
    const isOn = Object.prototype.hasOwnProperty.call(EXP_STATE.toggled, k.key);
    const row = document.createElement("div");
    row.className = "exp-row" + (isOn ? " on" : "") + (i % 2 ? " odd" : "");
    row.dataset.key = k.key;
    row.innerHTML = `
      <div class="ck"><input type="checkbox" ${isOn ? "checked" : ""} title="勾选 = 强制包含（即使与当前值相同）"></div>
      <div class="k">${escapeHtml(k.key)}</div>
      <div class="b">${escapeHtml(k.value)}</div>
      <div class="o"><input type="text" value="${escapeAttr(isOn ? EXP_STATE.toggled[k.key] : k.value)}"></div>
    `;
    const cb = row.querySelector("input[type=checkbox]");
    const inp = row.querySelector("input[type=text]");
    cb.addEventListener("change", () => {
      if (cb.checked) {
        // explicit pin (even when equal to current value)
        EXP_STATE.toggled[k.key] = inp.value || k.value;
        row.classList.add("on");
      } else {
        delete EXP_STATE.toggled[k.key];
        inp.value = k.value;
        row.classList.remove("on");
      }
      updateExpStatusAndPreview();
    });
    inp.addEventListener("input", () => {
      // typing a different value auto-includes; reverting to current excludes
      if (inp.value !== k.value) {
        EXP_STATE.toggled[k.key] = inp.value;
        cb.checked = true;
        row.classList.add("on");
      } else {
        delete EXP_STATE.toggled[k.key];
        cb.checked = false;
        row.classList.remove("on");
      }
      updateExpStatusAndPreview();
    });
    host.appendChild(row);
  });
  if (!shown) {
    const empty = document.createElement("div");
    empty.style.cssText = "padding: 16px; text-align: center; color: var(--muted);";
    empty.textContent = EXP_STATE.keys.length ? "无匹配 key" : "（先加载数据集配置）";
    host.appendChild(empty);
  }
  updateExpStatusAndPreview();
}

function updateExpStatusAndPreview() {
  const n = Object.keys(EXP_STATE.toggled).length;
  $("#exp-toggled-count").textContent = n;
  renderExpPreview();
  renderExpNameDisplay();
  renderExpBaselineWarning();
}

function countBaselineMatches() {
  const baselineMap = new Map(EXP_STATE.keys.map((k) => [k.key, String(k.value ?? "")]));
  const toggled = Object.entries(EXP_STATE.toggled);
  let matching = 0;
  for (const [k, v] of toggled) {
    if (baselineMap.has(k) && baselineMap.get(k) === String(v ?? "")) matching++;
  }
  return { matching, total: toggled.length };
}

function renderExpBaselineWarning() {
  const el = $("#exp-status");
  if (!el) return;
  const { matching, total } = countBaselineMatches();
  if (!total) return;
  if (EXP_STATE.baselineDs) {
    // baseline mode: equal values are a harmless no-op, just a hint
    el.textContent = matching === total
      ? `提示：${total} 个 key 与原值相同（保存无实际变化）`
      : (matching > 0 ? `提示：${matching}/${total} 个 key 与原值相同` : "");
    el.style.color = "var(--muted)";
    return;
  }
  if (matching === total) {
    el.textContent = `⚠ ${matching} 个 key 与基线完全一致 —— 勾选「强制保存」后仍可保存（等值覆盖：对本 bag 无效，对其他 bag 生效）`;
    el.style.color = "var(--bad)";
  } else if (matching > 0) {
    el.textContent = `提示：${matching}/${total} 个 key 与基线一致（无效覆盖）`;
    el.style.color = "var(--muted)";
  } else {
    el.textContent = "";
    el.style.color = "";
  }
}

function renderExpNameDisplay() {
  const input = $("#exp-name-input");
  if (!input) return;
  // when editing an existing experiment the input is locked; otherwise hint
  // at what would be auto-generated from the toggled keys if left blank.
  if (input.disabled) return;
  const auto = autoExpName();
  input.placeholder = auto ? `留空则用: ${auto}` : "留空且无改动则无法保存";
}

function renderExpPreview() {
  const lines = [];
  for (const [k, v] of Object.entries(EXP_STATE.toggled)) {
    lines.push(`${k}: ${v}`);
  }
  $("#exp-text").value = lines.join("\n");
}

function autoExpName() {
  const ks = Object.keys(EXP_STATE.toggled);
  if (!ks.length) return "";
  const stems = ks.slice(0, 2).map((k) => k.split(".").pop());
  let name = stems.join("_");
  name = name.replace(/[^A-Za-z0-9_\-]/g, "").slice(0, 40);
  return name;
}

function escapeHtml(s) {
  // quotes included so the result is safe in both text and attribute contexts
  return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function escapeAttr(s) {
  return String(s).replace(/"/g, "&quot;");
}

$("#exp-name-input").addEventListener("input", renderExpNameDisplay);
$("#exp-close").addEventListener("click", () => $("#exp-modal").classList.add("hidden"));
$("#exp-refresh-base").addEventListener("click", loadExpBase);
$("#exp-base-ds").addEventListener("change", populateExpFileSelect);
$("#exp-base-file").addEventListener("change", loadExpBase);
$("#exp-filter").addEventListener("input", () => {
  EXP_STATE.filter = $("#exp-filter").value;
  renderExpList();
});
$("#exp-clear-filter").addEventListener("click", () => {
  $("#exp-filter").value = "";
  EXP_STATE.filter = "";
  renderExpList();
});

$("#exp-save").addEventListener("click", async () => {
  // baseline mode: write checked keys straight back to the dataset config
  // (VIO 直读 toggle lives on the picker rows, not here)
  if (EXP_STATE.baselineDs) {
    const ds = EXP_STATE.baselineDs;
    const ov = EXP_STATE.toggled;
    if (!Object.keys(ov).length) {
      $("#exp-status").textContent = "没有可保存的改动（直接在「改为」列输入新值即可）";
      $("#exp-status").style.color = "var(--muted)";
      return;
    }
    $("#exp-status").textContent = "保存中…";
    $("#exp-status").style.color = "var(--muted)";
    try {
      await api(`/api/datasets/${encodeURIComponent(ds)}/config/estimator_config.yaml`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ overrides: ov }),
      });
      const { matching, total } = countBaselineMatches();
      const noop = matching === total ? `（${matching} 个 key 与原值相同，无实际变化）` : "";
      $("#exp-status").textContent = `✓ 配置已写回 ${ds}/stereo_auto_gen/estimator_config.yaml，下次基线回测生效 ${noop}`;
      $("#exp-status").style.color = "var(--ok)";
      EXP_STATE.toggled = {};       // edits are now the dataset's current values
      await loadExpBase();          // reload to show the fresh baseline
    } catch (e) {
      $("#exp-status").textContent = `保存失败: ${e.message}`;
      $("#exp-status").style.color = "var(--bad)";
    }
    return;
  }
  const text = $("#exp-text").value;
  // prefer user-typed name in the input; fall back to EXP_STATE.name (editing);
  // finally auto-generate from toggled keys if both are empty.
  const custom = $("#exp-name-input").value.trim();
  let name = custom || EXP_STATE.name || autoExpName();
  if (!name) { $("#exp-status").textContent = "请至少修改一个 key 再保存，或在实验名输入框填写自定义名称"; return; }
  // block save if all overrides equal baseline — unless 强制保存 is checked
  // (legit use: pin values that match THIS reference bag but differ on others)
  const { matching, total } = countBaselineMatches();
  if (total > 0 && matching === total && !$("#exp-force-save").checked) {
    const msg = `实验配置与基线完全一致（${matching} 个 key 都相同）。\n如确认要保存（等值覆盖：对本 bag 无效，对其他 bag 仍生效），请勾选「强制保存」后再点保存。`;
    $("#exp-status").textContent = `⚠ ${msg.replace(/\n/g, " ")}`;
    $("#exp-status").style.color = "var(--bad)";
    popupAlert(msg);
    return;
  }
  // 直读开关在行内勾选；保存实验时保持既有 meta（新建默认直读）
  const keptMeta = EXP_LIST_CACHE.find((x) => x.name === name);
  try {
    await api(`/api/experiments/${encodeURIComponent(name)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, offline_bag: keptMeta ? keptMeta.offline_bag !== false : true }),
    });
    EXP_STATE.name = name;
    $("#exp-status").textContent = `已保存: ${name}`;
    $("#exp-status").style.color = "var(--ok)";
    await loadExperiments();
    EXP_PICK.add(name);  // auto-check the just-saved experiment in the picker
    renderExpPicker();
    renderExpNameDisplay();
    if ($("#sub-pane-exp-config").classList.contains("active")) loadCfgExperiments();
    $("#exp-modal").classList.add("hidden");  // 保存成功后自动关闭弹窗
  } catch (e) { $("#exp-status").textContent = `保存失败: ${e.message}`; $("#exp-status").style.color = "var(--bad)"; }
});

$("#exp-del").addEventListener("click", async () => {
  const name = EXP_STATE.name;
  if (!name) return;
  if (!confirm(`删除实验 ${name}？`)) return;
  try {
    await api(`/api/experiments/${encodeURIComponent(name)}`, { method: "DELETE" });
    EXP_STATE.toggled = {}; EXP_STATE.name = "";
    const ni = $("#exp-name-input"); if (ni) { ni.value = ""; ni.disabled = false; }
    $("#exp-status").textContent = `已删除: ${name}`;
    await loadExperiments();
    renderExpList();
  } catch (e) { $("#exp-status").textContent = `删除失败: ${e.message}`; }
});

$("#bt-env").addEventListener("click", async () => {
  const box = $("#bt-env-box");
  box.classList.remove("hidden");
  box.innerHTML = "检查中…";
  try {
    const env = await api("/api/env");
    box.textContent =
      `主机 IP: ${env.host_ip}\n数据根目录: ${env.data_root}\n` +
      `NFS 导出: ${env.nfs_exported ? "✓ 已导出" : "✗ 未导出"}\n` +
      (env.setup_hint ? `设置指引: ${env.setup_hint}\n` : "") +
      `板端挂载点: ${env.board_mount} · ov_web 端口: ${env.ov_web_port}` +
      (env.nfs_detail ? `\nexportfs: ${env.nfs_detail}` : "");
  } catch (e) { box.textContent = e.message; }
});

$("#bt-mount").addEventListener("click", async () => {
  const ip = $("#bt-board").value;
  if (!ip) return alert("先添加并选择板子");
  const btn = $("#bt-mount");
  btn.disabled = true;
  btn.textContent = "挂载中…";
  try {
    const r = await api(`/api/boards/${ip}/mount`, { method: "POST" });
    let msg;
    if (!r.ok) {
      msg = "挂载失败\n\n" + (r.detail || "未知错误");
    } else if (r.already) {
      // 板端已有指向主机 DATA_ROOT 的 NFS 挂载（如 /mnt/nfs20），服务端跳过重复挂载
      msg = `板端数据已可用 ✓\n\n复用现有挂载： ${r.board_path || "(已存在)"}\n\n无需重复挂载，可直接启动回测。`;
    } else {
      msg = `挂载成功 ✓\n\n板端路径： ${r.board_path || ""}\n${r.detail || ""}`;
    }
    alert(msg);
  } catch (e) { alert("挂载请求失败： " + e.message); }
  btn.disabled = false;
  btn.textContent = "挂载数据";
});

$("#bt-deploy").addEventListener("click", async () => {
  const ip = $("#bt-board").value;
  if (!ip) return alert("先添加并选择板子");
  const btn = $("#bt-deploy");
  btn.disabled = true;
  btn.textContent = "部署中…";
  try {
    const r = await api(`/api/boards/${ip}/deploy`, { method: "POST" });
    if (r.ok) {
      alert(`部署成功 ✓\n\n板端路径： ${r.board_path || ""}\n构建来源： ${r.source || "镜像仓库"}\n${r.detail || ""}\n\n之后启动回测会使用这份自编译的 VIO（含 ov_web）。`);
    } else {
      alert("部署失败\n\n" + (r.detail || "未知错误"));
    }
  } catch (e) { alert("部署请求失败： " + e.message); }
  btn.disabled = false;
  btn.textContent = "部署 VIO";
});

/* ov_web readiness gate: the board chain takes a while to open port 9988, and
   pointing the iframe at it early leaves a dead "connection refused" page that
   only a manual refresh recovers from. Poll the server-side probe and only
   assign iframe.src once ov_web actually answers. */
let OVWEB_PROBE_TIMER = null;
function ensureVizIframe(url) {
  const iframe = $("#bt-iframe");
  if (!iframe || !url) return;
  if (iframe.dataset.liveUrl === url) return;  // already loaded this run
  clearTimeout(OVWEB_PROBE_TIMER);
  const wait = $("#bt-viz-wait");
  if (wait) wait.classList.remove("hidden");
  const probe = async () => {
    if (iframe.dataset.liveUrl === url) return;
    let ready = false;
    try {
      const host = new URL(url).hostname;
      const r = await api(`/api/boards/${encodeURIComponent(host)}/ov_web_ready`);
      ready = !!r.ready;
    } catch (e) { /* server unreachable — keep retrying */ }
    if (!ready) { OVWEB_PROBE_TIMER = setTimeout(probe, 2000); return; }
    iframe.src = url;
    iframe.dataset.liveUrl = url;
  };
  probe();
}
$("#bt-iframe").addEventListener("load", () => {
  const wait = $("#bt-viz-wait");
  if (wait) wait.classList.add("hidden");
});

$("#bt-viz-reload").addEventListener("click", () => {
  const ip = $("#bt-board").value;
  if (ip) {
    $("#bt-iframe").dataset.liveUrl = "";  // force re-probe + reload
    ensureVizIframe(`http://${ip}:9988/`);
  }
});

// Sidebar tabs: switch between 数据 / 配置 / 实验组 panes within each sidebar
$$(".sidebar-tab-btn").forEach((btn) =>
  btn.addEventListener("click", () => {
    const sb = btn.closest(".sidebar");
    if (!sb) return;
    sb.querySelectorAll(".sidebar-tab-btn").forEach((x) => x.classList.remove("active"));
    btn.classList.add("active");
    const key = btn.dataset.side;
    sb.querySelectorAll(".sidebar-pane").forEach((p) => {
      p.classList.toggle("active", p.dataset.sidePane === key);
    });
  })
);

// Sidebar pin button: toggle collapsed (unpinned) state — collapses sidebar to a vertical handle
$$(".sidebar-pin-btn").forEach((btn) =>
  btn.addEventListener("click", () => {
    const sb = btn.closest(".sidebar");
    if (!sb) return;
    sb.classList.toggle("collapsed");
    const pinned = !sb.classList.contains("collapsed");
    btn.classList.toggle("pinned", pinned);
    btn.textContent = pinned ? "📌" : "👉";
    btn.title = pinned ? "取消固定" : "固定";
  })
);


/* draggable grid splitter (sidebar width) — shared for manual + auto */
(function initGridSplitters() {
  const setup = (splitter, grid, storageKey) => {
    if (!splitter || !grid) return;
    const saved = parseInt(localStorage.getItem(storageKey) || "0", 10);
    if (saved >= 200 && saved <= 720) grid.style.setProperty("--sidebar-w", saved + "px");
    let dragging = false, startX = 0, startW = 0;
    splitter.addEventListener("mousedown", (e) => {
      const sb = grid.querySelector(".sidebar");
      if (sb && sb.classList.contains("collapsed")) return;
      dragging = true;
      startX = e.clientX;
      startW = grid.querySelector(".sidebar").getBoundingClientRect().width;
      splitter.classList.add("dragging");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const w = Math.max(220, Math.min(720, startW + (e.clientX - startX)));
      grid.style.setProperty("--sidebar-w", w + "px");
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      splitter.classList.remove("dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      const w = parseInt(grid.style.getPropertyValue("--sidebar-w") || "0", 10);
      if (w >= 220) localStorage.setItem(storageKey, String(w));
    });
  };
  setup(document.getElementById("manual-splitter"), document.querySelector(".manual-grid"), "manualSidebarW");
  setup(document.getElementById("auto-grid-splitter"), document.querySelector(".auto-grid"), "autoSidebarW");
})();

/* draggable splitter between viz and log */
(function initBtSplitter() {
  const splitter = document.getElementById("bt-splitter");
  const stack = splitter ? splitter.closest(".bt-viz-stack") : null;
  const viz = stack ? stack.querySelector(".bt-viz") : null;
  if (!splitter || !viz || !stack) return;
  let dragging = false, startY = 0, startH = 0;
  splitter.addEventListener("mousedown", (e) => {
    dragging = true;
    startY = e.clientY;
    startH = viz.getBoundingClientRect().height;
    document.body.style.cursor = "ns-resize";
    document.body.style.userSelect = "none";
    e.preventDefault();
  });
  document.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const dy = e.clientY - startY;
    const newH = Math.max(120, startH + dy);
    viz.style.flex = `0 0 ${newH}px`;
  });
  document.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  });
})();

/* draggable splitter between auto-viz and auto-tasks */
(function initAutoSplitter() {
  const splitter = document.getElementById("auto-splitter");
  const viz = document.querySelector(".auto-viz-card");
  if (!splitter || !viz) return;
  let dragging = false, startY = 0, startH = 0;
  splitter.addEventListener("mousedown", (e) => {
    dragging = true;
    splitter.classList.add("dragging");
    startY = e.clientY;
    startH = viz.getBoundingClientRect().height;
    document.body.style.cursor = "ns-resize";
    document.body.style.userSelect = "none";
    e.preventDefault();
  });
  document.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const dy = e.clientY - startY;
    const newH = Math.max(80, startH + dy);
    viz.style.flex = `0 0 ${newH}px`;
  });
  document.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    splitter.classList.remove("dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  });
})();

function pollBacktest(now = false) {
  clearTimeout(btTimer);
  const tick = async () => {
    const ip = $("#bt-board").value;
    if (ip && $("#tab-backtest").classList.contains("active") && !document.hidden) {
      try {
        const st = await api(`/api/backtest/status?ip=${ip}`);
        const procs = $("#bt-procs");
        procs.innerHTML = "";
        for (const [k, n] of Object.entries(st.processes)) {
          const d = document.createElement("div");
          d.className = "proc" + (n > 0 ? " on" : "");
          d.innerHTML = `<span class="dot"></span>${k} <span class="cnt">×${n}</span>`;
          procs.appendChild(d);
        }
        const m = document.createElement("div");
        m.className = "proc" + (st.mounted ? " on" : "");
        m.innerHTML = `<span class="dot"></span>mount ${st.mounted ? "已挂载" : "未挂载"}`;
        procs.appendChild(m);
        // 手动回测的“可视化/日志”只服务于当前这张板子上正在跑的手动批次。
        // backtest_status 读的是板子 current 软链指向的“当前运行”，会被自动回测或
        // 上一次崩溃的残留日志污染：vio.log 尾部仍留着旧 ERROR，即使批次早结束了也会
        // 一直显示。所以用 running（本板有手动批次在跑）作闸门——没批次不显示崩溃
        // 横幅/日志，批次结束即清空（信息已落在队列/结果里，不反复挂着）。
        const running = batchRunning() && LAST_BATCH_IP === ip;
        const vioCrashed = running && !!st.crash && !(st.processes.vio > 0);
        const old = $("#bt-crash-banner");
        if (old) old.remove();
        if (vioCrashed) {
          const b = document.createElement("div");
          b.id = "bt-crash-banner";
          b.className = "crash-banner";
          b.textContent = `VIO 已崩溃：${st.crash.cause || st.crash.died || "详见 vio.log"}`;
          procs.appendChild(b);
        }
        const log = $("#bt-log");
        const text = running
          ? Object.entries(st.logs).map(([f, t]) => `── ${f} ──\n${t || "(空)"}`).join("\n\n")
          : "";
        if (log.dataset.sig !== text) { log.textContent = text; log.dataset.sig = text; log.scrollTop = log.scrollHeight; }
        // 可视化为三态：本板手动批次运行中 → run（ov_web/vio 存活）/ launching；
        // 无手动批次 → idle。崩死的 vio 即使 ov_web 残留也不算存活。
        const alive = (st.processes.vio > 0) || (!vioCrashed && st.processes.ov_web > 0);
        if (running && alive) {
          if (st.ov_web_url) ensureVizIframe(st.ov_web_url);
          setVizMode("run");
        } else if (running) {
          setVizMode("launching");
        } else {
          setVizMode("idle");
        }
      } catch (e) { /* board unreachable; leave last state */ }
    }
    btTimer = setTimeout(tick, 3000);
  };
  if (now) tick(); else btTimer = setTimeout(tick, 3000);
}

let LAST_BATCH_STATUS = "";  // mirror of last polled batch status ("running" / "finished" / "")
let LAST_BATCH_IP = "";      // board ip of that last-polled batch, so 可视化/日志 can tell
                             // "a manual batch is running on THIS board" from an auto task
let LAST_BUILD_STATUS = null; // last polled build_status, for change-detection on build toasts
function batchRunning() {
  return LAST_BATCH_STATUS === "running";
}

function setVizMode(mode) {
  // Only affects the 可视化/日志 tab pane (launching / run / empty).
  // Data preview (#bt-viz-idle) lives in the data tab and is always visible there.
  const empty = $("#bt-viz-empty");
  const launching = $("#bt-viz-launching");
  const run = $("#bt-viz-run");
  const reloadBtn = $("#bt-viz-reload");
  if (mode === "run") {
    empty.classList.add("hidden"); launching.classList.add("hidden"); run.classList.remove("hidden");
    if (reloadBtn) reloadBtn.classList.remove("hidden");
  } else if (mode === "launching") {
    empty.classList.add("hidden"); run.classList.add("hidden"); launching.classList.remove("hidden");
    if (reloadBtn) reloadBtn.classList.add("hidden");
    // new batch starting → next run must re-probe ov_web and reload the iframe
    const iframe = $("#bt-iframe");
    if (iframe) iframe.dataset.liveUrl = "";
  } else {
    run.classList.add("hidden"); launching.classList.add("hidden"); empty.classList.remove("hidden");
    if (reloadBtn) reloadBtn.classList.add("hidden");
  }
}

// Cache for per-dataset topic/message counts (keyed by dataset name).
// Populated lazily by loadPreviewTopicCounts; survives across re-renders
// so toggling checkboxes doesn't re-fetch already-known datasets.
const TOPIC_COUNT_CACHE = new Map();

function topicCountBadge(name, kind) {
  const info = TOPIC_COUNT_CACHE.get(name);
  if (!info) return kind === "topics" ? "话题 …" : "消息 …";
  const t = info.topics || 0, m = info.messages || 0;
  if (kind === "topics") return t > 0 ? `话题 ${t}` : "无话题";
  return m > 0 ? `消息 ${m.toLocaleString()}` : "无消息";
}

function applyTopicCountBadges(card, name) {
  const info = TOPIC_COUNT_CACHE.get(name);
  if (!info) return;
  const t = info.topics || 0, m = info.messages || 0;
  const tb = card.querySelector(".badge.topics");
  const mb = card.querySelector(".badge.messages");
  if (tb) {
    tb.textContent = t > 0 ? `话题 ${t}` : "无话题";
    tb.classList.toggle("on", t > 0);
  }
  if (mb) {
    mb.textContent = m > 0 ? `消息 ${m.toLocaleString()}` : "无消息";
    mb.classList.toggle("on", m > 0);
  }
}

function renderPreviewGrid(host, names, onRemove) {
  if (!names.length) {
    host.innerHTML = '<div class="idle-empty">勾选左侧数据集后，这里展示已选数据预览（点击卡片可打开播放器，✕ 删除）</div>';
    return;
  }
  host.innerHTML = `<div class="idle-grid"></div>`;
  const grid = host.querySelector(".idle-grid");
  for (const name of names) {
    const d = dsCache.find((x) => x.name === name) || { name, has_bag: false, has_config: false };
    const leaf = name.split("/").pop();
    const parent = name.split("/").slice(0, -1).join("/") || "（根）";
    const card = document.createElement("div");
    card.className = "idle-card clickable";
    card.dataset.dsName = name;
    card.innerHTML = `
      <div class="thumb">${d.has_bag ? `<img src="/api/datasets/${encodeURIComponent(name)}/thumbnail" alt="" loading="lazy" onerror="this.parentNode.textContent='（无图像）'">` : "（无 bag）"}</div>
      <button class="idle-del-btn" title="删除该数据集">✕</button>
      <div class="body">
        <div class="name" title="${escapeHtml(name)}">${escapeHtml(leaf)}</div>
        <div class="meta">
          <span class="hint">${escapeHtml(parent)}</span>
          <span class="badge ${d.has_bag ? "on" : ""}">bag</span>
          <span class="badge ${d.has_config ? "on" : ""}">config</span>
          <span class="badge topics">${topicCountBadge(name, "topics")}</span>
          <span class="badge messages">${topicCountBadge(name, "messages")}</span>
        </div>
      </div>`;
    card.addEventListener("click", (e) => {
      if (e.target.classList.contains("idle-del-btn")) return;
      openDsPreviewModal(name);
    });
    card.querySelector(".idle-del-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      onRemove(name);
    });
    grid.appendChild(card);
  }
  // Only fetch counts for datasets not yet in the cache.
  const missing = names.filter((n) => !TOPIC_COUNT_CACHE.has(n));
  if (missing.length) loadPreviewTopicCounts(grid, missing);
}

async function loadPreviewTopicCounts(grid, names) {
  if (!names.length) return;
  const url = `/api/datasets/topic_counts?names=${encodeURIComponent(names.join(","))}`;
  try {
    const res = await api(url);
    const counts = res.counts || {};
    for (const name of names) {
      TOPIC_COUNT_CACHE.set(name, counts[name] || { topics: 0, messages: 0 });
    }
    for (const card of Array.from(grid.children)) {
      const name = card.dataset.dsName;
      if (!name || !TOPIC_COUNT_CACHE.has(name)) continue;
      applyTopicCountBadges(card, name);
    }
  } catch (e) {
    console.error("loadPreviewTopicCounts failed:", url, e);
  }
}

function renderBtIdleTable() {
  const host = $("#bt-viz-idle");
  const sel = getBtSelected();
  const title = $("#bt-viz-title");
  if (title) title.innerHTML = `已选回测数据 ${sel.length ? `<b>(${sel.length})</b>` : ""}`;
  renderPreviewGrid(host, sel, (name) => {
    const cb = document.querySelector(`#bt-ds-list input.bt-ds-check[value="${CSS.escape(name)}"]`);
    if (cb) {
      cb.checked = false;
      const itemLi = cb.closest(".ds-item");
      if (itemLi) syncGroupCheck(itemLi);
    }
    renderBtIdleTable();
    updateBtSelToggleLabel();
  });
}

/* =============================== 数据预览弹窗 =============================== */
$("#ds-preview-close").addEventListener("click", closeDsPreviewModal);
$("#ds-preview-modal").addEventListener("click", (e) => {
  if (e.target.id === "ds-preview-modal") closeDsPreviewModal();
});
function closeDsPreviewModal() {
  $("#ds-preview-modal").classList.add("hidden");
  // pause the shared player so timers don't keep firing after the modal closes
  PLAYER.playing = false;
  if (PLAYER.timer) { clearInterval(PLAYER.timer); PLAYER.timer = null; }
  PLAYER.name = null;
  $("#ds-preview-modal-body").innerHTML = "";
}

// The backtest preview modal reuses the dataset-tab player (showDataset) so the
// two surfaces stay in sync — same multi-window timeline, same series/chips.
async function openDsPreviewModal(name) {
  const modal = $("#ds-preview-modal");
  const body = $("#ds-preview-modal-body");
  modal.classList.remove("hidden");
  body.innerHTML = '<p class="hint">加载播放器（构建时间索引，首次约 1-2 秒）…</p>';
  await showDataset(name, body);
}

/* ------------------------------- boot ------------------------------- */
const _bootDsPromise = loadDatasets();
loadBoards();
pollBacktest();
renderBtIdleTable();

/* =============================== 自动回测 =============================== */
let autoTasksTimer = null;
let autoStatusTimer = null;

// picked sets for the auto pane (parallel to EXP_PICK / bt-ds-list state)
const AUTO_DS_PICK = new Set();
const AUTO_EXP_PICK = new Set([""]);  // baseline ("") is checked by default, mirrors manual pane
let AUTO_EXP_LIST_CACHE = [];

async function loadAutoConfig() {
  try {
    const c = await api("/api/auto/config");
    $("#auto-enabled").checked = !!c.enabled;
    $("#auto-github-url").value = c.github_url || "";
    const br = await loadBranches();
    fillBranchSelect($("#auto-branch"), br.branches, { selected: c.branch || "develop" });
    showBranchesError(br.error);
    $("#auto-hourly-check").checked = !!c.hourly_check;
    $("#auto-daily-time").value = c.daily_time || "02:00";
    if (c.board_ip) $("#auto-board-ip").value = c.board_ip;
    // populate datasets + experiments state sets from saved config
    AUTO_DS_PICK.clear();
    for (const n of (c.datasets || [])) AUTO_DS_PICK.add(n);
    AUTO_EXP_PICK.clear();
    const savedExps = c.experiments || [];
    if (savedExps.length) {
      for (const n of savedExps) AUTO_EXP_PICK.add(n);
    } else {
      // empty experiments list means "baseline only" by server convention
      AUTO_EXP_PICK.add("");
    }
    renderAutoDsList();
    AUTO_BASELINE_OFFLINE = !!c.offline_bag;
    await loadAutoExperiments();
    $("#auto-build-enabled").checked = !!c.build_enabled;
    $("#auto-use-proxy").checked = !!c.use_proxy;
    $("#auto-build-cmd").value = c.build_cmd || "";
    $("#auto-board-install-path").value = c.board_install_path || "/userdata/demo/install";
    // populate board dropdown if not yet
    const boardSel = $("#auto-board-ip");
    if (!boardSel.options.length) {
      const bs = await api("/api/boards");
      fillSelect(boardSel, bs.map((b) => b.ip));
      if (c.board_ip) boardSel.value = c.board_ip;
    }
  } catch (e) { /* ignore */ }
}

async function loadAutoExperiments() {
  let list = [];
  try { list = await api("/api/experiments"); } catch (e) { /* ignore */ }
  AUTO_EXP_LIST_CACHE = list;
  // prune any picked experiments that no longer exist on disk (keep baseline "" entry)
  for (const n of Array.from(AUTO_EXP_PICK)) {
    if (n === "") continue;
    if (!list.some((e) => e.name === n)) AUTO_EXP_PICK.delete(n);
  }
  $("#auto-exp-total").textContent = String(list.length + 1);
  renderAutoExpPicker();
}

function renderAutoDsList() {
  const box = $("#auto-ds-list");
  if (!box) return;
  box.innerHTML = "";
  if (!dsCache.length) {
    box.innerHTML = '<div class="hint">尚无数据集，到「数据集」Tab 添加</div>';
    $("#auto-ds-count").textContent = "0";
    renderAutoViz();
    return;
  }
  for (const [parent, items] of groupDatasets(dsCache)) {
    const withBag = items.filter((d) => d.has_bag);
    if (!withBag.length) continue;
    const li = document.createElement("li");
    li.className = "ds-group";
    li.innerHTML = `
      <div class="ds-group-head">
        <input type="checkbox" class="bt-grp-check">
        <span class="twist">▸</span>
        <span class="ds-group-name">${escapeHtml(parent)}</span>
        <span class="ds-meta">(${withBag.length})</span>
      </div>
      <ul class="ds-sublist collapsed"></ul>`;
    const sub = li.querySelector(".ds-sublist");
    for (const d of withBag) sub.appendChild(makeAutoDsLi(d));
    const grpCheck = li.querySelector(".bt-grp-check");
    grpCheck.checked = withBag.every((d) => AUTO_DS_PICK.has(d.name));
    grpCheck.indeterminate = withBag.some((d) => AUTO_DS_PICK.has(d.name)) && !grpCheck.checked;
    grpCheck.addEventListener("click", (e) => e.stopPropagation());
    grpCheck.addEventListener("change", () => {
      for (const d of withBag) {
        if (grpCheck.checked) AUTO_DS_PICK.add(d.name);
        else AUTO_DS_PICK.delete(d.name);
      }
      sub.querySelectorAll("input.bt-ds-check").forEach((c) => { c.checked = grpCheck.checked; });
      updateAutoDsCount();
      renderAutoViz();
    });
    li.querySelector(".ds-group-head").addEventListener("click", (e) => {
      if (e.target.tagName === "INPUT") return;
      sub.classList.toggle("collapsed");
      li.querySelector(".twist").textContent = sub.classList.contains("collapsed") ? "▸" : "▾";
      if (!sub.classList.contains("collapsed")) loadLazyThumbs(sub);
    });
    box.appendChild(li);
  }
  updateAutoDsCount();
  renderAutoViz();
  loadLazyThumbs(box);
}

function makeAutoDsLi(d) {
  const li = document.createElement("li");
  li.className = "ds-item";
  const leaf = d.name.split("/").pop();
  const checked = AUTO_DS_PICK.has(d.name) ? "checked" : "";
  li.innerHTML = `
    <input type="checkbox" value="${escapeHtml(d.name)}" class="bt-ds-check" ${checked}>
    ${d.has_bag
      ? `<img class="ds-thumb lazy" data-src="/api/datasets/${encodeURIComponent(d.name)}/thumbnail" alt="">`
      : `<span class="ds-thumb-placeholder">no img</span>`}
    <div class="ds-text">
      <span class="ds-name" title="${escapeHtml(d.name)}">${escapeHtml(leaf)}</span>
      <span class="ds-meta">
        <span class="badge ${d.has_bag ? "on" : ""}">bag</span>
        <span class="badge ${d.has_config ? "on" : ""}">config</span>
      </span>
    </div>`;
  const cb = li.querySelector("input.bt-ds-check");
  cb.addEventListener("click", (e) => e.stopPropagation());
  cb.addEventListener("change", () => {
    if (cb.checked) AUTO_DS_PICK.add(d.name);
    else AUTO_DS_PICK.delete(d.name);
    syncAutoGroupCheck(li);
    updateAutoDsCount();
    renderAutoViz();
  });
  li.addEventListener("click", (e) => {
    if (e.target.tagName === "INPUT") return;
    cb.checked = !cb.checked;
    if (cb.checked) AUTO_DS_PICK.add(d.name);
    else AUTO_DS_PICK.delete(d.name);
    syncAutoGroupCheck(li);
    updateAutoDsCount();
    renderAutoViz();
  });
  return li;
}

function syncAutoGroupCheck(itemLi) {
  const sub = itemLi.closest(".ds-sublist");
  if (!sub) return;
  const grp = sub.previousElementSibling.querySelector(".bt-grp-check");
  if (!grp) return;
  const boxes = sub.querySelectorAll("input.bt-ds-check");
  let on = 0;
  for (const c of boxes) if (c.checked) on++;
  grp.checked = on === boxes.length;
  grp.indeterminate = on > 0 && on < boxes.length;
}

function updateAutoDsCount() {
  $("#auto-ds-count").textContent = String(AUTO_DS_PICK.size);
  updateAutoDsToggleLabel();
}

function renderAutoViz() {
  const host = $("#auto-viz-idle");
  if (!host) return;
  const sel = Array.from(AUTO_DS_PICK);
  const cnt = $("#auto-viz-count");
  if (cnt) cnt.textContent = String(sel.length);
  renderPreviewGrid(host, sel, (name) => {
    AUTO_DS_PICK.delete(name);
    syncAutoDsCheckbox(name, false);
    renderAutoViz();
    updateAutoDsCount();
  });
}

function syncAutoDsCheckbox(name, checked) {
  const cb = document.querySelector(`#auto-ds-list input.bt-ds-check[value="${CSS.escape(name)}"]`);
  if (cb) {
    cb.checked = checked;
    const itemLi = cb.closest(".ds-item");
    if (itemLi) syncAutoGroupCheck(itemLi);
  }
}

function renderAutoExpPicker() {
  const host = $("#auto-exp-rows");
  if (!host) return;
  host.innerHTML = "";
  // baseline row (special) — clicking the name opens the same key-editor as
  // experiments, in baseline mode (edits write back to the dataset config)
  const baseRow = document.createElement("div");
  baseRow.className = "exp-pick-row baseline";
  baseRow.innerHTML = `<input type="checkbox" ${AUTO_EXP_PICK.has("") ? "checked" : ""}><span class="name clickable" title="像实验一样逐 key 编辑基线参数（保存写回数据集目录）">基线（不改参数，用数据集原配置）</span><label class="exp-offline-check" title="基线组：${OFFLINE_CHECK_TITLE}"><input type="checkbox" ${AUTO_BASELINE_OFFLINE ? "checked" : ""}>直读</label>`;
  baseRow.querySelector("input").addEventListener("change", (e) => {
    if (e.target.checked) AUTO_EXP_PICK.add(""); else AUTO_EXP_PICK.delete("");
    updateAutoExpPickerCount();
  });
  baseRow.querySelector(".exp-offline-check input").addEventListener("change", (e) => {
    AUTO_BASELINE_OFFLINE = e.target.checked;
  });
  baseRow.querySelector(".name").addEventListener("click", (e) => {
    e.stopPropagation(); e.preventDefault();
    const sel = Array.from(AUTO_DS_PICK);
    if (!sel.length) return popupAlert("先在左侧勾选数据集，基线用各数据集自带的配置");
    openExpModal("", { baselineDs: sel[0], baselineTotal: sel.length });
  });
  wireExpRowToggle(baseRow);
  host.appendChild(baseRow);
  for (const ex of AUTO_EXP_LIST_CACHE) {
    const row = document.createElement("div");
    row.className = "exp-pick-row";
    const checked = AUTO_EXP_PICK.has(ex.name) ? "checked" : "";
    row.innerHTML = `<input type="checkbox" ${checked}><span class="name clickable">${escapeHtml(ex.name)}</span><label class="exp-offline-check" title="${OFFLINE_CHECK_TITLE}"><input type="checkbox" ${ex.offline_bag === false ? "" : "checked"}>直读</label><span class="keys">${escapeHtml((ex.keys || []).join(", "))}</span><button class="exp-del-btn" title="删除该实验">✕</button>`;
    row.querySelector(".name").addEventListener("click", (e) => {
      e.stopPropagation(); e.preventDefault();
      openAutoExpModalFor(ex.name);
    });
    wireExpOfflineToggle(row, ex.name);
    row.querySelector("input").addEventListener("change", (e) => {
      if (e.target.checked) AUTO_EXP_PICK.add(ex.name);
      else AUTO_EXP_PICK.delete(ex.name);
      updateAutoExpPickerCount();
    });
    row.querySelector(".exp-del-btn").addEventListener("click", async (e) => {
      e.stopPropagation(); e.preventDefault();
      if (!confirm(`删除实验 ${ex.name}？`)) return;
      try {
        await api(`/api/experiments/${encodeURIComponent(ex.name)}`, { method: "DELETE" });
        AUTO_EXP_PICK.delete(ex.name);
        await Promise.all([loadExperiments(), loadAutoExperiments()]);
      } catch (err) { popupAlert(`删除失败: ${err.message}`); }
    });
    wireExpRowToggle(row);
    host.appendChild(row);
  }
  updateAutoExpPickerCount();
}

async function openAutoExpModalFor(name) {
  await openExpModal(name);
}

function updateAutoExpPickerCount() {
  $("#auto-exp-count").textContent = String(AUTO_EXP_PICK.size);
  updateAutoExpToggleLabel();
}

async function saveAutoConfig() {
  const status = $("#auto-save-status");
  status.textContent = "保存中…";
  const body = {
    enabled: $("#auto-enabled").checked,
    github_url: $("#auto-github-url").value.trim(),
    branch: $("#auto-branch").value.trim() || "master",
    hourly_check: $("#auto-hourly-check").checked,
    daily_time: $("#auto-daily-time").value || "02:00",
    board_ip: $("#auto-board-ip").value,
    datasets: Array.from(AUTO_DS_PICK),
    experiments: Array.from(AUTO_EXP_PICK),
    offline_bag: AUTO_BASELINE_OFFLINE,
    build_enabled: $("#auto-build-enabled").checked,
    use_proxy: $("#auto-use-proxy").checked,
    build_cmd: $("#auto-build-cmd").value.trim(),
    board_install_path: $("#auto-board-install-path").value.trim() || "/userdata/demo/install",
  };
  try {
    await api("/api/auto/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    status.textContent = "已保存";
    loadAutoStatus();
  } catch (e) {
    status.textContent = `失败：${e.message}`;
  }
}

async function loadAutoStatus() {
  const box = $("#auto-status");
  try {
    const s = await api("/api/auto/status");
    const cfg = s.config || {};
    box.innerHTML = `
      <span class="badge ${cfg.enabled ? "on" : ""}">${cfg.enabled ? "● 已启用" : "○ 已禁用"}</span>
      <span class="hint">scheduler: <b>${s.scheduler_alive ? "运行中" : "停止"}</b></span>
      <span class="hint">pending: <b>${s.pending}</b> · running: <b>${s.running}</b> · done: <b>${s.done}</b> · failed: <b>${s.failed}</b></span>
      <span class="hint">上次拉取: ${s.last_hourly_check || "—"}</span>
      <span class="hint">下次拉取: ${s.next_hourly_check || "—"}</span>
      <span class="hint">上次跑批: ${s.last_daily_run || "—"}</span>
      <span class="hint">下次跑批: ${s.next_daily_run || "—"}</span>
      <span class="hint">last_seen: <code>${(s.last_seen_sha || "").slice(0, 10) || "—"}</code></span>`;
  } catch (e) { box.innerHTML = `<span class="hint" style="color:var(--bad)">${escapeHtml(e.message)}</span>`; }
}

async function loadAutoTasks() {
  const body = $("#auto-tasks-body");
  body.innerHTML = `<tr><td colspan="10" class="hint">加载中…</td></tr>`;
  try {
    const ts = await api("/api/auto/tasks?limit=200");
    if (!ts.length) { body.innerHTML = `<tr><td colspan="10" class="hint">无任务</td></tr>`; return; }
    body.innerHTML = ts.map((t) => {
      const errCell = t.status === "failed"
        ? `<span class="st-failed" title="${escapeHtml(t.error)}">${escapeHtml((t.error || "").slice(0, 80))}</span>`
        : "<span class='hint'>—</span>";
      // 操作列恒为「预览 | 结果 | 删除」:running 预览可用;done/failed 结果/删除可用
      const running = t.status === "running";
      const finished = t.status === "done" || t.status === "failed";
      const rd = t.result_dir ? escapeHtml(t.result_dir) : "";
      // 运行中补充当前阶段（构建/部署/测试），用户能区分是在编译部署还是在跑测试
      const phaseLabel = { building: "构建中", deploying: "部署中", testing: "测试中" }[t.phase] || "";
      const action =
        `<button class="tiny" data-act="preview" data-ip="${escapeHtml(t.board_ip || "")}" ${running ? "" : "disabled"}>预览</button>` +
        `<span class="hint">|</span>` +
        `<button class="tiny" data-act="result" data-path="${rd}" ${finished && rd ? "" : "disabled"}>结果</button>` +
        `<span class="hint">|</span>` +
        `<button class="tiny danger" data-act="delete" data-id="${escapeHtml(t.id)}" ${running ? "disabled" : ""}>删除</button>`;
      // 配置列恒为「config | diff」:有结果→快照;未跑完→实验定义实时预览;基线 diff 置灰
      const cfgCell = t.experiment
        ? (rd
            ? `<a class="clickable q-cfg" data-path="${rd}" data-name="estimator_config.yaml" data-title="完整配置 (config)（结果快照，只读）">config</a>` +
              ` <span class="diff-sep">|</span> ` +
              `<a class="clickable q-cfg" data-path="${rd}" data-name="experiment.yaml" data-title="差异配置 (diff)：相对基线的改动（结果快照，只读）">diff</a>`
            : `<a class="clickable q-cfg-live" data-exp="${escapeHtml(t.experiment)}" data-ds="${escapeHtml(t.dataset || "")}" data-part="config" title="结果未收集：实时预览合并后的完整配置">config</a>` +
              ` <span class="diff-sep">|</span> ` +
              `<a class="clickable q-cfg-live" data-exp="${escapeHtml(t.experiment)}" data-ds="${escapeHtml(t.dataset || "")}" data-part="diff" title="结果未收集：实时预览实验片段">diff</a>`)
        : `<a class="clickable q-cfg-ds" data-ds="${escapeHtml(t.dataset || "")}" title="查看基线实际生效的配置（只读）">config</a>` +
          ` <span class="diff-sep">|</span> <span class="diff-off" title="基线无相对改动">diff</span>`;
      return `<tr>
        <td><code class="run-no">${escapeHtml(t.run_no || "—")}</code></td>
        <td class="commit-cell" title="${escapeHtml(t.commit_msg || "")}"><code>${escapeHtml(t.commit_short || "")}</code><br><span class="hint" style="font-size:10px">${escapeHtml((t.commit_author || "").slice(0, 20))}</span></td>
        <td>${escapeHtml(t.dataset || "")}</td>
        <td>${escapeHtml(t.experiment || "基线")}</td>
        <td class="st-${t.status}">${escapeHtml(t.status)}${running && phaseLabel ? ` <span class="phase-chip">${phaseLabel}</span>` : ""}</td>
        <td class="hint">${escapeHtml((t.queued_at || "").replace("T", " "))}</td>
        <td class="hint">${escapeHtml((t.finished_at || "").replace("T", " "))}</td>
        <td class="err-cell">${errCell}</td>
        <td>${cfgCell}</td>
        <td>${action}</td>
      </tr>`;
    }).join("");
    body.querySelectorAll('button[data-act="preview"]').forEach((btn) =>
      btn.addEventListener("click", () => {
        const ip = btn.dataset.ip;
        if (!ip) return popupAlert("无板 IP");
        // open viz in a new window so user can watch while auto pane stays usable
        window.open(`http://${ip}:9988/`, "_blank", "width=1280,height=720");
      })
    );
    body.querySelectorAll('button[data-act="result"]').forEach((btn) =>
      btn.addEventListener("click", () => openResultModal(btn.dataset.path))
    );
    body.querySelectorAll('button[data-act="delete"]').forEach((btn) =>
      btn.addEventListener("click", () => deleteAutoTask(btn.dataset.id))
    );
    // 配置列「config | diff」链接（弹窗，不下载）
    body.querySelectorAll(".q-cfg").forEach((a) =>
      a.addEventListener("click", () => openCfgViewModal(a.dataset.path, a.dataset.name, a.dataset.title || ""))
    );
    body.querySelectorAll(".q-cfg-live").forEach((a) =>
      a.addEventListener("click", () => openLiveExpCfgModal(a.dataset.exp, a.dataset.ds, a.dataset.part || "diff"))
    );
    body.querySelectorAll(".q-cfg-ds").forEach((a) =>
      a.addEventListener("click", () => { if (a.dataset.ds) openBaselineCfgModal(a.dataset.ds, 1); })
    );
  } catch (e) {
    body.innerHTML = `<tr><td colspan="10" class="hint" style="color:var(--bad)">${escapeHtml(e.message)}</td></tr>`;
  }
}

async function deleteAutoTask(id) {
  if (!id) return popupAlert("无任务 id");
  if (!confirm(`从任务队列移除该任务「${id}」？\n仅从队列移除，结果保留在「统计」可随时查看/删除。`)) return;
  try {
    const r = await api(`/api/auto/tasks/${encodeURIComponent(id)}`, { method: "DELETE" });
    popupAlert(r.detail || "已从队列移除");
    loadAutoTasks();
    loadAutoStatus();
  } catch (e) { popupAlert("移除失败：" + e.message); }
}

async function loadAutoCommits(branch = "") {
  refreshAllBranchSelects();  // 顺手刷新两个分支下拉(手动+自动)
  const sel = $("#bt-commit");
  if (!sel) return;
  try {
    const cs = await api("/api/auto/commits?limit=50" + (branch ? `&branch=${encodeURIComponent(branch)}` : ""));
    const cur = sel.value;
    sel.innerHTML = `<option value="">当前代码 (HEAD)</option>` +
      cs.map((c) => `<option value="${escapeHtml(c.sha)}" title="${escapeHtml(c.msg)}">${escapeHtml(c.short)} · ${escapeHtml((c.date || "").slice(0, 10))} · ${escapeHtml((c.author || "").slice(0, 20))} · ${escapeHtml((c.msg || "").slice(0, 60))}</option>`).join("");
    if (cur) sel.value = cur;
  } catch (e) { /* mirror not cloned yet; keep just HEAD option */ }
}

async function fetchAutoNow() {
  const btn = $("#auto-fetch-now");
  if (btn) { btn.disabled = true; btn.textContent = "拉取中…"; }
  try {
    const r = await api("/api/auto/hourly_check", { method: "POST" });
    let msg;
    if (!r.fetched) {
      msg = "拉取失败 ✗\n\n" + (r.error || "未知错误") +
        "\n\n若是网络问题：到「配置」面板勾选「git 走代理 (proxychains4)」后保存，再重试。";
    } else if (r.error) {
      msg = "已连接远端，但列出 commit 失败：\n" + r.error;
    } else if (!r.new_commits || r.new_commits.length === 0) {
      msg = "已是最新 ✓\n\n远端没有新 commit。";
    } else {
      msg = `拉取完成 ✓\n\n新 commit：${r.new_commits.length} 个\n入队任务：${r.enqueued} 个（commit × 数据集 × 实验组）`;
      if (r.enqueued === 0) msg += "\n\n入队 0 个：这些任务可能已在队列中。";
    }
    alert(msg);
    loadAutoTasks();
    loadAutoStatus();
    loadAutoCommits();
  } catch (e) { alert("拉取请求失败：" + e.message); }
  if (btn) { btn.disabled = false; btn.textContent = "立即拉取新 commit"; }
}

async function runAutoNow() {
  try {
    await api("/api/auto/daily_run", { method: "POST" });
    loadAutoTasks();
    loadAutoStatus();
  } catch (e) { alert("触发失败：" + e.message); }
}

// poll auto tasks + status when auto sub-pane is visible
let autoPolling = false;
async function pollAuto() {
  clearTimeout(autoTasksTimer);
  clearTimeout(autoStatusTimer);
  const visible = $("#sub-pane-auto") && $("#sub-pane-auto").classList.contains("active")
    && $("#tab-backtest").classList.contains("active") && !document.hidden;
  if (visible) {
    // single-flight: never stack a second fetch round while one is in flight
    if (!autoPolling) {
      autoPolling = true;
      await Promise.all([loadAutoTasks(), loadAutoStatus()]).catch(() => {});
      autoPolling = false;
    }
    autoTasksTimer = setTimeout(pollAuto, 5000);
  } else {
    autoStatusTimer = setTimeout(pollAuto, 15000);  // slow wake-up check when hidden
  }
}

/* =============================== 实验配置（sub-tab） =============================== */
async function loadCfgExperiments() {
  const host = $("#cfg-exp-list");
  const label = $("#cfg-exp-count-label");
  if (!host) return;
  host.innerHTML = '<div class="hint">加载中…</div>';
  let list = [];
  try { list = await api("/api/experiments"); }
  catch (e) { host.innerHTML = `<div class="hint" style="color:var(--bad)">${escapeHtml(e.message)}</div>`; return; }
  label.textContent = `共 ${list.length} 个实验片段`;
  if (!list.length) {
    host.innerHTML = "";
    return;
  }
  host.innerHTML = "";
  for (const ex of list) {
    const card = document.createElement("div");
    card.className = "cfg-exp-card";
    const keysHtml = (ex.keys && ex.keys.length)
      ? ex.keys.map((k) => `<code>${escapeHtml(k)}</code>`).join("")
      : '<span class="hint">（无覆盖键）</span>';
    card.innerHTML = `
      <div class="cfg-exp-head">
        <span class="cfg-exp-name clickable" title="点击编辑">${escapeHtml(ex.name)}</span>
        <label class="exp-offline-check" title="${OFFLINE_CHECK_TITLE}"><input type="checkbox" ${ex.offline_bag === false ? "" : "checked"}>直读</label>
        <button class="cfg-exp-edit small primary">编辑</button>
        <button class="cfg-exp-del small danger" title="删除该实验">✕</button>
      </div>
      <div class="cfg-exp-keys">${keysHtml}</div>`;
    card.querySelector(".cfg-exp-name").addEventListener("click", () => openExpModal(ex.name));
    card.querySelector(".cfg-exp-edit").addEventListener("click", () => openExpModal(ex.name));
    wireExpOfflineToggle(card, ex.name);
    card.addEventListener("click", (e) => {
      if (e.target.closest("button, .exp-offline-check")) return;  // 控件有自己的行为
      openExpModal(ex.name);
    });
    card.querySelector(".cfg-exp-del").addEventListener("click", async () => {
      if (!confirm(`删除实验 ${ex.name}？此操作不可撤销。`)) return;
      try {
        await api(`/api/experiments/${encodeURIComponent(ex.name)}`, { method: "DELETE" });
        EXP_PICK.delete(ex.name);
        AUTO_EXP_PICK.delete(ex.name);
        await Promise.all([loadExperiments(), loadCfgExperiments()]);
      } catch (e) { popupAlert(`删除失败：${e.message}`); }
    });
    host.appendChild(card);
  }
}

$("#cfg-exp-add").addEventListener("click", () => openExpModal(""));
$("#cfg-exp-refresh").addEventListener("click", loadCfgExperiments);

$("#auto-save").addEventListener("click", saveAutoConfig);
$("#auto-fetch-now").addEventListener("click", fetchAutoNow);
$("#auto-run-now").addEventListener("click", runAutoNow);

async function loadBranches(fetch = false) {
  try {
    const r = await api("/api/auto/branches" + (fetch ? "?fetch=1" : ""));
    return { branches: r.branches || [], error: r.error || "" };
  } catch (e) { return { branches: [], error: String(e && e.message || e) }; }
}

function showBranchesError(msg) {
  for (const sel of ["#bt-branch-err", "#auto-branch-err"]) {
    const el = $(sel);
    if (!el) continue;
    el.textContent = msg || "";
    el.classList.toggle("hidden", !msg);
  }
}

// 填充分支下拉；selected 不在列表时补一个 option 保留它（例如已保存但未 fetch 的分支）
function fillBranchSelect(sel, branches, { allowEmpty = false, emptyLabel = "", selected = "" } = {}) {
  if (!sel) return;
  const cur = selected || sel.value;
  const opts = [];
  if (allowEmpty) opts.push(`<option value="">${escapeHtml(emptyLabel)}</option>`);
  for (const b of branches) opts.push(`<option value="${escapeHtml(b)}">${escapeHtml(b)}</option>`);
  sel.innerHTML = opts.join("");
  if (cur && !branches.includes(cur)) {
    const o = document.createElement("option");
    o.value = cur;
    o.textContent = cur + " (未拉取)";
    sel.appendChild(o);
  }
  if (cur) sel.value = cur;
}

async function refreshAllBranchSelects(fetch = false) {
  const { branches, error } = await loadBranches(fetch);
  fillBranchSelect($("#auto-branch"), branches);
  fillBranchSelect($("#bt-branch"), branches, { allowEmpty: true, emptyLabel: "不构建（用板端现有版本）" });
  showBranchesError(error);
}

$("#auto-branch-refresh").addEventListener("click", async () => {
  const btn = $("#auto-branch-refresh");
  btn.disabled = true;
  await refreshAllBranchSelects(true);
  btn.disabled = false;
});

// 手动拉取源码：本地无代码（克隆失败/403）或想立即更新时用。proxySel 是所在
// 面板里「git 走代理」复选框的 selector；勾选后传给服务端走 proxychains4。
async function pullCode(btn, proxySel) {
  if (!btn) return;
  const proxyEl = $(proxySel);
  const useProxy = !!(proxyEl && proxyEl.checked);
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = "拉取中…";
  try {
    const r = await api("/api/auto/pull", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ use_proxy: useProxy }),
    });
    await refreshAllBranchSelects(false);
    popupAlert(r.ok ? `代码拉取成功 ✓\n\n${r.detail}` : `代码拉取失败 ✗\n\n${r.detail}`);
  } catch (e) {
    popupAlert("拉取请求失败：" + (e && e.message || e));
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
}
$("#auto-pull-code").addEventListener("click", (e) => pullCode(e.currentTarget, "#auto-use-proxy"));
$("#bt-pull-code").addEventListener("click", (e) => pullCode(e.currentTarget, "#bt-use-proxy"));
$("#auto-tasks-refresh").addEventListener("click", loadAutoTasks);
function updateAutoDsToggleLabel() {
  const btn = document.querySelector("#auto-ds-toggle");
  if (btn) btn.textContent = AUTO_DS_PICK.size > 0 ? "清空" : "全选";
}
$("#auto-ds-toggle").addEventListener("click", () => {
  if (AUTO_DS_PICK.size > 0) {
    AUTO_DS_PICK.clear();
  } else {
    for (const d of dsCache) if (d.has_bag) AUTO_DS_PICK.add(d.name);
  }
  renderAutoDsList();
  updateAutoDsToggleLabel();
});
function updateAutoExpToggleLabel() {
  const btn = document.querySelector("#auto-exp-toggle");
  if (btn) btn.textContent = AUTO_EXP_PICK.size > 0 ? "清空" : "全选";
}
$("#auto-exp-toggle").addEventListener("click", () => {
  if (AUTO_EXP_PICK.size > 0) {
    AUTO_EXP_PICK.clear();
  } else {
    for (const ex of AUTO_EXP_LIST_CACHE) AUTO_EXP_PICK.add(ex.name);
  }
  renderAutoExpPicker();
  updateAutoExpToggleLabel();
});
$("#auto-exp-edit").addEventListener("click", () => {
  openExpModal("", { forceNew: true });
});
pollAuto();


/* =============================== 批量回测 =============================== */
let batchTimer = null;
let lastBatchId = null;

function renderBtDsList() {
  const box = $("#bt-ds-list");
  box.innerHTML = "";
  for (const [parent, items] of groupDatasets(dsCache)) {
    const withBag = items.filter((d) => d.has_bag);
    if (!withBag.length) continue;
    const li = document.createElement("li");
    li.className = "ds-group";
    li.innerHTML = `
      <div class="ds-group-head">
        <input type="checkbox" class="bt-grp-check">
        <span class="twist">▸</span>
        <span class="ds-group-name">${escapeHtml(parent)}</span>
        <span class="ds-meta">(${withBag.length})</span>
      </div>
      <ul class="ds-sublist collapsed"></ul>`;
    const sub = li.querySelector(".ds-sublist");
    for (const d of withBag) sub.appendChild(makeBtDsLi(d));
    const grpCheck = li.querySelector(".bt-grp-check");
    grpCheck.addEventListener("click", (e) => e.stopPropagation());
    grpCheck.addEventListener("change", () => {
      sub.querySelectorAll("input.bt-ds-check").forEach((c) => c.checked = grpCheck.checked);
      maybeRefreshIdleTable();
    });
    li.querySelector(".ds-group-head").addEventListener("click", (e) => {
      if (e.target.tagName === "INPUT") return;
      sub.classList.toggle("collapsed");
      li.querySelector(".twist").textContent = sub.classList.contains("collapsed") ? "▸" : "▾";
      if (!sub.classList.contains("collapsed")) loadLazyThumbs(sub);
    });
    box.appendChild(li);
  }
  loadLazyThumbs(box);
}

function makeBtDsLi(d) {
  const li = document.createElement("li");
  li.className = "ds-item";
  const leaf = d.name.split("/").pop();
  const thumb = d.has_bag
    ? `<img class="ds-thumb lazy" data-src="/api/datasets/${encodeURIComponent(d.name)}/thumbnail" alt="">`
    : `<span class="ds-thumb-placeholder">no img</span>`;
  li.innerHTML = `
    <input type="checkbox" value="${escapeHtml(d.name)}" class="bt-ds-check">
    ${thumb}
    <div class="ds-text">
      <span class="ds-name" title="${escapeHtml(d.name)}">${escapeHtml(leaf)}</span>
      <span class="ds-meta">
        <span class="badge ${d.has_bag ? "on" : ""}">bag</span>
        <span class="badge ${d.has_config ? "on" : ""}">config</span>
      </span>
    </div>`;
  const cb = li.querySelector("input.bt-ds-check");
  cb.addEventListener("click", (e) => e.stopPropagation());
  cb.addEventListener("change", () => { syncGroupCheck(li); maybeRefreshIdleTable(); });
  li.addEventListener("click", (e) => {
    if (e.target.tagName === "INPUT") return;
    cb.checked = !cb.checked;
    syncGroupCheck(li);
    maybeRefreshIdleTable();
  });
  return li;
}

function maybeRefreshIdleTable() {
  // only re-render if the idle pane is currently visible
  if (!$("#bt-viz-idle").classList.contains("hidden")) renderBtIdleTable();
  updateBtSelToggleLabel();
}

function syncGroupCheck(itemLi) {
  const sub = itemLi.closest(".ds-sublist");
  if (!sub) return;
  const grp = sub.previousElementSibling.querySelector(".bt-grp-check");
  if (!grp) return;
  const boxes = sub.querySelectorAll("input.bt-ds-check");
  let on = 0;
  for (const c of boxes) if (c.checked) on++;
  grp.checked = on === boxes.length;
  grp.indeterminate = on > 0 && on < boxes.length;
}

function getBtSelected() {
  return $$("#bt-ds-list input.bt-ds-check:checked").map((i) => i.value);
}

function btVerbosity() { return ($("#bt-verbosity") || {}).value || "INFO"; }
function btVioLogLevel() { return ($("#bt-vio-log-level") || {}).value || "warn"; }
// 未勾选时返回 null（后端走 auto 配置的 use_proxy），勾选时才强制本批次走 proxychains4
function btUseProxy() { return $("#bt-use-proxy") && $("#bt-use-proxy").checked ? true : null; }

async function batchAction() {
  const ip = $("#bt-board").value;
  const sel = getBtSelected();
  const experiments = Array.from(EXP_PICK);
  const offlineBag = BT_BASELINE_OFFLINE;
  const commit = $("#bt-commit") ? $("#bt-commit").value : "";
  const branch = $("#bt-branch") ? $("#bt-branch").value : "";
  if (!ip) return popupAlert("先选择板子");
  if (!sel.length) return popupAlert("勾选至少一个数据集");
  if (!experiments.length) return popupAlert("至少勾选一个实验组（或基线）");
  if (commit && branch) return popupAlert("「VIO commit」和「代码分支」二选一：commit 走自动队列，分支在批次内构建");
  // If a specific commit is selected, route through /api/auto/enqueue
  // (which does checkout → build → deploy → backtest via the scheduler).
  if (commit) {
    try {
      const r = await api("/api/auto/enqueue", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ commit, datasets: sel, experiments, offline_bag: offlineBag, board_ip: ip }) });
      popupAlert(`已入队 ${r.enqueued} 个任务（commit ${r.commit_short}），切到「自动回测」查看进度`);
      pollAuto();
    } catch (e) { popupAlert(e.message); }
    return;
  }
  // Determine whether to start new or append to running batch on this board.
  let running = null;
  try {
    const list = await api("/api/batch");
    running = list.find((b) => b.ip === ip && b.status === "running") || null;
  } catch (e) { /* ignore — fall through to start */ }
  if (running && branch) {
    return popupAlert("该板已有运行中批次，追加会沿用原批次的代码版本；\n如需切换分支请先停止当前批次再启动新批次");
  }
  // Override only applies to single-dataset start (not append).
  let override = "";
  if (!running && LAUNCH_SCRIPT_OVERRIDE) {
    if (sel.length > 1) {
      popupAlert("自定义脚本仅支持单数据集回测；当前选中 " + sel.length + " 个数据集，将使用自动生成的脚本");
    } else {
      override = LAUNCH_SCRIPT_OVERRIDE;
    }
  }
  try {
    let b;
    if (running) {
      b = await api(`/api/batch/${running.id}/append`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ datasets: sel, experiments, offline_bag: offlineBag, verbosity: btVerbosity(), vio_log_level: btVioLogLevel(), use_proxy: btUseProxy() }) });
      lastBatchId = running.id;
    } else {
      b = await api("/api/batch/start", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ip, datasets: sel, experiments, offline_bag: offlineBag, launch_script_override: override, branch, verbosity: btVerbosity(), vio_log_level: btVioLogLevel(), use_proxy: btUseProxy() }) });
      lastBatchId = b.id;
    }
    switchBtTab("queue");
    pollBatch(true);
    pollBacktest(true);
    const queue = $("#bt-queue-card");
    if (queue) {
      queue.classList.add("flash");
      setTimeout(() => queue.classList.remove("flash"), 1200);
    }
  } catch (e) { popupAlert(e.message); }
}

// Update the batch action button label based on whether a batch is running
// on the currently selected board. Also drives the stop button's disabled
// state: gray when nothing is running anywhere, red/active when any batch is up.
function refreshBatchActionLabel(runningList) {
  const btn = $("#bt-batch-action");
  const stopBtn = $("#bt-batch-stop");
  if (!btn) return;
  const ip = $("#bt-board").value;
  const running = runningList.find((b) => b.ip === ip && b.status === "running");
  const anyRunning = runningList.some((b) => b.status === "running");
  btn.textContent = running ? "＋ 添加回测" : "▶ 启动回测";
  if (stopBtn) stopBtn.disabled = !anyRunning;
}

async function stopBatch() {
  if (!lastBatchId) {
    const ip = $("#bt-board").value;
    if (ip) {
      try { await api("/api/backtest/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ip }) }); } catch (e) { /* ignore */ }
      pollBacktest(true);
    }
    return;
  }
  try {
    await api(`/api/batch/${lastBatchId}/stop`, { method: "POST" });
  } catch (e) { /* ignore */ }
  pollBatch(true);
  pollBacktest(true);
}

function updateBtSelToggleLabel() {
  const btn = document.querySelector("#bt-sel-toggle");
  if (!btn) return;
  const any = $$("#bt-ds-list input.bt-ds-check:checked").length > 0;
  btn.textContent = any ? "清空" : "全选";
}
$("#bt-sel-toggle").addEventListener("click", () => {
  const any = $$("#bt-ds-list input.bt-ds-check:checked").length > 0;
  if (any) {
    $$("#bt-ds-list input[type=checkbox]").forEach((c) => { c.checked = false; c.indeterminate = false; });
  } else {
    $$("#bt-ds-list input.bt-ds-check").forEach((c) => c.checked = true);
    $$("#bt-ds-list input.bt-grp-check").forEach((c) => { c.checked = true; c.indeterminate = false; });
  }
  maybeRefreshIdleTable();
  updateBtSelToggleLabel();
});

const COLLAPSED_GROUPS = new Set();  // experiment labels collapsed by user (persists across re-renders)

async function pollBatch(now = false) {
  clearTimeout(batchTimer);
  const tick = async () => {
    if ($("#tab-backtest").classList.contains("active") && !document.hidden) {
      try {
        const list = await api("/api/batch");
        const body = $("#bt-queue-body");
        if (body && list.length) {
          const b = list[list.length - 1];
          lastBatchId = b.id;
          LAST_BATCH_STATUS = b.status;
          LAST_BATCH_IP = b.ip || "";
          refreshBatchActionLabel(list);
          // skip the DOM rebuild when nothing changed (keeps scroll + avoids
          // re-wiring listeners every 3s while a long item runs)
          const sig = JSON.stringify([b.id, b.status, b.ip, b.build_status, b.commit_short, b.items.map((i) =>
            [i.dataset, i.experiment || "", i.status, i.started_at, i.finished_at, i.error, i.result_dir, i.offline_bag])]);
          if (sig !== body.dataset.sig) {
            body.dataset.sig = sig;
          // 构建/部署横幅：指定分支的批次显示构建+部署；普通批次启动前也会自动部署
          const buildBanner = (b.branch || b.build_status)
            ? `<tr class="build-row"><td colspan="10">` +
              (b.branch ? `分支 <b>${escapeHtml(b.branch)}</b>` : "自动部署") +
              (b.commit_short ? ` <code>${escapeHtml(b.commit_short)}</code>` : "") + " · " +
              (b.build_status === "done" ? `<span class="st-done">${b.branch ? "构建+部署" : "部署"} ✓</span>`
                : b.build_status === "failed" ? `<span class="st-failed">${b.branch ? "构建/部署" : "部署"}失败 ✗</span> <span class="hint">${escapeHtml((b.build_log || "").slice(-200))}</span>`
                : `<span class="st-running">${escapeHtml(b.build_status || "准备中")}…</span>`) +
              `</td></tr>` : "";
          // 构建/部署进行中与完成的轻提示：进入 fetching/building/deploying 提醒等待，
          // 到达 done 提醒开始测试；二者都 5 秒后自动消失。
          const RUNNING_BUILD = new Set(["fetching", "building", "deploying"]);
          if (b.branch || b.build_status) {
            const cur = b.build_status || "";
            const prev = LAST_BUILD_STATUS;
            LAST_BUILD_STATUS = cur;
            if (cur === "done" && prev !== null && prev !== "done") {
              showToast("构建部署完成，开始测试");
            } else if (RUNNING_BUILD.has(cur) && !RUNNING_BUILD.has(prev)) {
              showToast("正在编译和部署程序，请等待…");
            }
          } else {
            LAST_BUILD_STATUS = null;
          }
          // group items by experiment label (baseline when empty; 直读/bag play
          // mode is already shown per-row in the 直读bag column)
          const groups = new Map();
          b.items.forEach((it) => {
            const label = it.experiment || "基线";
            if (!groups.has(label)) groups.set(label, []);
            groups.get(label).push(it);
          });
          let idx = 0;
          const rows = [];
          for (const [label, items] of groups) {
            const done = items.filter((i) => i.status === "done").length;
            const failed = items.filter((i) => i.status === "failed").length;
            const summary = `${items.length} 项 · ✓${done} ✗${failed}`;
            const collapsed = COLLAPSED_GROUPS.has(label);
            rows.push(`<tr class="group-row${collapsed ? " collapsed" : ""}" data-exp="${escapeHtml(label)}">
              <td colspan="10"><span class="group-caret">${collapsed ? "▸" : "▾"}</span>
              <b>${escapeHtml(label)}</b> <span class="hint">${summary}</span></td></tr>`);
            items.forEach((it) => {
              idx += 1;
              const err = it.error
                ? `<span class="err-cell" title="${escapeHtml(it.error)}">${escapeHtml(it.error)}</span>`
                : "<span class='hint'>—</span>";
              const bagFlag = it.offline_bag ? "✓" : "✗";
              // 操作列恒为「预览 | 结果 | 删除」:running 时预览可用、其余置灰;
              // done/failed 时结果/删除可用、预览置灰;pending 都置灰
              const running = it.status === "running";
              const finished = it.status === "done" || it.status === "failed";
              const rd = it.result_dir ? escapeHtml(it.result_dir) : "";
              const action =
                `<button class="tiny" data-act="preview" data-ip="${escapeHtml(b.ip || "")}" ${running ? "" : "disabled"}>预览</button>` +
                `<span class="hint">|</span>` +
                `<button class="tiny" data-act="result" data-path="${rd}" ${finished && rd ? "" : "disabled"}>结果</button>` +
                `<span class="hint">|</span>` +
                `<button class="tiny" data-act="delete" data-path="${rd}" ${finished && rd ? "" : "disabled"}>删除</button>`;
              // 配置列恒为「config | diff」（弹窗展示）:有结果→快照;未跑完→实验定义实时预览;
              // 基线 config=数据集实际生效配置、diff 置灰（基线无相对改动）
              const cfgCell = it.experiment
                ? (rd
                    ? `<a class="clickable q-cfg" data-path="${rd}" data-name="estimator_config.yaml" data-title="完整配置 (config)（结果快照，只读）">config</a>` +
                      ` <span class="diff-sep">|</span> ` +
                      `<a class="clickable q-cfg" data-path="${rd}" data-name="experiment.yaml" data-title="差异配置 (diff)：相对基线的改动（结果快照，只读）">diff</a>`
                    : `<a class="clickable q-cfg-live" data-exp="${escapeHtml(it.experiment)}" data-ds="${escapeHtml(it.dataset)}" data-part="config" title="结果未收集：实时预览合并后的完整配置">config</a>` +
                      ` <span class="diff-sep">|</span> ` +
                      `<a class="clickable q-cfg-live" data-exp="${escapeHtml(it.experiment)}" data-ds="${escapeHtml(it.dataset)}" data-part="diff" title="结果未收集：实时预览实验片段">diff</a>`)
                : `<a class="clickable q-cfg-ds" data-ds="${escapeHtml(it.dataset)}" title="查看基线实际生效的配置（只读）">config</a>` +
                  ` <span class="diff-sep">|</span> <span class="diff-off" title="基线无相对改动">diff</span>`;
              rows.push(`<tr class="item-row${collapsed ? " hidden" : ""}" data-exp="${escapeHtml(label)}">
                <td class="hint">${idx}</td>
                <td><code class="run-no">${escapeHtml(b.run_no || "—")}</code></td>
                <td class="ds-cell">${escapeHtml(it.dataset)}</td>
                <td class="st-${it.status}">${it.status}</td>
                <td class="hint ts">${escapeHtml(it.started_at || "—")}</td>
                <td class="hint ts">${escapeHtml(it.finished_at || "—")}</td>
                <td class="hint">${bagFlag}</td>
                <td>${err}</td>
                <td>${cfgCell}</td>
                <td>${action}</td></tr>`);
            });
          }
          body.innerHTML = buildBanner + (rows.join("") || "<tr><td colspan='10' class='hint'>未运行批量</td></tr>");
          // wire up group collapse
          body.querySelectorAll(".group-row").forEach((gr) => {
            gr.addEventListener("click", () => {
              const exp = gr.dataset.exp;
              const collapsed = gr.classList.toggle("collapsed");
              if (collapsed) COLLAPSED_GROUPS.add(exp); else COLLAPSED_GROUPS.delete(exp);
              gr.parentElement.querySelectorAll(`.item-row[data-exp="${CSS.escape(exp)}"]`).forEach((ir) => {
                ir.classList.toggle("hidden", collapsed);
              });
              gr.querySelector(".group-caret").textContent = collapsed ? "▸" : "▾";
            });
          });
          // wire up action buttons
          body.querySelectorAll('button[data-act="preview"]').forEach((btn) =>
            btn.addEventListener("click", (e) => {
              e.stopPropagation();
              switchBtTab("viz");
              const ip = btn.dataset.ip;
              if (ip) {
                setVizMode("run");
                ensureVizIframe(`http://${ip}:9988/`);
              }
            })
          );
          body.querySelectorAll('button[data-act="result"]').forEach((btn) =>
            btn.addEventListener("click", (e) => {
              e.stopPropagation();
              openResultModal(btn.dataset.path);
            })
          );
          body.querySelectorAll('button[data-act="delete"]').forEach((btn) =>
            btn.addEventListener("click", (e) => {
              e.stopPropagation();
              deleteFromQueue(btn.dataset.path);
            })
          );
          // 配置列「config | diff」链接（弹窗，不下载）
          body.querySelectorAll(".q-cfg").forEach((a) =>
            a.addEventListener("click", (e) => {
              e.stopPropagation(); e.preventDefault();
              openCfgViewModal(a.dataset.path, a.dataset.name, a.dataset.title || "");
            })
          );
          body.querySelectorAll(".q-cfg-live").forEach((a) =>
            a.addEventListener("click", (e) => {
              e.stopPropagation(); e.preventDefault();
              openLiveExpCfgModal(a.dataset.exp, a.dataset.ds, a.dataset.part || "diff");
            })
          );
          body.querySelectorAll(".q-cfg-ds").forEach((a) =>
            a.addEventListener("click", (e) => {
              e.stopPropagation(); e.preventDefault();
              if (a.dataset.ds) openBaselineCfgModal(a.dataset.ds, 1);
            })
          );
          } // end sig-changed
        } else if (body) {
          if (body.dataset.sig !== "empty") {
            body.dataset.sig = "empty";
            body.innerHTML = "<tr><td colspan='10' class='hint'>未运行批量</td></tr>";
          }
          LAST_BATCH_STATUS = "";
          refreshBatchActionLabel([]);
        }
      } catch (e) { /* ignore */ }
    }
    batchTimer = setTimeout(tick, 3000);
  };
  if (now) tick(); else batchTimer = setTimeout(tick, 3000);
}

$("#bt-batch-action").addEventListener("click", batchAction);
$("#bt-batch-stop").addEventListener("click", stopBatch);
$("#bt-board").addEventListener("change", () => { pollBatch(true); });  // refresh label

// 「VIO commit」和「代码分支」二选一（见 batchAction 的“二选一”报警）：选一个自动清
// 掉另一个，避免两边都选导致点「启动回测」卡在报警上。选分支后 commit 复位回 HEAD；
// 同时按分支刷新 commit 列表（origin/<branch>），让 commit 下拉跟着分支走。
$("#bt-branch").addEventListener("change", () => {
  const commit = $("#bt-commit");
  const branch = $("#bt-branch").value;
  if (branch) {
    if (commit) commit.value = "";  // 分支优先：commit 复位回「当前代码 (HEAD)」
    loadAutoCommits(branch);         // 该分支的提交填充 commit 下拉
  }
});
$("#bt-commit").addEventListener("change", () => {
  const branch = $("#bt-branch");
  if (branch && $("#bt-commit").value) branch.value = "";
});

// Content tabs: switch between 数据/队列 and 可视化/日志 in manual pane
function switchBtTab(key) {
  $$(".content-tab-btn").forEach((btn) => btn.classList.toggle("active", btn.dataset.btTab === key));
  $$(".content-pane").forEach((p) => p.classList.toggle("active", p.dataset.btPane === key));
}
$$(".content-tab-btn").forEach((btn) =>
  btn.addEventListener("click", () => switchBtTab(btn.dataset.btTab))
);

// ----- editable launch script (preview default before running, save override) -----
let LAUNCH_SCRIPT_OVERRIDE = "";  // non-empty → sent with /api/batch/start as override
let LAUNCH_SCRIPT_DEFAULT = "";    // last previewed default text (for "reset" button)
let LAUNCH_SCRIPT_SAVED_TEXT = ""; // text currently saved as override (mirrors LAUNCH_SCRIPT_OVERRIDE)
let LAUNCH_SCRIPT_SOURCE = "manual"; // which pane opened the modal: "manual" | "auto"
let AUTO_LAUNCH_TEMPLATE = "";      // persisted auto launch.sh template (mirrors /api/auto/config)

function launchSelForModal() {
  if (LAUNCH_SCRIPT_SOURCE === "auto") {
    return { ip: $("#auto-board-ip").value, datasets: Array.from(AUTO_DS_PICK), experiments: Array.from(AUTO_EXP_PICK) };
  }
  return { ip: $("#bt-board").value, datasets: getBtSelected(), experiments: Array.from(EXP_PICK) };
}
function offlineBagForModal(exp) {
  if (LAUNCH_SCRIPT_SOURCE === "auto") {
    return exp
      ? ((AUTO_EXP_LIST_CACHE.find((x) => x.name === exp) || {}).offline_bag !== false)
      : AUTO_BASELINE_OFFLINE;
  }
  return exp
    ? ((EXP_LIST_CACHE.find((x) => x.name === exp) || {}).offline_bag !== false)
    : BT_BASELINE_OFFLINE;
}
// auto tasks run with the server-side defaults (no verbosity UI in the auto pane)
function verbosityForModal() { return LAUNCH_SCRIPT_SOURCE === "auto" ? "INFO" : btVerbosity(); }
function vioLogLevelForModal() { return LAUNCH_SCRIPT_SOURCE === "auto" ? "warn" : btVioLogLevel(); }

async function openLaunchScriptModal(source) {
  LAUNCH_SCRIPT_SOURCE = source || "manual";
  const modal = $("#launch-script-modal");
  const ta = $("#launch-script-text");
  const status = $("#launch-script-status");
  const info = launchSelForModal();
  if (!info.ip) { popupAlert("先选择板子"); return; }
  const exp = info.experiments[0] || "";  // used only for a dataset-based default, if any
  const offlineBag = offlineBagForModal(exp);
  modal.classList.remove("hidden");

  if (LAUNCH_SCRIPT_SOURCE === "auto") {
    // load the persisted auto template if one is set
    let cur = "";
    try {
      const cfg = await api("/api/auto/config");
      cur = (cfg.launch_script_override || "").trim();
    } catch (e) { /* fall through to the dataset-free template */ }
    if (cur) {
      AUTO_LAUNCH_TEMPLATE = cur;
      ta.value = cur;
      status.textContent = "已加载自动回测 launch.sh 模板";
      updateLaunchScriptOverrideFlag();
      return;
    }
  } else if (LAUNCH_SCRIPT_OVERRIDE) {
    ta.value = LAUNCH_SCRIPT_SAVED_TEXT;
    status.textContent = "已加载保存的 override 脚本";
    updateLaunchScriptOverrideFlag();
    return;
  }

  // Default = dataset-free placeholder template (core launch logic; the dataset
  // rides on {{...}} tokens filled per-run). A selected dataset only optionally
  // seeds a concrete default; never required.
  if (info.datasets.length) {
    ta.value = "生成中…";
    status.textContent = "";
    try {
      const r = await api("/api/backtest/preview_script", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ip: info.ip, dataset: info.datasets[0], experiment: exp,
          offline_bag: offlineBag, verbosity: verbosityForModal(), vio_log_level: vioLogLevelForModal() }),
      });
      LAUNCH_SCRIPT_DEFAULT = r.text || "";
      ta.value = LAUNCH_SCRIPT_SOURCE === "auto"
        ? autoTemplateText(r)
        : LAUNCH_SCRIPT_DEFAULT;
      status.textContent = LAUNCH_SCRIPT_SOURCE === "auto"
        ? "已生成自动回测模板（{{run_dir}} / {{config_path}} / {{board_dataset}} 占位，作用于所有自动回测任务）"
        : `已生成默认脚本（config_path=${r.config_path}）`;
      updateLaunchScriptOverrideFlag();
      return;
    } catch (e) {
      ta.value = `（生成失败，改用数据集无关模板）${e.message}\n\n`;
    }
  }
  try {
    const r = await api("/api/backtest/default_template");
    LAUNCH_SCRIPT_DEFAULT = r.text || "";
    ta.value = LAUNCH_SCRIPT_SOURCE === "auto" ? autoTemplateText({ text: LAUNCH_SCRIPT_DEFAULT }) : LAUNCH_SCRIPT_DEFAULT;
    status.textContent = "数据集无关的 launch.sh 模板（{{dataset}}/{{board_dataset}}/{{config_path}}/{{run_dir}} 占位）";
  } catch (e) {
    ta.value = `（加载模板失败）${e.message}`;
  }
  updateLaunchScriptOverrideFlag();
}
// A reusable auto template must not pin this preview's run_dir/config_path/dataset
// path — rewrite the concrete per-run values into placeholders the scheduler
// fills per task (see backtest.start_backtest's {{token}} rendering).
function autoTemplateText(r) {
  let t = r.text || "";
  t = t.split(r.config_path).join("{{config_path}}")
       .split(r.board_dataset_path).join("{{board_dataset}}")
       .split(r.run_dir).join("{{run_dir}}");
  return t;
}
function updateLaunchScriptOverrideFlag() {
  const flag = $("#launch-script-override-flag");
  if (!flag) return;
  const has = LAUNCH_SCRIPT_SOURCE === "auto" ? !!AUTO_LAUNCH_TEMPLATE : !!LAUNCH_SCRIPT_OVERRIDE;
  flag.classList.toggle("hidden", !has);
}
$("#bt-launch-script").addEventListener("click", () => openLaunchScriptModal("manual"));
const _autoLaunchBtn = $("#auto-launch-script");
if (_autoLaunchBtn) _autoLaunchBtn.addEventListener("click", () => openLaunchScriptModal("auto"));
$("#launch-script-close").addEventListener("click", () => $("#launch-script-modal").classList.add("hidden"));
$("#launch-script-modal").addEventListener("click", (e) => {
  if (e.target.id === "launch-script-modal") $("#launch-script-modal").classList.add("hidden");
});

async function openResultModal(path) {
  if (!path) return popupAlert("无结果目录");
  const modal = $("#result-modal");
  const body = $("#result-modal-body");
  const pathEl = $("#result-path");
  pathEl.textContent = path;
  body.innerHTML = "<span class='hint'>加载中…</span>";
  modal.classList.remove("hidden");
  // result_dir is a filesystem path under the repo's results/ dir — map it to
  // the /results/ static mount so we can render the VIO artifacts directly
  const rel = String(path).replace(/\\/g, "/").split("/results/").pop();
  const base = "/results/" + rel;
  try {
    const stats = await api(`/api/results/stats?path=${encodeURIComponent(path)}`);
    const media = `<div class="result-media">` +
      `<img src="${base}/trajectory.png" title="轨迹图 (evo_traj)" onerror="this.classList.add('hidden')" alt="">` +
      `<img src="${base}/preview.jpg" title="首帧预览" onerror="this.classList.add('hidden')" alt="">` +
      `<video controls preload="metadata" src="${base}/video.mp4" title="ov_web 可视化录像" onerror="this.classList.add('hidden')"></video>` +
      `</div>`;
    const rows = Object.entries(stats).filter(([k]) => k !== "logs" && k !== "experiment_keys").map(([k, v]) => {
      let val = v;
      if (Array.isArray(v)) val = v.join(", ");
      else if (typeof v === "object") val = JSON.stringify(v);
      return `<tr><td class="hint">${escapeHtml(k)}</td><td>${escapeHtml(String(val))}</td></tr>`;
    }).join("");
    const logs = Array.isArray(stats.logs) ? stats.logs.map((l) => `<div class="hint">${escapeHtml(l)}</div>`).join("") : "";
    body.innerHTML = media + `<table class="table compact" style="margin:0">${rows || "<tr><td class='hint'>无统计</td></tr>"}</table>${logs ? `<div style="margin-top:8px"><div class="hint" style="margin-bottom:4px">日志文件：</div>${logs}</div>` : ""}`;
  } catch (e) { body.innerHTML = `<span class="hint" style="color:var(--bad)">${escapeHtml(e.message)}</span>`; }
}
$("#result-close").addEventListener("click", () => $("#result-modal").classList.add("hidden"));
$("#result-modal").addEventListener("click", (e) => {
  if (e.target.id === "result-modal") $("#result-modal").classList.add("hidden");
});

/* ---------------- 配置内容弹窗（实验片段 / 完整配置，一律只读） ---------------- */
async function openCfgViewModal(path, name, title = "") {
  const modal = $("#cfg-view-modal");
  $("#cfg-view-status").textContent = "";
  $("#cfg-view-title").textContent = title ||
    (name === "experiment.yaml" ? "实验片段 (experiment.yaml)（结果快照，只读）" : "完整配置 (estimator_config.yaml)（结果快照，只读）");
  $("#cfg-view-text").value = "加载中…";
  $("#cfg-view-path").textContent = path || "";
  modal.classList.remove("hidden");
  try {
    const r = await api(`/api/results/file?path=${encodeURIComponent(path)}&name=${encodeURIComponent(name)}`);
    $("#cfg-view-text").value = r.text || "(空文件)";
    $("#cfg-view-path").textContent = r.path || path;
  } catch (e) {
    $("#cfg-view-text").value = `读取失败：${e.message}`;
  }
}
$("#cfg-view-close").addEventListener("click", () => $("#cfg-view-modal").classList.add("hidden"));
$("#cfg-view-modal").addEventListener("click", (e) => {
  if (e.target.id === "cfg-view-modal") $("#cfg-view-modal").classList.add("hidden");
});
// Esc closes any open modal / popup-alert
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  document.querySelectorAll(".modal:not(.hidden)").forEach((m) => m.classList.add("hidden"));
  const vv = $("#video-view-player");
  if (vv && !vv.paused) { vv.pause(); vv.removeAttribute("src"); vv.load(); }
  const pa = $("#popup-alert");
  if (pa) pa.classList.add("hidden");
});
// 基线配置弹窗：每个数据集有自己的标定（内参/外参）与 estimator_config.yaml，
// 基线跑的就是数据集自带的那份 —— 只读展示当前选中数据集（第一个）的配置
async function openBaselineCfgModal(ds, total = 1) {
  const modal = $("#cfg-view-modal");
  $("#cfg-view-status").textContent = "";
  $("#cfg-view-title").textContent = `基线配置 · ${ds}（只读）`;
  $("#cfg-view-text").value = "加载中…";
  $("#cfg-view-path").textContent = `${ds}/stereo_auto_gen/estimator_config.yaml` +
    (total > 1 ? `（共选 ${total} 个数据集，各自用自己目录下的配置）` : "");
  modal.classList.remove("hidden");
  try {
    const r = await api(`/api/datasets/${encodeURIComponent(ds)}/config/estimator_config.yaml`);
    $("#cfg-view-text").value = r.text || "(空文件)";
  } catch (e) {
    $("#cfg-view-text").value = `读取失败：${e.message}`;
  }
}

// 在途行（回测未收集结果）：无结果快照可看，从实验定义实时预览
// part=config → 基线配置+实验片段的合并结果；part=diff → 实验片段本身
async function openLiveExpCfgModal(exp, ds, part) {
  const modal = $("#cfg-view-modal");
  $("#cfg-view-status").textContent = "";
  $("#cfg-view-title").textContent = part === "config"
    ? `完整配置 (config)：${exp}（实验定义，实时预览）`
    : `差异配置 (diff)：${exp}（实验定义，实时预览）`;
  $("#cfg-view-text").value = "加载中…";
  $("#cfg-view-path").textContent = part === "config"
    ? `experiments/${exp}.yaml 合并到 ${ds} 的基线配置（结果未收集，非快照）`
    : `experiments/${exp}.yaml（结果未收集，非快照）`;
  modal.classList.remove("hidden");
  try {
    const r = part === "config"
      ? await api(`/api/experiments/${encodeURIComponent(exp)}/merged?dataset=${encodeURIComponent(ds)}`)
      : await api(`/api/experiments/${encodeURIComponent(exp)}`);
    $("#cfg-view-text").value = r.text || "(空文件)";
  } catch (e) {
    $("#cfg-view-text").value = `读取失败：${e.message}`;
  }
}

// 异常详情弹窗：复用只读文本弹窗展示完整错误信息
function openErrorModal(title, text) {
  const modal = $("#cfg-view-modal");
  $("#cfg-view-status").textContent = "";
  $("#cfg-view-title").textContent = title;
  $("#cfg-view-text").value = text || "(无异常信息)";
  $("#cfg-view-path").textContent = "";
  modal.classList.remove("hidden");
}

/* ---------------- video 播放弹窗（只播不下载） ---------------- */
function openVideoModal(src, title) {
  const v = $("#video-view-player");
  $("#video-view-title").textContent = title || "video";
  v.src = src;
  $("#video-view-modal").classList.remove("hidden");
  v.play().catch(() => {});  // autoplay may be blocked; user can press play
}
function closeVideoModal() {
  const v = $("#video-view-player");
  v.pause();
  v.removeAttribute("src");
  v.load();
  $("#video-view-modal").classList.add("hidden");
}
$("#video-view-close").addEventListener("click", closeVideoModal);
$("#video-view-modal").addEventListener("click", (e) => {
  if (e.target.id === "video-view-modal") closeVideoModal();
});

/* ---------------- 图片查看弹窗（轨迹图/预览图，页内展示，ESC 关闭） ---------------- */
function openImgModal(src, title) {
  $("#img-view-title").textContent = title || "图片";
  $("#img-view-img").src = src;
  $("#img-view-modal").classList.remove("hidden");
}
function closeImgModal() {
  $("#img-view-img").src = "";
  $("#img-view-modal").classList.add("hidden");
}
$("#img-view-close").addEventListener("click", closeImgModal);
$("#img-view-modal").addEventListener("click", (e) => {
  if (e.target.id === "img-view-modal") closeImgModal();
});

/* ---------------- 删除结果（永久删除，不可恢复） ---------------- */
async function deleteResult(path) {
  if (!path) return;
  if (!confirm(`永久删除该结果？\n${path}\n\n结果及其文件都会被删除，无法恢复。`)) return;
  try {
    await api("/api/results", {
      method: "DELETE", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    loadStats();
  } catch (e) { popupAlert(`删除失败：${e.message}`); }
}
// 任务队列的「删除」：仅把该行从队列里移除，结果保留在「统计」（不删文件）。
// 统计页的删除才真正删结果。
async function deleteFromQueue(path) {
  if (!path) return;
  if (!confirm(`从任务队列移除该结果？\n${path}\n\n仅从队列移除，结果保留在「统计」可随时查看/删除。`)) return;
  try {
    await api("/api/batch/queue", {
      method: "DELETE", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    pollBatch();
  } catch (e) { popupAlert(`移除失败：${e.message}`); }
}
// 在途/失败行（已结束批次、无结果目录）：丢弃该记录，行即消失
async function discardLiveItem(btn) {
  const { batch: batchId, exp, ds } = btn.dataset;
  if (!batchId) return;
  const label = `${exp || "基线"} · ${ds}`;
  if (!confirm(`移除该在途/失败记录？\n${label}\n\n（尚未收集到结果，仅丢弃该记录，不影响其它结果）`)) return;
  try {
    await api(`/api/batch/${batchId}/item`, {
      method: "DELETE", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ experiment: exp || "", dataset: ds }),
    });
    loadStats();
  } catch (e) { popupAlert(`移除失败：${e.message}`); }
}
$("#launch-script-reset").addEventListener("click", async () => {
  const info = launchSelForModal();
  if (!info.ip) { popupAlert("先选择板子"); return; }
  if (!info.datasets.length) { popupAlert("先勾选一个数据集"); return; }
  const exp = info.experiments[0] || "";
  const offlineBag = offlineBagForModal(exp);
  const ta = $("#launch-script-text");
  const status = $("#launch-script-status");
  ta.value = "重新生成中…";
  status.textContent = "";
  try {
    const r = await api("/api/backtest/preview_script", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ip: info.ip, dataset: info.datasets[0], experiment: exp, offline_bag: offlineBag,
        verbosity: verbosityForModal(), vio_log_level: vioLogLevelForModal() }),
    });
    LAUNCH_SCRIPT_DEFAULT = r.text || "";
    ta.value = LAUNCH_SCRIPT_SOURCE === "auto" ? autoTemplateText(r) : LAUNCH_SCRIPT_DEFAULT;
    status.textContent = LAUNCH_SCRIPT_SOURCE === "auto"
      ? "已重置为默认模板（点「保存」后作用于自动回测任务）"
      : "已重置为默认脚本（点「保存」才会作为 override 使用）";
  } catch (e) {
    ta.value = `（生成失败）${e.message}`;
  }
});
$("#launch-script-save").addEventListener("click", async () => {
  const ta = $("#launch-script-text");
  const status = $("#launch-script-status");
  const text = ta.value;
  if (!text || text.startsWith("（") || text.startsWith("生成中") || text.startsWith("重新生成中")) {
    status.textContent = "脚本为空或正在生成，无法保存";
    return;
  }
  if (LAUNCH_SCRIPT_SOURCE === "auto") {
    try {
      await api("/api/auto/config", { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ launch_script_override: text }) });
      AUTO_LAUNCH_TEMPLATE = text;
      status.textContent = "已保存为自动回测模板（作用于所有自动回测任务）";
      updateLaunchScriptOverrideFlag();
    } catch (e) {
      status.textContent = `保存失败：${e.message}`;
    }
    return;
  }
  LAUNCH_SCRIPT_OVERRIDE = text;
  LAUNCH_SCRIPT_SAVED_TEXT = text;
  status.textContent = "已保存为 override，下次「启动回测」将上传此脚本到板子";
  updateLaunchScriptOverrideFlag();
});

/* =============================== 统计 =============================== */
const ST_COLLAPSED = new Set();  // collapsed group keys, persist across re-renders
// 表格组（编号组，depth>=1）默认折叠，展开过的记在这里（会话内保持）
const ST_EXPANDED = new Set();
const ST_TYPE_ORDER = { manual: 0, daily: 1, commit: 2 };
const ST_TYPE_LABEL = { manual: "手动回测", daily: "定时回测", commit: "commit 回测" };

function _stRdir(r) {
  const rel = String(r.dir || "").replace(/\\/g, "/").split("/results/").pop();
  return escapeHtml("/results/" + rel);
}

function statRowHtml(r, idx) {
  const meta = r.meta || r; // _meta.json fields merged
  const stats = meta.stats || {};
  const rdir = _stRdir(r);
  const end = stats.end ? `[${stats.end.join(", ")}]` : "-";
  // 漂移 = 终点坐标与零点的 2D 距离（假设 Z=0）
  const drift = Array.isArray(stats.end) && stats.end.length >= 2
    ? Math.hypot(Number(stats.end[0]) || 0, Number(stats.end[1]) || 0).toFixed(2) : null;
  const expName = r.experiment ? escapeHtml(r.experiment) : "基线";
  // 实验组名可点：基线 → 基线配置弹窗（只读）；实验 → 结果快照（只读）；
  // 在途行（live，结果未收集）→ 从实验定义实时预览
  const liveExpLink = (part, label, bold = false) =>
    `<a class="clickable cfg-live-exp" data-exp="${escapeHtml(r.experiment)}" data-ds="${escapeHtml(r.dataset || "")}" data-part="${part}" title="查看${part === "config" ? "完整" : "差异"}配置（实验定义，实时预览）">${bold ? `<b>${label}</b>` : label}</a>`;
  const expNameHtml = r.experiment
    ? (r.live
        ? liveExpLink("diff", expName, true)
        : `<a class="clickable cfg-view" data-path="${escapeHtml(r.dir)}" data-name="experiment.yaml" data-title="差异配置 (diff)：${expName}（结果快照，只读）" title="查看差异配置（结果快照，只读）"><b>${expName}</b></a>`)
    : `<a class="clickable cfg-edit-ds" data-ds="${escapeHtml(r.dataset || "")}" title="查看基线实际生效的配置（只读）"><b>${expName}</b></a>`;
  const status = meta.status || "-";
  const stCls = status === "done" ? "st-done" : (status === "failed" ? "st-failed" : (status === "running" || status === "pending" ? "st-running" : ""));
  const errTitle = meta.error ? ` title="${escapeHtml(meta.error)}"` : "";
  // 带异常的状态可点开查看异常详情（弹窗）；无异常只显示状态
  const statusCell = meta.error
    ? `<a class="clickable st-err-open" data-err="${escapeHtml(meta.error)}" data-exp="${expName}" data-ds="${escapeHtml(r.dataset || "")}" title="点击查看异常详情"><span class="${stCls}">${escapeHtml(status)}</span> ⚠</a>`
    : `<span class="${stCls}">${escapeHtml(status)}</span>`;
  // 缩略图/轨迹图：行内小图，点击弹窗放大
  const thumb = r.has_preview
    ? `<a class="clickable img-open" data-src="${rdir}/preview.jpg" data-title="数据集首帧预览 · ${escapeHtml(r.dataset || "")}" title="数据集首帧预览，点击放大"><img class="st-ds-preview" src="${rdir}/preview.jpg" alt=""></a>`
    : `<span class="hint">—</span>`;
  const traj = r.has_trajectory
    ? `<a class="clickable img-open" data-src="${rdir}/trajectory.png" data-title="轨迹图 (evo_traj) · ${expName}" title="轨迹图 (evo_traj)，点击放大"><img class="st-traj" src="${rdir}/trajectory.png" alt=""></a>` : "-";
  const time = stats.vio_time_avg_ms != null
    ? `<b>${stats.vio_time_avg_ms}</b> / <b>${stats.vio_time_max_ms}</b> ms` : "-";
  // 日志与 video 一律弹窗展示，不提供文件下载
  const logLinks = ["vio.log", "ov_web.log", "tf.log", "bag.log"]
    .filter((n) => r["has_" + n.replace(".log", "_log")])
    .map((n) => `<a class="clickable cfg-view" data-path="${escapeHtml(r.dir)}" data-name="${n}" data-title="日志：${n}（只读）">${n.replace(".log", "")}</a>`).join(" ");
  const videoLink = stats.video_mp4 ? `<a class="clickable video-view" data-src="${rdir}/video.mp4" data-title="video · ${expName}">video</a>` : "";
  const logs = [logLinks, videoLink].filter(Boolean).join(" ") || "-";
  // live=回测启动后尚未收集结果的在途行：运行中批次的项不可动；已结束批次
  // 的失败/跳过项可勾选、可「移除记录」（丢弃 batch.json 条目，无结果目录）
  const discardableLive = r.live && r.batch_status && r.batch_status !== "running";
  const livePickAttrs = `data-batch="${escapeHtml(r.batch_id || "")}" data-exp="${escapeHtml(r.experiment || "")}" data-ds="${escapeHtml(r.dataset || "")}"`;
  const pickCell = r.live
    ? (discardableLive ? `<input type="checkbox" class="st-pick st-pick-live" ${livePickAttrs}>` : "")
    : `<input type="checkbox" class="st-pick" data-path="${escapeHtml(r.dir)}">`;
  const delCell = r.live
    ? (discardableLive
        ? `<button class="tiny st-del-live" ${livePickAttrs} title="移除该在途/失败记录（无结果目录，直接丢弃）">移除</button>`
        : `<span class="hint">—</span>`)
    : `<button class="tiny st-del" data-path="${escapeHtml(r.dir)}" title="永久删除该结果">删除</button>`;
  return `<tr${errTitle}>
    <td class="st-pick-cell">${pickCell}<span class="st-idx">${idx}</span></td>
    <td class="st-ds" title="${escapeHtml(r.dataset || "")}">${escapeHtml(r.dataset || "-")}</td>
    <td>${expNameHtml}</td>
    <td class="st-ds-thumb">${thumb}</td>
    <td>${stats.path_len_m ?? "-"} m</td>
    <td class="mono">${end}</td>
    <td class="mono">${drift ?? "-"}${drift != null ? " m" : ""}</td>
    <td>${traj}</td>
    <td>${time}</td>
    <td>${logs}</td>
    <td>${statusCell}</td>
    <td>${delCell}</td>
  </tr>`;
}

// 一个编号一张表：所有数据集/实验组的行平铺，行内自带数据集与实验组列
function statTableHtml(rs) {
  const sorted = rs.slice()
    .sort((a, b) => String(a.dataset || "").localeCompare(String(b.dataset || "")) ||
                    String(a.experiment || "").localeCompare(String(b.experiment || "")));
  const rows = sorted.map((r, i) => statRowHtml(r, i + 1)).join("");
  return `<table class="st-table">
    <thead><tr>
      <th></th><th>数据集</th><th>实验组</th><th>缩略图</th><th>路程</th><th>终点坐标</th>
      <th title="终点坐标与零点的 2D 距离（假设 Z=0）">漂移</th>
      <th>evo_traj 轨迹图</th><th>耗时 avg/max</th><th>日志</th><th>状态</th><th>操作</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function stGroupHtml(key, depth, label, count, inner) {
  // 表格组（编号组，depth>=1）默认折叠；类型组（depth 0）默认展开
  const collapsed = depth >= 1 ? !ST_EXPANDED.has(key) : ST_COLLAPSED.has(key);
  return `<div class="st-group" data-depth="${depth}">
    <div class="st-ghead" data-key="${escapeHtml(key)}">
      <span class="group-caret">${collapsed ? "▸" : "▾"}</span>
      <input type="checkbox" class="st-gcheck" title="全选/清空本组">
      <b>${escapeHtml(label)}</b> <span class="hint">${count} 条</span>
    </div>
    <div class="st-children${collapsed ? " hidden" : ""}">${inner}</div>
  </div>`;
}

function stRefreshGroupChecks() {
  const box = $("#stats-list");
  // deepest groups first so parents see settled child states
  const groups = Array.from(box.querySelectorAll(".st-group"))
    .sort((a, b) => (+b.dataset.depth) - (+a.dataset.depth));
  for (const g of groups) {
    const picks = g.querySelectorAll(".st-pick");
    const n = g.querySelectorAll(".st-pick:checked").length;
    const gc = g.querySelector(":scope > .st-ghead .st-gcheck");
    if (gc) { gc.checked = n > 0 && n === picks.length; gc.indeterminate = n > 0 && n < picks.length; }
  }
  const total = box.querySelectorAll(".st-pick:checked").length;
  const cnt = $("#st-sel-count");
  if (cnt) cnt.textContent = total;
}

function renderStatsTree(filtered) {
  // 回测类型 → 编号 → 一张平铺表（数据集/实验组作为行内列）
  const tree = new Map();
  for (const r of filtered) {
    const type = r.type || (r.commit_short ? "commit" : "manual");
    const no = r.run_no || "(未编号)";
    if (!tree.has(type)) tree.set(type, new Map());
    if (!tree.get(type).has(no)) tree.get(type).set(no, []);
    tree.get(type).get(no).push(r);
  }
  const types = Array.from(tree.keys()).sort((a, b) => (ST_TYPE_ORDER[a] ?? 9) - (ST_TYPE_ORDER[b] ?? 9));
  const noKey = (n) => parseInt(String(n).replace(/\D/g, ""), 10) || 0;
  const parts = [];
  for (const t of types) {
    const nos = tree.get(t);
    let tCount = 0;
    const nParts = [];
    for (const [no, rs] of Array.from(nos.entries()).sort((a, b) => noKey(a[0]) - noKey(b[0]))) {
      tCount += rs.length;
      nParts.push(stGroupHtml(`${t}/${no}`, 1, no, rs.length, statTableHtml(rs)));
    }
    parts.push(stGroupHtml(t, 0, ST_TYPE_LABEL[t] || t, tCount, nParts.join("")));
  }
  return parts.join("");
}

async function loadStats() {
  const box = $("#stats-list");
  box.innerHTML = '<p class="hint">加载中…</p>';
  try {
    const rs = await api("/api/results");
    if (!rs.length) { box.innerHTML = '<p class="hint">暂无结果。到「回测」Tab 启动批量回测，结束后这里会显示统计数据。</p>'; return; }
    // populate filter dropdowns from the data
    const commits = new Set();
    const datasets = new Set();
    const exps = new Set();
    for (const r of rs) {
      if (r.commit_short) commits.add(r.commit_short);
      if (r.dataset) datasets.add(r.dataset);
      if (r.experiment) exps.add(r.experiment);
    }
    const fillFilter = (sel, items) => {
      const cur = sel.value;
      sel.innerHTML = `<option value="">全部</option>` + Array.from(items).sort().map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
      if (cur) sel.value = cur;
    };
    fillFilter($("#st-filter-commit"), commits);
    fillFilter($("#st-filter-dataset"), datasets);
    fillFilter($("#st-filter-experiment"), exps);
    // apply filters
    const fs = $("#st-filter-source").value;
    const fc = $("#st-filter-commit").value;
    const fd = $("#st-filter-dataset").value;
    const fe = $("#st-filter-experiment").value;
    const fst = $("#st-filter-status").value;
    const filtered = rs.filter((r) => {
      const rtype = r.type || (r.commit_short ? "commit" : "manual");
      return (!fs || rtype === fs) &&
        (!fc || r.commit_short === fc) &&
        (!fd || r.dataset === fd) &&
        (!fe || (r.experiment || "") === fe) &&
        (!fst || (r.status || "") === fst);
    });
    if (filtered.length === 0) {
      box.innerHTML = `<p class="hint">无匹配结果（共 ${rs.length} 条，筛掉 ${rs.length} 条）</p>`;
      stRefreshGroupChecks();
      return;
    }
    box.innerHTML = `<p class="hint">显示 ${filtered.length} / ${rs.length} 条</p>` + renderStatsTree(filtered);
    // wire collapse carets
    box.querySelectorAll(".st-ghead").forEach((h) => {
      h.addEventListener("click", (e) => {
        if (e.target.classList.contains("st-gcheck")) return;  // checkbox has own handler
        const key = h.dataset.key;
        const children = h.nextElementSibling;
        const isTableGroup = +(h.parentElement.dataset.depth) >= 1;
        let collapsed;
        if (isTableGroup) {
          // 表格组默认折叠：展开状态记在 ST_EXPANDED
          collapsed = ST_EXPANDED.has(key);  // 当前展开 → 点击后折叠
          if (collapsed) ST_EXPANDED.delete(key); else ST_EXPANDED.add(key);
          children.classList.toggle("hidden", collapsed);
        } else {
          collapsed = children.classList.toggle("hidden");
          if (collapsed) ST_COLLAPSED.add(key); else ST_COLLAPSED.delete(key);
        }
        h.querySelector(".group-caret").textContent = collapsed ? "▸" : "▾";
      });
    });
    // wire group checkboxes: cascade to all descendant leaf picks
    box.querySelectorAll(".st-gcheck").forEach((gc) => {
      gc.addEventListener("change", () => {
        const g = gc.closest(".st-group");
        g.querySelectorAll(".st-pick").forEach((p) => { p.checked = gc.checked; });
        stRefreshGroupChecks();
      });
    });
    box.querySelectorAll(".st-pick").forEach((p) =>
      p.addEventListener("change", stRefreshGroupChecks));
    // config / diff / log content popup (read-only, no file downloads)
    box.querySelectorAll(".cfg-view").forEach((a) =>
      a.addEventListener("click", (e) => {
        e.preventDefault();
        openCfgViewModal(a.dataset.path, a.dataset.name, a.dataset.title || "");
      }));
    // video popup (play in-page, no file downloads)
    box.querySelectorAll(".video-view").forEach((a) =>
      a.addEventListener("click", (e) => {
        e.preventDefault();
        openVideoModal(a.dataset.src, a.dataset.title || "");
      }));
    // image popup (trajectory / preview shown in-page, ESC to close)
    box.querySelectorAll(".img-open").forEach((a) =>
      a.addEventListener("click", (e) => {
        e.preventDefault();
        openImgModal(a.dataset.src, a.dataset.title || "");
      }));
    // baseline links: read-only modal bound to that row's dataset
    box.querySelectorAll(".cfg-edit-ds").forEach((a) =>
      a.addEventListener("click", (e) => {
        e.preventDefault();
        if (a.dataset.ds) openBaselineCfgModal(a.dataset.ds, 1);
      }));
    // in-flight rows (结果未收集): live preview from the experiment definition
    box.querySelectorAll(".cfg-live-exp").forEach((a) =>
      a.addEventListener("click", (e) => {
        e.preventDefault();
        openLiveExpCfgModal(a.dataset.exp, a.dataset.ds, a.dataset.part || "diff");
      }));
    // 状态带 ⚠：点开查看完整异常详情
    box.querySelectorAll(".st-err-open").forEach((a) =>
      a.addEventListener("click", (e) => {
        e.preventDefault();
        openErrorModal(`异常详情 · ${a.dataset.exp || "—"} · ${a.dataset.ds || ""}`, a.dataset.err);
      }));
    // per-row delete button
    box.querySelectorAll(".st-del").forEach((b) =>
      b.addEventListener("click", () => deleteResult(b.dataset.path)));
    // live rows of finished batches (failed/skipped, no result dir): discard record
    box.querySelectorAll(".st-del-live").forEach((b) =>
      b.addEventListener("click", () => discardLiveItem(b)));
    stRefreshGroupChecks();
  } catch (e) { box.innerHTML = `<p class="hint" style="color:var(--bad)">${escapeHtml(e.message)}</p>`; }
}

$("#st-refresh").addEventListener("click", loadStats);
["#st-filter-source", "#st-filter-commit", "#st-filter-dataset",
 "#st-filter-experiment", "#st-filter-status"].forEach((sel) =>
  $(sel).addEventListener("change", loadStats));
$("#st-filter-clear").addEventListener("click", () => {
  ["#st-filter-source", "#st-filter-commit", "#st-filter-dataset",
   "#st-filter-experiment", "#st-filter-status"].forEach((sel) => { $(sel).value = ""; });
  loadStats();
});
$("#st-sel-all").addEventListener("click", () => {
  $$("#stats-list .st-pick").forEach((p) => { p.checked = true; });
  stRefreshGroupChecks();
});
$("#st-sel-none").addEventListener("click", () => {
  $$("#stats-list .st-pick").forEach((p) => { p.checked = false; });
  stRefreshGroupChecks();
});
async function stExportReport(fmt) {
  // 在途/失败行（无结果目录）不参与报告导出
  const paths = $$("#stats-list .st-pick:checked").filter((p) => p.dataset.path).map((p) => p.dataset.path);
  if (!paths.length) { popupAlert("先勾选要导出的结果"); return; }
  const btn = $(fmt === "pdf" ? "#st-report-pdf" : "#st-report-html");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "生成中…";
  try {
    const r = await api("/api/stats/report", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths, format: fmt }),
    });
    window.open(r.url, "_blank");
  } catch (e) { popupAlert(`报告生成失败：${e.message}`); }
  btn.disabled = false;
  btn.textContent = label;
}
$("#st-report-html").addEventListener("click", () => stExportReport("html"));
$("#st-report-pdf").addEventListener("click", () => stExportReport("pdf"));

/* 批量删除勾选的结果：已收集结果永久删除；在途/失败记录（无结果目录）直接丢弃记录 */
$("#st-del-sel").addEventListener("click", async () => {
  const checked = $$("#stats-list .st-pick:checked");
  const paths = checked.filter((p) => p.dataset.path).map((p) => p.dataset.path);
  const lives = checked.filter((p) => !p.dataset.path && p.dataset.batch)
    .map((p) => ({ batch: p.dataset.batch, experiment: p.dataset.exp || "", dataset: p.dataset.ds }));
  const total = paths.length + lives.length;
  if (!total) { popupAlert("先勾选要删除的结果"); return; }
  const parts = [];
  if (paths.length) parts.push(`${paths.length} 条已收集结果（永久删除，不可恢复）`);
  if (lives.length) parts.push(`${lives.length} 条在途/失败记录（直接丢弃）`);
  if (!confirm(`删除选中的 ${total} 条？\n${parts.join("；")}`)) return;
  const btn = $("#st-del-sel");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "删除中…";
  const errs = [];
  for (const path of paths) {
    try {
      await api("/api/results", {
        method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
    } catch (e) { errs.push(`${path.split("/").filter(Boolean).pop()}: ${e.message}`); }
  }
  for (const lv of lives) {
    try {
      await api(`/api/batch/${lv.batch}/item`, {
        method: "DELETE", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ experiment: lv.experiment, dataset: lv.dataset }),
      });
    } catch (e) { errs.push(`${lv.batch} · ${lv.dataset}: ${e.message}`); }
  }
  btn.disabled = false;
  btn.textContent = label;
  loadStats();
  if (errs.length) popupAlert(`部分删除失败（${errs.length}/${total}）：\n${errs.join("\n")}`);
});

/* render backtest dataset list whenever datasets reload */
const _origLoadDatasets = loadDatasets;
loadDatasets = async function (refresh = false) {
  await _origLoadDatasets(refresh);
  renderBtDsList();
  maybeRefreshIdleTable();
};
// the boot loadDatasets() ran before this wrapper existed — paint the list
// as soon as it settles so the 回测 tab isn't empty on first switch
_bootDsPromise.then(() => { renderBtDsList(); maybeRefreshIdleTable(); });
// surface mirror/clone failures early so the branch dropdowns aren't silently empty
refreshAllBranchSelects();
