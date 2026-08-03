/* SQUILLA CONSOLE — front end.
   No framework: one module, explicit state, explicit renders. */

import { icon, hydrateIcons } from "./icons.js";

hydrateIcons();

const $ = (sel) => document.querySelector(sel);
const el = (id) => document.getElementById(id);

const PROBE_QUESTION = "罗塞塔石碑是什么?";

const state = {
  gateway: { connected: false },
  keys: [],
  activeId: null,
  models: [],
  modelSource: "—",
  modelsSeq: 0,
  streaming: false,
  currentTurn: null,
  providers: [],
};

/* ----------------------------------------------------------------- toasts */

function toast(message, kind = "info") {
  const box = el("toasts");
  const node = document.createElement("div");
  node.className = `toast toast--${kind}`;
  const ic = kind === "ok" ? "check" : kind === "bad" ? "x" : "circle-dot";
  node.innerHTML = `${icon(ic)}<div>${escapeHtml(message)}</div>`;
  box.appendChild(node);
  setTimeout(() => {
    node.classList.add("is-out");
    setTimeout(() => node.remove(), 340);
  }, kind === "bad" ? 7000 : 3600);
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* -------------------------------------------------------------------- http */

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...options,
  });
  // A session can lapse while the page sits open; bounce to the form rather
  // than letting every widget surface its own confusing error.
  if (resp.status === 401) {
    location.replace("/login");
    throw new Error("会话已过期");
  }
  const text = await resp.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!resp.ok) {
    throw new Error(data.detail || data.error || `${resp.status} ${resp.statusText}`);
  }
  return data;
}

function busy(button, on) {
  if (!button) return;
  if (on) {
    button.dataset.html = button.innerHTML;
    button.innerHTML = `${icon("loader-circle", "ic--spin")}处理中`;
    button.classList.add("is-busy");
    button.disabled = true;
  } else {
    if (button.dataset.html) button.innerHTML = button.dataset.html;
    button.classList.remove("is-busy");
    button.disabled = false;
  }
}

/* ------------------------------------------------------------------ tabs */

document.querySelectorAll(".navbtn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".navbtn").forEach((b) => b.classList.remove("is-on"));
    btn.classList.add("is-on");
    const tab = btn.dataset.tab;
    document.querySelectorAll(".panel").forEach((panel) => {
      const on = panel.dataset.panel === tab;
      panel.classList.toggle("is-live", on);
      if (on) {
        panel.classList.remove("enter");
        void panel.offsetWidth;
        panel.classList.add("enter");
      }
    });
    if (tab === "models" && state.models.length === 0) loadModelsFromGateway();
  });
});

/* ------------------------------------------------------------------ state */

async function refreshState() {
  try {
    const data = await api("/api/state");
    state.gateway = data.gateway || {};
    state.keys = data.keys || [];
    state.activeId = data.active_id;
    renderTop();
    renderKeys();
    renderQuick();
    renderGatewayCard();
  } catch (err) {
    toast(`读取状态失败: ${err.message}`, "bad");
  }
}

function activeKey() {
  return state.keys.find((k) => k.id === state.activeId) || null;
}

function renderTop() {
  const gw = state.gateway;
  const roGw = el("ro-gw");
  roGw.textContent = gw.connected ? `在线 v${gw.version || "?"}` : "离线";
  roGw.className = gw.connected ? "is-ok" : "is-bad";
  el("pulse").classList.toggle("is-live", !!gw.connected);

  const key = activeKey();
  el("ro-provider").textContent = gw.provider || key?.provider || "—";
  el("ro-model").textContent = gw.model || key?.model || "—";
  el("ro-key").textContent = key?.note || "—";
  el("cm-model").textContent = `模型 ${gw.model || key?.model || "—"}`;
  el("cm-key").textContent = `凭据 ${key?.note || "—"}`;
  if (gw.config_path) el("tty-session").title = gw.config_path;
}

