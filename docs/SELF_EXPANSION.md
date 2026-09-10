# Self-expansion — growing past the two default agents

The engine ships with exactly **two** agents: the **companion** (your interface) and
the **curator** (memory maintenance). That is deliberate. Two persistent sessions are
enough for most use, and every extra always-on agent is another context window to feed
and another thing to reason about. The design is: *start at two, add an agent only when
a real, recurring need shows up.*

This doc is the playbook for when it does.

## When to add an agent (and when not to)

Add a dedicated agent when a body of work is:

- **recurring** — you keep handing the companion the same kind of task, and
- **specialised** — it has its own context, tools, or cadence, and
- **worth isolating** — mixing it into the companion thread muddies both.

Good candidates: a build/monitor loop for one project, a research/watch agent, a
domain expert you consult repeatedly.

Do **not** spin up an agent for a one-off job. For that, the companion should use a
**Task subagent** — it runs in its own context window, reports back, and disappears.
Persistent agents are for durable, ongoing roles.

## How the pieces fit

- **`registry.json`** (runtime, at `~/.tpmem/agent-os/registry.json`) is the roster.
  An entry with `dispatcher_managed: true` is driven by the dispatcher.
- **`bin/spawn-agent <slug>`** launches a persistent tmux Claude session from a seed
  and writes/updates that agent's registry entry (session, session_id, restart
  command).
- **The dispatcher** wakes any managed agent when its inbox has pending rows, and
  respawns it from `restart.spawn` if the session has died with work waiting.
- **`agents/<slug>/seed.md`** is the agent's identity + boot procedure.

## Recipe: add an agent

1. **Write its seed.** Create `agents/<slug>/seed.md`. Copy the companion or curator
   seed as a starting point and rewrite: its role, what to read on boot, its channel,
   and its rules. Keep the dispatcher-driven tick loop (`agent-msg inbox` → handle →
   `companion-respond` / `agent-msg done` → stop).

2. **Give it an entity to log against** (optional but tidy):
   ```bash
   sqlite3 ~/.tpmem/kb.db "INSERT OR IGNORE INTO entities (type, slug, name, summary)
     VALUES ('agent', '<slug>', '<Name>', '<one-line role>');"
   ```

3. **Spawn it.** This registers it and brings it online:
   ```bash
   bin/spawn-agent <slug>                     # inherits the default model
   bin/spawn-agent <slug> --model <model-id>  # pin a specific model (persists across respawns)
   ```

4. **Talk to it.** A new tab appears in the gateway webapp automatically (the roster is
   polled). Messages you send in its channel become inbox rows; the dispatcher wakes it.

5. **Give it scheduled work** (optional) — a cron line calling `queue-job`, exactly
   like the curator's daily curation:
   ```bash
   0 * * * * TPMEM_DB=$HOME/.tpmem/kb.db /path/to/bin/queue-job <slug> "<instruction>"
   ```

## Removing an agent

```bash
tmux kill-session -t <slug>
# then delete its entry from ~/.tpmem/agent-os/registry.json
```

Once it's out of the registry the dispatcher ignores it and its tab disappears from the
webapp. Its channel history and KB notes are left intact.

## Keeping it honest

The engine has no fleet-size assumptions baked in — the dispatcher iterates whatever is
in the registry. But more agents means more autonomous Claude Code sessions running
unattended under `bypassPermissions`. Grow deliberately, and keep the seeds' "confirm
anything consequential" rule in every one you write.
