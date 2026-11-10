/* BatchCut Studio · 前端交互(vanilla JS,零依赖) */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  tasks: new Map(),        // index -> task
  filter: "all",
  stage: "idle",
  counts: {},
  total: 0,
  stopArmed: false,        // 两段式停止:第一次点=温和,再点=强制
  previewTask: null,
  previewPane: "source",
  RENDER_CAP: 600,
};

/* ============================================================
   工具
   ============================================================ */

const fmtETA = (s) => {
  if (s == null) return "—";
  s = Math.round(s);
  return s >= 60 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${s} 秒`;
};

const STATUS_TEXT = { pending: "待执行", running: "运行中", done: "完成", skip: "跳过", fail: "失败", cancel: "已取消" };
const STAGE_TEXT = { idle: "待命", running: "运行中", stopping: "停止中", done: "已完成" };

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function logLine(message, kind = "") {
  const box = $("log-lines");
  const el = document.createElement("div");
  el.className = `log-line ${kind}`;
  el.textContent = message;
  box.appendChild(el);
  const n = box.children.length;
  $("log-count").textContent = n;
  if (n > 500) box.removeChild(box.firstChild);
  if (!$("log-body").hidden) box.scrollTop = box.scrollHeight;
}

function classifyLog(message) {
  if (/失败|错误|不存在|Cannot|错误码|exited/.test(message)) return "bad";
  if (/完成|开始|硬件编码器/.test(message)) return "ok";
  if (/\[停止\]|\[强制\]|不再派新任务/.test(message)) return "sys";
  return "dim";
}

/* ============================================================
   配置面板
   ============================================================ */

function fillConfig(config) {
  $("cfg-raw-dir").value = config.rawDir || "";
  $("cfg-output-dir").value = config.outputDir || "";
  $("cfg-csv-path").value = config.csvPath || "";
  $("cfg-ffmpeg").value = config.ffmpeg || "";
  $("cfg-workers").value = config.workers ?? 3;
  $("cfg-crf").value = config.crf ?? 18;
  $("cfg-hw").value = config.hwEncoder || "auto";
  $("cfg-overwrite").checked = !!config.overwrite;
  $("cfg-template").value = config.outputTemplate || "{source}_{index}_{start}";
  setMode(config.mode || "smart");
}

function readConfig() {
  return {
    rawDir: $("cfg-raw-dir").value.trim(),
    outputDir: $("cfg-output-dir").value.trim(),
    csvPath: $("cfg-csv-path").value.trim(),
    ffmpeg: $("cfg-ffmpeg").value.trim(),
    workers: parseInt($("cfg-workers").value, 10) || 3,
    crf: parseInt($("cfg-crf").value, 10) || 18,
    hwEncoder: $("cfg-hw").value,
    overwrite: $("cfg-overwrite").checked,
    outputTemplate: $("cfg-template").value.trim() || "{source}_{index}_{start}",
    mode: state.mode || "smart",
  };
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll("#mode-seg button").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
  });
  const encode = mode === "encode";
  $("crf-field").style.display = encode ? "" : "none";
  $("hw-field").style.display = encode ? "" : "none";
}

/* ============================================================
   任务表渲染
   ============================================================ */

function visibleTasks() {
  const all = [...state.tasks.values()].sort((a, b) => a.index - b.index);
  const list = state.filter === "all" ? all : all.filter((t) => t.status === state.filter);
  return { list, capped: all.length > state.RENDER_CAP && list.length > state.RENDER_CAP, total: all.length };
}

function renderTable() {
  const { list, capped, total } = visibleTasks();
  const body = $("task-body");
  const existing = new Map([...body.children].map((tr) => [tr.dataset.index, tr]));
  const keep = new Set();

  list.slice(0, state.RENDER_CAP).forEach((t) => {
    keep.add(String(t.index));
    let tr = existing.get(String(t.index));
    if (!tr) {
      tr = document.createElement("tr");
      tr.dataset.index = t.index;
      tr.innerHTML = `
        <td class="cell-idx"></td>
        <td><span class="status-chip"></span></td>
        <td class="cell-path"></td>
        <td class="cell-time"></td>
        <td class="cell-path"></td>
        <td><div class="task-progress">
          <div class="task-progress-track"><div class="task-progress-fill"></div></div>
          <span class="task-progress-text"></span>
        </div></td>
        <td class="col-act">
          <button class="btn-pencil" title="编辑任务(写回 CSV)">
            <svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.8 9.94l-3.75-3.75L3 17.25zM20.7 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z" fill="currentColor"/></svg>
          </button><button class="btn-eye" title="预览">
          <svg viewBox="0 0 24 24"><path d="M12 5c-5 0-9 4.5-10 7 1 2.5 5 7 10 7s9-4.5 10-7c-1-2.5-5-7-10-7zm0 11a4 4 0 1 1 0-8 4 4 0 0 1 0 8zm0-1.6a2.4 2.4 0 1 0 0-4.8 2.4 2.4 0 0 0 0 4.8z" fill="currentColor"/></svg>
        </button></td>`;
      body.appendChild(tr);
      tr.querySelector(".btn-eye").addEventListener("click", () => openPreview(t.index));
      tr.querySelector(".btn-pencil").addEventListener("click", () => openEditor(t.index));
      existing.delete(String(t.index));
    }
    fillRow(tr, t);
  });
  existing.forEach((tr) => tr.remove());

  $("table-empty").hidden = total > 0;
  const note = $("table-note");
  note.hidden = !(capped || list.length > state.RENDER_CAP);
  if (!note.hidden) {
    note.textContent = `仅渲染前 ${state.RENDER_CAP} 行(共 ${list.length} 条显示中 / ${total} 条任务),筛选或导出可查看其余`;
  }
}

function fillRow(tr, t) {
  tr.querySelector(".cell-idx").textContent = t.index;
  tr.className = t.status === "fail" ? "is-fail" : t.problem ? "is-problem" : "";

  const chip = tr.querySelector(".status-chip");
  chip.className = `status-chip status-${t.status}`;
  chip.textContent = STATUS_TEXT[t.status] || t.status;

  const srcTd = tr.querySelector("td:nth-child(3)");
  const dur = t.sourceDuration ? ` · ${t.sourceDuration.toFixed(0)}s` : "";
  srcTd.innerHTML = "";
  const srcName = document.createElement("span");
  srcName.textContent = t.source || "—";
  srcName.title = t.source;
  srcTd.appendChild(srcName);
  if (t.problem) {
    const p = document.createElement("span");
    p.className = "problem-msg";
    p.textContent = `⚠ ${t.problem}`;
    srcTd.appendChild(p);
  } else if (t.error) {
    const e = document.createElement("span");
    e.className = "fail-msg";
    e.textContent = t.error;
    srcTd.appendChild(e);
  }
  const sub = document.createElement("span");
  sub.className = "sub";
  sub.textContent = dur ? `时长${dur.slice(2)}` : "";
  if (sub.textContent) srcTd.appendChild(sub);

  tr.querySelector("td:nth-child(4)").textContent = `${t.start} → ${t.end}`;

  const outTd = tr.querySelectorAll("td.cell-path")[1];
  outTd.textContent = t.output || "—";
  outTd.title = t.output || "";
  const meta = [];
  if (t.status === "done") meta.push(`${t.output_mb.toFixed(1)} MB · ${t.seconds.toFixed(1)}s`);
  if (t.status === "skip") meta.push("已存在,跳过");
  if (t.speed && t.status === "running") meta.push(t.speed);
  if (meta.length) {
    const m = document.createElement("span");
    m.className = "sub";
    m.textContent = meta.join(" · ");
    outTd.appendChild(m);
  }

  const pct = Math.round((t.progress || 0) * 100);
  const fill = tr.querySelector(".task-progress-fill");
  fill.style.width = `${t.status === "done" ? 100 : pct}%`;
  fill.classList.toggle("running", t.status === "running");
  tr.querySelector(".task-progress-text").textContent =
    t.status === "running" ? `${pct}%${t.speed ? " " + t.speed : ""}` : STATUS_TEXT[t.status] || "";

  const eye = tr.querySelector(".btn-eye");
  const srcOk = t.sourceExists !== false && !!t.source;
  const outOk = t.status === "done" || t.status === "skip";
  eye.disabled = !(srcOk || outOk);
  eye.title = outOk ? "预览成品 / 源片" : srcOk ? "预览源片(定位时间段)" : "无可预览内容";
}

function renderStats() {
  const c = state.counts || {};
  $("st-total").textContent = state.total || "—";
  $("st-done").textContent = c.done ?? 0;
  $("st-run").textContent = c.running ?? 0;
  $("st-skip").textContent = c.skip ?? 0;
  $("st-fail").textContent = c.fail ?? 0;
  $("st-eta").textContent = state.eta != null ? fmtETA(state.eta) : "—";

  const finished = (c.done ?? 0) + (c.skip ?? 0) + (c.fail ?? 0) + (c.cancel ?? 0);
  const pct = state.total ? Math.round((finished / state.total) * 100) : 0;
  $("global-progress").hidden = !state.total;
  $("global-progress-fill").style.width = `${pct}%`;
  $("global-progress-text").textContent = `${pct}% · ${finished}/${state.total}`;

  $("btn-retry").hidden = !(c.fail > 0 && state.stage !== "running");
  $("btn-report").hidden = !(c.fail > 0);
}

function renderStage() {
  const badge = $("stage-badge");
  badge.className = `stage-badge stage-${state.stage === "running" ? "running" : state.stage === "done" ? "done" : "idle"}`;
  badge.textContent = STAGE_TEXT[state.stage] || state.stage;

  const btn = $("btn-main");
  if (state.stage === "running") {
    btn.className = `btn ${state.stopArmed ? "stop" : "primary"}`;
    btn.disabled = false;
    btn.innerHTML = state.stopArmed
      ? `<svg viewBox="0 0 24 24"><path d="M8 5.5v13l11-6.5-11-6.5z" fill="currentColor" transform="rotate(180 12 12)"/></svg> 再点一次 = 强制终止`
      : `<svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor"/></svg> 停止(温和)`;
  } else {
    state.stopArmed = false;
    btn.className = "btn primary";
    btn.disabled = state.total === 0;
    btn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M8 5.5v13l11-6.5-11-6.5z" fill="currentColor"/></svg> 开始执行`;
  }
  $("btn-load").disabled = state.stage === "running";
  $("btn-validate").disabled = state.stage === "running";
}

