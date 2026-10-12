---
description: Refresh the saved Cigna session by signing in interactively (do this when 2FA is due).
---

Cigna challenges 2FA roughly every 30 days. When a run reports a Cigna login or
2FA failure, refresh the saved session by signing in by hand once.

This step opens a real browser and waits for a keypress, so it must run in the
user's own Terminal — not through this chat. Tell the user to run:

```
cd "$(pwd)" && .venv/bin/python scripts/setup_login.py cigna
```

Explain clearly:
1. A browser window opens at my.cigna.com — they sign in by hand.
2. When 2FA is prompted, they complete it AND tick **"remember this device for
   30 days"** — this is what lets the next ~month of automated runs skip 2FA.
3. They return to the Terminal and press Enter to save the session to
   `sessions/cigna.json`.
