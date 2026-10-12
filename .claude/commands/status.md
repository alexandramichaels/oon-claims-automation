---
description: Health check for the oon-claims tool — Keychain secrets, browser sessions, the scheduled job, and recent activity.
---

Run `.venv/bin/python scripts/doctor.py` from the project root and present the
output to the user.

If anything shows MISSING or NOT LOADED, tell the user exactly how to fix it:
- Missing Keychain secrets → point to the setup steps in README.md.
- Scheduled job not loaded → run `/regen-launchd`.
- Cigna session ~30 days old → a 2FA prompt is expected on the next run; that
  is normal and the tool handles it.