/* ============================================================
   SSE 事件流
   ============================================================ */

function applySnapshot(snap) {
  state.tasks = new Map(snap.tasks.map((t) => [t.index, t]));
  state.counts = snap.counts;
  state.total = snap.tasks.length;
  state.stage = snap.stage;
  fillConfig(snap.config);
  renderTable();
  renderStats();
  renderStage();
}

function handleEvent(ev) {
  if (ev.type === "snapshot") return applySnapshot(ev.state);
  if (ev.type === "task") {
    const t = state.tasks.get(ev.index);
    if (t) {
      Object.assign(t, ev.patch);
      const { list } = visibleTasks();
      if (list.some((x) => x.index === ev.index)) {
        const tr = $("task-body").querySelector(`tr[data-index="${ev.index}"]`);
        if (tr) fillRow(tr, t);
      }
    }
    return;
  }
  if (ev.type === "stats") {
    state.counts = ev.counts;
    state.total = ev.total;
    state.eta = ev.eta;
    renderStats();
    return;
  }
  if (ev.type === "log") return logLine(ev.message, classifyLog(ev.message));
  if (ev.type === "stage") {
    state.stage = ev.stage;
    if (ev.stage !== "running") state.stopArmed = false;
    renderStage();
    return;
  }
  if (ev.type === "done") {
    const s = ev.summary || {};
    const msg = `结束:成功 ${s.done ?? 0} · 跳过 ${s.skip ?? 0} · 失败 ${s.fail ?? 0} · 用时 ${s.elapsed ?? 0}s`;
    toast(msg, s.fail > 0 ? "bad" : "ok");
    logLine(msg, s.fail > 0 ? "bad" : "ok");
  }
  if (ev.type === "autolist-done") {
    state.autoSource = ev.source;
    state.autoSegments = ev.segments || [];
    const box = $("auto-result");
    box.hidden = false;
    $("auto-actions").hidden = false;
    if (!state.autoSegments.length) {
      box.className = "load-result err";
      box.textContent = "没有检测到切片点——试试调高灵敏度(场景)或缩短最短时长";
    } else {
      box.className = "load-result ok";
      box.textContent = `✓ ${ev.source} 检测到 ${state.autoSegments.length} 个片段` +
        (ev.kind === "black" ? "(黑场/信号丢失段)" : "(按场景切换划分)");
    }
    $("btn-auto").disabled = false;
  }
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    try {
      handleEvent(JSON.parse(e.data));
    } catch { /* 忽略坏帧 */ }
  };
  es.onerror = () => {
    /* EventSource 自动重连 */
  };
}

