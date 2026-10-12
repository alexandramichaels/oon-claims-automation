"""cigna_session_probe.py — reproduce the orchestrator's failed path.

Opens a context with the saved Cigna session (read-only), navigates to the
claim form, clicks 'Log in' on /guest if needed, and snapshots whatever
page actually rendered. Does NOT fill anything, does NOT mutate the saved
session. Use this to figure out what the orchestrator saw when it timed
out waiting for the username input.

Usage:
    python scripts/cigna_session_probe.py
"""

import datetime as dt
import json
import re

from playwright.sync_api import sync_playwright

from _common import CIGNA_SESSION, LOGS_DIR, load_config, log, stealth


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


def _snapshot(page, stamp, step, label):
    img = LOGS_DIR / f"sessprobe-{stamp}-{step}-{label}.png"
    js = LOGS_DIR / f"sessprobe-{stamp}-{step}-{label}.json"
    page.screenshot(path=str(img), full_page=True)
    data = page.evaluate(_DUMP_JS)
    data["url"] = page.url
    js.write_text(json.dumps(data, indent=1))
    log(f"  -> {img.name}, {js.name} ({len(data['controls'])} controls)")


def main():
    cfg = load_config()["cigna"]
    form_url = cfg["claim_form_url"]
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log(f"=== Cigna session probe ({stamp}) ===")
    log(f"Using saved session: {CIGNA_SESSION} (exists={CIGNA_SESSION.exists()})")

    with stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            accept_downloads=True,
            storage_state=str(CIGNA_SESSION) if CIGNA_SESSION.exists() else None,
        )
        page = context.new_page()
        page.goto(form_url, wait_until="load", timeout=60000)
        page.wait_for_timeout(2500)
        log(f"After form goto: {page.url}")
        _snapshot(page, stamp, 1, "after-goto")

        if "/guest" in page.url.lower():
            log("On /guest — clicking 'Log in'.")
            page.get_by_role(
                "button", name=re.compile(r"^\s*log ?in\s*$", re.I)
            ).first.click()
            page.wait_for_load_state("load", timeout=30000)
            page.wait_for_timeout(2500)
            log(f"After Log-in click: {page.url}")
            _snapshot(page, stamp, 2, "after-login-click")

        ident_count = page.locator(
            "input[type=text]:visible, input[type=email]:visible, "
            "input[autocomplete='username']:visible"
        ).count()
        log(f"Visible identifier inputs found: {ident_count}")

        log("=== Done. NOT mutating session. ===")
        browser.close()


if __name__ == "__main__":
    main()
