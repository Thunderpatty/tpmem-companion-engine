"""Telegram long-polling transport for tpmem-daemon.

Stdlib only — uses urllib.request. Long-poll getUpdates with offset tracking
to skip backlog and avoid replays.
"""
from __future__ import annotations
import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path


class TelegramTransport:
    def __init__(self, token, allowed_user_ids, chunk_chars=3800, media_dir=None):
        self.token = token
        self.allowed = set(int(u) for u in allowed_user_ids)
        self.chunk = max(1, int(chunk_chars))
        self.api = f"https://api.telegram.org/bot{token}"
        self.offset = 0
        self.media_dir = Path(media_dir) if media_dir else (Path.home() / ".tpmem" / "media")
        try:
            self.media_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logging.warning(f"telegram: could not create media dir {self.media_dir}: {e}")

    # ---------------------------------------------------------------- helpers
    def _get(self, method, params, timeout):
        url = f"{self.api}/{method}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)

    def _post(self, method, params, timeout=10):
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"{self.api}/{method}", data=data)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def _download_file(self, file_id, dest_stem):
        """getFile then download to media_dir. Returns abs path str, or None."""
        try:
            info = self._get("getFile", {"file_id": file_id}, timeout=15)
            if not info.get("ok"):
                logging.warning(f"telegram: getFile not ok for {file_id}: {info!r}")
                return None
            file_path = info["result"]["file_path"]          # e.g. photos/file_3.jpg
            ext = Path(file_path).suffix or ".bin"
            dest = self.media_dir / f"{dest_stem}{ext}"
            url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
            with urllib.request.urlopen(url, timeout=30) as r:
                dest.write_bytes(r.read())
            logging.info(f"telegram: saved attachment {dest}")
            return str(dest)
        except Exception as e:
            logging.warning(f"telegram: download failed for {file_id}: {e}")
            return None

    def _collect_attachments(self, m):
        """Download any photo / image-document on the message. Returns list of paths."""
        paths = []
        update_stem = f"tg-{m.get('message_id', 'x')}"
        # photos: a list of sizes — grab the largest (last)
        photos = m.get("photo")
        if photos:
            largest = photos[-1]
            p = self._download_file(largest["file_id"], f"{update_stem}-photo")
            if p:
                paths.append(p)
        # documents: only pull image/* documents
        doc = m.get("document")
        if doc and str(doc.get("mime_type", "")).startswith("image/"):
            p = self._download_file(doc["file_id"], f"{update_stem}-doc")
            if p:
                paths.append(p)
        return paths

    # ---------------------------------------------------------------- public
    def discard_backlog(self):
        """On startup, jump past any old updates so we don't replay them."""
        try:
            data = self._get("getUpdates", {"offset": -1, "limit": 1, "timeout": 0}, timeout=10)
            if data.get("ok") and data.get("result"):
                self.offset = data["result"][-1]["update_id"] + 1
                logging.info(f"telegram: discarded backlog, offset={self.offset}")
        except Exception as e:
            logging.warning(f"telegram: discard_backlog failed (will retry on poll): {e}")

    def poll_once(self, timeout=25):
        """Long-poll for up to `timeout` seconds. Returns list of allowed-sender messages."""
        data = self._get("getUpdates", {"offset": self.offset, "timeout": timeout}, timeout=timeout + 5)
        if not data.get("ok"):
            logging.warning(f"telegram: getUpdates not ok: {data!r}")
            return []
        out = []
        for upd in data.get("result", []):
            self.offset = max(self.offset, upd["update_id"] + 1)
            m = upd.get("message")
            if not m:
                continue
            user_id = m.get("from", {}).get("id")
            if user_id is None or user_id not in self.allowed:
                logging.info(f"telegram: ignoring message from unauthorized user_id={user_id}")
                continue
            text = m.get("text", "") or m.get("caption", "")
            attachments = self._collect_attachments(m)
            if attachments:
                refs = "\n".join(f"[attachment: {p}]" for p in attachments)
                text = f"{text}\n\n{refs}" if text else refs
            out.append({
                "external_id": str(upd["update_id"]),
                "user_id": str(user_id),
                "chat_id": str(m["chat"]["id"]),
                "text": text,
            })
        return out

    def send(self, chat_id, text):
        """Send `text` to chat_id, chunking to stay under Telegram's per-message cap."""
        if not text:
            text = "(empty)"
        for i in range(0, len(text), self.chunk):
            chunk = text[i:i + self.chunk]
            self._post("sendMessage", {"chat_id": chat_id, "text": chunk}, timeout=15)

    def send_chat_action(self, chat_id, action="typing"):
        """Show a transient indicator in the chat (auto-clears in ~5s).

        Best-effort: failures are logged and swallowed so the worker can proceed
        with the actual reply.
        """
        try:
            self._post("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=5)
        except Exception as e:
            logging.warning(f"telegram: sendChatAction failed (non-fatal): {e}")