/* ============================================================
   操作
   ============================================================ */

async function saveConfig() {
  await api("/api/config", readConfig());
}

async function loadCsv(content) {
  try {
    await saveConfig();
    const payload = content ? { content } : {};
    const res = await api("/api/load", payload);
    const box = $("load-result");
    box.hidden = false;
    if (res.problems.length) {
      box.className = "load-result warn";
      box.innerHTML = `装载 ${res.total} 个任务,<b>${res.problems.length}</b> 行有问题(黄色 ⚠ 标在表格里):<br>` +
        res.problems.slice(0, 5).map((p) => `第 ${p.index} 行 — ${p.problem}`).join("<br>") +
        (res.problems.length > 5 ? `<br>… 等 ${res.problems.length - 5} 条` : "");
    } else {
      box.className = "load-result ok";
      box.textContent = `✓ 装载 ${res.total} 个任务,全部通过校验`;
    }
    toast(`已装载 ${res.total} 个任务`, res.problems.length ? "" : "ok");
  } catch (err) {
    const box = $("load-result");
    box.hidden = false;
    box.className = "load-result err";
    box.textContent = err.message;
    toast(err.message, "bad");
  }
}

async function mainAction() {
  if (state.stage === "running") {
    const force = state.stopArmed;
    try {
      await api("/api/stop", { force });
      if (!force) {
        state.stopArmed = true;
        renderStage();
      }
    } catch (err) {
      toast(err.message, "bad");
    }
    return;
  }
  try {
    await saveConfig();
    await api("/api/start");
    logLine("—— 本轮开始 ——", "sys");
  } catch (err) {
    toast(err.message, "bad");
  }
}

