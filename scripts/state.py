"""SQLite state for the oon-claims tool.

Tracks which superbills have been turned into Cigna claims and which missing
superbills have already been nudged, so reruns never double-submit or
double-nudge. Importable by the other scripts and usable as a CLI by the
`/submit-claims` orchestrator.

CLI:
    python state.py init
    python state.py claim-status   --date YYYY-MM-DD          -> prints none|submitted|failed
    python state.py record-claim   --date YYYY-MM-DD --status submitted \\
                                    [--email-id ID] [--pdf PATH] [--name NAME]
    python state.py nudged         --date YYYY-MM-DD          -> exit 0 if nudged, 1 if not
    python state.py record-nudge   --date YYYY-MM-DD
    python state.py last-run                                  -> prints ISO datetime or empty
    python state.py record-run     --summary "text"
"""

import argparse
import datetime as dt
import sqlite3
import sys

from _common import STATE_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    session_date       TEXT PRIMARY KEY,   -- ISO YYYY-MM-DD (date of service)
    superbill_email_id TEXT,
    pdf_path           TEXT,
    claim_name         TEXT,
    status             TEXT,               -- submitted | failed
    submitted_at       TEXT
);
CREATE TABLE IF NOT EXISTS nudges (
    session_date TEXT PRIMARY KEY,         -- ISO YYYY-MM-DD of the un-billed session
    drafted_at   TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    started_at  TEXT,
    finished_at TEXT,
    summary     TEXT
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(STATE_DB)
    conn.executescript(SCHEMA)
    return conn


def claim_status(session_date: str) -> str:
    """Return 'submitted', 'failed', or 'none' for a given session date."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT status FROM claims WHERE session_date = ?", (session_date,)
        ).fetchone()
    return row[0] if row else "none"


def claim_status_by_email_id(email_id: str) -> str:
    """Return 'submitted', 'failed', or 'none' for a given superbill email id.

    Lets the orchestrator skip already-processed superbill emails before
    booting Playwright — important when polling multiple times a day.
    """
    if not email_id:
        return "none"
    with _conn() as conn:
        row = conn.execute(
            "SELECT status FROM claims WHERE superbill_email_id = ?",
            (email_id,),
        ).fetchone()
    return row[0] if row else "none"


def record_claim(session_date, status, email_id=None, pdf_path=None, name=None):
    """Insert or update the claim record for a session date."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO claims (session_date, superbill_email_id, pdf_path, "
            "claim_name, status, submitted_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_date) DO UPDATE SET "
            "superbill_email_id=excluded.superbill_email_id, "
            "pdf_path=excluded.pdf_path, claim_name=excluded.claim_name, "
            "status=excluded.status, submitted_at=excluded.submitted_at",
            (session_date, email_id, pdf_path, name, status,
             dt.datetime.now().isoformat(timespec="seconds")),
        )


def nudged(session_date: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM nudges WHERE session_date = ?", (session_date,)
        ).fetchone()
    return row is not None


def record_nudge(session_date: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO nudges (session_date, drafted_at) VALUES (?, ?)",
            (session_date, dt.datetime.now().isoformat(timespec="seconds")),
        )


def last_run() -> str:
    """ISO timestamp of the most recent recorded run, or '' if never run."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT finished_at FROM runs ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
    return row[0] if row and row[0] else ""


def record_run(summary: str) -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    with _conn() as conn:
        conn.execute(
            "INSERT INTO runs (started_at, finished_at, summary) VALUES (?, ?, ?)",
            (now, now, summary),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="oon-claims state store")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    p = sub.add_parser("claim-status")
    p.add_argument("--date", required=True)

    p = sub.add_parser("record-claim")
    p.add_argument("--date", required=True)
    p.add_argument("--status", required=True, choices=["submitted", "failed"])
    p.add_argument("--email-id")
    p.add_argument("--pdf")
    p.add_argument("--name")

    p = sub.add_parser("nudged")
    p.add_argument("--date", required=True)

    p = sub.add_parser("record-nudge")
    p.add_argument("--date", required=True)

    sub.add_parser("last-run")

    p = sub.add_parser("record-run")
    p.add_argument("--summary", required=True)

    args = parser.parse_args()

    if args.cmd == "init":
        _conn().close()
        print(f"Initialized {STATE_DB}")
    elif args.cmd == "claim-status":
        print(claim_status(args.date))
    elif args.cmd == "record-claim":
        record_claim(args.date, args.status, args.email_id, args.pdf, args.name)
        print(f"Recorded {args.status} claim for {args.date}")
    elif args.cmd == "nudged":
        sys.exit(0 if nudged(args.date) else 1)
    elif args.cmd == "record-nudge":
        record_nudge(args.date)
        print(f"Recorded nudge for {args.date}")
    elif args.cmd == "last-run":
        print(last_run())
    elif args.cmd == "record-run":
        record_run(args.summary)
        print("Recorded run")


if __name__ == "__main__":
    main()
