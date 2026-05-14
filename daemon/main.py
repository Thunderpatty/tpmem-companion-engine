#!/usr/bin/env python3
"""tpmem-daemon — async transport router for Claude Code agents.

Listens on transports (Telegram, IMAP email), routes incoming events to
per-agent profiles, and either:
  - spawns a fresh `claude -p` for that agent (with a KB-loaded preamble), or
  - hands the row off to a long-lived "companion" session that polls the inbox.

Also drains an outbox (replies queued by the companion) and a wakeup_queue
(self-scheduled future agent runs). Every wake is logged to the KB.

Run in foreground (development):
    python3 daemon/main.py
Run via systemd:
    systemctl --user start tpmem-daemon
"""
from __future__ import annotations
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
from pathlib import Path

import yaml

# --- paths -------------------------------------------------------------------
HOME       = Path.home()
DAEMON_DIR = Path(__file__).resolve().parent
CONFIG_DIR = HOME / ".config" / "tpmem-daemon"
DB_PATH    = Path(os.getenv("TPMEM_DB", str(HOME / ".tpmem" / "kb.db")))

sys.path.insert(0, str(DAEMON_DIR))
import spawner                                  # noqa: E402
from transports.telegram import TelegramTransport  # noqa: E402
from transports.email import EmailTransport, load_email_credentials  # noqa: E402

# --- config / secrets --------------------------------------------------------
def expand(p):
    return Path(os.path.expanduser(p))

def load_config():
    with open(CONFIG_DIR / "config.yaml") as f:
        return yaml.safe_load(f)

def load_secrets():
    """Load key=value pairs from ~/.config/tpmem-daemon/secrets.env."""
    out = {}
    env_file = CONFIG_DIR / "secrets.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out

# --- logging -----------------------------------------------------------------
def setup_logging(config):
    log_path = expand(config.get("logging", {}).get("path", str(HOME / ".tpmem" / "daemon.log")))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    level_name = config.get("logging", {}).get("level", "INFO")
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler()],
    )

# --- routing -----------------------------------------------------------------
def match_route(transport_name, payload, config):
    for rule in config.get("routing", []):
        if rule.get("transport") != transport_name:
            continue
        if re.search(rule["match"], payload):
            return rule["agent"]
    return None

# --- rate limit --------------------------------------------------------------
def exceeds_rate_limit(db, agent_slug, config):
    rl = config.get("rate_limits", {})
    cap = rl.get("per_agent", {}).get(agent_slug, rl.get("default", 20))
    n = db.execute("""
        SELECT COUNT(*) FROM notes
        WHERE source LIKE ?
          AND created_at > datetime('now','-1 hour')
    """, (f"daemon-relay:{agent_slug}:%",)).fetchone()[0]
    return n >= cap, n, cap

# --- KB audit logging --------------------------------------------------------
def log_wake(db, agent, prompt, outcome, source_ext):
    """Append an audit note. Requires an entity with slug='daemon-relay' (see schema.sql)."""
    db.execute("""
        INSERT INTO notes (entity_id, category, content, importance, tags, source)
        VALUES (
            (SELECT id FROM entities WHERE slug='daemon-relay'),
            'audit', ?, 5, 'daemon,wake', ?
        )
    """, (
        f"agent={agent} outcome={outcome} prompt={prompt[:300]}",
        f"daemon-relay:{agent}:{source_ext}",
    ))
    db.commit()