async function retryFailed() {
  try {
    const res = await api("/api/retry");
    if (res.retried > 0) {
      await api("/api/start");
      toast(`重试 ${res.retried} 个失败任务`, "ok");
    }
  } catch (err) {
    toast(err.message, "bad");
  }
}

function exportReport() {
  const out = $("cfg-output-dir").value.trim();
  if (!out) return toast("先填输出目录", "bad");
  const sep = out.includes("\\") ? "\\" : "/";
  const rel = out.split(sep).pop() || "";
  window.open(`/media/output/${encodeURIComponent("failed_clips.csv")}`);
}

/* ============================================================
   预览弹窗
   ============================================================ */

function openPreview(index) {
  const t = state.tasks.get(index);
  if (!t) return;
  state.previewTask = t;
  state.previewPane = "source";
  $("preview-title").textContent = `#${t.index} · ${t.output || t.source}`;
  renderPreview();
  $("preview").hidden = false;
}

function toSeconds(str) {
  if (!str) return 0;
  if (!str.includes(":")) return parseFloat(str) || 0;
  return str.split(":").reduce((acc, p) => acc * 60 + parseFloat(p || 0), 0);
}

function renderPreview() {
  const t = state.previewTask;
  if (!t) return;
  document.querySelectorAll(".modal-tabs .tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.p === state.previewPane);
  });
  $("tab-output").disabled = !(t.status === "done" || t.status === "skip");
  const video = $("preview-video");
  const enc = encodeURIComponent;
  if (state.previewPane === "output" && (t.status === "done" || t.status === "skip")) {
    video.src = `/media/output/${enc(t.output)}`;
    $("preview-meta").textContent = `成品 ${t.output_mb.toFixed(1)} MB · 剪辑耗时 ${t.seconds.toFixed(1)}s${t.speed ? " · " + t.speed : ""}`;
  } else {
    const s = toSeconds(t.start);
    const e = toSeconds(t.end);
    video.src = `/media/source/${enc(t.source)}#t=${s},${e}`;
    $("preview-meta").textContent = `源片 ${t.source || ""} · 定位 ${t.start} → ${t.end}(浏览器将直接跳到片段起点)`;
  }
  video.play().catch(() => {});
}

