-- tpmem-companion-engine — SQLite schema
--
-- Apply to your KB database (default ~/.tpmem/kb.db):
--     sqlite3 ~/.tpmem/kb.db < schema.sql
--
-- The engine uses these tables:
--   entities, notes   — the lightweight knowledge base agents read/write (tpmem)
--   notes_fts         — fts5 index over notes (kb search); triggers feed content + tags
--   relations         — entity-to-entity graph (kb entity relations / kb context)
--   handoffs          — session continuity records (kb handoff; backs the wrap system)
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
    status_updated_at DATETIME,         -- when status last changed (kb status)
    status_notes      TEXT,             -- why status changed (kb status)
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_entities_type   ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status);

-- entity-to-entity graph (kb entity relations / kb context one-hop)
CREATE TABLE IF NOT EXISTS relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id     INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    to_id       INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation    TEXT NOT NULL,          -- works_on | makes | depends_on | part_of | replaces ...
    context     TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(from_id, to_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_id);
CREATE INDEX IF NOT EXISTS idx_relations_to   ON relations(to_id);

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

-- ── full-text search over notes (kb search / kb context) ───────────────────
-- fts5 external-content index. The triggers feed BOTH content AND tags so tag
-- tokens are MATCH-searchable immediately — NOT only up to the last 'rebuild'.
-- (Feeding content only is a real defect: `tags:` MATCH and unscoped tag hits
-- silently rot between rebuilds. Found on the fleet 2026-09-20; ship it correct.)
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(content, tags, content='notes', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, content, tags) VALUES('delete', old.id, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, content, tags) VALUES('delete', old.id, old.content, old.tags);
    INSERT INTO notes_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;

-- ── session continuity (kb handoff / kb write-handoff; backs the wrap system) ──
CREATE TABLE IF NOT EXISTS handoffs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_end     DATETIME DEFAULT CURRENT_TIMESTAMP,
    session_start   DATETIME,
    project_slug    TEXT,
    completed       TEXT,
    next_steps      TEXT,
    open_questions  TEXT,
    blockers        TEXT,
    notes           TEXT,
    acceptance_criteria TEXT              -- if set, kb flips the project to pending-review
);
CREATE INDEX IF NOT EXISTS idx_handoffs_session_end ON handoffs(session_end DESC);
CREATE INDEX IF NOT EXISTS idx_handoffs_project     ON handoffs(project_slug);

-- NOTE: no `todos` table. `kb todos` is intentionally unsupported on a companion box —
-- the lab's reminder/scheduling subsystem (todo_* satellites + a daemon) is a whole
-- feature, not a memory table, and a bare stub would look like reminders work when they
-- don't. Port that deliberately if a box ever needs it.

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
