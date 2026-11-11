/* BatchCut Studio · 前端交互(vanilla JS,零依赖) */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  tasks: new Map(),        // index -> task
  labels: [],
  annotations: {},         // clip(output 相对路径) -> 标注记录
  selectedClip: null,
  reviewMode: false,
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
  // smart 模式也会用重编码兜底 + 硬件编码,只有纯 copy 模式才隐藏这两个字段
  const usesEncoder = mode !== "copy";
  $("crf-field").style.display = usesEncoder ? "" : "none";
  $("hw-field").style.display = usesEncoder ? "" : "none";
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
        <td><div class="anno-chips"></div></td>
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
  const durText = dur ? `时长${dur.slice(2)}` : "";
  sub.textContent = t.hasAudio === false ? (durText ? durText + " · 🔇无音轨" : "🔇无音轨") : durText;
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
  if (t.verify) {
    const v = document.createElement("span");
    v.className = "sub";
    v.style.color = t.verify === "bad" ? "var(--bad)" : t.verify === "warn" ? "var(--warn)" : "var(--ok)";
    v.textContent = (t.verify === "bad" ? "✗ 质检不合格" : t.verify === "warn" ? "⚠ 待复核" : "✓ 质检通过") +
      (t.verifyReason ? " · " + t.verifyReason : "");
    outTd.appendChild(v);
  }

  const pct = Math.round((t.progress || 0) * 100);
  const fill = tr.querySelector(".task-progress-fill");
  fill.style.width = `${t.status === "done" ? 100 : pct}%`;
  fill.classList.toggle("running", t.status === "running");
  tr.querySelector(".task-progress-text").textContent =
    t.status === "running" ? `${pct}%${t.speed ? " " + t.speed : ""}` : STATUS_TEXT[t.status] || "";

  const anno = state.annotations[t.output] || { labelIds: [], status: "pending" };
  const chips = tr.querySelector(".anno-chips");
  chips.innerHTML = "";
  if (!anno.labelIds.length) {
    const empty = document.createElement("span");
    empty.className = "anno-chip off";
    empty.textContent = "点行选中 · 数字键打标";
    chips.appendChild(empty);
  }
  state.labels.forEach((label) => {
    if (!anno.labelIds.includes(label.id)) return;  // 紧凑:只显示已打的标签
    const chip = document.createElement("span");
    chip.className = "anno-chip";
    chip.style.background = label.color + "33";
    chip.style.color = label.color;
    chip.style.borderColor = label.color;
    chip.textContent = label.hotkey ? label.hotkey + " " + label.name : label.name;
    chip.title = "点击摘除「" + label.name + "」";
    chip.addEventListener("click", (ev) => {
      ev.stopPropagation();
      annotate(t.output, { labelId: label.id });
    });
    chips.appendChild(chip);
  });
  if (anno.status === "approved") {
    const ok = document.createElement("span");
    ok.className = "anno-chip";
    ok.style.background = "rgba(52,211,153,.15)";
    ok.style.color = "var(--ok)";
    ok.textContent = "✓";
    chips.appendChild(ok);
  } else if (anno.status === "needs_fix" || anno.status === "rejected") {
    const bad = document.createElement("span");
    bad.className = "anno-chip";
    bad.style.background = "rgba(248,113,113,.15)";
    bad.style.color = "var(--bad)";
    bad.textContent = anno.status === "rejected" ? "剔除" : "需修正";
    chips.appendChild(bad);
  }

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
  bindBench();
  $("offline-retry").addEventListener("click", () => location.reload());
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

  $("task-body").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-index]");
    if (!tr || e.target.closest(".btn-eye") || e.target.closest(".btn-pencil") || e.target.closest(".anno-chip")) return;
    selectClip(Number(tr.dataset.index));
  });
  $("btn-labels").addEventListener("click", openLabelMgr);
  $("labelmgr-close").addEventListener("click", () => { $("labelmgr").hidden = true; });
  $("label-add").addEventListener("click", async () => {
    const name = $("label-new-name").value.trim();
    if (!name) return;
    await api("/api/labels-add", { name });
    $("label-new-name").value = "";
    state.labels = (await api("/api/labels")).labels;
    renderLabelMgr();
    renderTable();
  });
  $("btn-verify").addEventListener("click", async () => {
    try {
      const res = await api("/api/verify-all", {});
      const bad = res.results.filter((r) => r.verify === "bad").length;
      const warn = res.results.filter((r) => r.verify === "warn").length;
      toast(`质检完成:${res.results.length} 个成品 · ${bad} 不合格 · ${warn} 待复核`,
            bad ? "bad" : "ok");
    } catch (err) {
      toast(err.message, "bad");
    }
  });
  $("btn-export-ds").addEventListener("click", async () => {
    try {
      const res = await api("/api/export-dataset", { ratio: 0.8 });
      toast("数据集已导出:train " + res.train + " / val " + res.val + " · " + res.path, "ok");
    } catch (err) {
      toast(err.message, "bad");
    }
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
    state.labels = (await api("/api/labels")).labels;
    state.annotations = (await api("/api/annotations")).records;
    renderTable();
    $("offline").hidden = true;
  } catch (err) {
    $("offline").hidden = false;
    return;  // 离线时不渲染空表格误导人
  }
  connectEvents();
  logLine("BatchCut Studio 已就绪", "sys");
}

