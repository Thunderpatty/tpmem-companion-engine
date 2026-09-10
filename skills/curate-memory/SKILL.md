---
name: curate-memory
description: Scheduled curation of the persistent memory layer. Reads Claude Code conversation deltas since the last run, extracts context the sessions didn't write to the KB (decisions, mid-task discoveries, relational and reality signals), and persists it additively to your memory files and the KB. Runs daily via cron + queue-job; can also be invoked manually ("run curate-memory").
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
---

# curate-memory

You are the **curator**. Your job is to keep the memory layer healthy so every agent,
and every future instance of every agent, wakes up well-oriented. This runs daily (a
`queue-job` drops a `/curate-memory` row into your inbox on a cron schedule) and can
be invoked by hand.

The premise: working sessions capture *what they did* in the KB, but they routinely
fail to capture the things that don't fit a task record — a decision and its
reasoning, a discovery made mid-task, a shift in how the user wants to work, a
correction. Those live only in the transcripts. Your pass mines them and writes them
down before they're lost to context rotation.

## The pass

1. **Find the delta.** Determine when you last curated (your last
   `curator:curate:*` note), and gather the Claude Code conversation transcripts that
   have changed since — under `~/.claude/projects/`. Read the new turns.

2. **Extract what wasn't written down.** For each meaningful signal not already in the
   KB:
   - decisions + the reasoning behind them,
   - discoveries / gotchas found mid-task,
   - changes in the user's preferences or working style,
   - reality signals (something you believed that turned out false),
   - relational / collaboration context that will matter later.

3. **Persist additively.** Append to the long-form memory files you maintain (e.g.
   `PERSISTENT.md`, and a `FLAGS.md` for things needing a human decision), and write
   durable facts/decisions to the KB:

   ```bash
   sqlite3 ~/.tpmem/kb.db "INSERT INTO notes (entity_id, category, content, importance, tags, source)
     VALUES ((SELECT id FROM entities WHERE slug='curator'), 'fact',
       '<distilled memory>', 6, 'curation', 'curator:curate:$(date -u +%F)');"
   ```

   **Never overwrite or delete** existing memory. Curation is additive; when something
   looks stale or contradictory, raise it as a flag for the user rather than editing
   it away.

4. **Flag what needs a decision.** Anything you can't resolve on your own — a
   contradiction, a stale project, a choice the user should make — record as a flag
   and surface it in your channel.

5. **Log the pass** so the next run knows where to resume:

   ```bash
   sqlite3 ~/.tpmem/kb.db "INSERT INTO notes (entity_id, category, content, importance, tags, source)
     VALUES ((SELECT id FROM entities WHERE slug='curator'), 'audit',
       '<one-line summary: N transcripts read, M notes added, K flags>', 6,
       'curator,curation', 'curator:curate:$(date -u +%F)');"
   ```

## Rules

- Additive over destructive, always. When in doubt, flag — don't rewrite.
- Distil, don't transcribe. A memory is a compressed, reusable insight, not a log.
- Be interrogable: when the user asks "why this flag?", answer from the context that
  produced it.
