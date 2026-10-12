"""get_2fa_code.py — retrieve a Cigna 2FA verification code.

Cigna only challenges 2FA roughly every 30 days. When it does, this resolves
the code without you having to babysit the run:

  1. macOS Messages — reads the most recent verification code out of
     ~/Library/Messages/chat.db. Requires iPhone->Mac SMS forwarding to be on
     and Full Disk Access granted to whatever runs this.
  2. Telegram — the bot messages you "reply with your Cigna code" and polls
     for your reply. Works no matter how Cigna delivers the code.

Usage:
    python get_2fa_code.py [--timeout 300]

Prints the code on stdout (exit 0); exits 1 if no code could be obtained.
"""

import argparse
import datetime as dt
import email
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

from _common import keychain, load_config, log
from notify import send as telegram_send

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
MAC_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01
_CODE = re.compile(r"\b(\d{4,8})\b")


def _extract_code(text: str) -> str | None:
    """Find a verification code in a message body, preferring 6-digit codes."""
    if not text:
        return None
    low = text.lower()
    if not any(w in low for w in ("code", "cigna", "verif", "passcode")):
        return None
    matches = _CODE.findall(text)
    if not matches:
        return None
    for m in matches:
        if len(m) == 6:
            return m
    return matches[0]


def from_messages(within_seconds: int = 600) -> str | None:
    """Read the newest verification code from the macOS Messages database."""
    if not os.path.exists(CHAT_DB):
        return None
    cutoff_ns = int(
        (time.time() - within_seconds - MAC_EPOCH_OFFSET) * 1_000_000_000)
    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        log(f"Messages DB not readable ({exc}); grant Full Disk Access.")
        return None
    try:
        rows = conn.execute(
            "SELECT text, attributedBody FROM message "
            "WHERE is_from_me = 0 AND date > ? ORDER BY date DESC LIMIT 40",
            (cutoff_ns,),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        log(f"Messages DB query failed ({exc}).")
        return None
    finally:
        conn.close()

    for text, blob in rows:
        body = text
        if not body and blob:
            # Newer macOS stores the body in a binary archive; decoding it
            # loosely still exposes the digits of a verification code.
            body = bytes(blob).decode("utf-8", "ignore")
        code = _extract_code(body or "")
        if code:
            return code
    return None


def from_gmail(within_minutes: int = 15) -> str | None:
    """Read a Cigna verification code out of a recent email, over IMAP."""
    from gmail_client import _body_text, _imap  # local import: optional path

    try:
        conn = _imap()
    except Exception as exc:
        log(f"Gmail check skipped ({exc}).")
        return None
    try:
        conn.select("INBOX", readonly=True)
        since = dt.date.today().strftime("%d-%b-%Y")
        typ, data = conn.search(None, f'(SINCE "{since}" FROM "cigna")')
        if typ != "OK" or not data or not data[0]:
            return None
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            minutes=within_minutes)
        for num in reversed(data[0].split()):
            typ, fetched = conn.fetch(num, "(RFC822)")
            if typ != "OK":
                continue
            msg = email.message_from_bytes(fetched[0][1])
            try:
                received = parsedate_to_datetime(msg["Date"])
            except Exception:
                continue
            if received and received < cutoff:
                continue
            code = _extract_code(f"{msg['Subject']}\n{_body_text(msg)}")
            if code:
                return code
    finally:
        conn.logout()
    return None


def from_telegram(timeout: int = 300) -> str | None:
    """Ask via the Telegram bot and poll for a reply containing the code."""
    cfg = load_config()["telegram"]
    token = keychain(cfg["keychain_token_key"])
    chat_id = str(keychain(cfg["keychain_chat_id_key"]))
    api = f"https://api.telegram.org/bot{token}"

    def _updates(offset=None):
        params = {"timeout": 20}
        if offset is not None:
            params["offset"] = offset
        url = f"{api}/getUpdates?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp).get("result", [])

    # Establish a baseline so we ignore messages sent before we asked.
    offset = None
    for upd in _updates():
        offset = upd["update_id"] + 1

    telegram_send("Cigna needs a verification code. Reply to this message "
                  "with the code (digits only).")
    log("Asked for the Cigna 2FA code via Telegram; waiting for a reply.")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            updates = _updates(offset)
        except Exception:
            time.sleep(3)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != chat_id:
                continue
            m = _CODE.search(msg.get("text", "") or "")
            if m:
                return m.group(1)
    return None


def get_code(timeout: int = 300) -> str:
    """Resolve a Cigna 2FA code: Messages (with a polling window), then Gmail,
    then a Telegram reply.

    Cigna's SMS can land 10–30s after it's requested — iPhone->Mac forwarding
    adds its own lag on top. We poll Messages at t=0, t=15s, t=30s before
    falling through to Gmail and Telegram.
    """
    for delay in (0, 15, 15):
        if delay:
            log(f"No code in Messages yet — waiting {delay}s and rechecking.")
            time.sleep(delay)
        code = from_messages()
        if code:
            log("Got the Cigna 2FA code from Messages.")
            return code
    code = from_gmail()
    if code:
        log("Got the Cigna 2FA code from a recent email.")
        return code
    log("No code in Messages or email; asking via Telegram.")
    code = from_telegram(timeout=timeout)
    if code:
        log("Got the Cigna 2FA code from a Telegram reply.")
        return code
    raise RuntimeError("Could not obtain a Cigna 2FA code.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    try:
        print(get_code(timeout=args.timeout))
    except Exception as exc:
        log(f"ERROR: {exc}")
        raise SystemExit(1)
