"""cigna_submit.py — submit an out-of-network claim on my.cigna.com.

Logs into Cigna (reusing sessions/cigna.json, handling ~monthly 2FA), opens the
online claim form, uploads the superbill PDF, makes the fixed selections, names
the claim, and submits.

Usage:
    python cigna_submit.py --pdf PATH --session-date YYYY-MM-DD [--dry-run]

    --dry-run  fill everything and screenshot the review screen, but do NOT
               click the final submit button.

Prints a JSON object to stdout:
    {"status": "submitted"|"dry-run"|"failed", "claim_name": "...",
     "screenshot": "...", "detail": "..."}

NOTE ON SELECTORS: the claim-form interactions below use resilient text- and
role-based locators, but the exact Cigna DOM is verified during the one-time
/cigna-login discovery step and recorded in config/cigna_flow.md. If Cigna
changes its form, update the helpers in the "CLAIM FORM" section to match.
"""

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from _common import CIGNA_SESSION, LOGS_DIR, keychain, load_config, log, stealth
from get_2fa_code import get_code


# --------------------------------------------------------------------------
# LOGIN
# --------------------------------------------------------------------------

def _on_login_page(page) -> bool:
    if page.locator("input[type=password]:visible").count():
        return True
    url = page.url.lower()
    # /guest is Cigna's public landing page — served when an unauthenticated
    # request hits a secure URL. There's no password field on it; we have to
    # click 'Log in' to reach the actual sign-in form. See _login below.
    return "login" in url or "logon" in url or "/guest" in url


def _dismiss_onetrust_banner(page) -> None:
    """Dismiss Cigna's OneTrust cookie banner if it appeared.

    Cigna's OneTrust config (captured by cigna_onetrust_probe.py on
    2026-05-29) doesn't include Accept/Reject buttons — the only way to
    dismiss the banner is the close (X) button. The banner's dark-filter
    overlay intercepts clicks on everything underneath it, so any
    interaction with the page (e.g. the /guest 'Log in' button) will
    silently time out until the banner is gone. The banner is
    intermittent — appears on cookie-less / fresh-consent contexts.
    """
    close = page.locator(
        '#onetrust-consent-sdk button.onetrust-close-btn-handler'
        '.banner-close-button')
    if not close.count():
        return
    try:
        close.first.click(timeout=4000)
        log("Dismissed Cigna OneTrust cookie banner.")
        page.wait_for_timeout(800)
    except Exception as exc:
        log(f"WARNING: OneTrust dismiss click failed ({exc}).")


def _click_login_on_guest_page(page) -> None:
    """If we landed on Cigna's /guest page, click 'Log in' to reach the form.

    Cigna doesn't redirect unauthenticated requests straight to a login form;
    it serves a public landing page at /web/public/guest/... with a 'Log in'
    button that navigates to the actual sign-in screen.
    """
    if "/guest" not in page.url.lower():
        return
    _dismiss_onetrust_banner(page)
    log("On Cigna's guest landing page — clicking 'Log in'.")
    page.get_by_role(
        "button", name=re.compile(r"^\s*log ?in\s*$", re.I)
    ).first.click()
    page.wait_for_load_state("load", timeout=30000)
    page.wait_for_timeout(2500)


def _looks_like_2fa(page) -> bool:
    # Strongest signals first: the code-entry input field by name, or the
    # MFA challenge URL Cigna uses (auth0-style universal-login).
    if page.locator(
        'input[name="code"]:visible, '
        'input[autocomplete="one-time-code"]:visible'
    ).count():
        return True
    if "/mfa-" in page.url.lower() or "challenge" in page.url.lower():
        return True
    # Fallback: text on the page. Cover Cigna's current wording
    # ("Verify your identity", "Enter 6-digit code") as well as the older
    # / generic phrasing this used to look for.
    try:
        body = (page.locator("body").inner_text(timeout=3000) or "").lower()
    except Exception:
        return False
    return any(p in body for p in (
        "verify your identity", "we've sent a text message",
        "6-digit code", "verification code", "security code",
        "one-time", "enter the code"))


def _tick_remember_device(page) -> None:
    """Tick any 'remember this device' checkbox so 2FA is skipped ~30 days.

    Cigna custom-styles this checkbox: a <label for="..."> sits on top of
    the real <input>, so Locator.check() fails with "label intercepts
    pointer events." We try the native check first; on failure we click
    the associated label, which toggles the underlying input.
    """
    for cb in page.locator("input[type=checkbox]").all():
        try:
            label_text = (cb.evaluate(
                "el => {"
                "  const id = el.id;"
                "  const lbl = id ? document.querySelector("
                "    'label[for=\"' + CSS.escape(id) + '\"]') : null;"
                "  return (lbl && lbl.innerText) "
                "    || el.closest('label')?.innerText "
                "    || el.parentElement?.innerText || '';"
                "}") or "").lower()
        except Exception:
            label_text = ""
        if "remember" not in label_text and "trust" not in label_text:
            continue
        if cb.is_checked():
            log("'Remember this device' already ticked.")
            return
        try:
            cb.check(timeout=4000)
            log("Ticked 'remember this device'.")
            return
        except Exception:
            pass
        cid = cb.get_attribute("id")
        if cid:
            page.locator(f'label[for="{cid}"]').first.click()
            log("Ticked 'remember this device' via label click.")
            return
        try:
            cb.check(force=True)
            log("Ticked 'remember this device' (force).")
            return
        except Exception as exc:
            log(f"WARNING: could not tick 'remember this device' ({exc}).")
            return


