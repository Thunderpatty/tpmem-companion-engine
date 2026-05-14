"""Spawner — invoke `claude -p` with an agent's skill + KB-loaded preamble."""
from __future__ import annotations
import logging
import os
import sqlite3
import subprocess
from pathlib import Path

DB_PATH = Path.home() / ".tpmem" / "kb.db"


def _expand(p):
    return Path(os.path.expanduser(p))


def build_preamble(agent_slug):
    """Pull recent state for this agent so the fresh `claude -p` knows where it left off."""
    db = sqlite3.connect(DB_PATH)
    parts = []

    # Open tasks under this agent
    rows = db.execute("""
        SELECT slug, name, status FROM entities
        WHERE type='task' AND status IN ('active','blocked','pending-review')
          AND id IN (SELECT DISTINCT entity_id FROM notes WHERE source LIKE ?)
        ORDER BY updated_at DESC LIMIT 5
    """, (f"{agent_slug}:%",)).fetchall()
    if rows:
        parts.append("Open tasks under your slug:\n" +
                     "\n".join(f"  - {r[0]} ({r[2]}): {r[1]}" for r in rows))

    # Pending wakeups for this agent
    rows = db.execute("""
        SELECT id, fire_at, task_slug, substr(prompt,1,80) FROM wakeup_queue
        WHERE agent_slug=? AND fired_at IS NULL
        ORDER BY fire_at LIMIT 5
    """, (agent_slug,)).fetchall()
    if rows:
        parts.append("Pending wakeups:\n" +
                     "\n".join(f"  - id={r[0]} fire_at={r[1]} task={r[2]} prompt={r[3]}"
                              for r in rows))

    # Recent audit notes from this agent (last 24h)
    rows = db.execute("""
        SELECT created_at, substr(content,1,100) FROM notes
        WHERE source LIKE ? AND category IN ('audit','status','decision')
          AND created_at > datetime('now','-24 hours')
        ORDER BY created_at DESC LIMIT 8
    """, (f"{agent_slug}:%",)).fetchall()
    if rows:
        parts.append("Your recent decisions/status (last 24h):\n" +
                     "\n".join(f"  - [{r[0]}] {r[1]}" for r in rows))

    db.close()
    return "\n\n".join(parts) if parts else "(no prior state for this agent)"


def invoke(agent_slug, prompt, config):
    """Run `claude -p` for the given agent with preamble + prompt. Returns (rc, stdout, stderr)."""
    cc = config.get("claude", {})
    binary = str(_expand(cc.get("binary", "~/.local/bin/claude")))
    timeout = int(cc.get("per_invocation_timeout_seconds", 300))

    preamble = build_preamble(agent_slug)
    full_prompt = (
        f"You are running as the {agent_slug} agent. Load the skill at "
        f"~/.claude/skills/{agent_slug}/SKILL.md and follow it.\n\n"
        f"=== prior state (from tpmem) ===\n{preamble}\n\n"
        f"=== incoming message ===\n{prompt}\n"
    )

    cmd = [binary, "-p", full_prompt]
    if cc.get("permission_mode"):
        cmd += ["--permission-mode", cc["permission_mode"]]
    if cc.get("no_session_persistence"):
        cmd += ["--no-session-persistence"]
    for d in cc.get("add_dirs", []) or []:
        cmd += ["--add-dir", str(_expand(d))]
    cmd += ["--output-format", "text"]

    env = {**os.environ}
    # Ensure claude binary's directory is in PATH for any subprocesses it spawns
    env["PATH"] = f"{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")

    logging.info(f"spawner: agent={agent_slug} timeout={timeout}s prompt_len={len(prompt)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"claude -p timeout after {timeout}s"
    except FileNotFoundError as e:
        return -2, "", f"claude binary not found: {e}"
    except Exception as e:
        logging.exception("spawner: unexpected error")
        return -3, "", f"spawner error: {e}"
