#!/usr/bin/env python3
"""transport_bridge — OPTIONAL external transport for the companion engine.

OFF by default. The default interface is the local gateway webapp; you only need this
if you also want to reach your agents from Telegram or email.

It does two things and nothing else:
  1. INTAKE — long-poll Telegram / IMAP email for allowlisted senders, and for each
     message write BOTH a `messages` row (so it shows in the gateway feed) AND an
     `inbox` row routed to the default agent (so the dispatcher wakes it).
  2. DELIVERY — drain `outbox` rows (queued by an agent via companion-respond) and
     send them back out over their transport.

It never spawns `claude -p` and never decides when to wake an agent — that is the
dispatcher's job. This process is pure I/O plumbing between an external transport and
the SQLite bus.

Config:  ~/.config/tpmem-daemon/config.yaml   (see config.example.yaml)
Secrets: ~/.config/tpmem-daemon/secrets.env
Run:     python3 daemon/transport_bridge.py      (or via the systemd unit, disabled by default)
"""
from __future__ import annotations
import logging, os, signal, socket, sqlite3, ssl, sys, threading, time, urllib.error
from pathlib import Path

import yaml

HOME = Path.home()
DAEMON_DIR = Path(__file__).resolve().parent
CONFIG_DIR = HOME / ".config" / "tpmem-daemon"
DB_PATH = Path(os.getenv("TPMEM_DB", str(HOME / ".tpmem" / "kb.db")))

sys.path.insert(0, str(DAEMON_DIR))
from transports.telegram import TelegramTransport  # noqa: E402
from transports.email import EmailTransport, load_email_credentials  # noqa: E402


def expand(p):
    return Path(os.path.expanduser(p))


def load_config():
    with open(CONFIG_DIR / "config.yaml") as f:
        return yaml.safe_load(f) or {}


def load_secrets():
    out = {}
    env_file = CONFIG_DIR / "secrets.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def setup_logging(config):
    log_path = expand(config.get("logging", {}).get("path", str(HOME / ".tpmem" / "transport-bridge.log")))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, config.get("logging", {}).get("level", "INFO"), logging.INFO)
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
                        handlers=[logging.FileHandler(log_path), logging.StreamHandler()])


def intake(transport_name, msg, default_agent):
    """Write one incoming message into the bus: a channel row (for the feed) + an inbox
    row (so the dispatcher wakes the routed agent)."""
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA busy_timeout = 5000")
    try:
        db.execute("INSERT OR IGNORE INTO inbox "
                   "(transport, external_id, user_id, reply_to, payload, routed_agent) "
                   "VALUES (?, ?, ?, ?, ?, ?)",
                   (transport_name, msg["external_id"], msg["user_id"],
                    msg.get("reply_to") or msg.get("chat_id"), msg["text"], default_agent))
        # Mirror into the agent's channel so the message is visible in the gateway feed.
        db.execute("INSERT INTO messages (channel, sender, body) VALUES (?, 'human', ?)",
                   (default_agent, msg["text"]))
        db.commit()
    except sqlite3.IntegrityError:
        pass  # duplicate external_id — already ingested
    finally:
        db.close()


def telegram_poller(transport, default_agent, stop):
    while not stop.is_set():
        try:
            msgs = transport.poll_once(timeout=25)
            for m in msgs or []:
                intake("telegram", m, default_agent)
            if msgs:
                logging.info(f"telegram: ingested {len(msgs)} message(s)")
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, ConnectionError) as e:
            logging.warning(f"telegram poll: transient network ({type(e).__name__}): {e}")
            _nap(stop, 5)
        except Exception as e:
            logging.exception(f"telegram poll: unexpected error: {e}")
            _nap(stop, 5)


def email_poller(et, default_agent, stop, interval=60):
    while not stop.is_set():
        try:
            msgs = et.fetch_new()
            for m in msgs or []:
                intake("email", m, default_agent)
            if msgs:
                logging.info(f"email: ingested {len(msgs)} message(s)")
        except Exception as e:
            logging.warning(f"email poll: {type(e).__name__}: {e}")
        _nap(stop, interval)


