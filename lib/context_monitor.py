"""context_monitor — the token-usage watchdog behind graceful rotation.

For a dispatcher-managed agent, resolve its live context-window usage and, when it
crosses the agent's `rotate_threshold`, inject a ROTATE instruction into its tmux
session so it self-wraps BEFORE the harness would auto-compact (which would lose the
running thread). This is the trigger end of the continuity/wrap system:

    context-monitor  →  (agent runs) agent-wrap  →  rotate-agent  →  dispatcher respawn
                                                                     →  successor primes
                                                                        from NEXT.md

It is intentionally free of scheduler concerns: it exposes `check()` for one agent,
which both `bin/context-monitor` (the standalone CLI / --status view) and the
dispatcher loop call. That is why there is NO separate cron or watcher for it —
rotation is triggered by *token count*, evaluated on the dispatcher's existing cycle,
never by time. (See daemon/dispatcherd.py, which calls check() each cycle.)

Rules that make it safe:
  - BUSY-AWARE: never inject mid-turn. The caller passes the agent's idle state (the
    dispatcher's ground-truth transcript read); the standalone CLI falls back to the
    hook-state flag. Over-threshold-but-busy holds until the next cycle.
  - DEDUPED + RE-ARMING: fire ROTATE once per crossing (state file), and re-arm when
    usage drops below threshold again (i.e. after the successor boots fresh).
  - RENUDGE: if an agent was told to rotate but keeps working (+RENUDGE_GROWTH more
    tokens), inject one second notice, so an ignored ROTATE can't silently ride into
    the context-full wedge.

Env (all optional; sensible defaults):
  AGENT_STATE_DIR   default ~/.tpmem/agent-state   (where the state/cache files live)
  CC_CONTEXT_WINDOW passed through cc_session.WINDOW
"""
from __future__ import annotations
import json, os, time
from pathlib import Path

import cc_session as cc

# The engine install dir is two levels up from this file (lib/ -> engine root); its
# bin/ holds agent-wrap and rotate-agent, which the ROTATE message tells the agent to
# run. Resolving from __file__ means the dispatcher doesn't need COMPANION_ENGINE_DIR
# in its environment.
ENGINE = Path(os.environ.get("COMPANION_ENGINE_DIR",
                             Path(__file__).resolve().parent.parent))
BIN = ENGINE / "bin"
STATE_DIR = Path(os.environ.get("AGENT_STATE_DIR", Path.home() / ".tpmem/agent-state"))
STATE_FILE = STATE_DIR / "_context_monitor.json"

STALE_BUSY = 1800        # mirror the dispatcher: a 'busy' stamp older than this = crashed
RENUDGE_GROWTH = 50_000  # told to rotate but grew this much more without doing it -> renudge

ROTATE_MSG = (
    "[companion-engine] You have crossed your context rotate threshold "
    "({ctx:,} / {thr:,} tokens). ROTATE NOW, while you still have full context: "
    "(1) author your handoff with `{wrap}` (fill the wrap schema honestly — especially "
    "`open` with the EXACT stopping point and `insights` you can't rederive from the "
    "transcript); (2) once agent-wrap reports the KB readback verified, run "
    "`{rotate}` to hand off to your successor. Do this before any other work — do NOT "
    "let the harness auto-compact."
)
RENUDGE_MSG = (
    "[companion-engine] SECOND NOTICE: you are at {ctx:,} tokens and still climbing — "
    "you were told to rotate at {fired_ctx:,} and have not. STOP current work, run "
    "`{wrap}` then `{rotate}` NOW, before you wedge at context-full and lose the "
    "ability to self-wrap."
)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(s: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(s))
    except OSError:
        pass


def agent_busy(slug: str) -> bool:
    """Fallback busy test for the standalone CLI: the hook-state 'busy' flag when it
    isn't stale. Missing/unreadable/stale reads as idle (crash safety), matching the
    dispatcher's policy. The dispatcher itself passes its richer transcript-based idle
    state into check(), so this is only used when check() is called with idle=None."""
    p = STATE_DIR / f"{slug}.json"
    try:
        d = json.loads(p.read_text())
    except Exception:
        return False
    return d.get("state") == "busy" and (time.time() - d.get("ts", 0)) <= STALE_BUSY


