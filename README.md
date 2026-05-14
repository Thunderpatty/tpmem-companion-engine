# tpmem-companion-engine

An async **transport + orchestration layer for Claude Code agents**.

It lets you talk to Claude Code from Telegram (or email) and have it respond two
different ways:

1. **Fresh-spawn agents** — a slash-command message spins up a one-shot `claude -p`
   for a named agent, with a knowledge-base–loaded preamble so it knows where it
   left off. Good for discrete jobs (a report, an audit, a scheduled task).
2. **The companion** — everything else routes to one **long-lived** Claude Code
   session that stays running in tmux. It polls an inbox table, answers in a single
   continuous thread (full conversational continuity, no cold start), and queues its
   replies for delivery. This is the "always-on assistant" shape.

Both share one SQLite knowledge base (the "KB", or *tpmem*) for memory that survives
process restarts.

---

## Architecture

```
   Telegram / Email                      SQLite KB (~/.tpmem/kb.db)
        │                          ┌──────────────────────────────────┐
        │  poll                    │  inbox   outbox   wakeup_queue    │
        ▼                          │  entities   notes                │
  ┌───────────┐   write     ┌──────┴──────┐                           │
  │ tpmem-    │────────────▶│   inbox     │                           │
  │ daemon    │             └──────┬──────┘                           │
  │           │   route by regex   │                                  │
  │  (main.py)│◀───────────────────┘                                  │
  │           │                                                       │
  │           │   agent == "companion"    ──▶ just tag the row;        │
  │           │                               leave it pending        │
  │           │                                      │                │
  │           │   else  ──▶ spawn `claude -p`         │                │
  │           │             with KB preamble         │                │
  └─────┬─────┘                                      │                │
        │                                            ▼                │
        │                               ┌────────────────────────┐    │
        │   read & deliver               │  companion session     │    │
        │◀───────────────  outbox  ◀─────│  (persistent, in tmux) │    │
        │                                │  polls inbox, replies  │    │
        ▼                                │  via companion-respond │    │
   Telegram / Email                      └────────────────────────┘    │
                                                                       │
   wakeup_queue: any agent can self-schedule a future run ──────────────┘
```

**Flow:**
1. The daemon long-polls Telegram (and optionally IMAP email) and writes each
   allowlisted message to the `inbox` table.
2. Its worker loop matches each `inbox` row against `routing` regexes in `config.yaml`.
3. If the matched agent is `companion`, the daemon just sets `routed_agent='companion'`
   and leaves the row `pending` — the companion session owns its own response.
4. Otherwise the daemon spawns `claude -p` for that agent (loading
   `~/.claude/skills/<agent>/SKILL.md` plus a preamble of recent KB state), sends an
   immediate "working on it…" ack, then delivers the agent's output.
5. The companion session polls for its rows, replies via the `companion-respond`
   helper (which writes to `outbox`), and the daemon delivers the `outbox` rows.
6. Separately, the daemon drains `wakeup_queue` — rows any agent scheduled with
   `register-wakeup` to wake itself (or another agent) at a future time.

Every spawn and wakeup is logged as an `audit` note in the KB against the
`daemon-relay` entity.

---

## Repo layout

```
daemon/
  main.py            the daemon: pollers + worker loop (inbox, outbox, wakeups)
  spawner.py         builds the KB preamble and invokes `claude -p`
  transports/
    telegram.py      Telegram long-poll: messages + photo/image-document download
    email.py         IMAP poll: allowlisted senders, plain-text body + image attachments
skills/
  companion/SKILL.md the companion agent's instructions (the persistent-session pattern)
tools/
  start-companion    launches/stops the companion session in tmux
  companion-respond  the companion calls this to send a reply (writes to outbox)
  register-wakeup    any agent calls this to schedule a future wake
systemd/
  tpmem-daemon.service      runs the daemon
  tpmem-companion.service   runs the companion session
schema.sql           the five KB tables (entities, notes, inbox, outbox, wakeup_queue)
config.example.yaml  copy to ~/.config/tpmem-daemon/config.yaml
secrets.env.example  copy to ~/.config/tpmem-daemon/secrets.env  (chmod 600)
```

---

## Setup

