"""cigna_onetrust_probe.py — capture the OneTrust cookie banner on /guest.

The orchestrator hit a OneTrust consent banner that blocked the 'Log in'
click. The banner is intermittent — it appears on cookie-less contexts.
This probe opens a fresh (no-cookie) context, navigates to the Cigna
form URL, waits for /guest to render, and dumps every control under the
OneTrust consent SDK plus a screenshot. Use the output to wire up a
dismiss-if-present handler in cigna_submit.py.

Usage:
    python scripts/cigna_onetrust_probe.py
"""

import datetime as dt
import json

from playwright.sync_api import sync_playwright

from _common import LOGS_DIR, load_config, log, stealth


_DUMP_OT_JS = """() => {
  const root = document.querySelector('#onetrust-consent-sdk');
  if (!root) return {present: false};
  const controls = [];
  root.querySelectorAll('button, a, input, [role=button], [role=link]')
    .forEach(el => {
      const r = el.getBoundingClientRect();
      controls.push({
        tag: el.tagName.toLowerCase(),
        id: el.id || null,
        cls: el.getAttribute('class'),
        type: el.getAttribute('type'),
        role: el.getAttribute('role'),
        text: (el.innerText || '').trim().slice(0, 100),
        ariaLabel: el.getAttribute('aria-label'),
        visible: r.width > 0 && r.height > 0,
      });
    });
  return {
    present: true,
    rootClasses: root.getAttribute('class'),
    rootHTML: root.outerHTML.slice(0, 4000),
    controls,
  };
}"""


def main():
    cfg = load_config()["cigna"]
    form_url = cfg["claim_form_url"]
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log(f"=== Cigna OneTrust probe ({stamp}) ===")

    with stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=False)
        # No storage_state — force the no-cookie path that triggers OneTrust.
        context = browser.new_context()
        page = context.new_page()
        page.goto(form_url, wait_until="load", timeout=60000)
        page.wait_for_timeout(4000)
        log(f"After form goto: {page.url}")

        img = LOGS_DIR / f"onetrust-{stamp}.png"
        page.screenshot(path=str(img), full_page=True)
        log(f"  -> {img.name}")

        data = page.evaluate(_DUMP_OT_JS)
        data["url"] = page.url
        js = LOGS_DIR / f"onetrust-{stamp}.json"
        js.write_text(json.dumps(data, indent=1))
        log(f"  -> {js.name} (present={data.get('present')}, "
            f"{len(data.get('controls', []))} controls)")

        browser.close()


if __name__ == "__main__":
    main()