def resolve_context(slug: str, cfg: dict):
    """(ctx_tokens, jsonl_path) for the agent, or None if not resolvable.

    Prefer the registered session_id (deterministic transcript path — unambiguous even
    when agents share a project dir); fall back to resolving via the live tmux session.
    """
    sid = cfg.get("session_id")
    if sid:
        res = cc.context_from_jsonl(cc.project_dir_for_home() / f"{sid}.jsonl")
        if res is not None:
            return res
    return cc.session_context(cfg.get("session", slug))


def check(slug: str, cfg: dict, idle: bool | None = None,
          status_only: bool = False, dry_run: bool = False,
          log=print) -> bool:
    """Evaluate one agent's context usage and actuate rotation if warranted.

    idle: True/False from the caller's own state read; None -> use agent_busy() here.
    Returns True iff a ROTATE/renudge message was injected (caller should then skip the
    agent's normal tick this cycle — it's now busy handling the rotation).
    """
    if not cfg.get("dispatcher_managed"):
        return False
    session = cfg.get("session", slug)
    thr = int(cfg.get("rotate_threshold", 840_000))

    res = resolve_context(slug, cfg)
    if res is None:
        return False
    ctx, jsonl = res
    pct = ctx / cc.WINDOW * 100

    # Cache for the gateway to display. Separate file so it never races the busy/idle
    # hook that owns <slug>.json.
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / f"{slug}.ctx.json").write_text(json.dumps(
            {"ctx": ctx, "pct": round(pct, 1), "thr": thr, "ts": time.time()}))
    except Exception:
        pass

    if status_only:
        log(f"{slug}: {ctx:,} tokens ({pct:.1f}% of {cc.WINDOW:,}) | thr {thr:,}")
        return False

    state = load_state()
    st = state.setdefault(slug, {})

    if ctx < thr:
        if st.get("fired"):
            log(f"{slug}: context dropped below threshold ({ctx:,} < {thr:,}) — re-armed")
            state[slug] = {"fired": False}
            save_state(state)
        return False

    # Over threshold — but NEVER inject mid-turn (that is the wedge class the busy guard
    # exists to prevent). Hold until idle; retry next cycle.
    busy = (not idle) if idle is not None else agent_busy(slug)
    if busy:
        log(f"{slug}: over threshold ({ctx:,}) but BUSY — holding ROTATE until idle")
        return False

    wrap = f"{BIN}/agent-wrap"
    rotate = f"{BIN}/rotate-agent {slug}"

    if not st.get("fired"):
        if dry_run:
            log(f"{slug}: DRY-RUN would inject ROTATE at {ctx:,} (thr {thr:,})")
            return False
        cc.tmux_inject(session, ROTATE_MSG.format(ctx=ctx, thr=thr, wrap=wrap, rotate=rotate))
        st.update({"fired": True, "fired_ctx": ctx})
        save_state(state)
        log(f"{slug}: INJECTED ROTATE at {ctx:,} ({pct:.0f}% of {cc.WINDOW:,})")
        return True

    if ctx - st.get("fired_ctx", ctx) >= RENUDGE_GROWTH and not st.get("renudged"):
        if dry_run:
            log(f"{slug}: DRY-RUN would RENUDGE at {ctx:,} (fired at {st.get('fired_ctx'):,})")
            return False
        cc.tmux_inject(session, RENUDGE_MSG.format(
            ctx=ctx, fired_ctx=st.get("fired_ctx", 0), wrap=wrap, rotate=rotate))
        st["renudged"] = True
        save_state(state)
        log(f"{slug}: RENUDGED at {ctx:,} — ROTATE was ignored")
        return True

    log(f"{slug}: already told to rotate this crossing ({ctx:,}) — waiting on it")
    return False
