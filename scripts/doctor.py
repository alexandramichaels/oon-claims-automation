"""doctor.py — health check for the oon-claims tool.

Usage:
    python doctor.py

Reports, as a quick "is everything wired up?" snapshot:
  - Keychain items present (Cigna, Jane, Telegram)
  - Saved browser sessions and their age
  - launchd weekly job loaded?
  - Recent claims and nudges from state.db
  - Last successful run

Backs the /status slash command. Exits 0 if nothing is obviously broken,
1 if a required piece is missing.
"""

import datetime as dt
import os
import sqlite3
import subprocess

from _common import CIGNA_SESSION, SESSIONS_DIR, STATE_DB, load_config

LAUNCHD_LABEL = "com.example.oon-claims.weekly"
JANE_SESSION = SESSIONS_DIR / "jane.json"


def _keychain_present(key: str) -> bool:
    return subprocess.run(
        ["security", "find-generic-password", "-s", key, "-w"],
        capture_output=True, text=True).returncode == 0


def _messages_db_status() -> tuple[bool, str]:
    """Can we read ~/Library/Messages/chat.db?

    Used by get_2fa_code.from_messages to pick up Cigna's 2FA SMS without
    a human in the loop. Reading it requires:
      (a) iPhone -> Mac SMS forwarding turned on, AND
      (b) Full Disk Access granted to whatever Python runs this script.

    Returns (ok, detail). ok=True means we can read the DB. ok=False with
    "missing" means SMS forwarding probably isn't on; ok=False with
    "unreadable" means FDA needs to be granted.
    """
    path = os.path.expanduser("~/Library/Messages/chat.db")
    if not os.path.exists(path):
        return False, "missing — turn on iPhone -> Mac SMS forwarding"
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("SELECT COUNT(*) FROM message").fetchone()
        conn.close()
        return True, "readable"
    except sqlite3.OperationalError as exc:
        return False, (f"unreadable ({exc}) — grant Full Disk Access to "
                       f"the Python binary in System Settings -> Privacy & "
                       f"Security -> Full Disk Access")


def _age_days(path) -> str:
    if not path.exists():
        return "missing"
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime)
    return f"{(dt.datetime.now() - mtime).days}d old"


def main() -> None:
    cfg = load_config()
    ok = True
    print("=== oon-claims health check ===\n")

    print("Keychain secrets:")
    keys = [
        cfg["gmail"]["keychain_app_password_key"],
        cfg["cigna"]["keychain_username_key"],
        cfg["cigna"]["keychain_password_key"],
        cfg["jane"]["keychain_username_key"],
        cfg["jane"]["keychain_password_key"],
        cfg["telegram"]["keychain_token_key"],
        cfg["telegram"]["keychain_chat_id_key"],
    ]
    for key in keys:
        present = _keychain_present(key)
        ok = ok and present
        print(f"  [{'OK' if present else 'MISSING'}] {key}")

    print("\n2FA from Messages (Full Disk Access):")
    msg_ok, msg_detail = _messages_db_status()
    ok = ok and msg_ok
    print(f"  [{'OK' if msg_ok else 'NEEDS FIX'}] {msg_detail}")

    print("\nBrowser sessions:")
    print(f"  jane.json:  {_age_days(JANE_SESSION)}")
    print(f"  cigna.json: {_age_days(CIGNA_SESSION)}")
    if CIGNA_SESSION.exists():
        age = (dt.datetime.now()
               - dt.datetime.fromtimestamp(CIGNA_SESSION.stat().st_mtime)).days
        if age >= 28:
            print("  NOTE: Cigna session is ~30 days old — expect a 2FA "
                  "prompt soon.")

    print("\nScheduled job:")
    loaded = LAUNCHD_LABEL in subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True).stdout
    print(f"  [{'OK' if loaded else 'NOT LOADED'}] {LAUNCHD_LABEL}")
    if not loaded:
        print("  Run /regen-launchd to install the Monday job.")

    print("\nRecent activity:")
    if STATE_DB.exists():
        conn = sqlite3.connect(STATE_DB)
        try:
            claims = conn.execute(
                "SELECT session_date, status, claim_name FROM claims "
                "ORDER BY session_date DESC LIMIT 5").fetchall()
            nudges = conn.execute(
                "SELECT session_date FROM nudges "
                "ORDER BY session_date DESC LIMIT 5").fetchall()
            run = conn.execute(
                "SELECT finished_at, summary FROM runs "
                "ORDER BY finished_at DESC LIMIT 1").fetchone()
        except sqlite3.OperationalError:
            claims, nudges, run = [], [], None
        finally:
            conn.close()
        for d, status, name in claims:
            print(f"  claim  {d}  {status:9}  {name or ''}")
        for (d,) in nudges:
            print(f"  nudge  {d}")
        if run:
            print(f"  last run: {run[0]} — {run[1]}")
        if not claims and not nudges:
            print("  (no claims or nudges recorded yet)")
    else:
        print("  state.db not created yet (runs on first /submit-claims).")

    print(f"\n{'All required pieces present.' if ok else 'SETUP INCOMPLETE — see MISSING items above.'}")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
