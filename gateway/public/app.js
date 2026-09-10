/* ============================================================
   AGENT//OS control deck — client logic (vanilla JS, no build)
   ------------------------------------------------------------
   Auth flow:   token in cookie -> auto-connect, else show gate.
   Validate by hitting GET /agents (200 == good token).
   Tabs:        from GET /agents, polled every 3s for state/pending.
   Thread:      GET /messages on switch + live WS per channel.
   Sender:      You type -> POST sender "human" (so the gateway
                human->inbox bridge fires) -> displayed as "You".
   ============================================================ */

const $ = (id) => document.getElementById(id);
const COOKIE = "agentos_token";
const POLL_MS = 3000;

/* ---------- per-agent identity colors ----------
   The two defaults get fixed colors; any additional agents you spin up are
   assigned a color from the fallback palette by their position in the roster. */
const ACCENTS = {
  companion: "#ffb454",
  curator:   "#ff8fb1",
};
const FALLBACK_ACCENTS = ["#5ad1e6", "#6fdc8c", "#b89cff", "#ffd166", "#80d8ff", "#4da3ff", "#f7c948"];
function accentFor(slug, idx) {
  return ACCENTS[slug] || FALLBACK_ACCENTS[idx % FALLBACK_ACCENTS.length];
}

/* ---------- cookie helpers ---------- */
function setCookie(name, val, days) {
  const exp = new Date(Date.now() + days * 864e5).toUTCString();
  document.cookie = `${name}=${encodeURIComponent(val)}; expires=${exp}; path=/; SameSite=Strict`;
}
function getCookie(name) {
  return document.cookie.split("; ").reduce((acc, c) => {
    const [k, v] = c.split("=");
    return k === name ? decodeURIComponent(v) : acc;
  }, "");
}
function clearCookie(name) {
  document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
}

/* ---------- app state ---------- */
const state = {
  token: "",
  agents: [],          // [{slug, channel, state, paused, pending, ...}]
  active: null,        // active agent slug
  ws: null,
  lastId: 0,           // highest message id rendered in active channel
  seenIds: new Set(),  // de-dupe (WS + history overlap)
  pollTimer: null,
};

/* ---------- API ---------- */
function authHeaders() { return { Authorization: "Bearer " + state.token }; }
async function api(path) {
  const r = await fetch(path, { headers: authHeaders() });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

/* =========================================================
   LOGIN GATE
   ========================================================= */
$("gate-host").textContent = location.host;

$("gate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("gate-submit");
  const tok = $("gate-token").value.trim();
  const err = $("gate-error");
  err.textContent = "";
  if (!tok) { err.textContent = "token required"; return; }

  btn.classList.add("busy");
  try {
    // validate against /agents
    const r = await fetch("/agents", { headers: { Authorization: "Bearer " + tok } });
    if (r.status === 401) { throw new Error("invalid token — rejected by gateway"); }
    if (!r.ok) { throw new Error("gateway error (HTTP " + r.status + ")"); }
    // success
    state.token = tok;
    if ($("gate-remember").checked) setCookie(COOKIE, tok, 90);
    const data = await r.json();
    enterApp(data.agents || []);
  } catch (ex) {
    err.textContent = ex.message || "connection failed";
    btn.classList.remove("busy");
  }
});

function showGate() {
  $("app").hidden = true;
  $("gate").classList.remove("leaving");
  $("gate").style.display = "grid";
  $("gate-token").value = "";
  $("gate-submit").classList.remove("busy");
}

function enterApp(agents) {
  const gate = $("gate");
  gate.classList.add("leaving");
  setTimeout(() => { gate.style.display = "none"; }, 450);
  $("app").hidden = false;
  state.agents = agents;
  renderTabs();
  startPolling();
  // open first non-paused agent (or first)
  const first = agents.find((a) => !a.paused) || agents[0];
  if (first) selectAgent(first.slug);
}

/* =========================================================
   TABS / ROSTER
   ========================================================= */
