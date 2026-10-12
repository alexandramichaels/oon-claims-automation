---
description: Run the weekly oon-claims workflow now — download new superbills, submit Cigna claims, draft nudges for any missing superbills.
---

Run the oon-claims weekly workflow.

1. From the project root, run `.venv/bin/python scripts/orchestrate.py`.
   - If the user asked for a preview/test, add `--dry-run` (downloads superbills
     and fills the Cigna form to the review screen, but does NOT submit claims
     or save nudge drafts).
2. This is the exact script the Monday launchd job runs — running it here just
   does it now. It downloads new superbills, submits Cigna claims, drafts
   nudges for sessions with no superbill, and sends a Telegram summary.
3. Read the script output and give the user a short, plain summary: what was
   submitted, what was drafted, what failed.
4. If it reports a failure:
   - Cigna 2FA / login issue → tell the user to run `/cigna-login`.
   - Jane sign-in issue → tell the user to run `/jane-login`.
   - A single claim failed → tell the user to submit that one manually; the
     tool will retry it next run.

Cigna submissions are real and not easily reversible. If the user is unsure,
suggest a `--dry-run` first.
