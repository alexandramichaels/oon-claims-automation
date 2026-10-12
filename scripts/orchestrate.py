"""orchestrate.py — the weekly oon-claims workflow.

Run on demand or by the Monday launchd job. End to end, with no human in the
loop and no dependency on MCP connectors:

  1. Find new superbill emails from Jane (Gmail IMAP).
  2. Download each superbill PDF (logging into Jane as needed).
  3. Submit each as a Cigna out-of-network claim (handling ~monthly 2FA).
  4. For any session 5+ days old with no superbill, draft a nudge to the provider
     in Gmail Drafts for you to review and send.
  5. Send a Telegram summary.

Usage:
    python orchestrate.py [--dry-run]

    --dry-run  download superbills and fill the Cigna form to the review
               screen, but do not submit claims or save nudge drafts.

Idempotent: state.db is keyed by session date, so reruns never double-submit
a claim or double-draft a nudge.
"""

import argparse
import datetime as dt
import sys

import cigna_submit
import fetch_superbill
import gmail_client
import state
from _common import SESSIONS_DIR, load_config, log
from notify import send as telegram_send


def _today() -> dt.date:
    return dt.date.today()


def _alert(msg: str) -> None:
    """Best-effort immediate Telegram alert. A Telegram hiccup must never
    crash the run, so failures here are only logged."""
    try:
        telegram_send(msg)
    except Exception as exc:
        log(f"WARNING: Telegram alert failed: {exc}")


# Marker so an expired Jane session is announced once, not on every run.
_JANE_WARNED = SESSIONS_DIR / "jane.expired-warned"


def check_jane_session() -> list[str]:
    """Use the saved Jane session on every run (not only when a superbill
    arrives) and warn as soon as Jane stops accepting it."""
    try:
        signed_in = fetch_superbill.keepalive()
    except Exception as exc:
        log(f"WARNING: Jane session check failed: {exc}")
        return []
    if signed_in is None:
        log("Jane session check: no saved session.")
        return []
    if signed_in:
        log("Jane session check: signed in.")
        _JANE_WARNED.unlink(missing_ok=True)
        return []
    log("Jane session check: SIGNED OUT.")
    if _JANE_WARNED.exists():
        return []
    _JANE_WARNED.touch()
    return ["🔐 Jane has signed the tool out. Nothing is waiting yet, but the "
            "next superbill can't be downloaded until you run /jane-login on "
            "your laptop."]


def _resolve_session_date(fetched: dict, appointments: list[str],
                          claimed: set[str]) -> str | None:
    """Decide which session a superbill is for.

    Prefer a PDF date that matches a known appointment; else the PDF's best
    guess; else the earliest appointment that has no claim yet.
    """
    pdf_dates = fetched.get("dates") or []
    for d in pdf_dates:
        if d in appointments:
            return d
    if fetched.get("session_date"):
        return fetched["session_date"]
    unbilled = [a for a in sorted(appointments) if a not in claimed]
    return unbilled[0] if unbilled else None