function renderTabs() {
  const wrap = $("tabs");
  // build/update without nuking nodes we don't need to (keeps animation calm)
  wrap.innerHTML = "";
  state.agents.forEach((a, i) => {
    const ac = accentFor(a.slug, i);
    const btn = document.createElement("button");
    btn.className = "tab";
    btn.dataset.slug = a.slug;
    btn.style.setProperty("--ac", ac);
    btn.innerHTML = `
      <span class="tab-dot"></span>
      <span class="tab-body">
        <span class="tab-name">${esc(a.slug)}</span>
        <span class="tab-sub">idle</span>
        <span class="tab-think"><i></i><i></i><i></i></span>
      </span>
      <span class="tab-badge">0</span>`;
    btn.addEventListener("click", () => selectAgent(a.slug));
    wrap.appendChild(btn);
  });
  paintTabs();
}

function paintTabs() {
  state.agents.forEach((a) => {
    const tab = document.querySelector(`.tab[data-slug="${cssEsc(a.slug)}"]`);
    if (!tab) return;
    const busy = a.state === "busy";
    tab.classList.toggle("s-busy", busy && !a.paused);
    tab.classList.toggle("s-idle", !busy && !a.paused);
    tab.classList.toggle("s-paused", !!a.paused);
    tab.classList.toggle("active", a.slug === state.active);

    const sub = tab.querySelector(".tab-sub");
    const base = a.paused ? "paused" : (busy ? "thinking" : "idle");
    // append context-window usage (rotation fires ~84% of 1M)
    if (a.ctxPct != null) {
      const col = a.ctxPct >= 84 ? "#ff6f6f" : a.ctxPct >= 60 ? "#ffb454" : "#6a6a72";
      sub.innerHTML = `${base} · <span style="color:${col}">${Math.round(a.ctxPct)}%</span>`;
    } else {
      sub.textContent = base;
    }

    const badge = tab.querySelector(".tab-badge");
    const pend = a.pending || 0;
    tab.classList.toggle("has-pending", pend > 0);
    badge.textContent = pend > 99 ? "99+" : String(pend);
  });
}

/* poll /agents for live state + pending counts */
function startPolling() {
  const tick = async () => {
    try {
      const data = await api("/agents");
      state.agents = data.agents || [];
      setLink(true);
      // if roster changed (new agent), re-render tabs
      const slugs = state.agents.map((a) => a.slug).join(",");
      if (slugs !== state._slugCache) { state._slugCache = slugs; renderTabs(); }
      else paintTabs();
      paintHeader();
    } catch (ex) {
      setLink(false);
    }
  };
  tick();
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(tick, POLL_MS);
}

function setLink(live) {
  const el = $("link-state");
  el.classList.toggle("live", live && state.ws && state.ws.readyState === 1);
  el.classList.toggle("down", !live);
  el.querySelector(".ls-text").textContent = !live ? "gateway down" : (state.ws && state.ws.readyState === 1 ? "live" : "polling");
}

/* =========================================================
   CHANNEL SELECTION + HISTORY + WS
   ========================================================= */
function activeAgent() { return state.agents.find((a) => a.slug === state.active); }

async function selectAgent(slug) {
  if (state.active === slug) return;
  state.active = slug;
  state.lastId = 0;
  state.seenIds = new Set();
  paintTabs();
  paintHeader();

  const thread = $("thread");
  thread.innerHTML = `<div class="empty-state"><span class="es-glyph">◈</span><p>loading channel…</p></div>`;

  const ag = activeAgent();
  const channel = ag ? ag.channel : slug;

  // history
  try {
    const data = await api(`/messages?channel=${encodeURIComponent(channel)}&since=0`);
    thread.innerHTML = "";
    const msgs = data.messages || [];
    if (!msgs.length) {
      thread.innerHTML = `<div class="empty-state"><span class="es-glyph">◈</span><p>no messages yet — say hello</p></div>`;
    }
    msgs.forEach((m) => renderMessage(m, false));
    scrollBottom(true);
  } catch (ex) {
    thread.innerHTML = `<div class="empty-state"><span class="es-glyph">◈</span><p>failed to load channel</p></div>`;
  }

  openWS(channel);
}

