"""gmail_client.py — read Jane emails and save draft replies, over IMAP.

The weekly run needs to (a) find new superbill emails and the secure link
inside them, (b) find appointment-reminder emails to know which sessions
happened, and (c) drop a draft nudge into Gmail Drafts. All of this goes
through Gmail IMAP/SMTP with an app password (key in config/settings.yml) —
no Google Cloud project or OAuth flow required.

Importable; also a small CLI for spot-checking:
    python gmail_client.py superbills
    python gmail_client.py appointments
"""

import base64
import datetime as dt
import email
import imaplib
import re
import sys
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

from _common import keychain, load_config, log

_MONTHS = ("January February March April May June July August September "
           "October November December").split()
_DATE_IN_BODY = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),\s+(\d{4})\b")
_CT_LINK = re.compile(r"https://[^\s\"'<>]+\.janeapp\.com/ct/[^\s\"'<>]+")


def _imap() -> imaplib.IMAP4_SSL:
    cfg = load_config()["gmail"]
    conn = imaplib.IMAP4_SSL(cfg["imap_host"])
    conn.login(cfg["address"], keychain(cfg["keychain_app_password_key"]))
    return conn


def _body_text(msg: email.message.Message) -> str:
    """Return the richest available body text (HTML preferred for links)."""
    html, plain = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True) or b""
                text = payload.decode(part.get_content_charset() or "utf-8",
                                      "ignore")
            except Exception:
                continue
            if ctype == "text/html":
                html += text
            else:
                plain += text
    else:
        plain = (msg.get_payload(decode=True) or b"").decode("utf-8", "ignore")
    return html or plain


def _decode_ct_target(ct_url: str) -> str:
    """A Jane /ct/ tracking link ends with a base64 of its real destination."""
    last = ct_url.rstrip("/").split("/")[-1]
    padded = last + "=" * (-len(last) % 4)
    try:
        return base64.b64decode(padded).decode("utf-8", "ignore")
    except Exception:
        return ""


def _superbill_url(body: str) -> str | None:
    """Pick the /ct/ link whose decoded target is the secure superbill doc."""
    for ct in _CT_LINK.findall(body):
        target = _decode_ct_target(ct)
        if "secure_documents" in target:
            return target or ct
    return None


def _imap_since(date_iso: str) -> str:
    d = dt.date.fromisoformat(date_iso)
    return d.strftime("%d-%b-%Y")


def _search(conn, criteria: str) -> list[bytes]:
    conn.select("INBOX", readonly=True)
    typ, data = conn.search(None, criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def find_superbills(since_iso: str) -> list[dict]:
    """Superbill emails since `since_iso`, each with its secure document URL."""
    cfg = load_config()["gmail"]
    out: list[dict] = []
    conn = _imap()
    try:
        criteria = (f'(FROM "{cfg["superbill_sender"]}" '
                    f'SUBJECT "{cfg["superbill_subject"]}" '
                    f'SINCE "{_imap_since(since_iso)}")')
        for num in _search(conn, criteria):
            typ, data = conn.fetch(num, "(RFC822)")
            if typ != "OK":
                continue
            msg = email.message_from_bytes(data[0][1])
            url = _superbill_url(_body_text(msg))
            received = parsedate_to_datetime(msg["Date"])
            out.append({
                "message_id": (msg["Message-ID"] or "").strip("<>"),
                "received": received.date().isoformat(),
                "url": url,
            })
    finally:
        conn.logout()
    return out


def find_appointments(since_iso: str) -> list[str]:
    """ISO dates of sessions, from Jane appointment-reminder emails."""
    cfg = load_config()["gmail"]
    dates: set[str] = set()
    conn = _imap()
    try:
        criteria = (f'(FROM "{cfg["appointment_sender"]}" '
                    f'SUBJECT "{cfg["appointment_subject"]}" '
                    f'SINCE "{_imap_since(since_iso)}")')
        for num in _search(conn, criteria):
            typ, data = conn.fetch(num, "(RFC822)")
            if typ != "OK":
                continue
            msg = email.message_from_bytes(data[0][1])
            m = _DATE_IN_BODY.search(_body_text(msg))
            if m:
                month = _MONTHS.index(m.group(1)) + 1
                try:
                    dates.add(dt.date(int(m.group(3)), month,
                                      int(m.group(2))).isoformat())
                except ValueError:
                    pass
    finally:
        conn.logout()
    return sorted(dates)


def save_draft(subject: str, body: str) -> None:
    """Append a draft email to the provider into Gmail Drafts for review."""
    cfg = load_config()["gmail"]
    msg = EmailMessage()
    msg["From"] = cfg["address"]
    msg["To"] = cfg["provider_email"]
    msg["Subject"] = subject
    msg.set_content(body)

    conn = _imap()
    try:
        conn.append(cfg["drafts_folder"], "\\Draft",
                    imaplib.Time2Internaldate(dt.datetime.now().timestamp()),
                    msg.as_bytes())
    finally:
        conn.logout()
    log(f"Saved Gmail draft to {cfg['provider_email']}: {subject}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    since = load_config()["start_date"]
    if cmd == "superbills":
        for s in find_superbills(since):
            print(s)
    elif cmd == "appointments":
        for d in find_appointments(since):
            print(d)
    else:
        print("Usage: python gmail_client.py [superbills|appointments]")
        sys.exit(2)
