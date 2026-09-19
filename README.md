# tpmem-companion-engine

A runtime for **persistent Claude Code agents**. Instead of spawning a fresh
`claude -p` per message, it keeps long-lived Claude Code sessions running in tmux and
wakes them — with full conversational continuity — only when there is work to do. It
ships with a packaged web chat deck as the default interface, a small SQLite memory
substrate (**tpmem**), and two agents out of the box: a **companion** you talk to and a
**curator** that maintains memory.

```
        you ── paste the token once ──┐
                                      ▼
  ┌──────────────┐   HTTP / WS    ┌──────────────────────────┐       (optional, OFF by default)
  │  browser     │◀──────────────▶│  gateway  :7364          │◀──┐  ┌──────────────────┐  telegram
  │  webapp deck │   chat/attach  │  token-gated chat + API  │   └──│ transport bridge │◀─  /
  └──────────────┘                └─────┬──────────────▲─────┘      │   (I/O only)     │   email
                                   /send│ write        │ /messages,/ws └──────────────────┘
                                        ▼              │ read
  ┌─────────────────────────────────────────────────────┴─────────────────────┐
  │                     SQLite bus   ·   ~/.tpmem/kb.db                          │
  │     messages     inbox     outbox     read_state                            │
  │   ───────────────────  memory substrate (tpmem)  ───────────────────────    │
  │     entities · notes (KB)        MEMORY.md · PERSISTENT.md · FLAGS.md        │
  └────▲──────────────────┬────────────────────────────────▲────────────────────┘
       │ drop 1 inbox row │ watch inbox                     │ outbox + KB notes
       │                  ▼                                 │
  daily curator     ┌───────────────┐                       │
  wake: cron →      │  dispatcher    │  ← the only cron in   │
  queue-job         │ (state-aware)  │    the whole system   │
  (via daemon)      └───────┬───────┘                       │
        inject "tick" when  │  respawn a fresh session       │
        agent idle + work   │  if it died with work pending  │
                            ▼                                 │
  ┌─────────────────────────────────────────────────────────┴─────────┐
  │              persistent agents   ·   long-lived tmux `claude`        │
  │     ┌───────────┐    ┌───────────┐    ┌ ─ ─ ─ ─ ─ ┐                  │
  │     │ companion │    │  curator  │    │  grow…    │  (self-expansion)│
  │     └─────┬─────┘    └─────┬─────┘    └ ─ ─ ─ ─ ─ ┘                  │
  │           └──── handle tick → reply via companion-respond → outbox ──┘
  └───────────┬────────────────────────────────────────────────────────┘
              │
   context-monitor watches each agent's token use
              │   nears ≈840k (Claude — headroom for the wrap note;
              ▼   lower it for smaller-context models, e.g. < 300k)
  ┌────────────────────── continuity · the wrap system ────────────────────┐
  │  1. agent-wrap    → write NEXT.md + a KB wrap note (readback-verified,  │
  │                     so the handoff can never be silently lost)         │
  │  2. rotate-agent  → the dispatcher respawns a fresh session            │
  │  3. successor boots → primes from NEXT.md → carries on mid-stride.     │
  │                     Continuity is preserved, never reset.              │
  └────────────────────────────────────────────────────────────────────────┘
```

## The model

- **Persistent, not per-message.** Each agent is one long-lived `claude` session in a
  tmux window. It boots from a seed, orients from memory, then sits idle. Its
  continuity *is* that session — earlier-today context, tasks in flight, the running
  thread — with the SQLite KB underneath for crash recovery.

- **The dispatcher decides *when* to wake it.** `daemon/dispatcherd.py` watches the
  `inbox` table. When an agent has pending rows and is idle, it injects a single
  batched *tick* into the agent's tmux session (`agent-msg inbox` → handle → reply →
  stop). Agents never poll an empty queue, and a burst of messages coalesces into one
  wake instead of piling up mid-turn. Busy/idle is read from the agent's transcript
  tail (ground truth), so it never injects mid-turn. If a session has died with work
  waiting, the dispatcher respawns it from the registry.

