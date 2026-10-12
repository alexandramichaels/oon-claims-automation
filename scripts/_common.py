"""Shared helpers: paths, config, Keychain access, logging."""

import datetime as dt
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "settings.yml"
SESSIONS_DIR = REPO_ROOT / "sessions"
SUPERBILLS_DIR = REPO_ROOT / "superbills"
LOGS_DIR = REPO_ROOT / "logs"
STATE_DB = REPO_ROOT / "state.db"
CIGNA_SESSION = SESSIONS_DIR / "cigna.json"

_config_cache = None


def load_config() -> dict:
    """Parse config/settings.yml (cached for the process lifetime)."""
    global _config_cache
    if _config_cache is None:
        with open(CONFIG_PATH) as f:
            _config_cache = yaml.safe_load(f)
    return _config_cache


def keychain(key: str) -> str:
    """Fetch a secret from the macOS Keychain by service name.

    Raises RuntimeError with the exact `add` command if the item is missing,
    so setup failures are self-explanatory.
    """
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", key, "-w"],
            capture_output=True, text=True, check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Keychain item '{key}' not found. Add it with:\n"
            f"  security add-generic-password -s '{key}' -a '{key}' -w 'THE_VALUE'"
        ) from exc
    return out.stdout.rstrip("\n")


def log(msg: str) -> None:
    """Append a timestamped line to today's log file and echo it to stdout."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    line = f"[{dt.datetime.now():%H:%M:%S}] {msg}"
    with open(LOGS_DIR / f"{today}.log", "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def stealth():
    """The playwright-stealth config every browser script must use.

    iframe_content_window is OFF: on 2026-09-15 that evasion's patched
    HTMLIFrameElement getter recursed forever on my.cigna.com ("Maximum call
    stack size exceeded"), crashing Cigna's app into its "Unknown error"
    page. Verified with scripts/cigna_network_probe.py.
    """
    from playwright_stealth import Stealth
    return Stealth(iframe_content_window=False)
