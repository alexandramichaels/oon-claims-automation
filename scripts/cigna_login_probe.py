"""cigna_login_probe.py — diagnose the Cigna sign-in flow.

Walks the unauthenticated path the orchestrator takes — /guest → Log in →
username → Next → password → Next — and screenshots / dumps controls at
every stop. Goes far enough to trigger Cigna's 2FA challenge (Cigna WILL
text you a real verification code) so we can see what the 2FA page looks
like, but stops before entering the code. Nothing is submitted.

Usage:
    python scripts/cigna_login_probe.py

The screenshots + controls JSON land in logs/probe-<timestamp>-*.
"""

import datetime as dt
import json
import re

from playwright.sync_api import sync_playwright

from _common import LOGS_DIR, keychain, load_config, log, stealth


_DUMP_JS = """() => {
  const SEL = 'input,button,a,[role=button],[role=link]';
  const out = [];
  const seen = new Set();
  const collect = (el) => {
    if (seen.has(el)) return;
    seen.add(el);
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    out.push({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type'),
      role: el.getAttribute('role'),
      name: el.getAttribute('name'),
      id: el.id || null,
      ariaLabel: el.getAttribute('aria-label'),
      text: (el.innerText || '').trim().slice(0, 80),
      href: el.getAttribute('href'),
    });
  };
  const walk = (root) => {
    root.querySelectorAll('*').forEach(el => {
      if (el.matches && el.matches(SEL)) collect(el);
      if (el.shadowRoot) walk(el.shadowRoot);
    });
  };
  walk(document);
  return {controls: out, bodyText: document.body.innerText.slice(0, 2000)};
}"""


def _snapshot(page, stamp: str, step: int, label: str) -> None:
    """Screenshot + dump visible controls + the first 2KB of body text."""
    img = LOGS_DIR / f"probe-{stamp}-{step}-{label}.png"
    js = LOGS_DIR / f"probe-{stamp}-{step}-{label}.json"
    page.screenshot(path=str(img), full_page=True)
    data = page.evaluate(_DUMP_JS)
    data["url"] = page.url
    js.write_text(json.dumps(data, indent=1))
    log(f"  -> {img.name}, {js.name} ({len(data['controls'])} controls)")


def _click_next(page) -> None:
    page.get_by_role(
        "button",
        name=re.compile(r"log ?in|sign ?in|continue|next", re.I),
    ).first.click()
    page.wait_for_load_state("load", timeout=30000)
    page.wait_for_timeout(2500)


def main() -> None:
    cfg = load_config()["cigna"]
    form_url = cfg["claim_form_url"]
    username = keychain(cfg["keychain_username_key"])
    password = keychain(cfg["keychain_password_key"])

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log(f"=== Cigna login probe ({stamp}) ===")

    with stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto(form_url, wait_until="load", timeout=60000)
        page.wait_for_timeout(2500)
        log(f"After form goto: {page.url}")

        # 1. /guest → click Log in.
        if "/guest" in page.url.lower():
            page.get_by_role(
                "button", name=re.compile(r"^\s*log ?in\s*$", re.I)
            ).first.click()
            page.wait_for_load_state("load", timeout=30000)
            page.wait_for_timeout(2500)
            log(f"After Log-in click: {page.url}")
        _snapshot(page, stamp, 1, "username-page")

        # 2. Username page → fill → Next.
        ident = page.locator(
            "input[type=text]:visible, input[type=email]:visible, "
            "input[autocomplete='username']:visible")
        if not ident.count():
            log("STOPPING: no identifier field on this page.")
            browser.close()
            return
        ident.first.fill(username, timeout=20000)
        log("Filled username, clicking Next.")
        _click_next(page)
        log(f"After username Next: {page.url}")
        _snapshot(page, stamp, 2, "password-page")

        # 3. Password page → fill → Next.
        pwd = page.locator("input[type=password]:visible")
        if not pwd.count():
            log("STOPPING: no password field on this page.")
            browser.close()
            return
        pwd.first.fill(password)
        log("Filled password, clicking Next. (Cigna will text a 2FA code.)")
        _click_next(page)
        log(f"After password Next: {page.url}")
        page.wait_for_timeout(3000)  # let 2FA page settle
        _snapshot(page, stamp, 3, "post-password")

        log("=== Probe done. NOT entering the 2FA code; closing browser. ===")
        browser.close()


if __name__ == "__main__":
    main()
