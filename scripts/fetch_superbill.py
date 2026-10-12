"""fetch_superbill.py — download a superbill PDF from a Jane "View Superbill" link.

Superbill emails from Jane (notifications@janeapp.com) contain a "View
Superbill" button, not a PDF attachment. The button is a tracking redirect
that lands on a Jane `secure_documents` page — and that page requires signing
in to the practice's Jane portal. So this script:

  1. opens the link in a browser, reusing sessions/jane.json if present;
  2. if Jane asks for sign-in, stops and asks for /jane-login (Jane rejects
     automated sign-ins, so the session can only be refreshed by hand);
  3. downloads the superbill PDF into superbills/;
  4. reads candidate service dates out of the PDF.

Usage:
    python fetch_superbill.py "<view-superbill-url>"

Prints a JSON object to stdout:
    {"pdf_path": "...", "dates": ["YYYY-MM-DD", ...], "session_date": "YYYY-MM-DD"|null}

If the saved session has expired, the script exits non-zero with a message
telling you to run the /jane-login command.
"""

import datetime as dt
import json
import re
import sys
from pathlib import Path

import pdfplumber
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from _common import SUPERBILLS_DIR, SESSIONS_DIR, load_config, log

JANE_SESSION = SESSIONS_DIR / "jane.json"

_MONTHS = ("january february march april may june july august september "
           "october november december").split()
_NUMERIC = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")
_WORDY = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\.?\s+(\d{1,2}),?\s+(\d{4})\b", re.I)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _safe_iso(year: int, month: int, day: int) -> str:
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return ""


def _parse_dates(text: str) -> list[str]:
    """Pull every recognisable date from PDF text, as ISO strings, deduped."""
    found: set[str] = set()
    for y, m, d in _ISO.findall(text):
        found.add(_safe_iso(int(y), int(m), int(d)))
    for mo, d, y in _NUMERIC.findall(text):
        year = int(y) + 2000 if len(y) == 2 else int(y)
        found.add(_safe_iso(year, int(mo), int(d)))
    for name, d, y in _WORDY.findall(text):
        found.add(_safe_iso(int(y), _MONTHS.index(name.lower()) + 1, int(d)))
    return sorted(x for x in found if x)


def _pick_session_date(dates: list[str]) -> str | None:
    """Best single guess for the date of service: the latest non-future date."""
    today = dt.date.today().isoformat()
    past = [d for d in dates if d <= today]
    return (past or dates or [None])[-1]


def _dismiss_cookie_banner(page) -> None:
    for name in ("Essential only", "Allow all", "Accept"):
        try:
            btn = page.get_by_role("button", name=name)
            if btn.count():
                btn.first.click(timeout=3000)
                return
        except Exception:
            pass


def _needs_signin(page) -> bool:
    if page.locator("input[type=password]").count():
        return True
    body = (page.locator("body").inner_text(timeout=5000) or "").lower()
    return "sign in before continuing" in body or "sign in" in body[:400]


def _goto_or_download(page, url: str, pdf_path: Path) -> bool:
    """Open `url`. When signed in, Jane serves the superbill as a direct file
    download — capture and save it, return True. When not signed in, the URL
    loads the sign-in HTML page instead — return False."""
    try:
        with page.expect_download(timeout=15000) as dl_info:
            try:
                page.goto(url, wait_until="load", timeout=60000)
            except PWError as exc:
                # goto raises this exact error when the response is a download.
                if "Download is starting" not in str(exc):
                    raise
        dl_info.value.save_as(str(pdf_path))
        return True
    except PWTimeout:
        return False


def _download_pdf(page, context, pdf_path: Path) -> None:
    # 1) An explicit download/print control on the secure_documents page.
    for name in ("Download", "Download PDF", "Print", "Save"):
        for role in ("link", "button"):
            loc = page.get_by_role(role, name=re.compile(name, re.I))
            if loc.count() == 0:
                continue
            try:
                with page.expect_download(timeout=15000) as dl:
                    loc.first.click()
                dl.value.save_as(str(pdf_path))
                return
            except Exception:
                continue

    # 2) The page may itself serve a PDF — fetch the bytes directly.
    try:
        resp = context.request.get(page.url)
        body = resp.body()
        ctype = resp.headers.get("content-type", "")
        if "pdf" in ctype.lower() or body[:5] == b"%PDF-":
            pdf_path.write_bytes(body)
            return
    except Exception:
        pass

    # 3) Last resort: render the visible page to PDF.
    page.pdf(path=str(pdf_path), format="Letter", print_background=True)


def keepalive() -> bool | None:
    """Visit the portal with the saved session and report whether Jane still
    treats us as signed in. Returns None when there is no saved session.

    Signed in, login_url lands on /account, which has a "Sign Out" link
    (a[href="/logout"]); signed out, it lands on /login. Captured 2026-10-04
    — see logs/janeprobe-*-home-*.
    """
    if not JANE_SESSION.exists():
        return None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(JANE_SESSION))
        page = context.new_page()
        page.goto(load_config()["jane"]["login_url"], wait_until="load",
                  timeout=60000)
        page.wait_for_timeout(2000)
        signed_in = page.locator('a[href="/logout"]').count() > 0
        if signed_in:
            context.storage_state(path=str(JANE_SESSION))
        browser.close()
    return signed_in


def fetch(url: str) -> dict:
    SUPERBILLS_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    pdf_path = SUPERBILLS_DIR / f"superbill-{stamp}.pdf"

    log("Fetching superbill from Jane link...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            accept_downloads=True,
            storage_state=str(JANE_SESSION) if JANE_SESSION.exists() else None,
        )
        page = context.new_page()

        downloaded = _goto_or_download(page, url, pdf_path)
        if not downloaded:
            _dismiss_cookie_banner(page)
            if _needs_signin(page):
                # No automatic sign-in: Jane rejects it from this browser even
                # with the correct password (see CLAUDE.md, 2026-10-04).
                browser.close()
                raise RuntimeError(
                    "Jane has signed the tool out. Run the /jane-login "
                    "command to refresh the session interactively.")
            # Signed in, but the link rendered a page instead of a file.
            _download_pdf(page, context, pdf_path)

        context.storage_state(path=str(JANE_SESSION))
        browser.close()

    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        raise RuntimeError("Superbill download produced an empty file.")

    text = ""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            text = "\n".join(pg.extract_text() or "" for pg in pdf.pages)
    except Exception as exc:
        log(f"WARNING: could not read PDF text: {exc}")

    if "sign in" in text.lower()[:400]:
        raise RuntimeError(
            "Downloaded file is the Jane sign-in page, not a superbill. "
            "Run /jane-login to refresh the session.")

    dates = _parse_dates(text)
    result = {
        "pdf_path": str(pdf_path),
        "dates": dates,
        "session_date": _pick_session_date(dates),
    }
    log(f"Saved superbill -> {pdf_path.name} "
        f"({pdf_path.stat().st_size} bytes); dates found: {dates or 'none'}")
    return result


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print('Usage: python fetch_superbill.py "<view-superbill-url>"')
        sys.exit(2)
    try:
        print(json.dumps(fetch(sys.argv[1])))
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