function renderGatewayCard() {
  const gw = state.gateway;
  const rows = [
    ["WS", gw.ws || "—"],
    ["版本", gw.version || "—"],
    ["鉴权", gw.auth_mode || "—"],
    ["连接 ID", gw.conn_id || "—"],
    ["配置", gw.config_path || "—"],
    ["测活问题", gw.probe_question || PROBE_QUESTION],
  ];
  if (gw.error) rows.push(["错误", gw.error]);
  el("gw-kv").innerHTML = rows
    .map(([k, v]) => `<div class="kv__row"><b>${escapeHtml(k)}</b><span>${escapeHtml(v)}</span></div>`)
    .join("");
}

/* ------------------------------------------------------------------- keys */

function probeChip(test) {
  if (!test) return `<span class="chip"><span>未测</span></span>`;
  if (test.ok) {
    return `<span class="chip chip--ok">${icon("check")}<span>${test.latency_ms ?? "?"} ms</span></span>`;
  }
  return `<span class="chip chip--bad">${icon("x")}<span>${escapeHtml(
    test.status ? `HTTP ${test.status}` : "失败"
  )}</span></span>`;
}

function renderKeys() {
  const box = el("keys");
  if (state.keys.length === 0) {
    box.innerHTML = `<div class="empty">${icon("key")}<div>凭据库为空 — 用上面的表单添加第一条</div></div>`;
    return;
  }
  box.innerHTML = state.keys.map((k, i) => {
    const t = k.last_test;
    const probe = t
      ? `<div class="probe">
           <div class="probe__head">
             <span class="${t.ok ? "ok" : "bad"}">${t.ok ? "PASS" : "FAIL"}</span>
             <span>${t.latency_ms ?? "?"} ms</span>
             <span>${escapeHtml(t.model || "")}</span>
           </div>${escapeHtml(t.ok ? (t.answer || "").slice(0, 400) : t.error || "")}</div>`
      : "";
    return `
      <article class="keycard ${k.active ? "is-active" : ""}" style="animation-delay:${i * 0.04}s">
        <div class="keycard__top">
          <div style="min-width:0">
            <h3 class="keycard__note">${escapeHtml(k.note)}</h3>
            <div class="keycard__provider ${
              state.providers.length && !state.providers.some((p) => p.id === k.provider)
                ? "is-bad"
                : ""
            }">${escapeHtml(k.provider)}${
              state.providers.length && !state.providers.some((p) => p.id === k.provider)
                ? " · 网关不认识"
                : ""
            }</div>
          </div>
          <span class="spacer"></span>
          ${k.active ? `<span class="chip chip--on">${icon("zap")}<span>使用中</span></span>` : ""}
          ${probeChip(t)}
        </div>
        <div class="keycard__meta">
          <div><b>KEY</b><span>${escapeHtml(k.api_key_masked)}</span></div>
          <div><b>BASE</b><span>${escapeHtml(k.base_url || "—")}</span></div>
          <div><b>模型</b><span>${escapeHtml(k.model || "—")}</span></div>
        </div>
        ${probe}
        <div class="keycard__spring"></div>
        <div class="keycard__acts">
          <button class="btn btn--go btn--tiny" data-act="activate" data-id="${k.id}">
            ${icon("zap")}激活
          </button>
          <button class="btn btn--hot btn--tiny" data-act="test" data-id="${k.id}">
            ${icon("activity")}测活
          </button>
          <button class="btn btn--ghost btn--tiny" data-act="models" data-id="${k.id}">
            ${icon("layers")}拉模型
          </button>
          <button class="btn btn--ghost btn--tiny" data-act="edit" data-id="${k.id}">
            ${icon("pencil")}编辑
          </button>
          <button class="btn btn--ghost btn--tiny" data-act="del" data-id="${k.id}">
            ${icon("trash-2")}删除
          </button>
        </div>
      </article>`;
  }).join("");
}

