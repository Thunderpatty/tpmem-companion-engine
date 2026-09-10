/**
 * companion-engine gateway — HTTP + WebSocket over the SQLite bus.
 *
 * This is the DEFAULT interface to your agents: a chat webapp (served from public/)
 * backed by the tpmem `messages` table. The durable source of truth is the DB; this
 * service is a thin, swappable frontend — channel reads/history over HTTP, a live push
 * feed over WebSocket, and an authenticated /send that writes back to the bus (and
 * bridges a human message into `inbox` so the dispatcher wakes the agent).
 *
 * Auth: a shared bearer token (GATEWAY_TOKEN env, or ~/.tpmem/agent-os/gateway.token).
 * Env: GATEWAY_PORT (7364), TPMEM_DB (~/.tpmem/kb.db),
 *      AGENT_REGISTRY (~/.tpmem/agent-os/registry.json),
 *      AGENT_STATE_DIR (~/.tpmem/agent-state),
 *      DISPATCH_CTL (path to bin/dispatch-ctl — backend for the control buttons).
 */
import Fastify from "fastify";
import websocket from "@fastify/websocket";
import fstatic from "@fastify/static";
import multipart from "@fastify/multipart";
import Database from "better-sqlite3";
import { readFileSync, existsSync, writeFileSync, mkdirSync, openSync, fstatSync, readSync, closeSync } from "node:fs";
import { homedir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { execFile } from "node:child_process";

const HERE = dirname(fileURLToPath(import.meta.url));
const HOME = homedir();
const PORT = Number(process.env.GATEWAY_PORT ?? 7364);
const DB_PATH = process.env.TPMEM_DB ?? join(HOME, ".tpmem/kb.db");
const REGISTRY_PATH = process.env.AGENT_REGISTRY ?? join(HOME, ".tpmem/agent-os/registry.json");
const STATE_DIR = process.env.AGENT_STATE_DIR ?? join(HOME, ".tpmem/agent-state");
// CC project dir for an agent whose cwd is $HOME (every '/' and '.' in the path -> '-').
const PROJ_DIR = join(HOME, ".claude/projects", HOME.replace(/[/.]/g, "-"));
// The dispatch-ctl backend for the control buttons; resolves to <repo>/bin/dispatch-ctl.
const DISPATCH_CTL = process.env.DISPATCH_CTL ?? join(HERE, "..", "..", "bin", "dispatch-ctl");

function loadToken(): string {
  if (process.env.GATEWAY_TOKEN) return process.env.GATEWAY_TOKEN.trim();
  const f = join(HOME, ".tpmem/agent-os/gateway.token");
  if (existsSync(f)) return readFileSync(f, "utf8").trim();
  throw new Error("no gateway token (set GATEWAY_TOKEN or write ~/.tpmem/agent-os/gateway.token)");
}
const TOKEN = loadToken();

const db = new Database(DB_PATH);
db.pragma("journal_mode = WAL");
db.pragma("busy_timeout = 15000");

const qRecent = db.prepare(
  "SELECT id,channel,sender,body,priority,created_at FROM (SELECT id,channel,sender,body,priority,created_at FROM messages WHERE channel=? AND id>? ORDER BY id DESC LIMIT 500) ORDER BY id");
const qChannels = db.prepare("SELECT DISTINCT channel FROM messages ORDER BY channel");
const qMaxId = db.prepare("SELECT COALESCE(MAX(id),0) m FROM messages");
const qAfter = db.prepare(
  "SELECT id,channel,sender,body,priority,created_at FROM messages WHERE id>? ORDER BY id LIMIT 500");
const insMsg = db.prepare(
  "INSERT INTO messages(channel,sender,body,priority) VALUES (?,?,?,?)");
const insInbox = db.prepare(
  "INSERT INTO inbox(transport,payload,routed_agent,priority) VALUES ('gateway',?,?,?)");

// per-channel read cursor (single user) — drives the "unread" badge.
db.exec("CREATE TABLE IF NOT EXISTS read_state (channel TEXT PRIMARY KEY, last_read_id INTEGER NOT NULL DEFAULT 0)");
const qUnread = db.prepare(
  "SELECT COUNT(*) n FROM messages WHERE channel=? AND sender != 'human' AND id > COALESCE((SELECT last_read_id FROM read_state WHERE channel=?),0)");
const markRead = db.prepare(
  "INSERT INTO read_state(channel,last_read_id) VALUES(?, (SELECT COALESCE(MAX(id),0) FROM messages WHERE channel=?)) ON CONFLICT(channel) DO UPDATE SET last_read_id=excluded.last_read_id");
const qPendingFor = db.prepare(
  "SELECT COUNT(*) n FROM inbox WHERE routed_agent=? AND status='pending'");

function send(channel: string, sender: string, body: string, priority = "normal") {
  const info = insMsg.run(channel, sender, body, priority);
  // bridge: a human message in an agent's channel becomes an inbox row for it
  if (sender === "human") {
    try { insInbox.run(body, channel, priority); } catch { /* inbox optional */ }
  }
  return Number(info.lastInsertRowid);
}

// promisified execFile — no shell, so args can't inject.
function run(file: string, args: string[], timeout = 12000): Promise<string> {
  return new Promise((resolve, rejectP) => {
    execFile(file, args, { timeout }, (err, stdout, stderr) => {
      if (err) rejectP(new Error(stderr?.trim() || err.message));
      else resolve((stdout || "").trim());
    });
  });
}

const app = Fastify({ logger: false });
await app.register(websocket);
await app.register(fstatic, { root: join(HERE, "..", "public"), prefix: "/" });
await app.register(multipart, { limits: { fileSize: 100 * 1024 * 1024 } }); // 100MB upload cap

// Tolerate bodyless POSTs that arrive with a blank/unexpected Content-Type. The built-in
// json/text and multipart parsers still take precedence for their specific types.
app.addContentTypeParser("*", (_req, payload, done) => {
  let data = "";
  payload.on("data", (c) => { data += c; });
  payload.on("end", () => done(null, data || undefined));
  payload.on("error", (e) => done(e as any, undefined));
});

// Uploaded media lands where agents read `[attachment: <path>]` from.
const MEDIA_DIR = join(HOME, ".tpmem/media");
try { mkdirSync(MEDIA_DIR, { recursive: true }); } catch { /* exists */ }

function authed(req: any): boolean {
  const h = (req.headers?.authorization ?? "") as string;
  const bearer = h.startsWith("Bearer ") ? h.slice(7) : "";
  const q = (req.query?.token ?? "") as string;
  return bearer === TOKEN || q === TOKEN;
}

app.get("/health", async () => ({ ok: true, port: PORT }));

app.get("/channels", async (req, reply) => {
  if (!authed(req)) return reply.code(401).send({ error: "unauthorized" });
  return { channels: qChannels.all().map((r: any) => r.channel) };
});

// TRUE busy/idle from the agent's transcript tail — authoritative over the hook flag,
// which can stick 'busy' (a missed Stop hook) or false-stamp busy from a background proc
// under the agent's slug while the chat sits idle. Idle iff the last assistant turn ended
// cleanly (stop_reason 'end_turn'); a mid-flight turn reads busy. Tolerates a truncated
// final JSON line mid-flush. Returns null when unreadable so the caller falls back.
function transcriptState(sid?: string): string | null {
  if (!sid) return null;
  let fd: number | null = null;
  try {
    fd = openSync(join(PROJ_DIR, `${sid}.jsonl`), "r");
    const size = fstatSync(fd).size;
    const readLen = Math.min(65536, size);
    const buf = Buffer.alloc(readLen);
    readSync(fd, buf, 0, readLen, size - readLen);
    const lines = buf.toString("utf8").split("\n").filter(Boolean);
    for (let i = lines.length - 1; i >= 0; i--) {
      let d: any;
      try { d = JSON.parse(lines[i]); } catch { continue; }  // partial/truncated line — skip
      if (d?.type === "assistant") {
        return d?.message?.stop_reason === "end_turn" ? "idle" : "busy";
      }
    }
    return null;  // no assistant entry in the tail window
  } catch { return null; }
  finally { if (fd !== null) { try { closeSync(fd); } catch { /* */ } } }
}

// agent roster + live state for the UI (tabs + "thinking" indicators)
app.get("/agents", async (req, reply) => {
  if (!authed(req)) return reply.code(401).send({ error: "unauthorized" });
  let reg: any = { agents: {} };
  try { reg = JSON.parse(readFileSync(REGISTRY_PATH, "utf8")); } catch { /* empty */ }
  const out = Object.entries(reg.agents ?? {}).map(([slug, cfg]: any) => {
    let state = "idle";
    try {
      const s = JSON.parse(readFileSync(join(STATE_DIR, `${slug}.json`), "utf8"));
      // stale 'busy' (>30min) => treat idle. Matches the dispatcher's STALE_BUSY=1800.
      state = (s.state === "busy" && Date.now() / 1000 - (s.ts ?? 0) > 1800) ? "idle" : s.state;
    } catch { /* no hook yet */ }
    // transcript is ground truth — overrides a stuck/false hook flag (falls back to it if unreadable)
    { const ts = transcriptState(cfg.session_id); if (ts) state = ts; }
    let paused = false;
    try { paused = existsSync(join(STATE_DIR, `${slug}.paused`)); } catch { /* */ }
    const channel = cfg.channel ?? slug;
    const pending = (qPendingFor.get(channel) as any)?.n ?? 0;
    const unread = (qUnread.get(channel, channel) as any)?.n ?? 0;
    // context-window usage, if a monitor caches it (optional; no hook race)
    let ctx = null, ctxPct = null;
    try {
      const cj = JSON.parse(readFileSync(join(STATE_DIR, `${slug}.ctx.json`), "utf8"));
      ctx = cj.ctx; ctxPct = cj.pct;
    } catch { /* no cache */ }
    return { slug, channel, state, paused, pending, unread, ctx, ctxPct,
             dispatcher_managed: !!cfg.dispatcher_managed };
  });
  return { agents: out };
});

// mark a channel read up to its newest message (clears the unread badge)
app.post("/read", async (req, reply) => {
  if (!authed(req)) return reply.code(401).send({ error: "unauthorized" });
  const { channel } = (req.body ?? {}) as any;
  if (!channel) return reply.code(400).send({ error: "channel required" });
  markRead.run(channel, channel);
  return { ok: true, channel };
});

app.get("/messages", async (req, reply) => {
  if (!authed(req)) return reply.code(401).send({ error: "unauthorized" });
  const { channel, since } = req.query as any;
  if (!channel) return reply.code(400).send({ error: "channel required" });
  return { messages: qRecent.all(channel, Number(since ?? 0)) };
});

app.post("/send", async (req, reply) => {
  if (!authed(req)) return reply.code(401).send({ error: "unauthorized" });
  const { channel, sender, body, priority } = (req.body ?? {}) as any;
  if (!channel || !sender || !body)
    return reply.code(400).send({ error: "channel, sender, body required" });
  const id = send(channel, sender, body, priority ?? "normal");
  return { ok: true, id };
});

// File upload — store under the media dir, return its path. The app then sends a normal
// message referencing it as `[attachment: <path>]`, which agents read. Any file type.
app.post("/upload", async (req, reply) => {
  if (!authed(req)) return reply.code(401).send({ error: "unauthorized" });
  let data: any;
  try { data = await (req as any).file(); } catch (e: any) {
    return reply.code(400).send({ error: e?.message ?? "upload failed" });
  }
  if (!data) return reply.code(400).send({ error: "no file in request" });
  const orig = String(data.filename || "");
  const dot = orig.lastIndexOf(".");
  const ext = (dot >= 0 ? orig.slice(dot + 1) : "").replace(/[^a-z0-9]/gi, "").slice(0, 8).toLowerCase() || "bin";
  const stem = (dot >= 0 ? orig.slice(0, dot) : orig).replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  const name = `up-${Date.now()}-${Math.floor(Math.random() * 1e9)}${stem ? "-" + stem : ""}.${ext}`;
  const dest = join(MEDIA_DIR, name);
  try {
    const buf = await data.toBuffer();
    writeFileSync(dest, buf);
    return { ok: true, path: dest, name, bytes: buf.length, filename: orig, mimetype: data.mimetype || "" };
  } catch (e: any) {
    return reply.code(500).send({ ok: false, error: e?.message ?? "save failed" });
  }
});

// Serve an uploaded file back to the app. Authed; name is basename-only (no traversal).
app.get("/media/:name", async (req, reply) => {
  if (!authed(req)) return reply.code(401).send({ error: "unauthorized" });
  const { name } = req.params as any;
  if (!/^[A-Za-z0-9._-]+$/.test(name)) return reply.code(400).send({ error: "bad name" });
  const p = join(MEDIA_DIR, name);
  if (!existsSync(p)) return reply.code(404).send({ error: "not found" });
  const ext = (name.split(".").pop() || "").toLowerCase();
  const IMG: Record<string, string> = { png: "image/png", gif: "image/gif", webp: "image/webp",
    jpg: "image/jpeg", jpeg: "image/jpeg", bmp: "image/bmp", avif: "image/avif", svg: "image/svg+xml" };
  const INLINE_OTHER: Record<string, string> = { pdf: "application/pdf",
    txt: "text/plain; charset=utf-8", log: "text/plain; charset=utf-8", md: "text/plain; charset=utf-8",
    csv: "text/csv; charset=utf-8", json: "application/json; charset=utf-8",
    xml: "application/xml; charset=utf-8", yaml: "text/plain; charset=utf-8", yml: "text/plain; charset=utf-8" };
  const ct = IMG[ext] || INLINE_OTHER[ext] || "application/octet-stream";
  const inline = ext in IMG || ext in INLINE_OTHER;
  reply.header("Content-Type", ct)
       .header("Content-Disposition", `${inline ? "inline" : "attachment"}; filename="${name}"`)
       .header("Cache-Control", "private, max-age=86400");
  return reply.send(readFileSync(p));
});

// --- generic agent controls: each maps to a dispatch-ctl verb -------------------
// The gateway never leaks the underlying verb to the client — it only sees id/label/meta.
const ACTIONS = [
  { id: "interrupt", label: "Interrupt", group: "control", danger: true, confirm: true, verb: "esc" },
  { id: "flush", label: "Flush queue", group: "control", verb: "flush" },
  { id: "pause", label: "Pause", group: "control", verb: "pause" },
  { id: "resume", label: "Resume", group: "control", verb: "resume" },
  { id: "unstick", label: "Mark idle", group: "control", confirm: true, verb: "idle" },
];

app.get("/actions", async (req, reply) => {
  if (!authed(req)) return reply.code(401).send({ error: "unauthorized" });
  return { actions: ACTIONS.map(({ verb, ...meta }) => meta) };
});

app.post("/actions/:id", async (req, reply) => {
  if (!authed(req)) return reply.code(401).send({ error: "unauthorized" });
  const { id } = req.params as any;
  const { agent } = (req.body ?? {}) as any;
  const action = ACTIONS.find((a) => a.id === id);
  if (!action) return reply.code(404).send({ error: "unknown action" });
  // strict allowlist on agent — execFile (no shell) + regex blocks injection
  if (!agent || !/^[a-z0-9-]+$/.test(agent))
    return reply.code(400).send({ error: "valid agent required" });
  try {
    const output = await run(DISPATCH_CTL, [action.verb, agent], 10000);
    return { ok: true, action: id, agent, output };
  } catch (e: any) {
    return reply.code(500).send({ ok: false, error: e.message });
  }
});

// --- live feed: poll the bus and push new rows to subscribed sockets ---
const clients = new Set<{ socket: any; lastId: number; channel: string | null }>();

app.get("/ws", {
  websocket: true,
  preValidation: async (req: any, reply: any) => {
    if (!authed(req)) { await reply.code(401).send({ error: "unauthorized" }); }
  },
}, (socket: any, req: any) => {
  if (!authed(req)) { socket.close(1008, "unauthorized"); return; }
  const ch = (req.query as any)?.channel ?? null;
  const c = { socket, lastId: (qMaxId.get() as any).m as number, channel: ch };
  clients.add(c);
  socket.on("message", (raw: Buffer) => {
    try {
      const m = JSON.parse(raw.toString());
      if (m.type === "send" && m.channel && m.body)
        send(m.channel, m.sender ?? "human", m.body, m.priority ?? "normal");
    } catch { /* ignore malformed */ }
  });
  socket.on("close", () => clients.delete(c));
});

setInterval(() => {
  if (clients.size === 0) return;
  for (const c of clients) {
    const rows = qAfter.all(c.lastId) as any[];
    for (const r of rows) {
      if (c.channel && r.channel !== c.channel) { c.lastId = r.id; continue; }
      try { c.socket.send(JSON.stringify({ type: "message", ...r })); } catch { /* drop */ }
      c.lastId = r.id;
    }
  }
}, 1000);

app.listen({ port: PORT, host: "0.0.0.0" })
  .then(() => console.log(`companion-engine gateway listening on :${PORT} (db ${DB_PATH})`))
  .catch((e) => { console.error(e); process.exit(1); });
