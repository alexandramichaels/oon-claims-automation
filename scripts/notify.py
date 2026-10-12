"""notify.py — send a Telegram message via the oon-claims bot.

Usage:
    python notify.py "your message here"

Bot token and chat_id come from the macOS Keychain (keys in
config/settings.yml). Messages support a small amount of HTML (<b>, <a href>).
"""

import json
import sys
import urllib.parse
import urllib.request

from _common import keychain, load_config, log

# Telegram's hard message limit is 4096 characters. Failure alerts that
# embed a Playwright traceback can easily blow past that, which makes the
# alert itself fail with HTTP 400 — exactly when you most want to see it.
# We truncate well under the limit so we never lose the alert.
TELEGRAM_MAX_CHARS = 3800


def send(text: str) -> dict:
    cfg = load_config()["telegram"]
    token = keychain(cfg["keychain_token_key"])
    chat_id = keychain(cfg["keychain_chat_id_key"])

    if len(text) > TELEGRAM_MAX_CHARS:
        suffix = "\n\n[...truncated; see logs/ for the full message]"
        text = text[:TELEGRAM_MAX_CHARS - len(suffix)] + suffix

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=20) as resp:
        payload = json.load(resp)
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram send failed: {payload}")
    return payload


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python notify.py 'message'")
        sys.exit(2)
    send(" ".join(sys.argv[1:]))
    log("Telegram alert sent.")
