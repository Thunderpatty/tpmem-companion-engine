-- tpmem-companion-engine — SQLite schema
--
-- Apply to your KB database (default ~/.tpmem/kb.db):
--     sqlite3 ~/.tpmem/kb.db < schema.sql
--
-- The engine needs five tables:
--   entities, notes        — the lightweight knowledge base the agents read/write
--   inbox, outbox          — incoming messages and queued replies
--   wakeup_queue           — self-scheduled future agent runs
--
-- entities/notes are a minimal KB. If you already have a richer KB, you only
-- need inbox / outbox / wakeup_queue plus an entity with slug='daemon-relay'
-- (the daemon logs every wake against it).

-- ── knowledge base ─────────────────────────────────────────────────────────
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

-- ── transport queues ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS inbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    transport     TEXT NOT NULL,             -- telegram | email | ...
    external_id   TEXT,                      -- transport-side id (telegram update_id, imap uid)
    user_id       TEXT,                      -- sender id from the transport
    reply_to      TEXT,                      -- where a reply should go (telegram chat_id, email addr)
    payload       TEXT NOT NULL,             -- the message body (plus [attachment: <path>] refs)
    received_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    status        TEXT DEFAULT 'pending',    -- pending | processing | done | failed | refused
    routed_agent  TEXT,                      -- which agent profile we routed to
    result        TEXT,                      -- short outcome string
    processed_at  DATETIME,
    UNIQUE(transport, external_id)           -- idempotency: never reprocess the same update
);
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status, received_at);
CREATE INDEX IF NOT EXISTS idx_inbox_agent  ON inbox(routed_agent, received_at DESC);

CREATE TABLE IF NOT EXISTS outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    transport     TEXT NOT NULL,
    reply_to      TEXT NOT NULL,
    payload       TEXT NOT NULL,
    inbox_id      INTEGER REFERENCES inbox(id) ON DELETE SET NULL,  -- optional link back
    status        TEXT DEFAULT 'pending',    -- pending | sent | failed
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    sent_at       DATETIME,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, created_at);

CREATE TABLE IF NOT EXISTS wakeup_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_slug      TEXT NOT NULL,         -- which agent to wake (matches an entity slug / skill name)
    fire_at         DATETIME NOT NULL,     -- earliest time to fire
    prompt          TEXT NOT NULL,         -- prompt to inject when waking
    condition_type  TEXT DEFAULT 'time',   -- time | event | check
    condition_data  TEXT,                  -- JSON, free-form per condition_type
    task_slug       TEXT,                  -- optional link back to a task entity
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    fired_at        DATETIME,              -- NULL = pending; set when the daemon fires it
    result          TEXT                   -- short outcome string set by the daemon
);
CREATE INDEX IF NOT EXISTS idx_wakeup_fire  ON wakeup_queue(fire_at) WHERE fired_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_wakeup_agent ON wakeup_queue(agent_slug, fired_at);

-- ── required seed row ──────────────────────────────────────────────────────
-- The daemon logs every spawn/wakeup as an audit note against this entity.
INSERT OR IGNORE INTO entities (type, slug, name, summary)
VALUES ('meta', 'daemon-relay', 'tpmem-daemon relay log',
        'Audit trail of every agent wake the daemon performed.');

-- Optional: an entity for the companion agent, so it has somewhere to log to.
INSERT OR IGNORE INTO entities (type, slug, name, summary)
VALUES ('agent', 'companion', 'Companion agent',
        'Persistent single-thread Claude Code session handling ongoing conversation.');