async function annotate(clip, body) {
  try {
    const res = await api("/api/annotate", { clip, ...body, reviewer: "local-user" });
    state.annotations[clip] = res.record;
    const t = [...state.tasks.values()].find((x) => x.output === clip);
    if (t) {
      const tr = $("task-body").querySelector('tr[data-index="' + t.index + '"]');
      if (tr) fillRow(tr, t);
    }
  } catch (err) {
    toast(err.message, "bad");
  }
}

function selectClip(index) {
  state.selectedClip = index;
  const t = state.tasks.get(index);
  $("kbd-hint").hidden = false;
  $("kbd-clip").textContent = t ? index + " " + t.output : "—";
  document.querySelectorAll("#task-body tr").forEach((tr) => {
    tr.classList.toggle("is-selected", Number(tr.dataset.index) === index);
  });
}

function moveSelection(delta) {
  const { list } = visibleTasks();
  if (!list.length) return;
  const pos = list.findIndex((t) => t.index === state.selectedClip);
  const next = list[Math.min(list.length - 1, Math.max(0, pos + delta))] || list[0];
  selectClip(next.index);
  const tr = $("task-body").querySelector('tr[data-index="' + next.index + '"]');
  if (tr) tr.scrollIntoView({ block: "nearest" });
}

document.addEventListener("keydown", (e) => {
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
  if (!$("editor").hidden || !$("preview").hidden || !$("labelmgr").hidden || !$("bench").hidden) return;
  if (!state.selectedClip) return;
  const t = state.tasks.get(state.selectedClip);
  if (!t || !t.output) return;
  if (e.key === "Escape") { state.selectedClip = null; selectClip(-1); $("kbd-hint").hidden = true; return; }
  if (e.key === "j" || e.key === "J") { moveSelection(-1); e.preventDefault(); return; }
  if (e.key === "k" || e.key === "K") { moveSelection(1); e.preventDefault(); return; }
  if (e.key === "q" || e.key === "Q") {
    state.reviewMode = !state.reviewMode;
    $("kbd-hint").style.background = state.reviewMode ? "rgba(244,63,94,.12)" : "rgba(99,102,241,.1)";
    toast(state.reviewMode ? "审核模式:1 通过 / 2 需修正 / 3 剔除 / 4 待审" : "标注模式:数字键切标签", "");
    return;
  }
  if (/^[1-9]$/.test(e.key)) {
    if (state.reviewMode) {
      const map = { 1: "approved", 2: "needs_fix", 3: "rejected", 4: "pending" };
      if (map[e.key]) annotate(t.output, { status: map[e.key] });
    } else {
      const label = state.labels.find((l) => l.hotkey === e.key);
      if (label) annotate(t.output, { labelId: label.id });
    }
    e.preventDefault();
  }
});

function openLabelMgr() {
  $("labelmgr").hidden = false;
  renderLabelMgr();
}

function renderLabelMgr() {
  const box = $("label-list");
  box.innerHTML = "";
  state.labels.forEach((label) => {
    const row = document.createElement("div");
    row.className = "label-row";
    row.innerHTML = '<span class="dot" style="background:' + label.color + '"></span>' +
      "<span>" + label.name + '</span><span style="color:var(--text-3)">' +
      (label.hotkey ? "键 " + label.hotkey : "") + "</span>";
    const del = document.createElement("button");
    del.className = "del";
    del.textContent = "删除";
    del.addEventListener("click", async () => {
      await api("/api/labels-delete", { id: label.id });
      state.labels = (await api("/api/labels")).labels;
      renderLabelMgr();
      renderTable();
    });
    row.appendChild(del);
    box.appendChild(row);
  });
}