/* ============================================================
   编辑任务弹窗(播放器取点 → 写回 CSV)
   ============================================================ */

const fmtClock = (sec) => {
  sec = Math.max(0, sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}${s.toFixed(3).padStart(6, "0")}`;
};

function openEditor(index) {
  const t = state.tasks.get(index);
  if (!t || state.stage === "running") return;
  state.editIndex = index;
  $("editor-title").textContent = `编辑任务 #${index}`;
  $("edit-source").value = t.source || "";
  $("edit-start").value = t.start || "";
  $("edit-end").value = t.end || "";
  $("edit-output").value = t.output || "";
  $("edit-problem").hidden = !t.problem;
  $("edit-problem").textContent = t.problem ? `⚠ 当前问题: ${t.problem}` : "";
  $("editor-video").src = `/media/source/${encodeURIComponent(t.source || "")}`;
  $("editor").hidden = false;
}

function closeEditor() {
  $("editor").hidden = true;
  $("editor-video").pause();
}

async function saveEditor() {
  const index = state.editIndex;
  if (index == null) return;
  try {
    const res = await api("/api/update-task", {
      index,
      source: $("edit-source").value.trim(),
      start: $("edit-start").value.trim(),
      end: $("edit-end").value.trim(),
      output: $("edit-output").value.trim(),
      save: true,
    });
    closeEditor();
    toast(res.problem ? `已保存,仍有问题: ${res.problem}` : "已保存并写回 CSV", res.problem ? "" : "ok");
  } catch (err) {
    $("edit-problem").hidden = false;
    $("edit-problem").textContent = err.message;
  }
}

/* ============================================================
   事件绑定与启动
   ============================================================ */

