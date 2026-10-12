"""jane_login_probe.py — diagnose the Jane sign-in flow.

Walks the same path fetch_superbill.py takes — open a "View Superbill" link,
land on Jane's sign-in page, identifier → password — using the same browser
config and the same locators as production, and screenshots / dumps controls
at every stop. Stops after the password step: nothing is downloaded, and
sessions/jane.json is never written.

Usage:
    python scripts/jane_login_probe.py                # latest superbill link
    python scripts/jane_login_probe.py "<url>"        # a specific link
    python scripts/jane_login_probe.py --fresh        # ignore sessions/jane.json
    python scripts/jane_login_probe.py --headed       # visible browser
    python scripts/jane_login_probe.py --home         # is the session alive?

The screenshots + controls JSON land in logs/janeprobe-<timestamp>-*.
"""

import argparse
import datetime as dt
import json
import re

from playwright.sync_api import sync_playwright

import gmail_client
from _common import LOGS_DIR, keychain, load_config, log
from fetch_superbill import JANE_SESSION, _dismiss_cookie_banner

_DUMP_JS = """() => {
  const SEL = 'input,button,a,select,textarea,[role=button],[role=link],[role=alert],iframe';
  const out = [];
  document.querySelectorAll(SEL).forEach(el => {
    const r = el.getBoundingClientRect();
    out.push({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type'),
      role: el.getAttribute('role'),
      name: el.getAttribute('name'),
      id: el.id || null,
      placeholder: el.getAttribute('placeholder'),
      autocomplete: el.getAttribute('autocomplete'),
      ariaLabel: el.getAttribute('aria-label'),
      text: (el.innerText || '').trim().slice(0, 80),
      href: el.tagName === 'A' ? el.getAttribute('href') : null,
      src: el.tagName === 'IFRAME' ? el.getAttribute('src') : null,
      visible: !(r.width === 0 && r.height === 0),
    });
  });
  return {title: document.title, controls: out,
          bodyText: document.body.innerText.slice(0, 2000)};
}"""


def _snapshot(page, stamp: str, step: int, label: str) -> None:
    """Screenshot + dump controls + the first 2KB of body text."""
    img = LOGS_DIR / f"janeprobe-{stamp}-{step}-{label}.png"
    js = LOGS_DIR / f"janeprobe-{stamp}-{step}-{label}.json"
    page.screenshot(path=str(img), full_page=True)
    data = page.evaluate(_DUMP_JS)
    data["url"] = page.url
    js.write_text(json.dumps(data, indent=1))
    log(f"  -> {img.name}, {js.name} ({len(data['controls'])} controls)")


def _latest_superbill_url() -> str:
    since = dt.date.today() - dt.timedelta(days=60)
    found = sorted(gmail_client.find_superbills(since.isoformat()),
                   key=lambda sb: sb["received"])
    if not found:
        raise SystemExit("No superbill email in the last 60 days; pass a URL.")
    return found[-1]["url"]


def _click_first(page, names) -> str | None:
    """Click the first button matching any of `names` — the same loop
    fetch_superbill._jane_login uses. Returns the name that matched."""
    for name in names:
        btn = page.get_by_role("button", name=re.compile(name, re.I))
        if btn.count():
            log(f"  button /{name}/ matched {btn.count()}; clicking the first.")
            btn.first.click()
            return name
    log(f"  NO button matched any of {names}.")
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--home", action="store_true")
    args = ap.parse_args()

    cfg = load_config()["jane"]
    username = keychain(cfg["keychain_username_key"])
    password = keychain(cfg["keychain_password_key"])
    url = (cfg["login_url"] if args.home
           else args.url or _latest_superbill_url())

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    use_session = JANE_SESSION.exists() and not args.fresh
    log(f"=== Jane login probe ({stamp}) session={use_session} "
        f"headed={args.headed} ===")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            accept_downloads=True,
            storage_state=str(JANE_SESSION) if use_session else None,
        )
        page = context.new_page()
        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)

        try:
            page.goto(url, wait_until="load", timeout=60000)
        except Exception as exc:
            if "Download is starting" in str(exc):
                log("Link served a download — the saved session is VALID. "
                    "Nothing to probe; re-run with --fresh to see sign-in.")
                browser.close()
                return
            raise
        page.wait_for_timeout(2500)
        log(f"After goto: {page.url}")
        _snapshot(page, stamp, 1, "home" if args.home else "landing")
        if args.home:
            log(f"  Sign Out links: "
                f"{page.locator('a[href=\"/logout\"]').count()} "
                f"(0 = signed out)")
            browser.close()
            return
        _dismiss_cookie_banner(page)

        # Step 1: identifier — production's locators, with match counts.
        ident = page.get_by_placeholder(re.compile("username|email|phone", re.I))
        log(f"  identifier by placeholder: {ident.count()}")
        if ident.count() == 0:
            ident = page.locator(
                "input[type=text], input[type=email], input:not([type])")
            log(f"  identifier by fallback: {ident.count()}")
        if ident.count() == 0:
            log("STOPPING: no identifier field on this page.")
            browser.close()
            return
        ident.first.fill(username, timeout=15000)
        _click_first(page, ("Continue", "Sign In", "Next"))
        page.wait_for_timeout(2500)
        log(f"After identifier: {page.url}")
        _snapshot(page, stamp, 2, "after-identifier")

        # Step 2: password.
        pwd = page.locator("input[type=password]")
        log(f"  password fields: {pwd.count()}")
        try:
            pwd.first.wait_for(state="visible", timeout=15000)
        except Exception:
            log("STOPPING: no visible password field.")
            browser.close()
            return
        pwd.first.fill(password)
        _click_first(page, ("Sign In", "Continue", "Log In"))
        try:
            page.wait_for_load_state("load", timeout=30000)
        except Exception:
            log("  load state timed out after password submit.")
        page.wait_for_timeout(4000)
        log(f"After password: {page.url}")
        log(f"  password fields still present: "
            f"{page.locator('input[type=password]').count()}")
        _snapshot(page, stamp, 3, "after-password")

        if console:
            (LOGS_DIR / f"janeprobe-{stamp}-console.json").write_text(
                json.dumps(console[-100:], indent=1))
        log("=== Probe done. Session file untouched; closing browser. ===")
        browser.close()


if __name__ == "__main__":
    main()
