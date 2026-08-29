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
  // Catalogue metadata (source, age, per-key errors) kept beside the list so
  // the UI can say *why* a catalogue is short or stale instead of just showing
  // fewer cards than expected.
  catalog: {},
  // OpenSquilla version/update state and the running install job.
  sys: { current: {}, update: {}, releases: [], job: null },
  jobCursor: 0,
  jobPoll: null,
};

/* ----------------------------------------------------------------- toasts */

function toast(message, kind = "info") {
  const box = el("toasts");
  const node = document.createElement("div");
  node.className = `toast toast--${kind}`;
  const ic = kind === "ok" ? "check"
    : kind === "bad" ? "x"
    : kind === "warn" ? "triangle-alert"
    : "circle-dot";
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

/** Coarse duration for "synced N ago" labels. */
function humanAge(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s < 0) return "—";
  if (s < 60) return `${Math.round(s)} 秒`;
  if (s < 3600) return `${Math.round(s / 60)} 分钟`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} 小时`;
  return `${(s / 86400).toFixed(1)} 天`;
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
    // The version/update read shells out and hits GitHub, so it is deferred
    // until the panel is actually opened rather than run at boot.
    if (tab === "system" && !state.sys.current?.version) loadSystem();
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
  // 值单行省略，title 给全文，点击复制 —— UUID / 配置路径都是要拿去用的东西。
  el("gw-kv").innerHTML = rows
    .map(([k, v]) => `<div class="kv__row"><b>${escapeHtml(k)}</b>`
      + `<span title="${escapeHtml(v)}" data-copy="${escapeHtml(v)}">${escapeHtml(v)}</span></div>`)
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
             ${t.via === "gateway" ? `<span class="probe__via">经 OPENSQUILLA</span>` : ""}
           </div>${escapeHtml(
             t.ok
               ? t.answer
                 ? (t.answer || "").slice(0, 400)
                 : `网关接受了这条凭据，一次性探测通过${
                     t.first_response_ms ? `，首字 ${t.first_response_ms} ms` : ""
                   }。要看模型实际回答请用对话面板。`
               : t.error || ""
           )}</div>`
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
          <button class="btn btn--ghost btn--tiny" data-act="balance" data-id="${k.id}">
            ${icon("wallet")}查余额
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
      if (r.stale_activation) {
        // 测活通过但网关在用别的配置 —— 聊天会 401，必须说出来。
        toast(r.stale_activation, "warn");
      } else {
        toast(r.ok ? `测活通过 ${r.latency_ms}ms` : `测活失败: ${r.error || r.status}`,
              r.ok ? "ok" : "bad");
      }
    } else if (act === "balance") {
      busy(btn, true);
      const data = await api(`/api/keys/${id}/balance`);
      if (data.ok) {
        const bits = [];
        if (data.remaining != null) bits.push(`余额 ${data.remaining} ${data.unit}`);
        if (data.available != null) bits.push(`可用 ${data.available}`);
        if (data.used != null) bits.push(`已用 ${data.used}`);
        if (data.limit != null) bits.push(`额度 ${data.limit}`);
        // 基元律动一个钱包给所有 KEY 共用，不标一下会以为是单 KEY 余额。
        if (data.scope) bits.push(data.scope);
        toast(bits.join(" · "), "ok");
      } else {
        // 余额接口不是 OpenAI 规范的一部分，很多服务商压根不提供。
        toast(data.error || "查不到余额", "warn");
      }
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
    // 选填，只为查余额用（基元律动的余额接口不认 API KEY）
    account: el("f-account").value.trim(),
    password: el("f-password").value,
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
  el("e-account").value = entry.account || "";
  el("e-password").value = "";     // 同上，留空表示不改密码
  el("e-password").placeholder = entry.has_password
    ? "已存密码，留空 = 不改动"
    : "未存密码，填了才能查余额";
  // 已经填过账号的默认展开，省得以为没保存。
  el("e-web-wrap").open = !!(entry.account || entry.has_password);
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
  // 账号是普通字段可以清空；密码同 api_key，留空表示保留原值。
  body.account = el("e-account").value.trim();
  const newPass = el("e-password").value;
  if (newPass) body.password = newPass;

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

/* The catalogue is a server-side cache now: reads are instant and never touch
   the network, and a sync is an explicit action. `adoptCatalog` is shared by
   both paths so the staleness chips can't drift between them. */
function adoptCatalog(data) {
  state.models = data.models || [];
  state.catalog = {
    source: data.source || null,
    sourcesTried: data.sources_tried || [],
    syncedAt: data.synced_at || null,
    ageSeconds: data.age_seconds,
    stale: !!data.stale,
    cooldown: !!data.cooldown,
    lastError: data.last_error || null,
    errors: data.errors || [],
  };
  state.modelSource = SOURCE_LABEL[data.source] || data.source || "—";
  renderModels();
  renderQuick();
}

const SOURCE_LABEL = {
  endpoint: "端点直拉",
  gateway: "网关目录",
  discover: "提供方探测",
  cache: "本地缓存",
};

async function loadModelsFromGateway(event) {
  const btn = event?.currentTarget;
  const seq = ++state.modelsSeq;
  try {
    busy(btn, true);
    const data = await api("/api/models");
    if (seq !== state.modelsSeq) return; // a newer pull already won
    adoptCatalog(data);
    const n = state.models.length;
    if (n) toast(`目录 ${n} 个模型 · 来源 ${state.modelSource}`, "ok");
    else toast(data.last_error ? `目录为空: ${data.last_error}` : "目录为空，点「立即同步」", "bad");
  } catch (err) {
    if (seq === state.modelsSeq) toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
}

/* Force a multi-source refresh. This is the fix for a suspended key blanking
   the catalogue: every stored credential on an endpoint is tried, so one
   frozen account fails alone instead of taking the whole list down. */
async function syncModels(event) {
  const btn = event?.currentTarget;
  const seq = ++state.modelsSeq;
  try {
    busy(btn, true);
    const data = await api("/api/models?refresh=1");
    if (seq !== state.modelsSeq) return;
    adoptCatalog(data);
    const n = state.models.length;
    if (data.ok && n) {
      toast(`同步到 ${n} 个模型 · 来源 ${state.modelSource}`, "ok");
    } else {
      toast(`同步失败: ${data.last_error || "所有数据源均未返回模型"}`, "bad");
    }
    // Per-source notes matter here: they name which key was rejected and why,
    // which is the difference between "endpoint down" and "that account froze".
    (data.errors || []).slice(0, 4).forEach((line) => toast(line, "warn"));
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
el("btn-models-sync").addEventListener("click", syncModels);
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
  const cat = state.catalog || {};
  el("m-count").innerHTML =
    `${icon("layers")}<span>${rows.length} / ${state.models.length} · 来源 ${escapeHtml(state.modelSource)}</span>`;
  el("m-current").innerHTML =
    `${icon("check")}<span>当前 ${escapeHtml(current || "—")}</span>`;

  // Age and staleness are shown rather than hidden: a cached catalogue is fine,
  // silently serving a day-old one as if it were live is not.
  const ageChip = el("m-age");
  if (ageChip) {
    if (cat.syncedAt) {
      ageChip.hidden = false;
      ageChip.className = `chip ${cat.stale ? "chip--warn" : ""}`;
      ageChip.innerHTML = `${icon("clock")}<span>${escapeHtml(
        `${humanAge(cat.ageSeconds)}前同步${cat.stale ? " · 已过期" : ""}`
      )}</span>`;
    } else {
      ageChip.hidden = true;
    }
  }

  const box = el("models");
  if (rows.length === 0) {
    box.innerHTML = `<div class="empty">${icon("layers")}<div>${
      state.models.length === 0
        ? escapeHtml(cat.lastError
            ? `目录为空：${cat.lastError}`
            : "没有模型 — 点「立即同步」")
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

/* ------------------------------------------------- system / version panel */

/* OpenSquilla is installed as a uv tool from a GitHub Release wheel. That
   detail leaks into the UI for one reason worth stating: PyPI's `opensquilla`
   stops at 0.3.0 while releases are at 0.5.x, so "install the latest" has to
   mean the newest *release wheel*, never the newest PyPI version. The server
   enforces this; the picker below only ever offers versions that ship a wheel. */

async function loadSystem(event) {
  const btn = event?.currentTarget;
  try {
    busy(btn, true);
    const data = await api("/api/opensquilla");
    state.sys.current = data.current || {};
    state.sys.update = data.update || {};
    state.sys.layout = data.layout || {};
    state.sys.extras = data.extras || [];
    state.sys.releases = (data.update || {}).installable || [];
    renderSystem();
    // A job may already be running from a previous page load.
    if (data.job) adoptJob(data.job, { resetCursor: true });
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
}

function renderSystem() {
  const cur = state.sys.current || {};
  const upd = state.sys.update || {};
  const lay = state.sys.layout || {};

  const rows = [
    ["已安装版本", cur.version || "读不到"],
    ["最新版本", upd.latest || "未知"],
    ["网关", cur.gateway_running ? "运行中" : "未运行"],
    ["附加组件", (state.sys.extras || []).join(", ") || "无"],
    ["安装目录", lay.tool_dir || "—"],
    ["发布仓库", lay.repo || "—"],
  ];
  if (upd.channel_error) rows.push(["更新通道", `读取失败: ${upd.channel_error}`]);
  if (upd.releases_error) rows.push(["发布列表", `读取失败: ${upd.releases_error}`]);

  // Key in <b>, value in <span> — matching the existing .kv__row contract in
  // app.css. Swapping them renders the row with the two colours inverted.
  el("sys-version").innerHTML = rows.map(([k, v]) => `
    <div class="kv__row"><b>${escapeHtml(k)}</b><span title="${escapeHtml(v)}">${escapeHtml(v)}</span></div>
  `).join("");

  const note = el("sys-note");
  if (upd.update_available) {
    const size = upd.latest_wheel_size
      ? ` · wheel ${(upd.latest_wheel_size / 1048576).toFixed(0)} MB`
      : "";
    note.className = "note note--hot";
    note.textContent = `有新版本 ${upd.latest}（当前 ${upd.current || "?"}）${size}。安装会停网关、替换环境、再启动。`;
  } else if (upd.latest && upd.current) {
    note.className = "note";
    note.textContent = `已是最新（${upd.current}）。仍可在下拉里选择任意已发布版本重装或回退。`;
  } else {
    note.className = "note";
    note.textContent = "";
  }

  renderReleaseOptions();
  el("sys-gw").innerHTML = `
    <div class="kv__row"><b>状态</b><span title="${escapeHtml(cur.gateway_status || "—")}">${escapeHtml(cur.gateway_status || "—")}</span></div>
    <div class="kv__row"><b>控制台链路</b><span>${state.gateway.connected ? "已连接" : "未连接"}</span></div>`;

  const info = state.sys.authInfo || {};
  el("sys-auth").innerHTML = `
    <div class="kv__row"><b>账号</b><span>${escapeHtml(info.user || "—")}</span></div>
    <div class="kv__row"><b>上次修改</b><span>${
      info.updated_at ? escapeHtml(new Date(info.updated_at * 1000).toLocaleString("zh-CN")) : "—"
    }</span></div>
    <div class="kv__row"><b>已修改次数</b><span>${escapeHtml(String(info.rotations ?? "—"))}</span></div>`;
}

function renderReleaseOptions() {
  const box = el("sys-target");
  const showPre = el("sys-prerelease").checked;
  const rows = (state.sys.releases || []).filter((r) => showPre || !r.prerelease);
  const keep = box.value;
  box.innerHTML = rows.map((r) => {
    const mark = r.version === (state.sys.current || {}).version ? " · 当前" : "";
    const pre = r.prerelease ? " · 预发布" : "";
    return `<option value="${escapeHtml(r.version)}">${escapeHtml(r.version)}${mark}${pre}</option>`;
  }).join("") || `<option value="">（没有可安装版本）</option>`;
  // Preserve the operator's pick across re-renders; otherwise default to the
  // newest offered version.
  if (keep && rows.some((r) => r.version === keep)) box.value = keep;
}

el("sys-prerelease").addEventListener("change", renderReleaseOptions);
el("btn-sys-refresh").addEventListener("click", loadSystem);

el("btn-sys-install").addEventListener("click", async (event) => {
  const version = el("sys-target").value;
  if (!version) return toast("没有选中版本", "bad");
  const cur = (state.sys.current || {}).version || "?";
  const restart = el("sys-restart").checked;
  // Replacing the runtime takes the gateway down; make that explicit rather
  // than surprising someone mid-conversation.
  const warn = restart
    ? "安装过程中网关会短暂停止服务。"
    : "已选择不自动重启：安装后网关会保持停止状态，需要手动启动。";
  if (!confirm(`将 OpenSquilla 从 ${cur} 切换到 ${version}。\n${warn}\n\n继续？`)) return;
  // event.currentTarget is null once an await has run, so the finally block
  // would receive null, hit busy()'s `if (!button) return`, and leave the
  // button disabled forever — every later click then silently does nothing.
  // The rest of this file captures the element synchronously; match that.
  const btn = event.currentTarget;
  try {
    busy(btn, true);
    const data = await api("/api/opensquilla/install", {
      method: "POST",
      body: JSON.stringify({ version, restart_gateway: restart }),
    });
    toast(`已开始安装 ${version}`, "ok");
    adoptJob(data.job, { resetCursor: true });
    startJobPolling();
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
});

document.querySelectorAll("[data-gw]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const action = btn.dataset.gw;
    if (action === "stop" && !confirm("停止网关后对话功能不可用，确认停止？")) return;
    try {
      busy(btn, true);
      const data = await api(`/api/opensquilla/gateway/${action}`, { method: "POST" });
      const out = el("sys-gw-out");
      out.hidden = false;
      out.textContent = data.output || "(无输出)";
      toast(`${action}: ${data.ok ? "成功" : "失败"}`, data.ok ? "ok" : "bad");
      await loadSystem();
      await refreshState();
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      busy(btn, false);
    }
  });
});

/* -- install job log ---------------------------------------------------- */

function adoptJob(job, { resetCursor = false } = {}) {
  if (!job) return;
  const box = el("sys-joblog");
  if (resetCursor) {
    state.jobCursor = 0;
    box.textContent = "";
  }
  if (job.lines?.length) {
    box.textContent += (box.textContent ? "\n" : "") + job.lines.join("\n");
    box.scrollTop = box.scrollHeight;
  }
  state.jobCursor = job.next_cursor ?? state.jobCursor;
  state.sys.job = job;

  const chip = el("sys-job-state");
  const label = { running: "进行中", done: "完成", failed: "失败" }[job.state] || job.state;
  chip.className = `chip ${job.state === "done" ? "chip--on" : job.state === "failed" ? "chip--bad" : ""}`;
  chip.innerHTML = `${icon(job.state === "running" ? "loader-circle" : job.state === "done" ? "check" : "triangle-alert",
    job.state === "running" ? "ic--spin" : "")}<span>${escapeHtml(`${label} · ${job.elapsed}s`)}</span>`;

  if (job.state !== "running") {
    stopJobPolling();
    if (job.state === "done") {
      const r = job.result || {};
      toast(`安装完成: ${r.from || "?"} → ${r.to || "?"}${
        r.healthz_ok === false ? "（网关健康检查未通过）" : ""}`, r.healthz_ok === false ? "warn" : "ok");
    } else {
      toast(`安装失败: ${job.error || "未知错误"}`, "bad");
    }
    // The version readout and the link state both changed underneath us.
    loadSystem();
    refreshState();
  }
}

function startJobPolling() {
  stopJobPolling();
  state.jobPoll = setInterval(async () => {
    try {
      const data = await api(`/api/opensquilla/job?since=${state.jobCursor}`);
      if (data.job) adoptJob(data.job);
      else stopJobPolling();
    } catch {
      // A restart of the console itself kills the job log; stop rather than
      // spamming the toast rail.
      stopJobPolling();
    }
  }, 1500);
}

function stopJobPolling() {
  if (state.jobPoll) {
    clearInterval(state.jobPoll);
    state.jobPoll = null;
  }
}

/* -- password change (from the system panel, not the forced gate) -------- */

el("btn-pw-save").addEventListener("click", async (event) => {
  const current = el("pw-current").value;
  const next = el("pw-new").value;
  const again = el("pw-new2").value;
  const user = el("pw-user").value.trim();
  if (!current || !next) return toast("请填写当前密码和新密码", "bad");
  if (next !== again) return toast("两次输入的新密码不一致", "bad");
  const btn = event.currentTarget;
  try {
    busy(btn, true);
    const data = await api("/api/password", {
      method: "POST",
      body: JSON.stringify({ current, new_password: next, new_user: user || null }),
    });
    state.sys.authInfo = data.auth_info || {};
    ["pw-current", "pw-new", "pw-new2", "pw-user"].forEach((id) => { el(id).value = ""; });
    toast("密码已修改，其他设备上的登录已失效", "ok");
    renderSystem();
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
});

/* ------------------------------------------------- forced password gate */

/* The server rejects every business endpoint until the bootstrap credential is
   rotated, so this screen is the way forward rather than a decoration. It is
   rendered over the shell instead of on /login because the rotation needs an
   authenticated session. */

async function checkMustChange() {
  try {
    const auth = await api("/api/auth");
    state.sys.authInfo = auth.auth_info || state.sys.authInfo || {};
    const gate = el("pw-gate");
    if (auth.must_change) {
      gate.hidden = false;
      document.body.classList.add("is-gated");
      el("gate-current").focus();
      return true;
    }
    gate.hidden = true;
    document.body.classList.remove("is-gated");
    return false;
  } catch {
    return false;
  }
}

el("gate-save").addEventListener("click", async (event) => {
  const current = el("gate-current").value;
  const next = el("gate-new").value;
  const again = el("gate-new2").value;
  const user = el("gate-user").value.trim();
  const err = el("gate-err");
  const fail = (msg) => {
    err.hidden = false;
    err.textContent = msg;
  };
  err.hidden = true;
  if (!current || !next) return fail("请填写当前密码和新密码");
  if (next !== again) return fail("两次输入的新密码不一致");
  const btn = event.currentTarget;
  try {
    busy(btn, true);
    const data = await api("/api/password", {
      method: "POST",
      body: JSON.stringify({ current, new_password: next, new_user: user || null }),
    });
    state.sys.authInfo = data.auth_info || {};
    el("pw-gate").hidden = true;
    document.body.classList.remove("is-gated");
    toast("初始密码已修改，控制台已解锁", "ok");
    // Everything was 403 until a moment ago, so load it all now.
    await refreshState();
    await loadProviders();
    await loadModelsFromGateway();
    connectBridge();
  } catch (e) {
    fail(e.message);
  } finally {
    busy(btn, false);
  }
});

el("gate-logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  location.replace("/login");
});

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

/* --------------------------------------------------------- 回复轮次管理 */

/* 每个助手回复对应一个 bubble，按网关给的 turn_id 索引。
   以前只用一个全局 state.currentTurn，而关闭它依赖的结束事件名
   （message_complete / turn_complete / idle）网关根本不会发 —— 真实事件是
   session.event.done。于是 bubble 永远不关，下一轮回复继续往同一个
   节点里 append，所有回复糊成一坨。改成按 turn_id 建档后，每轮各自
   独立，谁先谁后都不会串。 */
const turns = new Map();

/* 网关的状态机：idle -> thinking -> streaming -> done，另加排队与失败态。 */
const PHASE_TEXT = {
  queued: "已排队",
  running: "已受理",
  thinking: "思考中",
  streaming: "输出中",
  done: "完成",
  aborted: "已中断",
  error: "失败",
};

function ensureTurn(turnId) {
  const key = turnId || "_pending";
  let rec = turns.get(key);
  if (rec) return rec;

  const log = el("log");
  const wrap = document.createElement("div");
  wrap.className = "turn turn--ai";
  wrap.innerHTML = `
    <div class="turn__tag">${icon("square-terminal")}SQUILLA
      <span class="turn__phase" data-phase="queued">
        <i class="turn__dot"></i><span class="turn__phase-text">已排队</span>
      </span>
    </div>
    <details class="turn__reason" hidden>
      <summary>思考过程</summary><div class="turn__reason-body"></div>
    </details>
    <div class="turn__body"></div>
    <div class="turn__meta" hidden></div>`;
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;

  rec = {
    wrap,
    body: wrap.querySelector(".turn__body"),
    phase: wrap.querySelector(".turn__phase"),
    phaseText: wrap.querySelector(".turn__phase-text"),
    reason: wrap.querySelector(".turn__reason"),
    reasonBody: wrap.querySelector(".turn__reason-body"),
    meta: wrap.querySelector(".turn__meta"),
    startedAt: Date.now(),
    done: false,
  };
  turns.set(key, rec);
  return rec;
}

/* 第一个带 turn_id 的事件到达时，把先前建的临时档案改挂到真 id 上，
   这样后续事件不会另开一个 bubble。 */
function adoptTurnId(turnId) {
  if (!turnId || turns.has(turnId)) return;
  const pending = turns.get("_pending");
  if (pending) {
    turns.delete("_pending");
    turns.set(turnId, pending);
  }
}

function setPhase(rec, phase) {
  if (!rec || rec.done) return;
  rec.phase.dataset.phase = phase;
  rec.phaseText.textContent = PHASE_TEXT[phase] || phase;
  if (phase === "streaming") rec.body.classList.add("caret");
  else rec.body.classList.remove("caret");
}

function finishTurn(rec, phase, payload = {}) {
  if (!rec) return;
  rec.done = true;
  rec.phase.dataset.phase = phase;
  const secs = ((Date.now() - rec.startedAt) / 1000).toFixed(1);
  rec.phaseText.textContent = `${PHASE_TEXT[phase] || phase} · ${secs}s`;
  rec.body.classList.remove("caret");

  // done 事件带完整账单，把实际路由到的模型和开销摊开给操作者看：
  // 配置里写的是 flash，路由器可能升到 pro，不显示就完全看不出来。
  const bits = [];
  const routed = payload.routed_model || payload.model;
  if (routed) {
    // 顶栏显示的是配置的模型，这里是实际执行的。两者不同时明确标「路由至」，
    // 否则光看到两个不一样的模型名会以为哪里坏了。
    const configured = state.gateway.model || "";
    bits.push(configured && routed !== configured ? `路由至 ${routed}` : routed);
  }
  if (payload.routed_tier) bits.push(`档 ${payload.routed_tier}`);
  const inTok = payload.input_tokens;
  const outTok = payload.output_tokens;
  // 别用「16306→104 tok」这种写法：小字号下箭头看起来像小数点，会被读成
  // 一个带小数的 token 数。明确写出入/出。
  if (inTok != null) bits.push(`入 ${inTok} tok`);
  if (outTok != null) bits.push(`出 ${outTok} tok`);
  if (payload.cached_tokens) bits.push(`命中缓存 ${payload.cached_tokens} tok`);
  if (payload.cost_usd != null) bits.push(`$${Number(payload.cost_usd).toFixed(6)}`);
  if (bits.length) {
    rec.meta.textContent = bits.join("  ·  ");
    rec.meta.hidden = false;
  }
}

function activeTurn() {
  for (const rec of turns.values()) if (!rec.done) return rec;
  return null;
}

function setStreaming(on) {
  state.streaming = on;
  el("btn-send").disabled = on;
  el("btn-stop").disabled = !on;
  el("cm-state").textContent = on ? "生成中" : "空闲";
}

async function send() {
  const box = el("msg");
  const text = box.value.trim();
  if (!text) return;
  if (!state.gateway.connected) {
    // 带上具体原因，否则「网关未连接」看不出是会话过期还是网关真挂了。
    return toast(state.gateway.error || "网关未连接", "bad");
  }
  addTurn("user", text);
  box.value = "";
  box.style.height = "auto";
  setStreaming(true);
  try {
    await api("/api/chat", { method: "POST", body: JSON.stringify({ message: text }) });
  } catch (err) {
    // 请求本身没送出去，网关不会有任何事件，得自己把占位轮次收成失败态。
    const rec = activeTurn();
    if (rec) {
      rec.body.textContent = `发送失败: ${err.message}`;
      finishTurn(rec, "error");
    } else {
      addTurn("system", `发送失败: ${err.message}`);
    }
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
// 决策打分。网关说"记录已过期"时给 accepted:false，不是报错，要分开讲。
document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest?.(".rt__vote");
  if (!btn) return;
  const wrap = btn.closest(".rt__rate");
  const decisionId = wrap?.dataset.decision;
  if (!decisionId) return;
  // 不用 busy()：它会把按钮文字换成"处理中"，这两个按钮只有一个字宽，会被撑坏。
  btn.disabled = true;
  try {
    const out = await api("/api/routing/feedback", {
      method: "POST",
      body: JSON.stringify({ decision_id: decisionId, rating: btn.dataset.rating }),
    });
    if (out.accepted === false) {
      toast("这条路由记录已过期，网关不再保留", "warn");
    } else {
      wrap.querySelectorAll(".rt__vote").forEach((b) => b.classList.remove("is-picked"));
      btn.classList.add("is-picked");
      toast(btn.dataset.rating === "up" ? "已记录：合适" : "已记录：不合适", "ok");
    }
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    btn.disabled = false;
  }
});

// 事件委托：任何带 data-copy 的元素点一下就把全文复制走。
document.addEventListener("click", async (ev) => {
  const node = ev.target.closest?.("[data-copy]");
  if (!node) return;
  const text = node.dataset.copy || "";
  if (!text || text === "—") return;
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制", "ok");
  } catch {
    toast("复制失败（浏览器拒绝了剪贴板权限）", "bad");
  }
});

el("btn-clear").addEventListener("click", () => {
  el("log").innerHTML = "";
  turns.clear(); // 否则旧档案还指着已被删掉的节点
  toast("已清屏（网关仍记得上文，要真正重开用「新对话」）", "warn");
});

// 清屏只擦浏览器 DOM，模型那边上下文还在；这个才是真正的新对话。
el("btn-reset").addEventListener("click", async (event) => {
  const btn = event.currentTarget;
  busy(btn, true);
  try {
    await api("/api/chat/reset", { method: "POST" });
    el("log").innerHTML = "";
    turns.clear();
    addTurn("system", "已开启新对话：网关上下文已清空。");
    toast("网关上下文已清空", "ok");
  } catch (err) {
    toast(err.message, "bad");
  } finally {
    busy(btn, false);
  }
});

/* ------------------------------------------------------ 路由 / 用量面板 */

// 网关的 trail 是一串英文 stage 名 + 裸参数，直接铺出来没人看得懂。
// 每个阶段翻成「做了什么 / 有没有生效 / 依据是什么」三件事。
const TRAIL_STAGES = {
  classify: (s) => ({
    name: "分类",
    desc: `判为 ${s.tier || "?"} 档${s.route_class ? `（路由类 ${s.route_class}）` : ""}`,
    fired: null,   // 分类不是"生效/未生效"，它总是发生
  }),
  confidence_gate: (s) => ({
    name: "置信度闸门",
    desc: s.applied
      ? `置信度低于 ${s.threshold}，回落到默认 ${s.default_tier} 档`
      : `置信度达标（阈值 ${s.threshold}），保留分类结果`,
    fired: !!s.applied,
  }),
  complaint_upgrade: (s) => ({
    name: "抱怨升档",
    desc: s.applied
      ? `检测到 ${s.terms_count} 个不满信号，升档`
      : "没有检测到不满信号",
    fired: !!s.applied,
  }),
  anti_downgrade: (s) => ({
    name: "防降档",
    desc: s.applied
      ? `${s.window_seconds}秒内曾用 ${s.previous_tier} 档，拉回同档避免忽高忽低`
      : `${s.window_seconds}秒窗口内无需拉回（上次 ${s.previous_tier || "无"}）`,
    fired: !!s.applied,
  }),
  final: (s) => ({
    name: "最终",
    desc: `${s.tier || "?"} 档${s.route_class ? `（${s.route_class}）` : ""}`,
    fired: null,
  }),
};

function renderTrail(d) {
  const trail = d.trail || [];
  if (!trail.length) return "";
  const steps = trail.map((s) => {
    const fn = TRAIL_STAGES[s.stage];
    // 未知阶段兜底：网关加了新 stage 也不至于空白，原样显示参数。
    const info = fn ? fn(s) : {
      name: s.stage || "?",
      desc: Object.entries(s).filter(([k]) => k !== "stage")
        .map(([k, v]) => `${k}=${v}`).join(" ") || "—",
      fired: s.applied === undefined ? null : !!s.applied,
    };
    const mark = info.fired === null ? "rt__step--flat"
      : info.fired ? "rt__step--on" : "rt__step--off";
    return `<li class="rt__step ${mark}">
      <span class="rt__step-n">${escapeHtml(info.name)}</span>
      <span class="rt__step-d">${escapeHtml(info.desc)}</span>
    </li>`;
  });
  // 分类器版本/思考档位/置信度这些是整条决策的属性，不属于某个阶段。
  const facts = [];
  if (d.confidence != null) facts.push(`置信度 ${(d.confidence * 100).toFixed(1)}%`);
  if (d.thinkingLevel) facts.push(`思考 ${d.thinkingLevel}`);
  if (d.classifier) facts.push(`分类器 ${d.classifier}`);
  if (d.baselineModel) facts.push(`基线 ${d.baselineModel}`);
  if (d.executedKind) facts.push(d.executedKind === "single" ? "单模型" : d.executedKind);
  if (d.fallbackHops) facts.push(`回退 ${d.fallbackHops} 跳`);

  const rated = d.decisionId
    ? `<div class="rt__rate" data-decision="${escapeHtml(d.decisionId)}">
         <span class="rt__dim">这次路由合适吗</span>
         <button type="button" class="rt__vote" data-rating="up" title="合适">好</button>
         <button type="button" class="rt__vote" data-rating="down" title="不合适">差</button>
       </div>`
    : "";

  return `<details class="rt__why">
    <summary>决策过程</summary>
    <ul class="rt__steps">${steps.join("")}</ul>
    ${facts.length ? `<div class="rt__facts">${escapeHtml(facts.join(" · "))}</div>` : ""}
    ${rated}
  </details>`;
}

function renderRouting(data) {
  const box = el("routing-box");
  const cfg = data.config || {};
  const decisions = data.decisions || [];
  const rows = [];

  rows.push(`<div class="rt__line">
    <span class="rt__k">模式</span>
    <span class="rt__v">${escapeHtml(cfg.mode || "—")}${
      cfg.router_enabled ? " · 路由器已启用" : " · 路由器关闭"
    }</span></div>`);
  if (cfg.selection_mode) {
    rows.push(`<div class="rt__line"><span class="rt__k">选择策略</span>
      <span class="rt__v">${escapeHtml(cfg.selection_mode)}</span></div>`);
  }

  // 候选池：每个模型在编队里担任什么角色（primary / critic / …）。
  const cands = (cfg.activation_preview || {}).candidates || [];
  if (cands.length) {
    // 供应商通常整池一样（都挂在同一个 KEY 上）。逐行重复 "@ tokenrhythm" 会把
    // 最长那行挤到换行，所以一致时抽到标题里，只有混用时才逐行标注。
    const providers = [...new Set(cands.map((c) => c.provider || "").filter(Boolean))];
    const shared = providers.length === 1 ? providers[0] : null;
    const items = cands.map((c) => {
      const per = shared || !c.provider
        ? ""
        : `<span class="rt__dim rt__at">@ ${escapeHtml(c.provider)}</span>`;
      return `<li>
        <span class="rt__role">${escapeHtml(c.role || "")}</span>
        <span class="rt__model" title="${escapeHtml(c.model || "")}">${escapeHtml(c.model || "")}</span>
        ${per}
      </li>`;
    });
    const head = shared
      ? `候选池<span class="rt__dim"> · ${escapeHtml(shared)}</span>`
      : "候选池";
    rows.push(`<div class="rt__grp">${head}<ul class="rt__list rt__list--cand">${items.join("")}</ul></div>`);
  }

  if (decisions.length) {
    // 全部列出，面板自身可滚动；原来砍到 6 条，剩下的看不到。
    const items = decisions.map((d) => {
      const when = d.tsMs ? new Date(d.tsMs).toLocaleTimeString("zh-CN", { hour12: false }) : "";
      // 请求的模型和实际执行的模型经常不同，两个都显示才看得出路由做了什么。
      const swapped = d.requestedModel && d.executedModel && d.requestedModel !== d.executedModel;
      const route = swapped
        ? `${escapeHtml(d.requestedModel)} <span class="rt__arrow">→</span> ${escapeHtml(d.executedModel)}`
        : escapeHtml(d.executedModel || d.model || "—");
      // 省钱比例右对齐成独立一列，纵向扫描比挤在时间后面快得多。
      const saving = d.savingsPct != null
        ? `<span class="rt__save">省 ${d.savingsPct}%</span>`
        : "";
      const fallback = d.fallbackReason
        ? `<div class="rt__dim rt__fb">回退 ${escapeHtml(d.fallbackReason)}</div>`
        : "";
      // 分档被抬升/压低时标出来，否则只看最终档位不知道路由器干预过。
      const moved = d.proposedTier && d.finalTier && d.proposedTier !== d.finalTier
        ? `<span class="rt__moved">${escapeHtml(d.proposedTier)} → ${escapeHtml(d.finalTier)}</span>`
        : "";
      return `<li class="rt__dec">
        <div class="rt__dec-top">
          <span class="rt__tier">${escapeHtml(d.finalTier || "?")}</span>
          <span class="rt__model">${route}</span>
        </div>
        <div class="rt__dec-bot">
          <span class="rt__dim">${when}</span>
          ${moved}
          ${saving}
        </div>
        ${fallback}
        ${renderTrail(d)}
      </li>`;
    });
    const more = `<span class="rt__dim"> · 共 ${decisions.length} 条，可滚动</span>`;
    rows.push(`<div class="rt__grp">最近决策${more}<ul class="rt__list rt__list--dec">${items.join("")}</ul></div>`);
  } else if (data.decisions_error) {
    rows.push(`<div class="rt__line rt__bad">决策查询失败: ${escapeHtml(data.decisions_error)}</div>`);
  }

  box.innerHTML = rows.join("");
}

async function loadRouting(btn) {
  if (btn) busy(btn, true);
  try {
    renderRouting(await api("/api/routing"));
  } catch (err) {
    el("routing-box").textContent = `读取失败: ${err.message}`;
  } finally {
    if (btn) busy(btn, false);
  }
}

async function loadUsage(btn) {
  if (btn) busy(btn, true);
  try {
    const u = await api("/api/usage");
    const pairs = [
      ["总 token", u.totalTokens],
      ["输入", u.totalInputTokens],
      ["输出", u.totalOutputTokens],
      ["缓存命中", u.totalCacheReadTokens],
      ["累计开销", u.totalCostUsd != null ? `$${u.totalCostUsd}` : null],
      ["会话数", u.totalSessions],
    ].filter(([, v]) => v != null);
    // 与 gw-kv 保持一致：键用 <b>，值用 <span>，否则 .kv__row 的样式不生效。
    el("usage-kv").innerHTML = pairs
      .map(([k, v]) => `<div class="kv__row"><b>${escapeHtml(k)}</b><span>${escapeHtml(String(v))}</span></div>`)
      .join("");
  } catch (err) {
    el("usage-kv").textContent = `读取失败: ${err.message}`;
  } finally {
    if (btn) busy(btn, false);
  }
}

el("btn-routing").addEventListener("click", (e) => loadRouting(e.currentTarget));
el("btn-usage").addEventListener("click", (e) => loadUsage(e.currentTarget));

el("btn-history").addEventListener("click", async (event) => {
  const btn = event.currentTarget;
  try {
    busy(btn, true);
    const data = await api("/api/chat/history?limit=40");
    el("log").innerHTML = "";
    turns.clear();
    // 网关 chat.history 把正文放在 `text` 字段。以前这里只读 `content`，
    // 于是每条都被当成空的跳过，界面看起来「拉取历史啥都没有」。
    // content 分支保留做兜底，万一别的网关版本用它。
    let shown = 0;
    for (const m of data.messages || []) {
      const role = m.role || m.author || "assistant";
      let text = m.text;
      if (!text) {
        text = typeof m.content === "string"
          ? m.content
          : (m.content || []).map((p) => p.text || "").join("");
      }
      if (!text) continue;
      // 网关会在用户消息前面塞一行时间戳（[2026-08-03T19:43+08:00 …]），
      // 回放时把它剥掉，否则每条历史都顶着一行噪音。
      text = text.replace(/^\[\d{4}-\d{2}-\d{2}T[^\]]*\]\n?/, "");
      addTurn(role === "user" ? "user" : "assistant", text);
      shown += 1;
    }
    toast(shown ? `载入 ${shown} 条历史` : "该会话暂无历史消息", shown ? "ok" : "warn");
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

let bridgeRetry = 0; // 连续重连次数，用于退避

function connectBridge() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sock = new WebSocket(`${proto}://${location.host}/ws`);

  sock.onopen = () => {
    bridgeRetry = 0;
    state.gateway.error = null;
  };

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
    const turnId = payload.turn_id || payload.task_id || "";
    if (turnId) adoptTurnId(turnId);

    // 排队 / 受理：此时还没有任何输出，先把 bubble 立起来给状态。
    if (name === "task.queued") {
      const rec = ensureTurn(turnId);
      setPhase(rec, "queued");
      if (payload.queue_depth > 1) {
        rec.phaseText.textContent = `已排队 · 第 ${payload.queue_position}/${payload.queue_depth}`;
      }
      return;
    }
    if (name === "task.running") {
      setPhase(ensureTurn(turnId), "running");
      return;
    }

    // 网关自己的状态机，最权威，直接照搬 to_state。
    if (name.endsWith("state_change")) {
      const rec = ensureTurn(turnId);
      const to = payload.to_state || "";
      if (to === "done") finishTurn(rec, "done", payload);
      else setPhase(rec, to);
      return;
    }

    // 思考流：折叠在「思考过程」里，不与正文混排。
    if (name.endsWith("thinking")) {
      const rec = ensureTurn(turnId);
      setPhase(rec, "thinking");
      const piece = payload.text ?? payload.delta ?? "";
      if (piece) {
        rec.reason.hidden = false;
        rec.reasonBody.textContent += piece;
      }
      return;
    }

    if (name.endsWith("text_delta") || name.endsWith("message_delta")) {
      const rec = ensureTurn(turnId);
      setPhase(rec, "streaming");
      rec.body.textContent += payload.delta ?? payload.text ?? "";
      el("log").scrollTop = el("log").scrollHeight;
      return;
    }

    if (name.endsWith("tool_call") || name.endsWith("tool.start")) {
      const rec = ensureTurn(turnId);
      const chip = document.createElement("div");
      chip.className = "tool";
      chip.textContent = payload.name || payload.tool || "tool";
      rec.body.appendChild(chip);
      return;
    }

    // done 是真正的收尾事件（不是 message_complete / turn_complete，那两个
    // 网关压根不发）。它带 text_snapshot，用来兜住漏掉的增量。
    if (name.endsWith("session.event.done") || name === "session.event.done") {
      const rec = ensureTurn(turnId);
      const snapshot = payload.text_snapshot ?? payload.text ?? "";
      if (snapshot && snapshot.length > rec.body.textContent.length) {
        rec.body.textContent = snapshot;
      }
      if (payload.reasoning_content && !rec.reasonBody.textContent) {
        rec.reason.hidden = false;
        rec.reasonBody.textContent = payload.reasoning_content;
      }
      finishTurn(rec, "done", payload);
      setStreaming(false);
      return;
    }

    if (name.endsWith("aborted") || name === "task.aborted") {
      finishTurn(ensureTurn(turnId), "aborted", payload);
      setStreaming(false);
      return;
    }

    if (name === "task.failed" || name.endsWith("error")) {
      const rec = turns.get(turnId) || activeTurn();
      if (rec) {
        if (!rec.body.textContent) {
          rec.body.textContent = payload.message || payload.error || "网关未说明失败原因";
        }
        finishTurn(rec, "error", payload);
      } else {
        addTurn("system", payload.message || JSON.stringify(payload));
      }
      setStreaming(false);
    }
  };

  sock.onclose = (event) => {
    state.gateway.connected = false;

    // 1008 = 后端拒绝握手，只会因为 session 失效。无脑重连的话会永远卡在
    // 「连接中」，用户根本不知道该重新登录 —— 直接把人送去登录页。
    if (event.code === 1008) {
      state.gateway.error = "会话已过期，正在跳转登录…";
      renderTop();
      // 控制台是单页应用，根路径即全部界面，不需要 next 回跳参数。
      setTimeout(() => location.replace("/login"), 900);
      return;
    }

    // 其余情况（网关重启、网络抖动）退避重连，并把重试进度显示出来，
    // 不要静默地假装还在连。
    bridgeRetry = Math.min(bridgeRetry + 1, 6);
    const wait = Math.min(1600 * bridgeRetry, 10000);
    state.gateway.error = `连接中断，${Math.round(wait / 1000)}s 后重试（第 ${bridgeRetry} 次）`;
    renderTop();
    setTimeout(connectBridge, wait);
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

/* Logout is always available — the console is never unauthenticated now. */
(() => {
  const btn = document.getElementById("btn-logout");
  if (!btn) return;
  btn.hidden = false;
  btn.addEventListener("click", async () => {
    await api("/api/logout", { method: "POST" }).catch(() => {});
    location.replace("/login");
  });
})();

/* Boot order matters while a password rotation is owed: the server answers 403
   on every business endpoint and closes /ws, so firing the usual startup calls
   would fill the screen with failures that have nothing to do with the actual
   problem. Check the gate first, and only wire the console up behind it. */
startField();
(async () => {
  const gated = await checkMustChange();
  if (gated) {
    addTurn("system", "控制台已锁定：请先修改初始密码。");
    return;
  }
  connectBridge();
  refreshState();
  loadProviders();
  addTurn("system", `控制台就绪。测活固定问题：${PROBE_QUESTION}`);
  setInterval(() => { if (!state.streaming) refreshState(); }, 20000);
})();