def _login(page) -> None:
    cfg = load_config()["cigna"]
    username = keychain(cfg["keychain_username_key"])
    password = keychain(cfg["keychain_password_key"])
    log("Cigna login required — signing in.")

    _click_login_on_guest_page(page)

    # Cigna's auth0 may skip the identifier step entirely when a partial
    # session cookie remembers who we are — we land directly on
    # /u/login/password with no visible text/email input. Only fill the
    # identifier when it's actually present; otherwise fall through to the
    # password-page handler below.
    ident = page.locator(
        "input[type=text]:visible, input[type=email]:visible, "
        "input[autocomplete='username']:visible")
    if ident.count():
        ident.first.fill(username, timeout=20000)
        pwd = page.locator("input[type=password]:visible")
        if pwd.count():
            pwd.first.fill(password)
        page.get_by_role("button", name=re.compile(
            "log ?in|sign ?in|continue|next", re.I)).first.click()
        page.wait_for_load_state("load", timeout=30000)
        page.wait_for_timeout(2500)
    else:
        log("No identifier field — auth0 remembered us; "
            "jumping straight to password.")

    # Password page (either the second step of an identifier-first flow, or
    # the only step when auth0 skipped the identifier).
    pwd = page.locator("input[type=password]:visible")
    if pwd.count() and not _looks_like_2fa(page):
        pwd.first.fill(password)
        page.get_by_role("button", name=re.compile(
            "log ?in|sign ?in|continue|next", re.I)).first.click()
        page.wait_for_load_state("load", timeout=30000)
        page.wait_for_timeout(2500)

    if _looks_like_2fa(page):
        log("Cigna is asking for a 2FA code.")
        code = get_code(timeout=300)
        code_field = page.locator(
            'input[name="code"]:visible, '
            'input[autocomplete="one-time-code"]:visible, '
            'input[type=tel]:visible, '
            'input[type=text]:visible')
        code_field.first.fill(code)
        _tick_remember_device(page)
        page.get_by_role("button", name=re.compile(
            "verify|submit|continue|confirm", re.I)).first.click()
        page.wait_for_load_state("load", timeout=30000)
        page.wait_for_timeout(2500)

    if _on_login_page(page):
        raise RuntimeError(
            "Cigna sign-in did not complete. Run /cigna-login to refresh the "
            "session interactively.")


# --------------------------------------------------------------------------
# CLAIM FORM  (selectors captured via cigna_discover.py against the live form)
# --------------------------------------------------------------------------

# The four "Answer a few questions" items, by their stable radio-group `name`.
# In every group the "No" option is value="N".
NO_QUESTION_GROUPS = (
    "autoAccidWorkIllnessInj-radio",  # related to an auto accident?
    "workIllnessInj-radio",           # work-related injury?
    "medicareCoverage-radio",         # patient covered by Medicare?
    "otherInsPatientCov-radio",       # other insurance / coverage?
)


def _upload_pdf(page, pdf_path: str) -> None:
    page.locator("input[type=file]").first.set_input_files(pdf_path)
    log("Uploaded superbill PDF; waiting for Cigna to validate it...")
    validating = page.get_by_text(re.compile("validating", re.I)).first
    # Let the "Validating..." indicator appear, then wait for it to clear.
    try:
        validating.wait_for(state="visible", timeout=15000)
    except Exception:
        pass
    try:
        validating.wait_for(state="hidden", timeout=180000)
        log("Cigna finished validating the PDF.")
    except Exception:
        log("WARNING: PDF still shows 'Validating' after the wait window.")
    page.wait_for_timeout(3000)


def _check_radio(page, name: str, value: str) -> None:
    """Select one radio in a group by its stable name + value."""
    radio = page.locator(f'input[name="{name}"][value="{value}"]').first
    radio.wait_for(state="attached", timeout=15000)
    radio.scroll_into_view_if_needed()
    try:
        radio.check(timeout=6000)
    except Exception:
        # Custom-styled radio: click its <label> instead.
        rid = radio.get_attribute("id")
        if rid:
            page.locator(f'label[for="{rid}"]').first.click()
        else:
            radio.check(force=True)
    log(f"Selected radio {name} = {value}")


def _select_claimant(page) -> None:
    """'Who is this claim for?' — pick the patient (the only real option)."""
    select = page.get_by_label(re.compile("who is this claim for", re.I))
    if select.count() == 0:
        select = page.locator("select")
    options = [o for o in select.first.locator("option").all_inner_texts()
               if o.strip().lower() != "select"]
    if options:
        select.first.select_option(label=options[0])
        log(f"Selected claimant: {options[0]}")
    else:
        log("WARNING: no claimant option found to select.")