# --- inbox processing --------------------------------------------------------
def process_inbox(transport, config, stop):
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA busy_timeout = 5000")
    # Only process unrouted rows. Companion-routed rows stay pending until the
    # companion session picks them up (it filters routed_agent='companion').
    rows = db.execute("""
        SELECT id, transport, user_id, reply_to, payload
        FROM inbox
        WHERE status='pending' AND routed_agent IS NULL
        ORDER BY received_at LIMIT 5
    """).fetchall()

    for inbox_id, tname, user_id, reply_to, payload in rows:
        if stop.is_set():
            break
        agent = match_route(tname, payload, config)
        if not agent:
            db.execute(
                "UPDATE inbox SET status='refused', result=?, processed_at=CURRENT_TIMESTAMP WHERE id=?",
                ("no matching route", inbox_id))
            db.commit()
            logging.info(f"inbox {inbox_id}: no route for transport={tname}")
            continue

        # Companion route: claim the row (set routed_agent) but leave status='pending'.
        # The companion's tick loop reads it and writes a reply via the outbox.
        # Daemon does NOT spawn, does NOT ack — companion handles its own response feel.
        if agent == "companion":
            db.execute(
                "UPDATE inbox SET routed_agent='companion' WHERE id=?", (inbox_id,))
            db.commit()
            logging.info(f"inbox {inbox_id} → companion (handed off, daemon will not spawn)")
            continue

        # Rate check
        over, n, cap = exceeds_rate_limit(db, agent, config)
        if over:
            db.execute(
                "UPDATE inbox SET status='refused', result=?, routed_agent=?, processed_at=CURRENT_TIMESTAMP WHERE id=?",
                (f"rate limit ({n}/{cap}/hr)", agent, inbox_id))
            db.commit()
            log_wake(db, agent, payload, f"refused: rate limit ({n}/{cap}/hr)", f"inbox:{inbox_id}")
            try:
                transport.send(reply_to, f"[daemon] rate limit hit for {agent} ({n}/{cap}/hr). Try later.")
            except Exception as e:
                logging.exception(f"failed to send rate-limit reply: {e}")
            continue

        db.execute("UPDATE inbox SET status='processing', routed_agent=? WHERE id=?", (agent, inbox_id))
        db.commit()

        # Ack — user gets immediate feedback that the daemon picked up the message.
        # claude -p can take 30-180s; without this the chat looks dead.
        ack = f"({agent}) working on it…"
        try:
            transport.send_chat_action(reply_to, "typing")
            transport.send(reply_to, ack)
        except Exception as e:
            logging.warning(f"failed to send ack for inbox {inbox_id}: {e}")

        logging.info(f"inbox {inbox_id} → spawning agent={agent}")
        rc, out, err = spawner.invoke(agent, payload, config)
        outcome = "ok" if rc == 0 else f"failed rc={rc}"
        body = (out or "").strip() or config.get("reply", {}).get("empty_response_text", "[daemon] (empty)")
        if rc != 0:
            body = f"[daemon] agent {agent} failed (rc={rc}): {err.strip()[:300] or '(no stderr)'}"

        try:
            transport.send(reply_to, body)
            sent = "sent"
        except Exception as e:
            logging.exception(f"failed to send reply: {e}")
            sent = f"send-failed: {e}"

        db.execute(
            "UPDATE inbox SET status='done', result=?, processed_at=CURRENT_TIMESTAMP WHERE id=?",
            (f"{outcome} | {sent}", inbox_id))
        db.commit()
        log_wake(db, agent, payload, f"{outcome} | {sent}", f"inbox:{inbox_id}")

    db.close()

