"""cigna_network_probe.py — why does Cigna show "Unknown error" to us?

Loads my.cigna.com under several browser configurations and records, for
each one: the final URL, the visible page text, a screenshot, and every
network request that failed or returned HTTP >= 400. No saved session is
used and nothing is filled in. Compare the configs to see whether the
failure follows the browser build, the stealth patch, or the network.

Configs:
  bundled+stealth  — what cigna_submit.py uses (Playwright Chromium + Stealth)
  bundled-plain    — Playwright Chromium, no Stealth
  chrome+stealth   — the installed Google Chrome, with Stealth
  chrome-plain     — the installed Google Chrome, no Stealth
  bundled+stealth-noiframe — Stealth with the iframe evasion switched off

Usage:
    python scripts/cigna_network_probe.py [config ...]
Output: logs/netprobe-<stamp>-<config>.png / .json
"""

import datetime as dt
import json
import sys

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from _common import LOGS_DIR, log

URL = "https://my.cigna.com/"
CONFIGS = {
    "bundled+stealth": {"channel": None, "stealth": True},
    "bundled-plain": {"channel": None, "stealth": False},
    "chrome+stealth": {"channel": "chrome", "stealth": True},
    "chrome-plain": {"channel": "chrome", "stealth": False},
    # Stealth minus the iframe.contentWindow evasion, which recursed
    # infinitely on Cigna's page on 2026-09-15 (see CLAUDE.md).
    "bundled+stealth-noiframe": {"channel": None, "stealth": True,
                                 "stealth_kw": {"iframe_content_window": False}},
}


def _run(p, name, cfg, stamp):
    kw = {"headless": False}
    if cfg["channel"]:
        kw["channel"] = cfg["channel"]
    browser = p.chromium.launch(**kw)
    context = browser.new_context()
    page = context.new_page()
    bad, console = [], []

    def on_response(r):
        if r.status >= 400:
            bad.append({"status": r.status, "method": r.request.method,
                        "url": r.url[:300]})

    def on_failed(req):
        bad.append({"status": "FAILED", "method": req.method,
                    "url": req.url[:300], "error": req.failure})

    page.on("response", on_response)
    page.on("requestfailed", on_failed)
    page.on("console", lambda m: m.type in ("error", "warning")
            and console.append(f"{m.type}: {m.text[:300]}"))

    try:
        page.goto(URL, wait_until="load", timeout=60000)
    except Exception as exc:
        log(f"  goto error: {exc}")
    page.wait_for_timeout(8000)

    body = ""
    try:
        body = page.locator("body").inner_text(timeout=5000)[:1500]
    except Exception:
        pass
    buttons = [b.strip() for b in page.get_by_role("button").all_inner_texts()
               if b.strip()][:20]
    img = LOGS_DIR / f"netprobe-{stamp}-{name}.png"
    page.screenshot(path=str(img), full_page=True)
    out = {
        "config": name,
        "browser_version": browser.version,
        "user_agent": page.evaluate("navigator.userAgent"),
        "webdriver": page.evaluate("navigator.webdriver"),
        "url": page.url,
        "unknown_error_page": "unknown error" in body.lower(),
        "buttons": buttons,
        "body": body,
        "bad_requests": bad,
        "console": console[:40],
    }
    (LOGS_DIR / f"netprobe-{stamp}-{name}.json").write_text(
        json.dumps(out, indent=1))
    log(f"  {name}: v{browser.version} webdriver={out['webdriver']} "
        f"unknown_error={out['unknown_error_page']} buttons={buttons} "
        f"bad_requests={len(bad)}")
    browser.close()


def main():
    names = sys.argv[1:] or list(CONFIGS)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log(f"=== Cigna network probe ({stamp}) ===")
    for name in names:
        cfg = CONFIGS[name]
        if cfg["stealth"]:
            stealth = Stealth(**cfg.get("stealth_kw", {}))
            with stealth.use_sync(sync_playwright()) as p:
                _run(p, name, cfg, stamp)
        else:
            with sync_playwright() as p:
                _run(p, name, cfg, stamp)
    log("=== Done. ===")


if __name__ == "__main__":
    main()
