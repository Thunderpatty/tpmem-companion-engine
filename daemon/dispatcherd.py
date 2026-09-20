#!/usr/bin/env python3
"""dispatcherd — the state-aware delivery dispatcher.

Owns the decision of WHEN to wake a persistent agent, so agents never poll an empty
queue and bursts never pile up mid-turn. This replaces per-message `claude -p` spawns
and fragile self-scheduling: agents sit idle in tmux and are woken by an injected
"tick" only when there is work for them.

Per cycle (default 10s), for each registered `dispatcher_managed` agent:
  - skip if paused (a `<slug>.paused` control flag exists)
  - ROTATION, part 1: consume any `<slug>.rotate` marker (dropped by rotate-agent) —
    kill the tmux session and respawn a fresh successor that primes from NEXT.md. Only
    with a durable wrap present; honors a respawn cooldown.
  - ROTATION, part 2: run the context-monitor token check (lib/context_monitor.py) on
    this same loop — no separate cron/watcher. If the agent has crossed its
    rotate_threshold and is idle, inject a ROTATE instruction so it self-wraps before
    the harness would auto-compact.
  - determine busy/idle from the agent's transcript tail (ground truth), falling back
    to the hook-state flag when the transcript can't be read
  - count pending inbox rows (routed_agent=slug, status='pending'); note criticals
  - NEVER inject while the agent is busy (mid-turn injection is the pile-up cause)
  - when idle + pending: inject ONE batched wake (coalescing the whole burst)
  - if the tmux session is dead, respawn it from the registry's restart command
    (the fresh agent drains its own inbox on boot)

Watermark + re-nudge avoid spam: re-wake only on new rows, a flush flag, or if pending
persists past RENUDGE seconds (recovery from a missed wake).

Env (for isolation/testing):
  TPMEM_DB, AGENT_STATE_DIR, AGENT_REGISTRY, DISPATCH_CYCLE, DISPATCH_RENUDGE
Flags:
  --once     run a single cycle and exit (testing)
  --dry-run  decide + log but do not inject
"""
from __future__ import annotations
import json, os, sqlite3, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import cc_session as cc  # noqa: E402
import context_monitor as ctxmon  # noqa: E402  (the token watchdog — run on this loop)

DB = Path(os.environ.get("TPMEM_DB", Path.home() / ".tpmem/kb.db"))
STATE_DIR = Path(os.environ.get("AGENT_STATE_DIR", Path.home() / ".tpmem/agent-state"))
REGISTRY = Path(os.environ.get("AGENT_REGISTRY", Path.home() / ".tpmem/agent-os/registry.json"))
CYCLE = float(os.environ.get("DISPATCH_CYCLE", "10"))
RENUDGE = float(os.environ.get("DISPATCH_RENUDGE", "120"))
STALE_BUSY = 1800  # a 'busy' flag older than this = crashed mid-turn; safe to treat as idle.

DEFAULT_WAKE = ("tick: {n} message(s) pending ({crit} critical) — drain your inbox and "
                "handle everything now as one batch. The dispatcher wakes you when "
                "there's more; do NOT self-schedule a wakeup.")