/* ============================================================
   时间轴打点台
   ============================================================ */

const bench = {
  source: "", duration: 0, start: null, end: null, segments: [], rate: 1,
};

function fmtShort(sec) {
  sec = Math.max(0, sec);
  const m = Math.floor(sec / 60);
  return `${String(m).padStart(2, "0")}:${(sec % 60).toFixed(1)}s`;
}

async function openBench() {
  $("bench").hidden = false;
  if (!$("bench-source").options.length) {
    const res = await api("/api/sources");
    $("bench-source").innerHTML = res.files.map((f) => `<option value="${f}">${f}</option>`).join("") ||
      '<option value="">来源目录暂无视频</option>';
  }
  if ($("bench-source").value && $("bench-source").value !== state.benchLoaded) loadBenchSource();
}

async function loadBenchSource() {
  bench.source = $("bench-source").value;
  state.benchLoaded = bench.source;
  bench.segments = [];
  bench.start = bench.end = null;
  renderBenchList();
  const video = $("bench-video");
  video.src = `/media/source/${encodeURIComponent(bench.source)}`;
  $("bench-kfs").innerHTML = '<span style="color:var(--text-3);font-size:11px;padding:14px">关键帧加载中…</span>';
  try {
    const res = await api(`/api/keyframes?source=${encodeURIComponent(bench.source)}`);
    bench.duration = res.duration;
    $("bench-kfs").innerHTML = "";
    const kfBox = $("bench-kfs");
    const cap = Math.min(res.keyframes.length, 2000);
    for (let i = 0; i < cap; i++) {
      const tick = document.createElement("div");
      tick.className = "bench-kf";
      tick.style.left = `${(res.keyframes[i] / res.duration) * 100}%`;
      kfBox.appendChild(tick);
    }
  } catch {
    $("bench-kfs").innerHTML = "";
  }
}

function benchSeekTo(sec) {
  const v = $("bench-video");
  if (!bench.duration) return;
  v.currentTime = Math.min(bench.duration - 0.05, Math.max(0, sec));
}

function renderBenchSeg() {
  const seg = $("bench-seg");
  if (bench.start == null || bench.end == null) {
    seg.hidden = true;
    return;
  }
  seg.hidden = false;
  seg.style.left = `${(bench.start / bench.duration) * 100}%`;
  seg.style.width = `${((bench.end - bench.start) / bench.duration) * 100}%`;
}

function renderBenchList() {
  $("bench-count").textContent = bench.segments.length;
  const box = $("bench-list");
  box.innerHTML = "";
  bench.segments.forEach((seg, i) => {
    const [a, b, labels, status] = seg;
    const row = document.createElement("div");
    row.className = "bench-row" + (bench.selected === i ? " is-selected" : "");
    row.innerHTML =
      `<span>#${i + 1}</span><span>${fmtShort(a)} → ${fmtShort(b)}</span>` +
      `<span>(${(b - a).toFixed(1)}s)</span>`;

    // 标签 chips:点一下切换
    const lblBox = document.createElement("span");
    lblBox.className = "row-labels";
    state.labels.forEach((label) => {
      const on = (labels || []).includes(label.name);
      const chip = document.createElement("span");
      chip.className = "lbl" + (on ? " on" : "");
      chip.style.background = on ? label.color + "33" : "transparent";
      chip.style.color = on ? label.color : "var(--text-3)";
      chip.style.borderColor = on ? label.color : "var(--line)";
      chip.textContent = label.name;
      chip.addEventListener("click", (e) => {
        e.stopPropagation();
        const set = new Set(seg[2] || []);
        set.has(label.name) ? set.delete(label.name) : set.add(label.name);
        seg[2] = [...set];
        renderBenchList();
      });
      lblBox.appendChild(chip);
    });
    row.appendChild(lblBox);

    // 审核状态
    const st = document.createElement("span");
    st.className = "row-status st-" + (status || "pending");
    st.textContent = ({ approved: "已通过", needs_fix: "需修正", rejected: "剔除" })[status] || "待审核";
    row.appendChild(st);

    const del = document.createElement("button");
    del.className = "del";
    del.textContent = "删除";
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      bench.segments.splice(i, 1);
      renderBenchList();
    });
    row.appendChild(del);

    row.addEventListener("click", () => {
      bench.selected = i;
      benchSeekTo(a);
      renderBenchList();
    });
    box.appendChild(row);
  });
}

