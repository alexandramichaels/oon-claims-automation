#!/bin/bash
# run_weekly.sh — invoked by the macOS launchd job every Monday.
# Runs the weekly oon-claims workflow with the project's own venv.
set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1
mkdir -p logs

# Hard cap on a single invocation. On 2026-06-08 a run hung on the Gmail IMAP
# call (no socket timeout) and never exited. launchd treats a still-running
# invocation as in-progress and refuses to start new ones, so the hung process
# silently wedged every scheduled run for ~6 days. This watchdog guarantees no
# run survives past MAX_RUN_SECS, so launchd's next slot always recovers.
# macOS ships no `timeout`/`gtimeout`, hence the hand-rolled watchdog.
# 900s clears the worst-case legitimate run (Cigna 2FA Telegram wait is 300s
# plus browser work) with headroom, while being far below a multi-day wedge.
MAX_RUN_SECS=900

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] launchd: starting orchestrate.py"

  "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/orchestrate.py" &
  PY_PID=$!

  # Watchdog: if orchestrate.py is still alive after MAX_RUN_SECS, kill it
  # (TERM, then KILL) so this script can exit and free the launchd slot.
  (
    sleep "$MAX_RUN_SECS"
    if kill -0 "$PY_PID" 2>/dev/null; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] launchd: WATCHDOG killing orchestrate.py (PID $PY_PID) after ${MAX_RUN_SECS}s"
      kill -TERM "$PY_PID" 2>/dev/null
      sleep 10
      kill -KILL "$PY_PID" 2>/dev/null
    fi
  ) &
  WATCHDOG_PID=$!

  wait "$PY_PID"
  RC=$?

  # orchestrate.py finished on its own — stop the watchdog.
  kill "$WATCHDOG_PID" 2>/dev/null
  wait "$WATCHDOG_PID" 2>/dev/null

  if [ "$RC" -eq 143 ] || [ "$RC" -eq 137 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] launchd: finished (exit $RC — killed by watchdog)"
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] launchd: finished (exit $RC)"
  fi
} >> logs/launchd.log 2>&1