el("keys").addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-act]");
  if (!btn) return;
  const { act, id } = btn.dataset;
  const entry = state.keys.find((k) => k.id === id);
  try {
    if (act === "activate") {
      busy(btn, true);
      await api(`/api/keys/${id}/activate`, { method: "POST" });
      toast("已激活并写入网关", "ok");
      await refreshState();
    } else if (act === "test") {
      busy(btn, true);
      const data = await api(`/api/keys/${id}/test`, { method: "POST" });
      const r = data.result || {};
      state.keys = data.keys || state.keys;
      renderKeys();
      showProbe(r, entry);
      toast(r.ok ? `测活通过 ${r.latency_ms}ms` : `测活失败: ${r.error || r.status}`,
            r.ok ? "ok" : "bad");
    } else if (act === "models") {
      busy(btn, true);
      // Activate this credential first so subsequent model switches apply to it.
      await api(`/api/keys/${id}/activate`, { method: "POST" });
      await loadModelsFromEndpoint(id);
      await refreshState();  // Update the active indicator
      document.querySelector('.navbtn[data-tab="models"]').click();
    } else if (act === "edit") {
      openEditor(entry);
    } else if (act === "del") {
      if (!confirm(`删除凭据「${entry?.note}」?`)) return;
      await api(`/api/keys/${id}`, { method: "DELETE" });
      toast("已删除", "ok");
      await refreshState();
    }
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
});

el("btn-key-add").addEventListener("click", async (event) => {
  const body = {
    note: el("f-note").value.trim() || "未命名凭据",
    provider: el("f-provider").value.trim() || "custom",
    base_url: el("f-base").value.trim(),
    api_key: el("f-key").value.trim(),
    model: el("f-model").value.trim(),
  };
  if (!body.api_key) return toast("API KEY 不能为空", "bad");
  const btn = event.currentTarget;
  try {
    busy(btn, true);
    await api("/api/keys", { method: "POST", body: JSON.stringify(body) });
    ["f-note", "f-base", "f-key", "f-model"].forEach((id) => (el(id).value = ""));
    toast("凭据已入库", "ok");
    await refreshState();
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
});

el("btn-keys-refresh").addEventListener("click", refreshState);

/* ------------------------------------------------------------ providers
   The provider id is not free text: onboarding.provider.configure rejects
   anything outside the gateway's own catalogue with
   `unknown provider: '...'`, and because that call only happens on 激活, a
   typo here surfaces later as an unrelated-looking failure. Both pickers are
   therefore populated from /api/providers. */

function providerOptions(selected) {
  const rows = state.providers;
  if (rows.length === 0) {
    const val = selected || "custom";
    return `<option value="${escapeHtml(val)}">${escapeHtml(val)}</option>`;
  }
  return rows
    .map((p) => {
      const on = p.id === selected ? " selected" : "";
      const need = p.requires_base_url ? " · 需 BASE URL" : "";
      return `<option value="${escapeHtml(p.id)}"${on}>${escapeHtml(p.id)} · ${escapeHtml(p.label)}${need}</option>`;
    })
    .join("");
}

async function loadProviders() {
  try {
    const data = await api("/api/providers");
    state.providers = data.providers || [];
    if (state.providers.length) {
      const cur = el("f-provider").value || "custom";
      el("f-provider").innerHTML = providerOptions(cur);
      // The catalogue lands after the first render, so redraw to apply the
      // unknown-provider badges that could not be computed yet.
      renderKeys();
    }
  } catch {
    /* Leave the single fallback option in place; the form still works. */
  }
}

/* -------------------------------------------------------------- editor */
let editingId = null;
const sheet = el("edit-sheet");

function openEditor(entry) {
  if (!entry) return;
  editingId = entry.id;
  el("e-note").value = entry.note || "";

  // A stored provider the gateway does not recognise would otherwise be swapped
  // for the first option without the user noticing, hiding the very field that
  // made 激活 fail. Surface it as a selected-but-invalid option instead.
  const known = state.providers.some((p) => p.id === entry.provider);
  let html = providerOptions(entry.provider);
  if (entry.provider && !known && state.providers.length) {
    html =
      `<option value="${escapeHtml(entry.provider)}" selected>` +
      `${escapeHtml(entry.provider)} · 网关不认识，请重新选择</option>` +
      html;
  }
  el("e-provider").innerHTML = html;
  el("e-base").value = entry.base_url || "";
  el("e-key").value = "";          // Blank means "keep the stored secret".
  el("e-model").value = entry.model || "";
  sheet.hidden = false;
  el("e-note").focus();
}

function closeEditor() {
  sheet.hidden = true;
  editingId = null;
  el("e-key").value = "";
}

el("edit-close").addEventListener("click", closeEditor);
el("edit-cancel").addEventListener("click", closeEditor);
sheet.addEventListener("click", (e) => {
  if (e.target.dataset.close) closeEditor();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !sheet.hidden) closeEditor();
});

