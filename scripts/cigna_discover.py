"""cigna_discover.py — one-off: capture the Cigna claim form's structure.

Logs into Cigna, opens the online claim form, uploads a superbill PDF, and
dumps the page's accessibility tree + screenshots so the real selectors for
cigna_submit.py can be written. Does NOT submit anything.

Usage:
    python cigna_discover.py --pdf PATH
"""

import argparse
import datetime as dt
import json

from playwright.sync_api import sync_playwright

from _common import CIGNA_SESSION, LOGS_DIR, load_config, log, stealth
from cigna_submit import _login, _on_login_page


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    args = parser.parse_args()

    cfg = load_config()
    form_url = cfg["cigna"]["claim_form_url"]
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    with stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            accept_downloads=True,
            storage_state=str(CIGNA_SESSION) if CIGNA_SESSION.exists() else None,
        )
        page = context.new_page()
        page.goto(form_url, wait_until="load", timeout=60000)
        page.wait_for_timeout(2500)
        if _on_login_page(page):
            _login(page)
            context.storage_state(path=str(CIGNA_SESSION))
            page.goto(form_url, wait_until="load", timeout=60000)
            page.wait_for_timeout(2500)

        page.screenshot(path=str(LOGS_DIR / f"discover-{stamp}-1-form.png"),
                        full_page=True)

        file_inputs = page.locator("input[type=file]")
        if file_inputs.count():
            file_inputs.first.set_input_files(args.pdf)
            page.wait_for_timeout(5000)
            log("Uploaded PDF.")
        else:
            log("No file input found on the initial form page.")

        page.wait_for_timeout(4000)  # let validation finish
        page.screenshot(path=str(LOGS_DIR / f"discover-{stamp}-2-uploaded.png"),
                        full_page=True)

        controls = page.evaluate(
            """() => {
              const SEL = 'input,select,textarea,button,[role=radio],'
                + '[role=checkbox],[role=button],[role=combobox],[role=switch]';
              const out = [];
              const seen = new Set();
              const collect = (el) => {
                if (seen.has(el)) return;
                seen.add(el);
                const r = el.getBoundingClientRect();
                const root = el.getRootNode();
                let label = '';
                if (el.id && root.querySelector) {
                  const l = root.querySelector(
                    'label[for="' + CSS.escape(el.id) + '"]');
                  if (l) label = l.innerText.trim();
                }
                if (!label && el.closest('label'))
                  label = el.closest('label').innerText.trim();
                let ctx = '', p = el.parentElement;
                for (let i = 0; i < 6 && p; i++) {
                  const t = (p.innerText || '').trim();
                  if (t && t.length < 180) ctx = t;
                  p = p.parentElement;
                }
                out.push({
                  tag: el.tagName.toLowerCase(),
                  type: el.getAttribute('type'),
                  role: el.getAttribute('role'),
                  name: el.getAttribute('name'),
                  id: el.id || null,
                  value: el.getAttribute('value'),
                  placeholder: el.getAttribute('placeholder'),
                  ariaLabel: el.getAttribute('aria-label'),
                  visible: !(r.width === 0 && r.height === 0),
                  label: label.slice(0, 100),
                  ctx: ctx.replace(/\\s+/g, ' ').slice(0, 150),
                  text: (el.innerText || '').trim().slice(0, 70),
                  options: el.tagName === 'SELECT'
                    ? [...el.options].map(o => o.text.trim()) : undefined,
                });
              };
              const walk = (root) => {
                root.querySelectorAll('*').forEach(el => {
                  if (el.matches && el.matches(SEL)) collect(el);
                  if (el.shadowRoot) walk(el.shadowRoot);
                });
              };
              walk(document);
              return out;
            }"""
        )
        (LOGS_DIR / f"discover-{stamp}-controls.json").write_text(
            json.dumps(controls, indent=1))

        print(f"=== {len(controls)} VISIBLE CONTROLS ===")
        for c in controls:
            print(json.dumps({k: v for k, v in c.items() if v}))
        print(f"\nScreenshots + controls JSON saved in logs/ ({stamp}).")
        browser.close()


if __name__ == "__main__":
    main()