function benchAppend() {
  if (bench.start == null || bench.end == null || bench.end <= bench.start) {
    return toast("先用 S / E 设好起点和终点", "bad");
  }
  const m = parseFloat($("bench-margin").value) || 0;
  let a = Math.max(0, bench.start - m);
  let b = Math.min(bench.duration, bench.end + m);
  bench.segments.push([Number(a.toFixed(3)), Number(b.toFixed(3)), [], "pending"]);
  bench.segments.sort((x, y) => x[0] - y[0]);
  bench.selected = bench.segments.findIndex((sg) => sg[0] === Number(a.toFixed(3)));
  renderBenchList();
  benchSeekTo(b);           // 连标:播放头跳到终点,起点自动衔接
  bench.start = b;
  bench.end = null;
  renderBenchSeg();
  $("bench-start").textContent = fmtShort(bench.start) + " (衔接)";
  $("bench-end").textContent = "—";
}

/** 标注工作台的快捷键:选中片段后打标签/定审核状态 */
function benchHotkey(label) {
  const i = bench.selected;
  if (i == null || !bench.segments[i]) return;
  const seg = bench.segments[i];
  const set = new Set(seg[2] || []);
  set.has(label.name) ? set.delete(label.name) : set.add(label.name);
  seg[2] = [...set];
  renderBenchList();
}

function benchSetStatus(status) {
  const i = bench.selected;
  if (i == null || !bench.segments[i]) return toast("先点一个片段选中它", "bad");
  bench.segments[i][3] = status;
  renderBenchList();
  toast("状态:" + ({ approved: "已通过", needs_fix: "需修正", rejected: "剔除", pending: "待审核" })[status], "");
}

/** 保存标注到库里(片段成为"标注点",带源视频与时间区间) */
async function benchSaveAnnotations() {
  if (!bench.segments.length) return toast("还没有标注片段", "bad");
  const issues = benchSelfCheck();
  if (issues.length) {
    toast("保存前请先处理:" + issues.join(";"), "bad");
    throw new Error(issues.join(";"));
  }
  let saved = 0;
  for (let i = 0; i < bench.segments.length; i++) {
    const [a, b, labels, status] = bench.segments[i];
    const clip = `${bench.source.replace(/\.[^.]+$/, "")}__${String(a).padStart(8, "0")}.mp4`;
    await api("/api/annotate", {
      clip, labelId: undefined, reviewer: "local-user",
      intervals: [[a, b]], source: bench.source, labels, status,
    }).catch(() => {});
    saved++;
  }
  toast(`已保存 ${saved} 个标注`, "ok");
}

async function adoptBench() {
  if (!bench.segments.length) return toast("还没有标记任何片段", "bad");
  try {
    // 先落库成标注(带标签/审核),再转入切割车间
    await benchSaveAnnotations();
    const rows = bench.segments.map(([a, b, labels, status]) => ({
      source: bench.source, start: String(a), end: String(b),
      labels: (labels || []).join("+"), review_status: status || "pending",
    }));
    const res = await api("/api/adopt", { rows });
    toast(`已转入剪辑车间 ${res.total} 个任务`, "ok");
    closeBench();
  } catch (err) {
    toast(err.message, "bad");
  }
}

function closeBench() {
  // 关闭前兜底:提醒未打标签或未审核的片段(对标 BORIS 的 UNPAIRED 检查)
  const untagged = bench.segments.filter(([, , labels]) => !labels || !labels.length).length;
  const unreviewed = bench.segments.filter(([, , , status]) => !status || status === "pending").length;
  if (untagged || unreviewed) {
    toast(`提醒:${untagged} 个片段未打标签,${unreviewed} 个未审核——下次打开仍会保留`, "");
  }
  $("bench").hidden = true;
  $("bench-video").pause();
}

/** 保存前自检:返回问题列表(空 = 可以放心落库) */
function benchSelfCheck() {
  const issues = [];
  const untagged = bench.segments.filter(([, , labels]) => !labels || !labels.length);
  const unreviewed = bench.segments.filter(([, , , status]) => !status || status === "pending");
  if (untagged.length) issues.push(`${untagged.length} 个片段没打标签`);
  if (unreviewed.length) issues.push(`${unreviewed.length} 个片段还没定审核状态(A 通过 / X 需修正 / R 剔除)`);
  const overlapped = [];
  for (let i = 1; i < bench.segments.length; i++) {
    if (bench.segments[i][0] < bench.segments[i - 1][1] - 0.01) overlapped.push(i + 1);
  }
  if (overlapped.length) issues.push(`片段 #${overlapped.join(", #")} 与前一段重叠`);
  return issues;
}

