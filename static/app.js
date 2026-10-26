/* ══════════════════════════════════════════════════════════
   llama.cpp 服务管理器 —— 前端逻辑
   ══════════════════════════════════════════════════════════ */

const $ = (id) => document.getElementById(id);

const state = {
  models: [],
  presets: {},
  server: {},
  logSeq: 0,
  activePreset: "推荐",
  polling: false,
  manualModel: null,
};

/* ── 工具 ────────────────────────────────────────────────── */

function humanSize(n) {
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + " " + u[i];
}

function toast(msg, kind = "") {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show " + kind;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.className = "toast " + kind; }, 2800);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

async function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

/* ── 参数收集 ────────────────────────────────────────────── */

function gatherConfig() {
  const sel = $("modelSelect");
  return {
    model: state.manualModel || (sel.value || ""),
    ngl: $("pNgl").value,
    ctx: $("pCtx").value,
    parallel: $("pParallel").value,
    ub: $("pUb").value,
    fa: $("pFa").value,
    ctk: $("pCtk").value,
    ctv: $("pCtv").value,
    port: $("pPort").value,
  };
}

/* ── 模型 ────────────────────────────────────────────────── */

function renderModels(models) {
  state.models = models;
  const sel = $("modelSelect");
  const prev = sel.value;

  if (!models.length) {
    sel.innerHTML = '<option value="">（未找到 GGUF 模型）</option>';
    $("modelMeta").textContent =
      "未找到模型。请把 .gguf 放到 ~/models 或 ~/.cache/llama.cpp，然后点「刷新」。";
    return;
  }

  sel.innerHTML = models.map((m) =>
    `<option value="${m.path}">${m.name}   ·   ${humanSize(m.size)}</option>`
  ).join("");

  // 保持原选择
  if (prev && models.some((m) => m.path === prev)) {
    sel.value = prev;
  } else {
    sel.selectedIndex = 0;
  }
  state.manualModel = null;
  updateModelMeta();
}

async function updateModelMeta() {
  const path = $("modelSelect").value;
  if (!path) return;

  const m = state.models.find((x) => x.path === path);
  const parts = [];
  if (m) parts.push(humanSize(m.size));
  parts.push(path.substring(0, path.lastIndexOf("/")));

  try {
    const info = await api("/api/model-info?path=" + encodeURIComponent(path));
    if (info.params && info.params.arch) {
      parts.unshift(info.params.arch);
    }
    if (info.params && info.params.ctx_train) {
      parts.push("原生上下文 " + (info.params.ctx_train / 1024).toFixed(0) + "K");
    }
  } catch (e) { /* 忽略 */ }

  $("modelMeta").textContent = parts.join("   ·   ");
  updateEstimate();
}

/* ── 预设 ────────────────────────────────────────────────── */

function renderPresets(presets) {
  state.presets = presets;
  const box = $("presetChips");
  box.innerHTML = "";
  Object.keys(presets).forEach((name) => {
    const el = document.createElement("div");
    el.className = "chip";
    el.textContent = name;
    el.dataset.name = name;
    el.onclick = () => applyPreset(name);
    box.appendChild(el);
  });
  applyPreset("推荐");
}

function applyPreset(name) {
  const p = state.presets[name];
  if (!p) return;

  state.activePreset = name;
  $("pCtx").value = p.ctx;
  $("pParallel").value = p.parallel;
  $("pFa").value = p.fa;
  $("pCtk").value = p.ctk;
  $("pCtv").value = p.ctv;

  document.querySelectorAll(".chip").forEach((c) => {
    c.classList.toggle("active", c.dataset.name === name);
  });
  $("presetDesc").textContent = p.desc || "";

  updateEstimate();
}

/* ── 显存估算 ────────────────────────────────────────────── */

let estimateTimer = null;

function updateEstimate() {
  clearTimeout(estimateTimer);
  estimateTimer = setTimeout(doEstimate, 180);
}

async function doEstimate() {
  const cfg = gatherConfig();
  if (!cfg.model) {
    $("estMain").textContent = "--";
    $("estDetail").textContent = "";
    $("estWarn").textContent = "";
    return;
  }

  try {
    const r = await post("/api/estimate", {
      model: cfg.model,
      ctx: parseInt(cfg.ctx) || 4096,
      ctk: cfg.ctk,
      ctv: cfg.ctv,
    });

    $("estMain").textContent =
      `${r.model.toFixed(1)} + ${r.kv.toFixed(2)} + ${r.compute.toFixed(1)}` +
      `  =  ${r.total.toFixed(1)} GB`;

    $("estDetail").textContent =
      `${r.layers} 层 × ${r.kv_heads} KV头 × ${r.dim} 维` +
      `  =  ${r.per_tok_kib} KiB/token  (${r.source})`;

    const vram = state.lastVram;
    const warn = $("estWarn");
    if (!vram) {
      warn.textContent = "";
    } else {
      const ratio = r.total / vram.free;
      if (ratio > 0.9) {
        warn.textContent = `危险：将占空闲显存的 ${(ratio * 100).toFixed(0)}%，很可能崩溃`;
        warn.style.color = "var(--red)";
      } else if (ratio > 0.75) {
        warn.textContent = `注意：占用 ${(ratio * 100).toFixed(0)}%，余量偏紧`;
        warn.style.color = "var(--amber)";
      } else {
        warn.textContent = `正常：空闲 ${vram.free.toFixed(1)} GB，占用 ${(ratio * 100).toFixed(0)}%`;
        warn.style.color = "var(--accent)";
      }
    }
  } catch (e) {
    $("estMain").textContent = "--";
  }
}

/* ── 端口检查 ────────────────────────────────────────────── */

async function checkPort() {
  const p = $("pPort").value;
  const hint = $("portHint");
  if (!p) { hint.textContent = ""; return; }
  try {
    const r = await api("/api/port-check?port=" + encodeURIComponent(p));
    if (r.in_use) {
      hint.textContent = "已被占用";
      hint.style.color = "var(--red)";
    } else {
      hint.textContent = "可用";
      hint.style.color = "var(--accent)";
    }
  } catch (e) { hint.textContent = ""; }
}

/* ── 命令预览 ────────────────────────────────────────────── */

function updateCmdPreview() {
  const c = gatherConfig();
  if (!c.model) {
    $("cmdText").textContent = "（请先选择模型）";
    return;
  }
  const name = c.model.substring(c.model.lastIndexOf("/") + 1);
  const parts = [
    "llama-server", "-m", name,
    "-ngl", c.ngl, "-c", c.ctx,
    "--parallel", c.parallel, "-ub", c.ub,
  ];
  if (c.fa !== "auto") parts.push("-fa", c.fa);
  parts.push("-ctk", c.ctk, "-ctv", c.ctv,
             "--host", "127.0.0.1", "--port", c.port);
  $("cmdText").textContent = parts.join(" ");
  $("cmdText").title = parts.join(" ");
}

/* ── 显存 ────────────────────────────────────────────────── */

function renderVram(vram) {
  state.lastVram = vram;
  if (!vram) {
    $("vramValue").textContent = "不可用";
    return;
  }
  $("vramValue").textContent =
    `${vram.used.toFixed(1)} / ${vram.total.toFixed(1)} GB`;

  const pct = (vram.used / vram.total) * 100;
  const fill = $("vramFill");
  fill.style.width = pct + "%";
  fill.style.background =
    pct < 70 ? "var(--accent)" : pct < 88 ? "var(--amber)" : "var(--red)";
}

/* ── 服务状态 ────────────────────────────────────────────── */

function renderServer(s) {
  state.server = s;
  const dot = $("statusDot");
  const txt = $("statusText");

  dot.className = "dot " + (s.status || "idle");

  if (s.status === "ready") {
    txt.textContent = s.message || "运行中";
    txt.style.color = "var(--accent)";
  } else if (s.status === "starting") {
    txt.textContent = "启动中…";
    txt.style.color = "var(--amber)";
  } else if (s.status === "error") {
    txt.textContent = s.message || "异常";
    txt.style.color = "var(--red)";
  } else {
    txt.textContent = "未启动";
    txt.style.color = "var(--text-dim)";
  }

  const running = s.running;
  $("btnStart").disabled = running;
  $("btnStop").disabled = !running;
  $("btnRestart").disabled = !running;
  $("btnOpen").disabled = !running;

  if (running && s.uptime) {
    const m = Math.floor(s.uptime / 60);
    const sec = s.uptime % 60;
    $("statusClock").textContent =
      `运行 ${m}:${String(sec).padStart(2, "0")}`;
  } else {
    $("statusClock").textContent = "";
  }

  // 服务停止时，把端口提示恢复为实测状态
  if (!running && s.status === "idle") {
    checkPort();
    state.runningModel = null;
  }

  // 运行中：让界面显示真正在跑的那个模型和参数
  const cfg = s.running_cfg;
  if (running && cfg) {
    if (state.runningModel !== cfg.model) {
      state.runningModel = cfg.model;
      syncUiToRunning(cfg);
    }
  }
}

/* ── 运行态同步 ──────────────────────────────────────────── */

function syncUiToRunning(cfg) {
  // 模型下拉框切到实际运行的那个
  const sel = $("modelSelect");
  const has = Array.from(sel.options).some((o) => o.value === cfg.model);
  if (has) {
    sel.value = cfg.model;
    state.manualModel = null;
  } else {
    state.manualModel = cfg.model;
    const opt = document.createElement("option");
    opt.value = cfg.model;
    opt.textContent = cfg.model.substring(cfg.model.lastIndexOf("/") + 1) +
                      "   ·   运行中";
    sel.appendChild(opt);
    sel.value = cfg.model;
  }

  // 参数框同步
  $("pNgl").value = cfg.ngl;
  $("pCtx").value = cfg.ctx;
  $("pParallel").value = cfg.parallel;
  $("pUb").value = cfg.ub;
  $("pFa").value = cfg.fa;
  $("pCtk").value = cfg.ctk;
  $("pCtv").value = cfg.ctv;
  $("pPort").value = cfg.port;

  // 取消预设高亮（因为参数已是运行态，可能不对应任何预设）
  document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
  $("presetDesc").textContent = "参数已同步为当前运行中的配置";

  updateModelMeta();
  updateCmdPreview();
}

/* ── 日志 ────────────────────────────────────────────────── */

function appendLogs(lines) {
  if (!lines.length) return;

  const body = $("logBody");
  const empty = body.querySelector(".log-empty");
  if (empty) empty.remove();

  const frag = document.createDocumentFragment();
  lines.forEach(([seq, text, tag]) => {
    state.logSeq = Math.max(state.logSeq, seq);
    const div = document.createElement("span");
    div.className = "log-line " + (tag || "");
    div.textContent = text + "\n";
    frag.appendChild(div);
  });
  body.appendChild(frag);

  // 限制 DOM 节点数
  while (body.childElementCount > 3000) {
    body.removeChild(body.firstElementChild);
  }

  if ($("optAutoScroll").checked) {
    body.scrollTop = body.scrollHeight;
  }
}

/* ── 轮询 ────────────────────────────────────────────────── */

async function poll() {
  if (state.polling) return;
  state.polling = true;
  try {
    const r = await api("/api/state?since=" + state.logSeq);

    renderVram(r.vram);
    renderServer(r.server);
    appendLogs(r.logs || []);

    // 首次加载时渲染模型和预设
    if (!state.bootstrapped) {
      state.bootstrapped = true;
      renderModels(r.models || []);
      renderPresets(r.presets || {});
      checkPort();
    }

  } catch (e) {
    // 后端可能已退出
    $("statusText").textContent = "无法连接后端";
    $("statusDot").className = "dot error";
  } finally {
    state.polling = false;
  }
  updateCmdPreview();
}

/* ── 事件绑定 ────────────────────────────────────────────── */

function bind() {
  $("modelSelect").onchange = () => {
    state.manualModel = null;
    updateModelMeta();
  };

  $("btnRefresh").onclick = async () => {
    const r = await api("/api/state?since=" + state.logSeq);
    renderModels(r.models || []);
    toast("已刷新模型列表");
  };

  $("btnBrowse").onclick = () => {
    const p = prompt("输入 GGUF 模型的完整路径：", "/home/xuechengzou/models/");
    if (!p) return;
    state.manualModel = p;
    const sel = $("modelSelect");
    const opt = document.createElement("option");
    opt.value = p;
    opt.textContent = p.substring(p.lastIndexOf("/") + 1) + "   ·   手动";
    sel.appendChild(opt);
    sel.value = p;
    updateModelMeta();
  };

  ["pNgl", "pCtx", "pParallel", "pUb", "pFa", "pCtk", "pCtv"].forEach((id) => {
    $(id).addEventListener("input", updateEstimate);
    $(id).addEventListener("change", updateEstimate);
  });

  $("pPort").addEventListener("input", () => {
    clearTimeout(window._portTimer);
    window._portTimer = setTimeout(checkPort, 350);
  });

  $("btnStart").onclick = async () => {
    const cfg = gatherConfig();
    if (!cfg.model) { toast("请先选择模型", "error"); return; }

    $("btnStart").disabled = true;
    try {
      const r = await post("/api/start", cfg);
      if (!r.ok) {
        toast(r.error || "启动失败", "error");
        $("btnStart").disabled = false;
      } else {
        toast("已启动，等待就绪…", "ok");
        state.logSeq = 0;
        $("logBody").innerHTML = "";
      }
    } catch (e) {
      toast("请求失败：" + e.message, "error");
      $("btnStart").disabled = false;
    }
  };

  $("btnStop").onclick = async () => {
    try {
      await post("/api/stop");
      toast("已停止");
    } catch (e) { toast("停止失败", "error"); }
  };

  $("btnRestart").onclick = async () => {
    const cfg = gatherConfig();
    try {
      toast("正在重启…");
      await post("/api/restart", cfg);
    } catch (e) { toast("重启失败", "error"); }
  };

  $("btnOpen").onclick = async () => {
    await post("/api/open", { port: parseInt($("pPort").value) || 8080 });
  };

  $("btnClearLog").onclick = async () => {
    await post("/api/clear-log");
    $("logBody").innerHTML = '<div class="log-empty">日志已清空</div>';
    state.logSeq = 0;
  };

  // 键盘快捷键
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      if (!$("btnStart").disabled) $("btnStart").click();
    }
  });
}

/* ── 启动 ────────────────────────────────────────────────── */

(function init() {
  $("logBody").innerHTML = '<div class="log-empty">等待启动服务…</div>';
  bind();
  poll();
  // 统一轮询节奏：800ms 一次，poll 内部有并发保护
  setInterval(poll, 800);
})();
