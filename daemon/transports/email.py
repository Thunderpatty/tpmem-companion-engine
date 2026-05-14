"""Email IMAP transport for tpmem-daemon.

Polls Gmail IMAP for unread messages from allowlisted senders, marks them
\\Seen, returns them for the daemon to drop into the inbox.

Stdlib only (imaplib, email, ssl, tomllib).

Credentials resolution order:
  1. ~/.config/tpmem-daemon/secrets.env
       (TPMEM_EMAIL_USER, TPMEM_EMAIL_PASSWORD, optional _HOST/_PORT)
  2. Parse ~/.config/himalaya/config.toml [accounts.alerts] section
  3. None (caller logs and skips)
"""
from __future__ import annotations
import email
import email.utils
import imaplib
import logging
import socket
import ssl
import tomllib
from email.header import decode_header
from pathlib import Path


def load_email_credentials():
    """Returns (user, password, host, port) or None."""
    sec = Path.home() / ".config" / "tpmem-daemon" / "secrets.env"
    if sec.exists():
        env = {}
        for line in sec.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
        if env.get("TPMEM_EMAIL_USER") and env.get("TPMEM_EMAIL_PASSWORD"):
            return (
                env["TPMEM_EMAIL_USER"],
                env["TPMEM_EMAIL_PASSWORD"],
                env.get("TPMEM_EMAIL_HOST", "imap.gmail.com"),
                int(env.get("TPMEM_EMAIL_PORT", "993")),
            )

    himalaya = Path.home() / ".config" / "himalaya" / "config.toml"
    if himalaya.exists():
        try:
            with open(himalaya, "rb") as f:
                cfg = tomllib.load(f)
            for name, account in cfg.get("accounts", {}).items():
                # Prefer the default account; fall back to 'alerts' by name
                pick = account.get("default") or name == "alerts"
                if not pick:
                    continue
                backend = account.get("backend", {})
                auth = backend.get("auth", {})
                if backend.get("type") == "imap" and auth.get("raw"):
                    return (
                        backend.get("login", account.get("email")),
                        auth["raw"],
                        backend.get("host", "imap.gmail.com"),
                        int(backend.get("port", 993)),
                    )
        except Exception as e:
            logging.warning(f"email: parse himalaya config failed: {e}")

    return None


def _decode_header(s):
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for piece, charset in parts:
        if isinstance(piece, bytes):
            try:
                out.append(piece.decode(charset or "utf-8", errors="replace"))
            except Exception:
                out.append(piece.decode("utf-8", errors="replace"))
        else:
            out.append(piece)
    return " ".join(out).strip()


def _extract_body(msg):
    """Plain-text body. Falls back to first decodable part."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8",
                                          errors="replace")
        for part in msg.walk():
            if part.get_content_type().startswith("text/"):
                payload = part.get_payload(decode=True)
                if payload:
                    try:
                        return payload.decode(part.get_content_charset() or "utf-8",
                                              errors="replace")
                    except Exception:
                        pass
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return msg.get_payload() or ""


class EmailTransport:
    def __init__(self, user, password, host="imap.gmail.com", port=993,
                 mailbox="INBOX", sender_allowlist=None, max_body_chars=20000,
                 media_dir=None):
        self.user = user
        self.password = password
        self.host = host
        self.port = port
        self.mailbox = mailbox
        self.allowlist = [s.lower() for s in (sender_allowlist or [])]
        self.max_body = int(max_body_chars)
        self.media_dir = Path(media_dir) if media_dir else (Path.home() / ".tpmem" / "media")
        try:
            self.media_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logging.warning(f"email: could not create media dir {self.media_dir}: {e}")

    def _save_image_attachments(self, msg, stem):
        """Save any image/* parts to media_dir. Returns list of abs path strs."""
        paths = []
        if not msg.is_multipart():
            return paths
        idx = 0
        for part in msg.walk():
            if part.get_content_maintype() != "image":
                continue
            data = part.get_payload(decode=True)
            if not data:
                continue
            fname = part.get_filename()
            ext = Path(fname).suffix if fname else ("." + part.get_content_subtype())
            dest = self.media_dir / f"{stem}-{idx}{ext}"
            try:
                dest.write_bytes(data)
                paths.append(str(dest))
                logging.info(f"email: saved attachment {dest}")
                idx += 1
            except Exception as e:
                logging.warning(f"email: could not write attachment {dest}: {e}")
        return paths

    def _connect(self):
        ctx = ssl.create_default_context()
        m = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ctx, timeout=30)
        m.login(self.user, self.password)
        m.select(self.mailbox)
        return m

    def _allowed(self, sender_addr, full_sender):
        if not self.allowlist:
            return True
        s = (sender_addr or full_sender or "").lower()
        return any(rule in s for rule in self.allowlist)

    def fetch_new(self):
        """Pull all UNSEEN messages from allowlisted senders. Mark them \\Seen."""
        out = []
        try:
            m = self._connect()
        except (socket.timeout, ssl.SSLError, imaplib.IMAP4.error, OSError) as e:
            logging.warning(f"email: connect failed ({type(e).__name__}): {e}")
            return out

        try:
            typ, data = m.search(None, "UNSEEN")
            if typ != "OK" or not data or not data[0]:
                return out
            uids = data[0].split()

            for uid in uids:
                typ, msg_data = m.fetch(uid, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                full_sender = _decode_header(msg.get("From", ""))
                _, sender_addr = email.utils.parseaddr(full_sender)
                subject = _decode_header(msg.get("Subject", ""))
                date = msg.get("Date", "")

                if not self._allowed(sender_addr, full_sender):
                    # Mark seen anyway so we don't re-check it forever
                    m.store(uid, "+FLAGS", "\\Seen")
                    logging.info(f"email: ignored non-allowlisted sender: "
                                 f"{sender_addr or full_sender}")
                    continue

                body = _extract_body(msg)
                payload = (
                    f"From: {full_sender}\n"
                    f"Subject: {subject}\n"
                    f"Date: {date}\n\n"
                    f"{body[:self.max_body]}"
                )

                # Save image attachments and append refs AFTER the truncated
                # body so they survive max_body truncation.
                attachments = self._save_image_attachments(msg, f"email-uid-{uid.decode()}")
                if attachments:
                    refs = "\n".join(f"[attachment: {p}]" for p in attachments)
                    payload = f"{payload}\n\n{refs}"

                out.append({
                    "external_id": f"imap-uid-{uid.decode()}",
                    "user_id": sender_addr or full_sender,
                    "reply_to": sender_addr or full_sender,
                    "text": payload,
                    "subject": subject,
                })

                m.store(uid, "+FLAGS", "\\Seen")
        finally:
            try:
                m.close()
                m.logout()
            except Exception:
                pass
        return out