function bindBench() {
  $("btn-bench").addEventListener("click", openBench);
  $("bench-close").addEventListener("click", closeBench);
  $("bench").addEventListener("click", (e) => { if (e.target === $("bench")) closeBench(); });
  $("bench-source").addEventListener("change", loadBenchSource);

  const track = $("bench-track");
  track.addEventListener("click", (e) => {
    const rect = track.getBoundingClientRect();
    benchSeekTo(((e.clientX - rect.left) / rect.width) * bench.duration);
  });

  const video = $("bench-video");
  video.addEventListener("timeupdate", () => {
    $("bench-clock").textContent = fmtShort(video.currentTime);
    if (bench.duration) {
      $("bench-head").style.left = `${(video.currentTime / bench.duration) * 100}%`;
      if (bench.start != null && bench.end != null) renderBenchSeg();
    }
  });

  $("bench-setstart").addEventListener("click", () => {
    bench.start = video.currentTime;
    $("bench-start").textContent = fmtShort(bench.start);
    renderBenchSeg();
  });
  $("bench-setend").addEventListener("click", () => {
    bench.end = video.currentTime;
    $("bench-end").textContent = fmtShort(bench.end);
    renderBenchSeg();
  });
  $("bench-append").addEventListener("click", benchAppend);
  $("bench-mute").addEventListener("click", () => {
    const v = $("bench-video");
    v.muted = !v.muted;
    $("bench-mute").textContent = v.muted ? "🔊" : "🔇";
    $("bench-mute").title = v.muted ? "恢复声音" : "静音播放(坏音轨可脱离音轨看画面)";
  });
  $("bench-clear").addEventListener("click", () => { bench.segments = []; renderBenchList(); });
  $("bench-adopt").addEventListener("click", adoptBench);
  $("bench-export").addEventListener("click", async () => {
    try {
      await benchSaveAnnotations();
      const res = await api("/api/annotate-export", {});
      toast(`标注清单已导出:${res.rows} 行 → ${res.path}`, "ok");
    } catch (err) {
      toast(err.message, "bad");
    }
  });

  document.addEventListener("keydown", (e) => {
    if ($("bench").hidden) return;
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    // 标注工作台:选中片段时,数字键打标签 / A/X/R 定审核状态
    if (bench.selected != null && !e.shiftKey && !e.ctrlKey && !e.altKey) {
      if (/^[1-9]$/.test(e.key)) {
        const label = state.labels.find((l) => l.hotkey === e.key);
        if (label) { benchHotkey(label); e.preventDefault(); return; }
      }
      if (e.key === "a" || e.key === "A") { benchSetStatus("approved"); e.preventDefault(); return; }
      if (e.key === "x" || e.key === "X") { benchSetStatus("needs_fix"); e.preventDefault(); return; }
      if (e.key === "r" || e.key === "R") { benchSetStatus("rejected"); e.preventDefault(); return; }
    }
    const v = $("bench-video");
    if (e.key === "Escape") return closeBench();
    if (e.key === " ") { v.paused ? v.play() : v.pause(); e.preventDefault(); return; }
    if (e.key === "s" || e.key === "S") { bench.start = v.currentTime; $("bench-start").textContent = fmtShort(bench.start); renderBenchSeg(); return; }
    if (e.key === "e" || e.key === "E") { bench.end = v.currentTime; $("bench-end").textContent = fmtShort(bench.end); renderBenchSeg(); return; }
    if (e.key === "Enter") { benchAppend(); e.preventDefault(); return; }
    if (e.key === "j" || e.key === "J") { v.playbackRate = 0.5; if (v.paused) v.play(); return; }
    if (e.key === "k" || e.key === "K") { v.pause(); v.playbackRate = 1; bench.rate = 1; return; }
    if (e.key === "l" || e.key === "L") {
      v.play();
      bench.rate = bench.rate >= 4 ? 1 : bench.rate * 2;
      v.playbackRate = bench.rate;
      return;
    }
    if (e.key === "ArrowLeft") { benchSeekTo(v.currentTime - (e.shiftKey ? 5 : 1)); e.preventDefault(); return; }
    if (e.key === "ArrowRight") { benchSeekTo(v.currentTime + (e.shiftKey ? 5 : 1)); e.preventDefault(); return; }
    if (e.key === ",") { benchSeekTo(v.currentTime - 0.04); return; }
    if (e.key === ".") { benchSeekTo(v.currentTime + 0.04); return; }
  });
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
