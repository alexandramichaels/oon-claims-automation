"""setup_login.py — one-time interactive login for Jane or Cigna.

Usage:
    python setup_login.py jane
    python setup_login.py cigna

Opens a visible browser window at the login page. You sign in by hand —
including any 2FA. For Cigna, also tick "remember this device for 30 days"
so the saved session skips 2FA on the next ~30 days of automated runs.

When you press Enter at the terminal, the Playwright storage_state (cookies +
localStorage) is saved to sessions/<target>.json. fetch_superbill.py and
cigna_submit.py reuse that file. Re-run this whenever a session expires
(the /jane-login and /cigna-login slash commands do exactly that).
"""

import sys

from playwright.sync_api import sync_playwright

from _common import SESSIONS_DIR, load_config, stealth

TARGETS = {
    "jane": {
        "label": "Jane (practice portal)",
        "url_key": ("jane", "login_url"),
        "hint": "Sign in to your Jane patient account for the practice.",
    },
    "cigna": {
        "label": "Cigna (my.cigna.com)",
        "url_key": ("cigna", "claim_form_url"),
        "hint": ("Sign in to my.cigna.com. Complete 2FA if prompted, and "
                 "TICK 'remember this device for 30 days' so automated runs "
                 "skip 2FA for the next month."),
    },
}


def _safe_close(browser) -> None:
    try:
        browser.close()
    except Exception:
        pass


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in TARGETS:
        print("Usage: python setup_login.py [jane|cigna]")
        sys.exit(2)

    target = sys.argv[1]
    cfg = TARGETS[target]
    config = load_config()
    section, key = cfg["url_key"]
    url = config[section][key]

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session_file = SESSIONS_DIR / f"{target}.json"

    print(f"\n=== Login setup: {cfg['label']} ===\n")
    if session_file.exists():
        print(f"NOTE: {session_file.name} already exists — it will be "
              f"overwritten when you confirm.\n")

    saved = False
    with stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print(f"(Could not auto-load {url}: {exc})")
            print("You can type the address into the browser window yourself.\n")

        print("A browser window has opened.")
        print(f"  {cfg['hint']}\n")
        print("Keep the browser window OPEN — do not close it, and do not quit")
        print("this script. Signing in alone does NOT save anything: the session")
        print("is only captured when you come back here and press ENTER.\n")
        print("(Ctrl+C to cancel — this saves nothing and discards the login.)\n")
        try:
            input(">>> Signed in? Come back here and press ENTER to SAVE. <<< ")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled — NO SESSION WAS SAVED. Nothing changed.")
            print("Re-run this script and press ENTER (not Ctrl+C) to save.")
            _safe_close(browser)
            sys.exit(1)

        try:
            state = context.storage_state(path=str(session_file))
            saved = True
        except Exception as exc:
            print(f"\nERROR: could not save the session: {exc}")
        _safe_close(browser)

    if not saved:
        sys.exit(1)
    cookies = len(state.get("cookies", []))
    print(f"\nSaved session -> {session_file}  ({cookies} cookie(s))")
    if cookies == 0:
        print("WARNING: no cookies captured — if automated runs hit a login "
              "page, re-run this and make sure the sign-in completes.")
    print()


if __name__ == "__main__":
    main()