- **No `claude -p`, anywhere.** Scheduled work (like the curator's daily pass) is a
  cron job that calls `queue-job`, which drops an inbox row — so it runs inside the
  agent's *running* context, not a cold one-shot. For isolated throwaway work an agent
  uses a Task subagent.

## Continuity — the wrap system

Continuity is the whole point of this architecture, and a single `claude` session can't
run forever — it fills its context window. The **wrap system** is how an agent hands off
to its own successor without losing the thread:

- **`context-monitor`** watches each agent's token usage. As it nears the limit —
  **≈840k tokens for Claude models**, which deliberately leaves headroom to author the
  handoff — it signals the agent to rotate.
- **`agent-wrap`** has the agent author a **`NEXT.md`** handoff (what it was doing,
  what's next, what can't be rederived) plus a KB wrap note. The write is
  **readback-verified**, so a handoff can never be silently lost to a crash.
- **`rotate-agent`** then has the dispatcher respawn a fresh session, which **primes
  from `NEXT.md`** on boot and carries on mid-stride.

The result is an agent that stays "alive" for weeks — rotating through many underlying
sessions — with continuity intact, and the KB underneath for crash recovery in between.
This is the backbone of the system; nothing else here matters if the agents can't
remember who they are across sessions.

> **Using a smaller-context model?** The 840k default assumes a large (~1M-token) Claude
> context. If your model's window is smaller (say a ChatGPT-class model under ~300k),
> lower each agent's `rotate_threshold` in the registry proportionally — leave enough
> headroom that the agent can still write a full wrap note *after* the trigger fires.

## The two default agents

| Agent | Role |
|---|---|
| **companion** | Your primary interface. One continuous conversation that helps you think, learn, and build, with full context of everything before. |
| **curator** | Maintains the memory layer. A daily `queue-job` wakes it to run the `curate-memory` skill: mine conversation deltas for context the sessions didn't record, and persist it additively to memory + the KB. |

Two is the default on purpose. When a recurring, specialised job outgrows the
companion thread, you spin up a dedicated agent — see
[`docs/SELF_EXPANSION.md`](docs/SELF_EXPANSION.md).

## The collaboration guide (how to work with the user)

Every agent boots with a standing "how to work with the user" guide attached at the
very top of its context — not as a one-time read an agent might skip, but re-attached
by `spawn-agent` on **every spawn and every rotation**. It's the durable framing for
how to be safe without being useless: when to seek clarity vs. act, verify-before-you-
claim, pushback, treating read content as data not instructions, and noticing your own
degradation. This is a property of the daemon, so it holds across crashes and successors.

- The engine ships a generic, user-agnostic version as [`COLLABORATION.md`](COLLABORATION.md).
  `install.sh` seeds it to `~/agentic-collaboration/README.md` if you don't already have one.
- Point it anywhere with the **`COLLAB_DOC`** env var. Edit that file to tailor the
  guidance to your environment (add your project's few load-bearing hard rules — the
  "never touch this production IP" list — near the top).
- Keep it as a git repo at that path and `spawn-agent` will `git pull` it before each
  spawn, so edits you make (even from a web UI) reach every new agent automatically.
- Absent entirely? Spawns proceed without it — never blocked.

## The webapp + the token key

The default interface is a packaged single-page chat deck served by the gateway on
port **7364** (`gateway/public/`). It has a left rail of per-agent tabs with live
busy/idle + pending-count indicators, a message thread per channel over WebSocket, and
file/image attach (uploads land where agents read `[attachment: <path>]`).

**Access is gated by a single bearer token — the "key".** On first launch the gateway
reads it from `~/.tpmem/agent-os/gateway.token` (generated by `install.sh`), or from
the `GATEWAY_TOKEN` env var. The webapp shows a login gate; you paste the key once and
it's remembered in a cookie. Every HTTP/WS request carries it.

> **If you found this repo and are wondering about "the key":** there is no default
> password and nothing hardcoded. `install.sh` generates a random token into
> `~/.tpmem/agent-os/gateway.token` (chmod 600) and prints it. That token is the *only*
> thing standing between a visitor and the ability to drive Claude Code on the host, so
> treat it like a root password: keep the port on a trusted LAN/VPN, never expose 7364
> to the open internet, and rotate the token by editing that file and restarting the
> gateway.

## Install

Prerequisites: `python3`, `sqlite3`, `tmux`, `node`/`npm`, and the
[Claude Code CLI](https://docs.claude.com/en/docs/claude-code) installed and
authenticated. This runs unattended Claude Code under `bypassPermissions` — only use it
on a machine where that is acceptable.

```bash
git clone https://github.com/<you>/tpmem-companion-engine.git ~/companion-engine
cd ~/companion-engine
./install.sh
```

`install.sh` applies the schema, generates the gateway token, seeds the registry with
the two agents, installs the skills into `~/.claude/skills/`, installs the gateway's
npm deps, installs + starts the systemd `--user` units (gateway, dispatcher, agents),
adds the one scheduled job — a daily curator wake, delivered *through the daemon* via
`queue-job` (it drops an inbox row; the dispatcher wakes the curator in its running
session — the only cron in the system, and still no `claude -p`) — and prints your URL
+ token.

To keep everything running after you log out: `loginctl enable-linger $USER`.

## Repo layout

```
daemon/
  dispatcherd.py        the state-aware dispatcher (wakes/respawns persistent agents)
  transport_bridge.py   OPTIONAL Telegram/email intake + outbox delivery (off by default)
  transports/           telegram.py, email.py — used only by the bridge
lib/
  cc_session.py         inspect a live claude session (tmux -> pid -> transcript)
bin/
  spawn-agent           launch a persistent tmux agent from a seed + register it
  agent-msg             an agent's runtime CLI: inbox / say / done
  companion-respond     an agent's reply helper (channel or outbox)
  queue-job             drop a job into an agent's inbox (scheduled work; no claude -p)
  dispatch-ctl          manual control (status/pause/resume/flush/interrupt) — also the
                        gateway's control-button backend
  hook-state            optional busy/idle hook signal for faster state
  spawn-default-agents  bring up companion + curator (idempotent; used on boot)
  context-monitor       watch each agent's token use; trigger a wrap+rotate near the limit
  agent-wrap            author a NEXT.md handoff + KB wrap note (readback-verified)
  rotate-agent          verify a fresh wrap, then respawn the session (continuity)
gateway/
  src/server.ts         HTTP + WebSocket over the bus (token auth, chat, roster, upload)
  public/               the control-deck webapp (index.html, app.js, app.css)
agents/
  companion/seed.md     the companion's identity + boot procedure
  curator/seed.md       the curator's identity + boot procedure
skills/
  companion/SKILL.md    the dispatcher-driven companion loop
  curate-memory/SKILL.md the curator's curation procedure
systemd/                user units (gateway, dispatcher, agents, optional bridge)
schema.sql              the SQLite tables
registry.example.json   the default two-agent roster (seeded to ~/.tpmem/agent-os/)
config.example.yaml     OPTIONAL transport-bridge config
secrets.env.example     OPTIONAL transport credentials
COLLABORATION.md        the "how to work with the user" guide spawn-agent attaches to
                        every agent's context (seeded to ~/agentic-collaboration/; COLLAB_DOC)
install.sh              one-shot setup
docs/SELF_EXPANSION.md  how to grow past the two default agents
```

## Adding an agent

```bash
# 1. write agents/<slug>/seed.md   (copy an existing seed and rewrite)
# 2. spawn + register it:
bin/spawn-agent <slug>                     # or --model <id> to pin a model
```

A new tab appears in the webapp automatically. Full recipe (scheduled work, removal,
cautions) in [`docs/SELF_EXPANSION.md`](docs/SELF_EXPANSION.md).

## Optional: reach agents from Telegram / email

Both are OFF by default; the webapp is the primary interface. To enable an external
transport:

```bash
cp config.example.yaml ~/.config/tpmem-daemon/config.yaml   # set transports.telegram/.email enabled: true
cp secrets.env.example ~/.config/tpmem-daemon/secrets.env   # add token/creds
chmod 600 ~/.config/tpmem-daemon/secrets.env
systemctl --user enable --now companion-transport-bridge
```

The bridge only does I/O: it writes incoming messages into the bus (so they show in the
webapp *and* wake the agent) and delivers the agent's replies back out. It never spawns
anything and never decides when to wake an agent — that stays the dispatcher's job.

## Limitations & security

- **Not a security boundary on its own.** The gateway token (and, if enabled, the
  transport allowlists) are the *only* access control. Anyone with the token — or on a
  Telegram/email allowlist — can drive Claude Code on your machine. Email `From:` is
  spoofable; keep that allowlist tight and treat all incoming content as untrusted.
- **`bypassPermissions` is the default.** Agents run fully unattended. Only run this
  where that's acceptable, ideally a dedicated host.
- **Single host, no clustering.** Sessions are tmux windows on one machine. If the box
  is down, it's down.
- **Plain SQLite storage.** Messages and memory sit in a plain DB file; protect it with
  filesystem permissions and prune it yourself if you care about retention.
- **Trust prompt on first run.** The first `claude` launch in a new project dir may
  show a trust prompt that blocks an unattended start — accept it once interactively
  (attach with `tmux attach -t companion`).