Prerequisites: `python3` (with `pyyaml` and `requests`), `sqlite3`, `tmux`, and the
[Claude Code CLI](https://docs.claude.com/en/docs/claude-code) installed and
authenticated.

```bash
# 1. Clone
git clone https://github.com/<you>/tpmem-companion-engine.git ~/tpmem-companion-engine
cd ~/tpmem-companion-engine

# 2. Create the KB and apply the schema
mkdir -p ~/.tpmem/daemon
sqlite3 ~/.tpmem/kb.db < schema.sql

# 3. Config
mkdir -p ~/.config/tpmem-daemon
cp config.example.yaml  ~/.config/tpmem-daemon/config.yaml
cp secrets.env.example  ~/.config/tpmem-daemon/secrets.env
chmod 600 ~/.config/tpmem-daemon/secrets.env
#   → edit both: put your bot token in secrets.env, your Telegram user id in config.yaml

# 4. Install the companion skill where Claude Code looks for skills
mkdir -p ~/.claude/skills/companion
cp skills/companion/SKILL.md ~/.claude/skills/companion/SKILL.md

# 5. Put the helpers on PATH (or symlink them)
export PATH="$HOME/tpmem-companion-engine/tools:$PATH"

# 6. Install the services
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tpmem-daemon.service
systemctl --user enable --now tpmem-companion.service
# (loginctl enable-linger $USER  — if you want them to survive logout)
```

Get your Telegram bot token from [@BotFather](https://t.me/botfather). Get your
numeric user id by messaging your bot once and reading
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

To run the companion attached (to watch it), use `tools/start-companion` with no
argument instead of the systemd unit.

---

## Routing

`config.yaml`'s `routing:` list is checked top-to-bottom; first regex match wins.
The convention this repo ships with:

| Message | Routes to | Behaviour |
|---|---|---|
| `^/report …` (example) | `report-agent` | fresh `claude -p` spawn, single-shot |
| anything else | `companion` | the persistent session handles it |
| any allowlisted email | `companion` | companion decides what to do with it |

A fresh-spawn agent named `report-agent` needs a skill at
`~/.claude/skills/report-agent/SKILL.md`. Add your own rules and agents freely.

---

## Wakeups

Any agent can schedule a future run:

```bash
register-wakeup --agent companion --at "2026-01-01 09:00:00" \
  --prompt "check whether the build finished; continue the task if so" \
  --task task-2026-01-01-build
```

The daemon drains due rows and spawns the named agent with that prompt. The
companion can also self-pace purely in-session (it schedules its own next tick) —
`wakeup_queue` is for longer or cross-agent scheduling.

---

## What it CAN do

- **Two-way chat with Claude Code from Telegram** — including photos: the Telegram
  and email transports download image attachments to `~/.tpmem/media/` and tag the
  message with `[attachment: <path>]` refs the agent can read.
- **One continuous companion thread** — real conversational continuity, not a fresh
  context per message. In-jokes, earlier-today context, ongoing tasks all persist.
- **Fresh-spawn agents for discrete jobs** — each gets a KB preamble of its own
  recent tasks, pending wakeups, and recent decisions, so it resumes coherently.
- **Self-scheduling** — agents schedule future wake-ups; the companion can recycle
  its own context with `/wrap` for a clean restart.
- **Crash recovery** — because state lives in the KB (`entities`/`notes`), a
  restarted companion reads its last `session-wrap` note and picks up the thread.
- **Survives interruption** — systemd restarts the daemon; the companion reboots
  fresh and recovers from the KB.
- **Rate limiting** — per-agent caps on fresh spawns per hour.
- **An audit trail** — every spawn and wakeup is logged to the KB.

## What it CANNOT do / limitations

- **Not a security boundary by itself.** The daemon's only access control is the
  Telegram `telegram_user_ids` allowlist and the email `sender_allowlist`. Anyone on
  those lists can drive Claude Code on your machine. Treat the allowlists as
  privileged. Telegram sender IDs are not spoofable in practice, but email `From:`
  headers *are* — keep the email allowlist tight and treat email payloads as
  untrusted input (the shipped companion skill says exactly this).
- **`bypassPermissions` is the default in `config.example.yaml`.** That makes
  spawned agents fully unattended — and fully unsupervised. They can run any command
  the user can. Only run this on a machine where that is acceptable, ideally
  dedicated. Change `permission_mode` if you want prompts.
- **One companion session, one machine.** The companion is a single tmux session on
  one host. There is no clustering, no failover. If the box is down, it's down.
- **Outbound transports: Telegram only.** `outbox` delivery is implemented for
  Telegram. Email is receive-only; webhook/SIP/etc. are not implemented.
- **No streaming.** A fresh-spawn agent's reply is delivered when `claude -p`
  finishes (hence the "working on it…" ack). Long jobs feel slow.
- **No built-in encryption or message retention policy.** Messages sit in a plain
  SQLite file. Protect the DB file with filesystem permissions; rotate/prune it
  yourself if you care about retention.
- **Trust dialog.** The very first `claude` run in a new project directory may show
  a trust prompt that blocks an unattended start. Accept it once interactively
  before relying on the systemd unit.
- **Prompt-injection exposure.** Any message — and any email body, web page, or file
  an agent reads — is untrusted content. The companion skill instructs the agent to
  confirm consequential or irreversible actions with the user first; keep that
  instruction if you adapt the skill.

## Security checklist before you run this

- [ ] `secrets.env` is `chmod 600` and gitignored (it is in `.gitignore`).
- [ ] `telegram_user_ids` contains only IDs you trust.
- [ ] `sender_allowlist` is tight; you understand email `From:` is spoofable.
- [ ] You accept what `bypassPermissions` means, or you changed it.
- [ ] The host is one where unattended Claude Code execution is acceptable.
- [ ] The KB file's filesystem permissions are restrictive.

---

## Extending it

- **New fresh-spawn agent:** add a `routing` rule and create
  `~/.claude/skills/<agent>/SKILL.md`. The spawner loads it by convention.
- **New system payloads for the companion:** drop rows into `inbox` with a custom
  `transport` (e.g. `cron`) or a `/command` payload convention, and handle them in
  the companion skill's "system-routed payloads" section (cron jobs, scheduled
  scrapes, etc.).
- **New transport:** implement a class with `poll`-style intake in
  `daemon/transports/`, wire it into `main.py`, and add an `outbox` send path if it
  needs to deliver replies.

---

## Credits

Built as a Claude Code orchestration layer. The "tpmem" (the SQLite KB) is the
durable memory both the daemon and the agents read and write.