function openWS(channel) {
  if (state.ws) { try { state.ws.onclose = null; state.ws.close(); } catch {} state.ws = null; }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws?channel=${encodeURIComponent(channel)}&token=${encodeURIComponent(state.token)}`;
  const ws = new WebSocket(url);
  state.ws = ws;
  ws.onopen = () => setLink(true);
  ws.onclose = () => {
    setLink(true); // gateway may still be up; we just lost the socket
    // auto-reconnect to the *current* channel if still active
    setTimeout(() => {
      const ag = activeAgent();
      if (ag && state.ws === ws) openWS(ag.channel);
    }, 1500);
  };
  ws.onmessage = (e) => {
    let m; try { m = JSON.parse(e.data); } catch { return; }
    if (m.type === "message") {
      const ag = activeAgent();
      if (ag && m.channel === ag.channel) {
        const atBottom = isNearBottom();
        renderMessage(m, true);
        if (atBottom) scrollBottom();
      }
    }
  };
}

/* =========================================================
   RENDERING
   ========================================================= */
function isMe(sender) { return sender === "human" || sender === "You"; }
function displaySender(sender) { return isMe(sender) ? "You" : sender; }

// Image extensions render inline; any other attachment renders as a download chip.
const IMG_EXT = /\.(png|jpe?g|gif|webp|bmp|avif|svg)$/i;

// Render a message body, turning `[attachment: /path/up-xxx.ext]` refs (same convention
// the mobile app + agents use) into either an inline image or a file-download chip served
// from /media/<basename>. Text around the ref is HTML-escaped; the basename is
// charset-validated before it hits the DOM.
function renderBody(body) {
  const re = /\[attachment:\s*([^\]]+)\]/g;
  let out = "", last = 0, m;
  while ((m = re.exec(body)) !== null) {
    out += esc(body.slice(last, m.index));
    const base = m[1].trim().split("/").pop();
    if (base && /^[A-Za-z0-9._-]+$/.test(base)) {
      const url = `/media/${encodeURIComponent(base)}?token=${encodeURIComponent(state.token)}`;
      if (IMG_EXT.test(base)) {
        out += `<a class="msg-img-link" href="${url}" target="_blank" rel="noopener"><img class="msg-img" src="${url}" alt="attachment" loading="lazy"></a>`;
      } else {
        out += `<a class="msg-file-link" href="${url}" target="_blank" rel="noopener" download="${esc(base)}"><span class="msg-file-ico">📄</span><span class="msg-file-name">${esc(base)}</span></a>`;
      }
    } else {
      out += esc(m[0]);
    }
    last = re.lastIndex;
  }
  out += esc(body.slice(last));
  return out;
}

function renderMessage(m, animate) {
  if (m.id != null) {
    if (state.seenIds.has(m.id)) return;
    state.seenIds.add(m.id);
    if (m.id > state.lastId) state.lastId = m.id;
  }
  const thread = $("thread");
  const es = thread.querySelector(".empty-state");
  if (es) es.remove();

  const mine = isMe(m.sender);
  const crit = m.priority === "critical";
  const ag = activeAgent();
  const ac = ag ? accentFor(ag.slug, state.agents.indexOf(ag)) : "var(--amber)";

  const el = document.createElement("div");
  el.className = `msg ${mine ? "me" : "them"}${crit ? " critical" : ""}`;
  if (!mine) el.style.setProperty("--ac", ac);
  if (!animate) el.style.animation = "none";
  el.innerHTML = `
    <div class="msg-head">
      <span class="msg-sender">${esc(displaySender(m.sender))}</span>
      <span class="msg-time">${fmtTime(m.created_at)}</span>
    </div>
    <div class="bubble">${renderBody(m.body || "")}</div>`;
  thread.appendChild(el);
}

function paintHeader() {
  const ag = activeAgent();
  if (!ag) return;
  const ac = accentFor(ag.slug, state.agents.indexOf(ag));
  document.documentElement.style.setProperty("--stage-ac", ac);
  $("conv-accent").style.setProperty("--ac", ac);
  $("conv-name").textContent = ag.slug;
  $("conv-channel").textContent = "#" + ag.channel;

  const managed = $("conv-managed");
  managed.hidden = !ag.dispatcher_managed;
  const paused = $("conv-paused");
  paused.hidden = !ag.paused;

  const st = $("conv-status");
  const busy = ag.state === "busy" && !ag.paused;
  st.classList.toggle("busy", busy);
  st.style.setProperty("--ac", ac);
  st.innerHTML = busy
    ? `<span class="think-dot"></span>thinking…`
    : (ag.paused ? "paused" : "idle");

  // busy banner above composer
  const bb = $("busy-banner");
  bb.hidden = !busy;
  bb.style.setProperty("--ac", ac);
  $("bb-name").textContent = ag.slug;
}

/* =========================================================
   COMPOSER  (send as "human" so the bridge fires; show as You)
   ========================================================= */
const bodyInput = $("body");
const prioSel = $("prio");

prioSel.addEventListener("change", () => { prioSel.dataset.prio = prioSel.value; });

bodyInput.addEventListener("input", () => {
  bodyInput.style.height = "auto";
  bodyInput.style.height = Math.min(bodyInput.scrollHeight, 180) + "px";
});

bodyInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("send-form").requestSubmit();
  }
});

$("send-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ag = activeAgent();
  if (!ag) return;
  const text = bodyInput.value.trim();
  if (!text && !state.pending.length) return;   // nothing to send
  const priority = prioSel.value;

  const sendBtn = $("send");
  sendBtn.disabled = true;
  try {
    // Upload any staged files first, collecting their [attachment: <path>] refs so the
    // file(s) + text go out as ONE message (same convention the mobile app + agents use).
    const refs = [];
    for (const p of state.pending) {
      const fd = new FormData();
      fd.append("file", p.file);
      const r = await fetch("/upload", { method: "POST", headers: authHeaders(), body: fd });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok || !j.path) throw new Error(j.error || `upload failed (${r.status})`);
      refs.push(`[attachment: ${j.path}]`);
    }
    const body = [text, ...refs].filter(Boolean).join(" ");
    if (!body) return;

    // Clear the composer now that the payload is assembled.
    clearPending();
    bodyInput.value = "";
    bodyInput.style.height = "auto";

    // Prefer WS (instant), fall back to POST. Either way sender="human" so the gateway's
    // human->inbox bridge delivers it to the agent.
    const payload = { channel: ag.channel, sender: "human", body, priority };
    let sentViaWS = false;
    if (state.ws && state.ws.readyState === 1) {
      try { state.ws.send(JSON.stringify({ type: "send", ...payload })); sentViaWS = true; } catch {}
    }
    if (!sentViaWS) {
      await fetch("/send", {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }
  } catch (ex) {
    renderMessage({ sender: "system", body: `⚠ send failed: ${ex.message}`, created_at: new Date().toISOString() }, true);
    scrollBottom();
  } finally {
    sendBtn.disabled = false;
    bodyInput.focus();
  }
});

/* ---- image / file upload: POST /upload, then send an [attachment: <path>] message
   (identical convention + storage to the mobile app) ---- */
const fileInput = $("file");
const attachBtn = $("attach");
const attachTray = $("attach-tray");
attachBtn.addEventListener("click", () => fileInput.click());

// Staged (not-yet-sent) files: [{ file, url, isImage, name }]. They ride out with the next
// message on submit, so file(s) + text land as ONE message (like the mobile app). Images
// get an object URL for a thumbnail preview; other files get a labelled chip (url null).
state.pending = [];

function fmtBytes(n) {
  if (!n && n !== 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function renderPending() {
  if (!state.pending.length) { attachTray.hidden = true; attachTray.innerHTML = ""; return; }
  attachTray.hidden = false;
  attachTray.innerHTML = state.pending.map((p, i) => {
    const x = `<button type="button" class="attach-x" data-i="${i}" title="remove">✕</button>`;
    if (p.isImage && p.url) {
      return `<div class="attach-thumb"><img src="${p.url}" alt="staged image">${x}</div>`;
    }
    return `<div class="attach-file" title="${esc(p.name)}">` +
      `<span class="attach-file-ico">📄</span>` +
      `<span class="attach-file-meta"><span class="attach-file-name">${esc(p.name)}</span>` +
      `<span class="attach-file-size">${esc(fmtBytes(p.file.size))}</span></span>${x}</div>`;
  }).join("");
}
function clearPending() {
  state.pending.forEach((p) => { if (p.url) { try { URL.revokeObjectURL(p.url); } catch {} } });
  state.pending = [];
  renderPending();
}
function stageFile(file) {
  if (!file) return;
  const isImage = /^image\//.test(file.type) || IMG_EXT.test(file.name || "");
  state.pending.push({ file, url: isImage ? URL.createObjectURL(file) : null, isImage, name: file.name || "file" });
  renderPending();
  bodyInput.focus();
}

// Remove a staged image via its ✕ button.
attachTray.addEventListener("click", (e) => {
  const b = e.target.closest(".attach-x");
  if (!b) return;
  const i = +b.dataset.i;
  const p = state.pending[i];
  if (p) { try { URL.revokeObjectURL(p.url); } catch {} state.pending.splice(i, 1); renderPending(); }
});

fileInput.addEventListener("change", () => {
  Array.from(fileInput.files || []).forEach(stageFile);
  fileInput.value = "";               // reset so re-picking the same file re-fires change
});

// Clipboard paste: stage any FILE in the clipboard (screenshot, copied pic, or a copied
// file of any type) instead of dropping raw bytes as text. DataTransferItemList isn't
// reliably for..of-iterable, so use Array.from; also fall back to clipboardData.files (some
// browsers only populate that). Plain-text paste is untouched — text items are kind
// "string" (skipped here), and we only preventDefault when a file was actually staged.
bodyInput.addEventListener("paste", (e) => {
  const dt = e.clipboardData;
  if (!dt) return;
  let staged = false;
  for (const it of Array.from(dt.items || [])) {
    if (it.kind === "file") {
      const f = it.getAsFile();
      if (f) { stageFile(f); staged = true; }
    }
  }
  if (!staged) {
    for (const f of Array.from(dt.files || [])) {
      stageFile(f); staged = true;
    }
  }
  if (staged) e.preventDefault();
});

/* =========================================================
   LOGOUT
   ========================================================= */
$("logout").addEventListener("click", () => {
  clearCookie(COOKIE);
  if (state.ws) { try { state.ws.onclose = null; state.ws.close(); } catch {} }
  clearInterval(state.pollTimer);
  Object.assign(state, { token: "", agents: [], active: null, ws: null, lastId: 0, seenIds: new Set(), _slugCache: null });
  $("thread").innerHTML = "";
  $("tabs").innerHTML = "";
  showGate();
});

/* =========================================================
   UTILITIES
   ========================================================= */
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function cssEsc(s) {
  return (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&");
}
function fmtTime(ts) {
  if (!ts) return "";
  // sqlite gives "YYYY-MM-DD HH:MM:SS" (UTC). Make it local & friendly.
  const d = new Date(ts.includes("T") ? ts : ts.replace(" ", "T") + "Z");
  if (isNaN(d)) return ts;
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return sameDay ? t : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + t;
}
function scrollBottom(instant) {
  const thread = $("thread");
  if (instant) { const b = thread.style.scrollBehavior; thread.style.scrollBehavior = "auto"; thread.scrollTop = thread.scrollHeight; thread.style.scrollBehavior = b; }
  else thread.scrollTop = thread.scrollHeight;
}
function isNearBottom() {
  const t = $("thread");
  return t.scrollHeight - t.scrollTop - t.clientHeight < 120;
}

/* =========================================================
   BOOTSTRAP — auto-connect from cookie
   ========================================================= */
(async function boot() {
  const tok = getCookie(COOKIE);
  if (!tok) { showGate(); return; }
  state.token = tok;
  try {
    const r = await fetch("/agents", { headers: { Authorization: "Bearer " + tok } });
    if (!r.ok) throw new Error("stale");
    const data = await r.json();
    // skip gate entirely
    $("gate").style.display = "none";
    $("app").hidden = false;
    state.agents = data.agents || [];
    renderTabs();
    startPolling();
    const first = state.agents.find((a) => !a.paused) || state.agents[0];
    if (first) selectAgent(first.slug);
  } catch (ex) {
    clearCookie(COOKIE);
    showGate();
  }
})();