def process_superbills(appointments: list[str], dry_run: bool) -> list[str]:
    cfg = load_config()
    since = cfg["start_date"]
    lines: list[str] = []

    superbills = sorted(gmail_client.find_superbills(since),
                        key=lambda s: s["received"])
    log(f"Found {len(superbills)} superbill email(s) since {since}.")

    claimed = {a for a in appointments
               if state.claim_status(a) == "submitted"}

    for sb in superbills:
        if not sb["url"]:
            msg = f"Superbill email {sb['received']}: no document link found."
            log(f"WARNING: {msg}")
            lines.append(f"⚠️ {msg}")
            continue
        if state.claim_status_by_email_id(sb["message_id"]) == "submitted":
            log(f"Superbill email {sb['received']} already submitted "
                f"— skipping (no Playwright).")
            continue
        try:
            fetched = fetch_superbill.fetch(sb["url"])
        except Exception as exc:
            log(f"ERROR downloading superbill ({sb['received']}): {exc}")
            if "/jane-login" in str(exc):
                msg = (f"🔐 Claims: Jane has signed the tool out — run "
                       f"/jane-login on your laptop. The superbill from the "
                       f"{sb['received']} email was NOT downloaded; the tool "
                       f"will retry it next run.")
            else:
                msg = (f"⚠️ Claims: could not download the superbill "
                       f"from the {sb['received']} email ({exc}). The claim "
                       f"was NOT submitted — handle it manually.")
            _alert(msg)
            lines.append(msg)
            continue

        session_date = _resolve_session_date(fetched, appointments, claimed)
        if not session_date:
            lines.append(f"⚠️ Downloaded a superbill ({sb['received']}) but "
                          f"could not tell which session it is for — submit "
                          f"this one manually.")
            continue

        status = state.claim_status(session_date)
        if status == "submitted":
            log(f"Claim for {session_date} already submitted — skipping.")
            continue

        try:
            result = cigna_submit.submit(fetched["pdf_path"], session_date,
                                         dry_run=dry_run)
        except Exception as exc:
            log(f"ERROR submitting Cigna claim for {session_date}: {exc}")
            state.record_claim(session_date, "failed",
                               email_id=sb["message_id"],
                               pdf_path=fetched["pdf_path"])
            if "/cigna-login" in str(exc):
                msg = (f"🔐 Claims: Cigna login/2FA expired — run "
                       f"/cigna-login on your laptop. The {session_date} "
                       f"claim was NOT submitted; the tool will retry it "
                       f"next run.")
            else:
                msg = (f"❌ Claims: Cigna submission FAILED for the "
                       f"{session_date} session ({exc}). Submit it manually "
                       f"at my.cigna.com — the tool will also retry it next "
                       f"run.")
            _alert(msg)
            lines.append(msg)
            continue

        claimed.add(session_date)
        if dry_run:
            lines.append(f"🧪 Dry run: prepared claim '{result['claim_name']}' "
                          f"for {session_date} (not submitted).")
            continue

        state.record_claim(session_date, "submitted",
                           email_id=sb["message_id"],
                           pdf_path=fetched["pdf_path"],
                           name=result["claim_name"])
        lines.append(f"✅ Submitted Cigna claim '{result['claim_name']}' "
                      f"for {session_date}.")

    return lines


def check_missing(appointments: list[str], dry_run: bool) -> list[str]:
    cfg = load_config()
    threshold = _today() - dt.timedelta(days=cfg["missing_superbill_days"])
    provider = cfg["gmail"]["provider_first_name"]
    sender = cfg["gmail"]["your_first_name"]
    lines: list[str] = []

    for appt in appointments:
        appt_date = dt.date.fromisoformat(appt)
        if appt_date > threshold:
            continue
        # Any claim row (submitted OR failed) means we already have the
        # superbill — a failed Cigna submission is not a missing superbill.
        if state.claim_status(appt) != "none":
            continue
        if state.nudged(appt):
            continue

        pretty = appt_date.strftime("%B %-d")
        subject = f"Superbill for our session {appt_date.strftime('%-m/%-d')}"
        body = (
            f"Hi {provider},\n\n"
            f"I don't think I got a superbill for our session on {pretty}. "
            f"Could you send it over when you get a chance so I can submit it "
            f"to insurance?\n\n"
            f"Thanks so much!\n"
            f"{sender}\n"
        )
        if dry_run:
            lines.append(f"🧪 Dry run: would draft a nudge for {appt}.")
            continue
        try:
            gmail_client.save_draft(subject, body)
        except Exception as exc:
            log(f"ERROR drafting nudge for {appt}: {exc}")
            lines.append(f"⚠️ Could not draft the nudge for {appt}: {exc}")
            continue
        state.record_nudge(appt)
        lines.append(f"✉️ No superbill for {appt} — drafted a nudge to "
                      f"{provider} in Gmail Drafts. Review and send it.")

    return lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    mode = " (dry run)" if args.dry_run else ""
    log(f"=== oon-claims weekly run{mode} ===")

    lines: list[str] = []
    try:
        appointments = gmail_client.find_appointments(
            load_config()["start_date"])
        log(f"Known sessions since start_date: {appointments or 'none'}")
        lines += check_jane_session()
        lines += process_superbills(appointments, args.dry_run)
        lines += check_missing(appointments, args.dry_run)
    except Exception as exc:
        log(f"FATAL: {exc}")
        try:
            telegram_send(f"❌ Claims run failed: {exc}")
        except Exception:
            pass
        sys.exit(1)

    if not lines:
        lines = ["Nothing to do — no new superbills, no overdue sessions."]
    summary = f"Claims{mode}:\n" + "\n".join(lines)
    log(summary)
    state.record_run(summary.replace("\n", " | "))
    try:
        telegram_send(summary)
    except Exception as exc:
        log(f"WARNING: could not send Telegram summary: {exc}")


if __name__ == "__main__":
    main()