def deliver_outbox(senders, stop):
    """Drain pending outbox rows and send them over their transport."""
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA busy_timeout = 5000")
    rows = db.execute("SELECT id, transport, reply_to, payload FROM outbox "
                      "WHERE status='pending' ORDER BY created_at LIMIT 20").fetchall()
    for oid, tname, reply_to, payload in rows:
        if stop.is_set():
            break
        sender = senders.get(tname)
        if not sender:
            db.execute("UPDATE outbox SET status='failed', error=?, sent_at=CURRENT_TIMESTAMP WHERE id=?",
                       (f"no delivery path for transport {tname}", oid))
            db.commit()
            continue
        try:
            sender(reply_to, payload)
            db.execute("UPDATE outbox SET status='sent', sent_at=CURRENT_TIMESTAMP WHERE id=?", (oid,))
            db.commit()
            logging.info(f"outbox {oid} -> {tname} to={reply_to} ({len(payload)} chars)")
        except Exception as e:
            db.execute("UPDATE outbox SET status='failed', error=?, sent_at=CURRENT_TIMESTAMP WHERE id=?",
                       (str(e)[:300], oid))
            db.commit()
            logging.warning(f"outbox {oid}: send failed: {e}")
    db.close()


def _nap(stop, secs):
    for _ in range(int(secs)):
        if stop.is_set():
            break
        time.sleep(1)


def outbox_worker(senders, stop):
    while not stop.is_set():
        try:
            deliver_outbox(senders, stop)
        except Exception as e:
            logging.exception(f"outbox worker error: {e}")
        _nap(stop, 3)


def main():
    config = load_config()
    secrets = load_secrets()
    setup_logging(config)
    logging.info("transport_bridge starting")

    tcfg = config.get("transports", {})
    default_agent = config.get("default_agent", "companion")
    stop = threading.Event()
    senders = {}   # transport name -> send(reply_to, payload)
    threads = []

    tg = tcfg.get("telegram", {})
    if tg.get("enabled"):
        if "TELEGRAM_BOT_TOKEN" not in secrets:
            logging.error("telegram enabled but TELEGRAM_BOT_TOKEN missing in secrets.env — skipping")
        else:
            allowed = set(int(x) for x in tg.get("user_ids", []))
            if not allowed:
                logging.error("telegram enabled but user_ids allowlist empty — refusing to listen")
            else:
                transport = TelegramTransport(token=secrets["TELEGRAM_BOT_TOKEN"],
                                              allowed_user_ids=allowed,
                                              chunk_chars=tg.get("chunk_chars", 3800))
                transport.discard_backlog()
                senders["telegram"] = transport.send
                threads.append(threading.Thread(target=telegram_poller,
                                                 args=(transport, default_agent, stop),
                                                 name="tg-poll", daemon=True))
                logging.info(f"telegram: ready, allowed={sorted(allowed)}")

    em = tcfg.get("email", {})
    if em.get("enabled"):
        creds = load_email_credentials()
        if not creds:
            logging.warning("email enabled but no credentials resolvable — skipping")
        else:
            user, pw, host, port = creds
            et = EmailTransport(user, pw, host, port,
                                sender_allowlist=em.get("sender_allowlist", []),
                                max_body_chars=int(em.get("max_body_chars", 20000)))
            interval = int(em.get("poll_interval_seconds", 60))
            threads.append(threading.Thread(target=email_poller,
                                            args=(et, default_agent, stop, interval),
                                            name="email-poll", daemon=True))
            logging.info(f"email: ready, user={user}, poll={interval}s")

    if not threads:
        logging.error("no transports enabled in config.yaml (transports.telegram / .email). "
                      "Nothing to do — exiting. The default interface is the gateway webapp.")
        return 0

    threads.append(threading.Thread(target=outbox_worker, args=(senders, stop),
                                    name="outbox", daemon=True))

    def shutdown(signum, frame):
        logging.info(f"signal {signum} — shutting down")
        stop.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    for t in threads:
        t.start()
    while not stop.is_set():
        time.sleep(1)
    for t in threads:
        t.join(timeout=30)
    logging.info("transport_bridge stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
