---
description: Refresh the saved Jane portal session by signing in interactively.
---

The tool downloads superbills by signing into the practice's Jane portal. If a
run reported a Jane sign-in failure, the saved session needs refreshing.

This step opens a real browser and waits for a keypress, so it must run in the
user's own Terminal — not through this chat. Tell the user to run:

```
cd "$(pwd)" && .venv/bin/python scripts/setup_login.py jane
```

Explain: a browser window opens at the Jane portal; they sign in by hand; then
they return to the Terminal and press Enter to save the session to
`sessions/jane.json`.