# --- outbox processing -------------------------------------------------------
def process_outbox(transport, stop):
    """Drain pending outbox rows — send them via transport, mark sent/failed.

    The companion (and any future in-session agent) writes here via the
    companion-respond helper. The daemon owns transport plumbing — agents
    never send directly.
    """
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA busy_timeout = 5000")
    rows = db.execute("""
        SELECT id, transport, reply_to, payload FROM outbox
        WHERE status='pending' ORDER BY created_at LIMIT 10
    """).fetchall()
    for oid, tname, reply_to, payload in rows:
        if stop.is_set():
            break
        if tname != "telegram":
            # Future transports (webhook, email-send, sip) need their own send paths.
            db.execute(
                "UPDATE outbox SET status='failed', error=?, sent_at=CURRENT_TIMESTAMP WHERE id=?",
                (f"unsupported outbound transport: {tname}", oid))
            db.commit()
            continue
        try:
            transport.send(reply_to, payload)
            db.execute(
                "UPDATE outbox SET status='sent', sent_at=CURRENT_TIMESTAMP WHERE id=?", (oid,))
            db.commit()
            logging.info(f"outbox {oid} → telegram chat={reply_to} ({len(payload)} chars)")
        except Exception as e:
            db.execute(
                "UPDATE outbox SET status='failed', error=?, sent_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(e)[:300], oid))
            db.commit()
            logging.warning(f"outbox {oid}: send failed: {e}")
    db.close()


def process_wakeups(config, stop):
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA busy_timeout = 5000")
    rows = db.execute("""
        SELECT id, agent_slug, prompt, task_slug
        FROM wakeup_queue
        WHERE fired_at IS NULL AND fire_at <= datetime('now')
        ORDER BY fire_at LIMIT 5
    """).fetchall()
    for wid, agent, prompt, task_slug in rows:
        if stop.is_set():
            break
        over, n, cap = exceeds_rate_limit(db, agent, config)
        if over:
            db.execute(
                "UPDATE wakeup_queue SET fired_at=CURRENT_TIMESTAMP, result=? WHERE id=?",
                (f"refused: rate limit ({n}/{cap}/hr)", wid))
            db.commit()
            log_wake(db, agent, prompt, f"refused: rate limit", f"wakeup:{wid}")
            continue
        logging.info(f"wakeup {wid} → spawning agent={agent} task={task_slug}")
        rc, out, err = spawner.invoke(agent, prompt, config)
        outcome = "ok" if rc == 0 else f"failed rc={rc}"
        db.execute(
            "UPDATE wakeup_queue SET fired_at=CURRENT_TIMESTAMP, result=? WHERE id=?",
            (outcome, wid))
        db.commit()
        log_wake(db, agent, f"[wakeup id={wid} task={task_slug}] {prompt}", outcome, f"wakeup:{wid}")
    db.close()

# --- healthcheck -------------------------------------------------------------
def touch_healthcheck(config):
    p = expand(config.get("healthcheck", {}).get("path", str(DAEMON_DIR / "healthcheck.touch")))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


# --- companion restart flag --------------------------------------------------
COMPANION_RESTART_FLAG = HOME / ".tpmem" / "daemon" / "companion-restart.flag"


def check_companion_restart():
    """If the companion's /wrap routine touched the flag, restart its systemd unit.

    This lets a long-lived companion session deliberately recycle itself for a
    clean context window — see skills/companion/SKILL.md (the /wrap routine).
    """
    if not COMPANION_RESTART_FLAG.exists():
        return
    try:
        # Delete flag BEFORE restart so a slow restart can't loop us
        COMPANION_RESTART_FLAG.unlink()
    except Exception as e:
        logging.warning(f"companion-restart: could not delete flag: {e}")
        return

    logging.info("companion-restart: flag detected, running systemctl --user restart tpmem-companion")
    import subprocess as sp
    try:
        r = sp.run(["systemctl", "--user", "restart", "tpmem-companion"],
                   check=False, timeout=30, capture_output=True, text=True)
        outcome = "ok" if r.returncode == 0 else f"rc={r.returncode} stderr={r.stderr.strip()[:200]}"
    except sp.TimeoutExpired:
        outcome = "timeout"
    except Exception as e:
        outcome = f"error: {e}"
        logging.exception(f"companion-restart: systemctl call failed: {e}")
    logging.info(f"companion-restart: {outcome}")

# --- threads -----------------------------------------------------------------
def telegram_poller(transport, stop):
    while not stop.is_set():
        try:
            msgs = transport.poll_once(timeout=25)
            if msgs:
                logging.info(f"telegram: {len(msgs)} new message(s)")
                db = sqlite3.connect(DB_PATH)
                db.execute("PRAGMA busy_timeout = 5000")
                for m in msgs:
                    try:
                        db.execute("""
                            INSERT INTO inbox (transport, external_id, user_id, reply_to, payload)
                            VALUES (?, ?, ?, ?, ?)
                        """, ("telegram", m["external_id"], m["user_id"], m["chat_id"], m["text"]))
                        db.commit()
                    except sqlite3.IntegrityError:
                        # Duplicate update_id (already in inbox). Skip.
                        pass
                db.close()
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError) as e:
            # Transient network blips on long-poll. One short line, no traceback.
            logging.warning(f"telegram poll: transient network ({type(e).__name__}): {e}")
            for _ in range(5):
                if stop.is_set(): break
                time.sleep(1)
        except Exception as e:
            logging.exception(f"telegram poll: unexpected error: {e}")
            for _ in range(5):
                if stop.is_set(): break
                time.sleep(1)

