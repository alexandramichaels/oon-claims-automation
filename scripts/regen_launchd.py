"""regen_launchd.py — install/refresh the macOS launchd job.

Usage:
    python regen_launchd.py

Reads the schedule from config/settings.yml, writes a plist to
~/Library/LaunchAgents/, and reloads it via launchctl. The job runs
scripts/run_weekly.sh (which runs orchestrate.py) at each configured slot.
launchd runs a job missed while the laptop was asleep once on the next wake,
so a closed lid is tolerated.

Idempotent — safe to re-run after changing the schedule in settings.yml.
Backs the /regen-launchd slash command.
"""

import subprocess
from pathlib import Path

from _common import LOGS_DIR, REPO_ROOT, load_config

LABEL = "com.example.oon-claims.weekly"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

DAY_TO_WEEKDAY = {"Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4,
                  "Fri": 5, "Sat": 6, "Sun": 7}

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>{script}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>{workdir}</string>
  <key>StartCalendarInterval</key>
  <array>
{intervals}
  </array>
  <key>StandardOutPath</key>
  <string>{stdout}</string>
  <key>StandardErrorPath</key>
  <string>{stderr}</string>
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
"""

INTERVAL_ENTRY = """    <dict>
      <key>Weekday</key>
      <integer>{weekday}</integer>
      <key>Hour</key>
      <integer>{hour}</integer>
      <key>Minute</key>
      <integer>{minute}</integer>
    </dict>"""


def _expand_schedule(schedule: list[dict]) -> list[tuple[int, int, int]]:
    """Expand the (days, times) blocks into individual (weekday, h, m) slots."""
    slots: list[tuple[int, int, int]] = []
    for block in schedule:
        for day_name in block["days"]:
            weekday = DAY_TO_WEEKDAY[day_name]
            for time_str in block["times"]:
                hour_str, minute_str = time_str.split(":")
                slots.append((weekday, int(hour_str), int(minute_str)))
    slots.sort()
    return slots


def main() -> None:
    schedule = load_config()["schedule"]
    slots = _expand_schedule(schedule)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    script = REPO_ROOT / "scripts" / "run_weekly.sh"
    script.chmod(0o755)

    intervals = "\n".join(
        INTERVAL_ENTRY.format(weekday=w, hour=h, minute=m)
        for w, h, m in slots
    )
    plist = PLIST_TEMPLATE.format(
        label=LABEL,
        script=script,
        workdir=REPO_ROOT,
        intervals=intervals,
        stdout=LOGS_DIR / "launchd.out.log",
        stderr=LOGS_DIR / "launchd.err.log",
    )
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(plist)
    print(f"Wrote {PLIST_PATH}")

    subprocess.run(["launchctl", "unload", str(PLIST_PATH)],
                   capture_output=True)
    result = subprocess.run(["launchctl", "load", "-w", str(PLIST_PATH)],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"launchctl load failed: {result.stderr.strip()}")
        raise SystemExit(1)

    loaded = LABEL in subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True).stdout
    days = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    print(f"Loaded: {loaded}. {len(slots)} slot(s):")
    for w, h, m in slots:
        print(f"  {days[w]} {h:02d}:{m:02d}")


if __name__ == "__main__":
    main()