el("edit-save").addEventListener("click", async (event) => {
  if (!editingId) return;
  const body = {
    note: el("e-note").value.trim() || "未命名凭据",
    provider: el("e-provider").value,
    base_url: el("e-base").value.trim(),
    model: el("e-model").value.trim(),
  };
  // Only send api_key when the field was filled, so an untouched form cannot
  // wipe the stored secret.
  const newKey = el("e-key").value.trim();
  if (newKey) body.api_key = newKey;

  const btn = event.currentTarget;
  try {
    busy(btn, true);
    await api(`/api/keys/${editingId}`, { method: "PUT", body: JSON.stringify(body) });
    toast("凭据已更新", "ok");
    closeEditor();
    await refreshState();
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
});

el("btn-test-all").addEventListener("click", async (event) => {
  const btn = event.currentTarget;
  try {
    busy(btn, true);
    const data = await api("/api/test/all", { method: "POST" });
    state.keys = data.keys || state.keys;
    renderKeys();
    const results = Object.values(data.results || {});
    const pass = results.filter((r) => r.ok).length;
    toast(`测活完成: ${pass}/${results.length} 通过`, pass ? "ok" : "bad");
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
});

/* -------------------------------------------------------------- quick bar */

function renderQuick() {
  const keySel = el("q-key");
  keySel.innerHTML = state.keys
    .map((k) => `<option value="${k.id}" ${k.active ? "selected" : ""}>${escapeHtml(k.note)} · ${escapeHtml(k.provider)}</option>`)
    .join("") || `<option value="">（无凭据）</option>`;

  const modelSel = el("q-model");
  const current = state.gateway.model || activeKey()?.model || "";
  const ids = state.models.map((m) => m.id);
  if (current && !ids.includes(current)) ids.unshift(current);
  modelSel.innerHTML = ids.length
    ? ids.map((id) => `<option value="${escapeHtml(id)}" ${id === current ? "selected" : ""}>${escapeHtml(id)}</option>`).join("")
    : `<option value="">（先拉取模型列表）</option>`;
}

el("q-key").addEventListener("change", async (event) => {
  const id = event.target.value;
  if (!id) return;
  try {
    await api(`/api/keys/${id}/activate`, { method: "POST" });
    toast("凭据已切换", "ok");
    await refreshState();
  } catch (err) {
    toast(err.message, "bad");
  }
});

el("q-model").addEventListener("change", (event) => switchModel(event.target.value));

el("btn-probe-active").addEventListener("click", async (event) => {
  const key = activeKey();
  if (!key) return toast("先添加并激活一条凭据", "bad");
  const btn = event.currentTarget;
  try {
    busy(btn, true);
    const data = await api(`/api/keys/${key.id}/test`, { method: "POST" });
    state.keys = data.keys || state.keys;
    renderKeys();
    showProbe(data.result || {}, key);
    toast(data.result?.ok ? `通过 ${data.result.latency_ms}ms` : `失败: ${data.result?.error}`,
          data.result?.ok ? "ok" : "bad");
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
});

function showProbe(result, key) {
  const verdict = result.ok ? "PASS" : "FAIL";
  const head = `${verdict} · ${result.latency_ms ?? "?"}ms · ${result.model || ""}`;
  const body = result.ok ? result.answer || "" : result.error || "";
  el("probe-last").innerHTML =
    `<div class="probe__head"><span class="${result.ok ? "ok" : "bad"}">${escapeHtml(head)}</span>` +
    `<span class="probe__src">直连端点</span></div>` +
    `<div class="dim">${escapeHtml(key?.note || "")} — 问题: ${escapeHtml(PROBE_QUESTION)}</div>\n` +
    escapeHtml(body);
  // Leave a trace in the transcript too — the terminal is the operator's log.
  addTurn(
    "system",
    `测活 ${verdict} · ${key?.note || "?"} · ${result.model || "?"} · ` +
    `${result.latency_ms ?? "?"}ms（直连端点，不经网关）`
  );
}

/* ----------------------------------------------------------------- models */

async function loadModelsFromGateway(event) {
  const btn = event?.currentTarget;
  const seq = ++state.modelsSeq;
  try {
    busy(btn, true);
    const data = await api("/api/models");
    if (seq !== state.modelsSeq) return; // a newer pull already won
    const models = data.models || [];

    // The gateway only lists what its configured provider exposes. With no
    // provider wired yet that is legitimately empty — fall back to the active
    // credential's own /models so the catalogue is never blank for no reason.
    if (models.length === 0 && (el("q-key").value || state.activeId)) {
      await loadModelsFromEndpoint(null, null, seq);
      return;
    }

    state.models = models;
    state.modelSource = "网关";
    renderModels();
    renderQuick();
    toast(`网关返回 ${models.length} 个模型`, models.length ? "ok" : "bad");
  } catch (err) {
    if (seq === state.modelsSeq) toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
}

async function loadModelsFromEndpoint(keyId, event, inheritSeq) {
  const id = keyId || el("q-key").value || state.activeId;
  if (!id) return toast("没有可用凭据", "bad");
  const btn = event?.currentTarget;
  const seq = inheritSeq ?? ++state.modelsSeq;
  try {
    busy(btn, true);
    const data = await api(`/api/models/endpoint?key_id=${encodeURIComponent(id)}`);
    if (seq !== state.modelsSeq) return;
    state.models = data.models || [];
    state.modelSource = "端点";
    renderModels();
    renderQuick();
    toast(`端点返回 ${data.count} 个模型`, "ok");
  } catch (err) {
    if (seq === state.modelsSeq) toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
}

el("btn-models-gw").addEventListener("click", loadModelsFromGateway);
el("btn-models-ep").addEventListener("click", (e) => loadModelsFromEndpoint(null, e));

const filterBox = el("m-filter");
filterBox.addEventListener("input", () => {
  filterBox.parentElement.classList.toggle("has-text", filterBox.value.length > 0);
  renderModels();
});
el("m-clear").addEventListener("click", () => {
  filterBox.value = "";
  filterBox.parentElement.classList.remove("has-text");
  renderModels();
  filterBox.focus();
});

function renderModels() {
  const q = filterBox.value.trim().toLowerCase();
  const rows = state.models.filter(
    (m) => !q || m.id.toLowerCase().includes(q) || (m.provider || "").toLowerCase().includes(q)
  );
  const current = state.gateway.model || activeKey()?.model || "";
  el("m-count").innerHTML =
    `${icon("layers")}<span>${rows.length} / ${state.models.length} · 来源 ${escapeHtml(state.modelSource)}</span>`;
  el("m-current").innerHTML =
    `${icon("check")}<span>当前 ${escapeHtml(current || "—")}</span>`;

  const box = el("models");
  if (rows.length === 0) {
    box.innerHTML = `<div class="empty">${icon("layers")}<div>${
      state.models.length === 0
        ? "没有模型 — 点「从端点拉取」或「网关列表」"
        : "该过滤条件没有匹配的模型"
    }</div></div>`;
    return;
  }
  box.innerHTML = rows.map((m, i) => `
    <button class="model ${m.id === current ? "is-on" : ""}" data-model="${escapeHtml(m.id)}"
            style="animation-delay:${Math.min(i, 24) * 0.02}s" type="button">
      <div class="model__id">${escapeHtml(m.id)}</div>
      <div class="model__row">
        <span class="chip">${icon("cpu")}<span>${escapeHtml(m.provider || "—")}</span></span>
        ${m.id === current
          ? `<span class="chip chip--on">${icon("check")}<span>当前</span></span>`
          : `<span class="chip chip--ghost">${icon("zap")}<span>点击切换</span></span>`}
      </div>
    </button>`).join("");
}

el("models").addEventListener("click", (event) => {
  const card = event.target.closest("button[data-model]");
  if (card) switchModel(card.dataset.model);
});

async function switchModel(model) {
  if (!model) return;
  try {
    await api("/api/model/switch", { method: "POST", body: JSON.stringify({ model }) });
    toast(`已切换到 ${model}`, "ok");
    await refreshState();
    renderModels();
  } catch (err) {
    toast(err.message, "bad");
  }
}

/* ------------------------------------------------------------------- chat */

function addTurn(role, text = "") {
  const log = el("log");
  const wrap = document.createElement("div");
  const cls = role === "user" ? "me" : role === "assistant" ? "ai" : "sys";
  const tag = role === "user" ? "你" : role === "assistant" ? "SQUILLA" : "系统";
  const ic = role === "user" ? "arrow-right" : role === "assistant" ? "square-terminal" : "circle-dot";
  wrap.className = `turn turn--${cls}`;
  wrap.innerHTML = `<div class="turn__tag">${icon(ic)}${tag}</div><div class="turn__body"></div>`;
  wrap.querySelector(".turn__body").textContent = text;
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;
  return wrap.querySelector(".turn__body");
}

function setStreaming(on) {
  state.streaming = on;
  el("btn-send").disabled = on;
  el("btn-stop").disabled = !on;
  el("cm-state").textContent = on ? "生成中" : "空闲";
  if (!on && state.currentTurn) {
    state.currentTurn.classList.remove("caret");
    state.currentTurn = null;
  }
}

async function send() {
  const box = el("msg");
  const text = box.value.trim();
  if (!text) return;
  if (!state.gateway.connected) return toast("网关未连接", "bad");
  addTurn("user", text);
  box.value = "";
  box.style.height = "auto";
  setStreaming(true);
  try {
    await api("/api/chat", { method: "POST", body: JSON.stringify({ message: text }) });
  } catch (err) {
    addTurn("system", `发送失败: ${err.message}`);
    setStreaming(false);
  }
}

el("btn-send").addEventListener("click", send);
el("btn-stop").addEventListener("click", async () => {
  try {
    await api("/api/chat/abort", { method: "POST" });
    toast("已请求中断", "ok");
  } catch (err) {
    toast(err.message, "bad");
  }
});
el("btn-clear").addEventListener("click", () => (el("log").innerHTML = ""));

el("btn-history").addEventListener("click", async (event) => {
  const btn = event.currentTarget;
  try {
    busy(btn, true);
    const data = await api("/api/chat/history?limit=40");
    el("log").innerHTML = "";
    for (const m of data.messages || []) {
      const role = m.role || m.author || "assistant";
      const text = typeof m.content === "string"
        ? m.content
        : (m.content || []).map((p) => p.text || "").join("");
      if (text) addTurn(role === "user" ? "user" : "assistant", text);
    }
    toast(`载入 ${(data.messages || []).length} 条历史`, "ok");
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
});

const msgBox = el("msg");
msgBox.addEventListener("input", () => {
  msgBox.style.height = "auto";
  msgBox.style.height = `${Math.min(msgBox.scrollHeight, 190)}px`;
});
msgBox.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    send();
  }
});

/* ------------------------------------------------------- gateway events */

function connectBridge() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sock = new WebSocket(`${proto}://${location.host}/ws`);

  sock.onmessage = (event) => {
    let frame;
    try { frame = JSON.parse(event.data); } catch { return; }

    if (frame.type === "gateway") {
      state.gateway.connected = frame.connected;
      if (frame.version) state.gateway.version = frame.version;
      state.gateway.error = frame.error;
      renderTop();
      renderGatewayCard();
      if (frame.connected) refreshState();
      return;
    }
    if (frame.type !== "event") return;

    const name = frame.event || "";
    const payload = frame.payload || {};

    if (name.endsWith("text_delta") || name.endsWith("message_delta")) {
      const delta = payload.delta ?? payload.text ?? "";
      if (!state.currentTurn) {
        state.currentTurn = addTurn("assistant", "");
        state.currentTurn.classList.add("caret");
      }
      state.currentTurn.textContent += delta;
      el("log").scrollTop = el("log").scrollHeight;
      return;
    }
    if (name.endsWith("tool_call") || name.endsWith("tool.start")) {
      const target = state.currentTurn || addTurn("assistant", "");
      state.currentTurn = target;
      const chip = document.createElement("div");
      chip.className = "tool";
      chip.textContent = `⚙ ${payload.name || payload.tool || "tool"}`;
      target.appendChild(chip);
      return;
    }
    if (name.endsWith("message_complete") || name.endsWith("turn_complete") ||
        name.endsWith("idle") || name.endsWith("aborted")) {
      if (payload.text && state.currentTurn && !state.currentTurn.textContent) {
        state.currentTurn.textContent = payload.text;
      }
      setStreaming(false);
      return;
    }
    if (name.endsWith("error")) {
      addTurn("system", payload.message || JSON.stringify(payload));
      setStreaming(false);
    }
  };

  sock.onclose = () => {
    state.gateway.connected = false;
    renderTop();
    setTimeout(connectBridge, 1600);
  };
}

/* ------------------------------------------------------------- ambience */

function startField() {
  const canvas = el("field");
  const ctx = canvas.getContext("2d");
  let w, h, dots;
  const COUNT = 58;

  function resize() {
    w = canvas.width = window.innerWidth;
    h = canvas.height = window.innerHeight;
  }
  function seed() {
    dots = Array.from({ length: COUNT }, () => ({
      x: Math.random() * w,
      y: Math.random() * h,
      vx: (Math.random() - 0.5) * 0.16,
      vy: (Math.random() - 0.5) * 0.16,
      r: Math.random() * 1.5 + 0.4,
    }));
  }
  resize(); seed();
  window.addEventListener("resize", () => { resize(); seed(); });

  (function frame() {
    ctx.clearRect(0, 0, w, h);
    for (const d of dots) {
      d.x += d.vx; d.y += d.vy;
      if (d.x < 0 || d.x > w) d.vx *= -1;
      if (d.y < 0 || d.y > h) d.vy *= -1;
    }
    for (let i = 0; i < dots.length; i++) {
      for (let j = i + 1; j < dots.length; j++) {
        const a = dots[i], b = dots[j];
        const dist = Math.hypot(a.x - b.x, a.y - b.y);
        if (dist < 148) {
          ctx.strokeStyle = `rgba(74,125,255,${(1 - dist / 148) * 0.16})`;
          ctx.lineWidth = 0.6;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
      ctx.fillStyle = "rgba(53,242,230,0.42)";
      ctx.beginPath();
      ctx.arc(dots[i].x, dots[i].y, dots[i].r, 0, Math.PI * 2);
      ctx.fill();
    }
    requestAnimationFrame(frame);
  })();
}

/* ------------------------------------------------------------------ boot */

/* On phones the sidebar cards (quick switch, gateway status, last probe) are
   collapsed so the transcript owns the screen; this toggles them back in. The
   button itself is hidden by CSS above 860px, so no width check is needed. */
(() => {
  const btn = document.getElementById("btn-aside");
  const panel = document.querySelector('[data-panel="chat"]');
  if (!btn || !panel) return;
  btn.addEventListener("click", () => {
    const open = panel.classList.toggle("show-aside");
    btn.classList.toggle("is-on-mobile", open);
    btn.setAttribute("aria-expanded", String(open));
    if (open) panel.querySelector(".aside")?.scrollIntoView({ block: "nearest" });
  });
})();

/* Only offer logout when a password is actually configured, so the rail does
   not show a dead control on an unauthenticated deployment. */
(async () => {
  const btn = document.getElementById("btn-logout");
  if (!btn) return;
  try {
    const auth = await api("/api/auth");
    if (!auth.enabled) return;
    btn.hidden = false;
    btn.addEventListener("click", async () => {
      await api("/api/logout", { method: "POST" }).catch(() => {});
      location.replace("/login");
    });
  } catch { /* the api() helper already redirects on 401 */ }
})();

startField();
connectBridge();
refreshState();
loadProviders();
addTurn("system", `控制台就绪。测活固定问题：${PROBE_QUESTION}`);
setInterval(() => { if (!state.streaming) refreshState(); }, 20000);