def email_poller(email_transport, stop, interval=60):
    """Poll IMAP for new mail every `interval` seconds, write to inbox."""
    while not stop.is_set():
        try:
            msgs = email_transport.fetch_new()
            if msgs:
                logging.info(f"email: {len(msgs)} new message(s)")
                db = sqlite3.connect(DB_PATH)
                db.execute("PRAGMA busy_timeout = 5000")
                for m in msgs:
                    try:
                        db.execute("""
                            INSERT INTO inbox (transport, external_id, user_id, reply_to, payload)
                            VALUES (?, ?, ?, ?, ?)
                        """, ("email", m["external_id"], m["user_id"], m["reply_to"], m["text"]))
                        db.commit()
                    except sqlite3.IntegrityError:
                        pass
                db.close()
        except Exception as e:
            logging.warning(f"email poll: {type(e).__name__}: {e}")
        for _ in range(interval):
            if stop.is_set(): break
            time.sleep(1)


def worker(transport, config, stop):
    interval = config.get("healthcheck", {}).get("interval_seconds", 10)
    while not stop.is_set():
        try:
            process_inbox(transport, config, stop)
            process_outbox(transport, stop)
            process_wakeups(config, stop)
            check_companion_restart()
            touch_healthcheck(config)
        except Exception as e:
            logging.exception(f"worker error: {e}")
        # Sleep in small slices so we wake quickly on stop
        for _ in range(interval):
            if stop.is_set():
                break
            time.sleep(1)

# --- entry -------------------------------------------------------------------
def main():
    config = load_config()
    secrets = load_secrets()
    setup_logging(config)
    logging.info("tpmem-daemon starting")

    if "TELEGRAM_BOT_TOKEN" not in secrets:
        logging.error("TELEGRAM_BOT_TOKEN not found in ~/.config/tpmem-daemon/secrets.env. Aborting.")
        return 2

    allowed = set(int(x) for x in config.get("auth", {}).get("telegram_user_ids", []))
    if not allowed:
        logging.error("auth.telegram_user_ids is empty in config.yaml. "
                      "Aborting (refusing to listen with no allowlist).")
        return 2

    transport = TelegramTransport(
        token=secrets["TELEGRAM_BOT_TOKEN"],
        allowed_user_ids=allowed,
        chunk_chars=config.get("reply", {}).get("telegram_chunk_chars", 3800),
    )
    transport.discard_backlog()
    logging.info(f"telegram: ready, allowed user_ids={sorted(allowed)}")

    stop = threading.Event()
    def shutdown(signum, frame):
        logging.info(f"signal {signum} — shutting down")
        stop.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    t1 = threading.Thread(target=telegram_poller, args=(transport, stop), name="tg-poll", daemon=True)
    t2 = threading.Thread(target=worker, args=(transport, config, stop), name="worker", daemon=True)
    t1.start()
    t2.start()

    # Email transport (optional — only starts if configured + creds resolvable)
    email_cfg = config.get("email", {})
    if email_cfg.get("enabled", False):
        creds = load_email_credentials()
        if not creds:
            logging.warning("email: enabled in config but no credentials found "
                            "(checked ~/.config/tpmem-daemon/secrets.env). Skipping.")
        else:
            user, pw, host, port = creds
            allowlist = email_cfg.get("sender_allowlist", [])
            interval = int(email_cfg.get("poll_interval_seconds", 60))
            et = EmailTransport(user, pw, host, port,
                                sender_allowlist=allowlist,
                                max_body_chars=int(email_cfg.get("max_body_chars", 20000)))
            t3 = threading.Thread(target=email_poller, args=(et, stop, interval),
                                  name="email-poll", daemon=True)
            t3.start()
            logging.info(f"email: ready, user={user}, allowlist={allowlist}, poll={interval}s")

    while not stop.is_set():
        time.sleep(1)

    logging.info("waiting for threads to finish")
    t1.join(timeout=30)
    t2.join(timeout=30)
    logging.info("tpmem-daemon stopped")
    return 0

if __name__ == "__main__":
    sys.exit(main())