def log(msg: str) -> None:
    print(f"[dispatcherd {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def registry() -> dict:
    try:
        return json.loads(REGISTRY.read_text()).get("agents", {})
    except Exception as e:
        log(f"registry read error ({REGISTRY}): {e}")
        return {}


def disp_state_path() -> Path:
    return STATE_DIR / "_dispatcher.json"


def load_disp_state() -> dict:
    p = disp_state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def save_disp_state(s: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        disp_state_path().write_text(json.dumps(s))
    except OSError:
        pass


_LOCAL_CMD_TAGS = ("<local-command-", "<command-name>", "<command-message>",
                   "<command-args>", "<command-stdout>", "<command-contents>")


def _is_local_command(content) -> bool:
    """A user-role transcript entry that is actually a local slash-command artifact
    (the /model, /config, /clear … caveat, invocation, and stdout), not a real prompt."""
    if isinstance(content, str):
        return any(tag in content for tag in _LOCAL_CMD_TAGS)
    if isinstance(content, list):
        return any(isinstance(b, dict) and isinstance(b.get("text"), str)
                   and any(tag in b["text"] for tag in _LOCAL_CMD_TAGS) for b in content)
    return False


def transcript_state(slug: str, cfg: dict):
    """Ground-truth wakeability from the agent's transcript tail. This is authoritative
    over the hook flag (which can stick 'busy' on a missed Stop hook, or false-stamp busy
    from background procs running under the agent's slug while the chat sits idle).

    Returns True iff the last meaningful entry is an assistant turn that ended
    (stop_reason 'end_turn') → genuinely idle/wakeable. False if mid-flight (tool_use /
    still generating) or a pending user prompt/tool_result → keep holding. None if
    unreadable → caller falls back to the hook flag. Skips truncated/partial JSON lines.
    """
    try:
        sid = cfg.get("session_id")
        if not sid:
            return None
        jf = cc.project_dir_for_home() / f"{sid}.jsonl"
        with open(jf, "rb") as f:
            f.seek(0, 2); size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", "replace")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue  # truncated/partial line mid-flush — skip
            t = d.get("type")
            if t == "system":
                continue  # hook/system noise — not a turn boundary
            if t == "assistant":
                return (d.get("message") or {}).get("stop_reason") == "end_turn"
            if t == "user":
                # Local slash-command artifacts (/model, /config, …) are written as
                # user-role entries but are NOT a prompt awaiting a response. Treat them
                # as noise and keep scanning back for the real last turn — otherwise an
                # idle session whose last action was a slash command reads as 'busy'
                # forever and the dispatcher never delivers its pending inbox.
                if d.get("isMeta") or _is_local_command((d.get("message") or {}).get("content")):
                    continue
                return False  # a real prompt or in-flight tool_result after the last turn → busy
        return None
    except Exception:
        return None


def hook_state(slug: str) -> str:
    p = STATE_DIR / f"{slug}.json"
    if not p.exists():
        return "idle"  # no hook yet → assume wakeable
    try:
        d = json.loads(p.read_text())
    except Exception:
        return "idle"
    if d.get("state") == "busy" and (time.time() - d.get("ts", 0)) > STALE_BUSY:
        return "idle"  # stale busy → crashed mid-turn; safe to wake
    return d.get("state", "idle")


def agent_state(slug: str, cfg: dict) -> str:
    """Prefer the transcript (ground truth); fall back to the hook flag when the
    transcript is unreadable (no session_id, no hooks wired, brand-new session)."""
    ts = transcript_state(slug, cfg)
    if ts is True:
        return "idle"
    if ts is False:
        return "busy"
    return hook_state(slug)


def pending(slug: str) -> tuple[int, int, int]:
    """(count, max_id, critical_count) of pending inbox rows for slug."""
    try:
        con = sqlite3.connect(str(DB), timeout=15)
        row = con.execute(
            """SELECT count(*), COALESCE(max(id),0),
                      COALESCE(sum(CASE WHEN priority='critical' THEN 1 ELSE 0 END),0)
               FROM inbox WHERE routed_agent=? AND status='pending'""", (slug,)).fetchone()
        con.close()
        return int(row[0]), int(row[1]), int(row[2])
    except sqlite3.Error as e:
        log(f"{slug}: inbox read error: {e}")
        return 0, 0, 0


def has_durable_wrap(slug: str) -> bool:
    """A rotation is only safe if a handoff actually exists. NEXT.md is what the
    successor primes from; the KB wrap row is the durable copy. Require BOTH. NEXT.md
    lives in the engine dir (resolved via context_monitor.ENGINE, same as the scripts)."""
    if not (ctxmon.ENGINE / "agents" / slug / "NEXT.md").exists():
        return False
    try:
        con = sqlite3.connect(str(DB), timeout=15)
        row = con.execute("SELECT 1 FROM notes WHERE source LIKE ? LIMIT 1",
                          (f"wrap:{slug}:%",)).fetchone()
        con.close()
        return row is not None
    except sqlite3.Error:
        return False


def handle_rotation(slug: str, cfg: dict, st: dict, dry: bool) -> bool:
    """Consume a `<slug>.rotate` marker (dropped by rotate-agent): kill the agent's tmux
    session and respawn it so a fresh successor primes from NEXT.md. This is the EXTERNAL
    half of rotation — an agent can't kill its own session and respawn (it dies mid-
    script), so rotate-agent drops a marker and we do it from out here.

    Returns True if a marker was present (caller skips the rest of this agent's cycle).
    Safety: NEVER kill without a durable wrap; honor a 60s respawn cooldown."""
    marker = STATE_DIR / f"{slug}.rotate"
    if not marker.exists():
        return False
    restart = cfg.get("restart", {})
    spawn = restart.get("spawn")
    session = cfg.get("session", slug)
    if restart.get("method") != "tmux" or not spawn:
        log(f"{slug}: .rotate marker but no tmux spawn configured — clearing, ignoring")
        if not dry:
            try: marker.unlink()
            except OSError: pass
        return True
    if not has_durable_wrap(slug):
        log(f"{slug}: .rotate marker but NO durable wrap (NEXT.md/KB missing) — "
            f"REFUSING to kill; leaving marker for visibility")
        return True
    now = time.time()
    if now - st.get("last_respawn", 0) <= 60:
        log(f"{slug}: rotation requested but respawn on cooldown — retry next cycle")
        return True
    log(f"{slug}: ROTATE — kill session '{session}' + respawn ({spawn})"
        + (" [dry-run]" if dry else ""))
    if not dry:
        try:
            subprocess.run(["tmux", "kill-session", "-t", session],
                           capture_output=True, timeout=15)
        except Exception as e:
            log(f"{slug}: kill-session failed: {e}")
        time.sleep(1)  # let tmux fully reap before spawn-agent's session_exists check
        try:
            subprocess.run(spawn.split(), capture_output=True, timeout=30)
        except Exception as e:
            log(f"{slug}: rotate respawn failed: {e}")
        st["last_respawn"] = now
        try: marker.unlink()
        except OSError: pass
    return True


def respawn(slug: str, cfg: dict, st: dict, now: float) -> None:
    """Session is dead — relaunch from the registry's restart command (the fresh agent
    drains its pending inbox on boot). A 60s cooldown guards against a spawn storm."""
    restart = cfg.get("restart", {})
    spawn = restart.get("spawn")
    if restart.get("method") != "tmux" or not spawn:
        log(f"{slug}: session dead and no tmux spawn configured — skipping")
        return
    if now - st.get("last_respawn", 0) <= 60:
        log(f"{slug}: session dead — respawn on cooldown, skipping")
        return
    log(f"{slug}: session DEAD — respawning ({spawn})")
    try:
        subprocess.run(spawn.split(), capture_output=True, timeout=30)
    except Exception as e:
        log(f"{slug}: respawn failed: {e}")
    st["last_respawn"] = now


def cycle(reg: dict, disp: dict, dry: bool) -> None:
    for slug, cfg in reg.items():
        if not cfg.get("dispatcher_managed"):
            continue
        session = cfg.get("session", slug)
        st = disp.setdefault(slug, {"last_id": 0, "last_ts": 0, "last_crit_id": 0})

        if (STATE_DIR / f"{slug}.paused").exists():
            log(f"{slug}: PAUSED — holding")
            continue

        # Rotation, part 1 — the external kill+respawn. If rotate-agent left a marker,
        # consume it (respawn a fresh successor) and skip the rest of this agent's cycle.
        if handle_rotation(slug, cfg, st, dry):
            continue

        # Rotation, part 2 — the token watchdog. Run context-monitor's check on this same
        # cycle (no separate cron/watcher): if the agent has crossed its rotate_threshold
        # and is idle, it injects a ROTATE instruction. This must run before the tick
        # logic (an over-threshold agent may have an empty inbox but still needs to hand
        # off) and before the cnt==0 early-out below.
        idle = cc.session_exists(session) and agent_state(slug, cfg) == "idle"
        try:
            if ctxmon.check(slug, cfg, idle=idle, dry_run=dry, log=log):
                continue  # injected ROTATE — the agent is now busy handling it
        except Exception as e:
            log(f"{slug}: context-check error: {e}")

        cnt, max_id, crit = pending(slug)
        flush = (STATE_DIR / f"{slug}.flush").exists()
        if cnt == 0 and not flush:
            continue

        state = agent_state(slug, cfg)
        session_live = cc.session_exists(session)
        # A 'busy' state with a LIVE session = genuinely mid-turn → hold (never inject
        # mid-turn). A 'busy' state with NO live session = a corpse (killed mid-turn,
        # never ran its Stop hook) → treat as dead and respawn immediately.
        corpse = (state == "busy") and not session_live
        if state == "busy" and session_live:
            log(f"{slug}: busy — holding {cnt} pending ({crit} crit)")
            continue

        now = time.time()
        new_normal = max_id > st["last_id"]
        new_crit = crit > 0 and max_id > st["last_crit_id"]
        stale = cnt > 0 and (now - st["last_ts"]) > RENUDGE
        if not (flush or new_crit or new_normal or stale or corpse):
            continue

        reason = ("flush" if flush else "critical" if new_crit
                  else "new" if new_normal else "corpse" if corpse else "renudge")

        wake = cfg.get("wake_prompt", DEFAULT_WAKE).format(n=cnt, crit=crit)
        # Cache-buster: append a unique timestamp so no two tick prompts are byte-identical.
        wake = f"{wake} [t={int(time.time())}]"
        if crit:
            wake = "[CRITICAL] " + wake
        log(f"{slug}: WAKE ({reason}) — {cnt} pending, {crit} crit → session '{session}'"
            + (" [dry-run]" if dry else ""))
        if not dry:
            if session_live and not corpse:
                try:
                    cc.tmux_inject(session, wake)
                except subprocess.TimeoutExpired:
                    # A wedged tmux send-keys is timeout-bounded in tmux_inject; catch it
                    # per-agent so one stuck session can't starve the rest. Don't advance
                    # the watermark → retry next cycle.
                    log(f"{slug}: inject wedged (tmux send-keys timed out) — retry next cycle")
                    continue
            else:
                respawn(slug, cfg, st, now)
        st["last_id"] = max_id
        st["last_crit_id"] = max(st["last_crit_id"], max_id if crit else st["last_crit_id"])
        st["last_ts"] = now
        if flush:
            try:
                (STATE_DIR / f"{slug}.flush").unlink()
            except OSError:
                pass


def main() -> int:
    once = "--once" in sys.argv
    dry = "--dry-run" in sys.argv
    log(f"start cycle={CYCLE}s renudge={RENUDGE}s db={DB} registry={REGISTRY} once={once} dry={dry}")
    while True:
        reg = registry()
        disp = load_disp_state()
        try:
            cycle(reg, disp, dry)
        except Exception as e:
            log(f"cycle error: {e}")
        save_disp_state(disp)
        if once:
            return 0
        time.sleep(CYCLE)


if __name__ == "__main__":
    sys.exit(main())