def _check_certification(page) -> None:
    box = page.get_by_label(re.compile("certify that the information", re.I))
    if box.count() == 0:
        box = page.locator('input[type="checkbox"]')
    if not box.first.is_checked():
        try:
            box.first.check(timeout=6000)
        except Exception:
            box.first.check(force=True)
    log("Ticked the certification checkbox.")


def _set_claim_name(page, name: str) -> None:
    page.locator('input[name="claimSubName"]').first.fill(name)
    log(f"Named the claim: {name}")


def _screenshot(page, tag: str) -> str:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / f"cigna-{tag}-{dt.datetime.now():%Y%m%d-%H%M%S}.png"
    page.screenshot(path=str(path), full_page=True)
    return str(path)


def _claim_name(session_date: str) -> str:
    cfg = load_config()["cigna"]
    mmdd = dt.date.fromisoformat(session_date).strftime("%m/%d")
    return cfg["claim_name_format"].format(date=mmdd)


def _form_ready(page, timeout_ms: int = 5000) -> bool:
    """Probe that the claim form actually rendered.

    Cigna sometimes leaves a soft-expired session on the form URL without a
    login redirect — the page loads, no password field shows up, but the
    form itself never renders. We detect that by waiting briefly for the
    upload input. If it doesn't appear, the saved session is dead and we
    should sign in fresh.
    """
    try:
        page.locator("input[type=file]").first.wait_for(
            state="attached", timeout=timeout_ms)
        return True
    except Exception:
        return False


def _open_form(browser, form_url: str, use_saved_session: bool):
    """Open a context, load the form, and run the login flow if needed.

    Returns (context, page). When use_saved_session is False, the saved
    Cigna session file is deleted first, forcing a clean login.
    """
    if not use_saved_session and CIGNA_SESSION.exists():
        CIGNA_SESSION.unlink()
        log("Discarded the saved Cigna session.")
    storage = (str(CIGNA_SESSION)
               if use_saved_session and CIGNA_SESSION.exists() else None)
    context = browser.new_context(accept_downloads=True, storage_state=storage)
    page = context.new_page()
    page.goto(form_url, wait_until="load", timeout=60000)
    page.wait_for_timeout(2500)
    if _on_login_page(page):
        _login(page)
        context.storage_state(path=str(CIGNA_SESSION))
        page.goto(form_url, wait_until="load", timeout=60000)
        page.wait_for_timeout(2500)
    return context, page


# --------------------------------------------------------------------------
# DRIVER
# --------------------------------------------------------------------------

def submit(pdf_path: str, session_date: str, dry_run: bool = False) -> dict:
    if not Path(pdf_path).exists():
        raise FileNotFoundError(pdf_path)
    cfg = load_config()
    claim_name = _claim_name(session_date)
    form_url = cfg["cigna"]["claim_form_url"]
    payee = cfg["cigna"]["payee"]
    headless = cfg["playwright"]["headless"]

    log(f"Submitting Cigna claim '{claim_name}' (dry_run={dry_run}).")
    with stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(headless=headless)
        context, page = _open_form(browser, form_url, use_saved_session=True)

        if not _form_ready(page):
            log("Claim form did not render — likely a soft-expired Cigna "
                "session. Signing in fresh and retrying once.")
            context.close()
            context, page = _open_form(browser, form_url,
                                       use_saved_session=False)
            if not _form_ready(page):
                raise RuntimeError(
                    "Cigna claim form failed to render even after a fresh "
                    "sign-in. Check my.cigna.com manually.")

        _upload_pdf(page, pdf_path)
        _select_claimant(page)
        for group in NO_QUESTION_GROUPS:
            _check_radio(page, group, "N")
        _check_radio(page, "payee-radio", payee)
        _check_certification(page)
        _set_claim_name(page, claim_name)

        review_shot = _screenshot(page, "review")

        if dry_run:
            context.storage_state(path=str(CIGNA_SESSION))
            browser.close()
            log(f"Dry run — stopped before submit. Review: {review_shot}")
            return {"status": "dry-run", "claim_name": claim_name,
                    "screenshot": review_shot, "detail": "not submitted"}

        page.get_by_role("button", name=re.compile(
            r"submit claim", re.I)).first.click()
        page.wait_for_load_state("load", timeout=45000)
        page.wait_for_timeout(3000)
        confirm_shot = _screenshot(page, "confirmation")
        context.storage_state(path=str(CIGNA_SESSION))
        browser.close()

    log(f"Claim '{claim_name}' submitted. Confirmation: {confirm_shot}")
    return {"status": "submitted", "claim_name": claim_name,
            "screenshot": confirm_shot, "detail": "submitted"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--session-date", required=True,
                        help="ISO date of service, YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = submit(args.pdf, args.session_date, dry_run=args.dry_run)
        print(json.dumps(result))
    except Exception as exc:
        log(f"ERROR: {exc}")
        print(json.dumps({"status": "failed", "detail": str(exc)}))
        sys.exit(1)
