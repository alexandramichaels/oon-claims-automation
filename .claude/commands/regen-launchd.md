---
description: Install or refresh the weekly macOS launchd job that runs the oon-claims workflow every Monday.
---

Run `.venv/bin/python scripts/regen_launchd.py` from the project root.

This writes the launchd plist to `~/Library/LaunchAgents/` and loads it. The job
runs `scripts/run_weekly.sh` (the orchestrator) on the schedule in
`config/settings.yml` (default: Monday 9:00 AM).

After running, confirm to the user whether the job loaded, and on what
schedule. Run this again any time the schedule in `settings.yml` changes.
