-- tpmem-companion-engine — SQLite schema
--
-- Apply to your KB database (default ~/.tpmem/kb.db):
--     sqlite3 ~/.tpmem/kb.db < schema.sql
--
-- The engine uses these tables:
--   entities, notes   — the lightweight knowledge base agents read/write (tpmem)
--   messages          — the durable per-channel chat log (the gateway's source of truth)
--   inbox             — pending work routed to an agent (the dispatcher wakes on it)
--   outbox            — replies queued for an OPTIONAL external transport to deliver
--   read_state        — per-channel read cursor for the webapp's unread badge
--
-- Scheduled work uses cron + `queue-job` (which drops a row in `inbox`), so there is no
-- separate wakeup queue — a scheduled task is just an inbox row the dispatcher wakes on.
--
-- The default install talks to agents entirely through `messages` + `inbox` via the
-- local gateway webapp; `outbox` + the transport bridge are only used if you enable
-- an external transport (Telegram / email), which is OFF by default.

-- ── knowledge base (tpmem) ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,          -- person | project | task | tool | concept | agent | meta ...
    slug        TEXT UNIQUE NOT NULL,   -- short identifier
    name        TEXT,
    summary     TEXT,
    status      TEXT DEFAULT 'active',  -- active | pending-review | blocked | done | archived ...
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_entities_type   ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status);

CREATE TABLE IF NOT EXISTS notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id    INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    category     TEXT NOT NULL,         -- decision | fact | todo | risk | milestone | audit | status ...
    content      TEXT NOT NULL,
    importance   INTEGER DEFAULT 5,     -- 1-10; higher = surface first
    tags         TEXT,                  -- comma-separated
    source       TEXT,                  -- provenance, e.g. 'companion:<task>:<checkpoint>'
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_notes_entity     ON notes(entity_id);
CREATE INDEX IF NOT EXISTS idx_notes_category   ON notes(category);
CREATE INDEX IF NOT EXISTS idx_notes_importance ON notes(importance DESC);

-- ── chat channels (durable source of truth for the webapp) ─────────────────
-- One row per message in a channel. The gateway reads/streams this table; a human
-- message in an agent's channel is also mirrored into `inbox` so the dispatcher
-- wakes that agent.
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel     TEXT NOT NULL,               -- usually the agent slug
    sender      TEXT NOT NULL,               -- 'human' | '<agent-slug>' | 'system'
    body        TEXT NOT NULL,
    priority    TEXT DEFAULT 'normal',       -- normal | critical
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel, id);

-- ── work queues ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS inbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    transport     TEXT NOT NULL,             -- gateway | cron | telegram | email | ...
    external_id   TEXT,                      -- transport-side id (telegram update_id, imap uid)
    user_id       TEXT,                      -- sender id from the transport
    reply_to      TEXT,                      -- where an external reply should go (chat_id, email addr)
    payload       TEXT NOT NULL,             -- the message body (plus [attachment: <path>] refs)
    received_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    status        TEXT DEFAULT 'pending',    -- pending | processing | done | failed | refused
    routed_agent  TEXT,                      -- which agent this row is for (the dispatcher wakes it)
    priority      TEXT DEFAULT 'normal',     -- normal | critical (criticals bypass the watermark)
    result        TEXT,                      -- short outcome string
    processed_at  DATETIME,
    UNIQUE(transport, external_id)           -- idempotency: never reprocess the same external event
);
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status, received_at);
CREATE INDEX IF NOT EXISTS idx_inbox_agent  ON inbox(routed_agent, status, received_at DESC);

CREATE TABLE IF NOT EXISTS outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    transport     TEXT NOT NULL,
    reply_to      TEXT NOT NULL,
    payload       TEXT NOT NULL,
    inbox_id      INTEGER REFERENCES inbox(id) ON DELETE SET NULL,  -- optional link back
    origin_agent  TEXT,                      -- which agent queued the reply
    status        TEXT DEFAULT 'pending',    -- pending | sent | failed
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    sent_at       DATETIME,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, created_at);

-- ── webapp read cursor ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS read_state (
    channel      TEXT PRIMARY KEY,
    last_read_id INTEGER NOT NULL DEFAULT 0
);

-- ── seed rows ─────────────────────────────────────────────────────────────────
-- Every agent gets an entity so it has somewhere to log to; the two defaults:
INSERT OR IGNORE INTO entities (type, slug, name, summary)
VALUES ('agent', 'companion', 'Companion agent',
        'Persistent single-thread Claude Code session — the primary interface.');
INSERT OR IGNORE INTO entities (type, slug, name, summary)
VALUES ('agent', 'curator', 'Curator agent',
        'Persistent session that maintains the memory layer on a schedule.');