function bind() {
  $("btn-load").addEventListener("click", () => loadCsv());
  $("btn-validate").addEventListener("click", () => loadCsv());
  $("btn-main").addEventListener("click", mainAction);
  $("btn-retry").addEventListener("click", retryFailed);
  $("btn-report").addEventListener("click", exportReport);

  document.querySelectorAll("#mode-seg button").forEach((b) => {
    b.addEventListener("click", () => {
      setMode(b.dataset.mode);
      saveConfig().catch(() => {});
    });
  });

  document.querySelectorAll("#filter-chips .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll("#filter-chips .chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      state.filter = chip.dataset.f;
      renderTable();
    });
  });

  $("log-toggle").addEventListener("click", () => {
    const body = $("log-body");
    body.hidden = !body.hidden;
    $("logbar").classList.toggle("open", !body.hidden);
    if (!body.hidden) $("log-lines").scrollTop = $("log-lines").scrollHeight;
  });

  $("preview-close").addEventListener("click", () => {
    $("preview").hidden = true;
    $("preview-video").pause();
  });
  $("preview").addEventListener("click", (e) => {
    if (e.target === $("preview")) {
      $("preview").hidden = true;
      $("preview-video").pause();
    }
  });
  document.querySelectorAll(".modal-tabs .tab").forEach((b) => {
    b.addEventListener("click", () => {
      state.previewPane = b.dataset.p;
      renderPreview();
    });
  });

  /* 编辑弹窗 */
  $("editor-close").addEventListener("click", closeEditor);
  $("editor-cancel").addEventListener("click", closeEditor);
  $("editor-save").addEventListener("click", saveEditor);
  $("editor").addEventListener("click", (e) => {
    if (e.target === $("editor")) closeEditor();
  });
  $("edit-set-start").addEventListener("click", () => {
    $("edit-start").value = fmtClock($("editor-video").currentTime);
  });
  $("edit-set-end").addEventListener("click", () => {
    $("edit-end").value = fmtClock($("editor-video").currentTime);
  });

  /* 自动生成清单 */
  $("btn-auto").addEventListener("click", async () => {
    const source = $("auto-source").value;
    if (!source) return toast("先在「来源目录」填好路径并装载,才会列出源视频", "bad");
    try {
      await saveConfig();
      $("btn-auto").disabled = true;
      $("auto-result").hidden = false;
      $("auto-result").className = "load-result";
      $("auto-result").textContent = "检测中——全帧解码需要一点时间(已降采样提速)…";
      await api("/api/autolist", {
        source,
        kind: $("auto-kind").value,
        threshold: parseFloat($("auto-threshold").value) || 0.35,
        minSeg: parseFloat($("auto-minseg").value) || 2,
      });
    } catch (err) {
      $("btn-auto").disabled = false;
      $("auto-result").hidden = true;
      toast(err.message, "bad");
    }
  });
  $("btn-adopt").addEventListener("click", async () => {
    try {
      const res = await api("/api/adopt", { source: state.autoSource, segments: state.autoSegments });
      toast(`已装载 ${res.total} 个自动任务(尚未写回 CSV)`, "ok");
    } catch (err) {
      toast(err.message, "bad");
    }
  });
  $("btn-savecsv").addEventListener("click", async () => {
    try {
      const res = await api("/api/save-csv");
      toast(`已写回 ${res.path}`, "ok");
    } catch (err) {
      toast(err.message, "bad");
    }
  });
  $("auto-kind").addEventListener("change", () => {
    const black = $("auto-kind").value === "black";
    $("auto-th-field").querySelector(".field-label").textContent = black ? "最短黑场(s)" : "灵敏度";
    $("auto-threshold").value = black ? 0.5 : 0.35;
  });

  /* CSV 拖拽:读文件内容发后端解析 */
  const drop = $("csv-drop");
  ["dragenter", "dragover"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", async (e) => {
    const file = e.dataTransfer.files[0];
    if (!file) return;
    if (!/\.csv$/i.test(file.name)) return toast("请拖入 .csv 文件", "bad");
    const content = await file.text();
    await loadCsv(content);
  });
  drop.addEventListener("click", async () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".csv";
    input.onchange = async () => {
      if (!input.files[0]) return;
      await loadCsv(await input.files[0].text());
    };
    input.click();
  });
}

async function boot() {
  bind();
  try {
    applySnapshot(await api("/api/state"));
    await refreshSources();
  } catch (err) {
    toast(`连接后端失败: ${err.message}`, "bad");
  }
  connectEvents();
  logLine("BatchCut Studio 已就绪", "sys");
}

async function refreshSources() {
  try {
    const res = await api("/api/sources");
    const sel = $("auto-source");
    sel.innerHTML = "";
    if (!res.files.length) {
      sel.innerHTML = '<option value="">来源目录暂无视频</option>';
      return;
    }
    res.files.forEach((f) => {
      const opt = document.createElement("option");
      opt.value = f;
      opt.textContent = f;
      sel.appendChild(opt);
    });
  } catch { /* 静默 */ }
}

boot();
