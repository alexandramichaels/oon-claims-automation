# CLAUDE.md — oon-claims

Orchestration guide for Claude Code sessions in this project. Read this first.

> Published copy: names, addresses and appointment dates are replaced with
> placeholders. The engineering log below is otherwise as written while the
> tool was being built and maintained.

## What this is

A personal automation that submits the owner's out-of-network claims to Cigna
each week. The provider's practice uses Jane (janeapp.com) for billing. See README.md for the
full description and setup.

## Architecture

A pure-Python pipeline. The weekly launchd job runs `scripts/orchestrate.py`
directly — there is **no** dependency on Claude or MCP connectors at run time.

Flow: Gmail (IMAP) → find superbill emails → `fetch_superbill.py` (Jane login,
download PDF) → `cigna_submit.py` (Cigna login + 2FA, submit) → `state.py`
(record) → nudge check against Jane appointment-reminder emails → `gmail_client`
saves a Gmail draft → `notify.py` Telegram summary.

## Scripts (`scripts/`)

- `orchestrate.py` — the weekly workflow. Entry point. `--dry-run` supported.
- `gmail_client.py` — Gmail IMAP: find superbills, find appointments, save drafts.
- `fetch_superbill.py` — follow a Jane "View Superbill" link, log into Jane,
  download the PDF.
- `cigna_submit.py` — log into Cigna, submit the online claim, upload the PDF.
- `get_2fa_code.py` — resolve a Cigna 2FA code (Messages → email → Telegram).
- `setup_login.py jane|cigna` — interactive: save a browser session.
- `cigna_discover.py` — maintenance: dump the Cigna claim form's controls
  (pierces shadow DOM) if the form changes and selectors need re-capturing.
- `state.py` — SQLite state (claims, nudges, runs). Keyed by session date.
- `notify.py` — send a Telegram message.
- `doctor.py` — health check. Backs `/status`.
- `regen_launchd.py` / `run_weekly.sh` — install/run the Monday launchd job.

## Commands (`.claude/commands/`)

`/submit-claims`, `/status`, `/cigna-login`, `/jane-login`, `/regen-launchd`.

## Rules

1. **Cigna submissions are real and not easily reversed.** When in doubt, run
   `orchestrate.py --dry-run` first (fills the form to the review screen, never
   clicks submit).
2. **Never double-submit.** `state.db` is keyed by session date; the orchestrator
   skips any date already marked `submitted`. Do not bypass this.
3. **PHI stays local.** Never commit `superbills/`, `sessions/`, `state.db`, or
   `logs/`. Never print superbill contents or credentials into chat.
4. **Secrets live in the Keychain** under `oon-claims/*`. Never hardcode
   them or write them to files.
5. **Cigna form selectors** in `cigna_submit.py` were captured from the live
   form (the radio groups have stable `name` attributes; the form is in shadow
   DOM, which Playwright locators pierce). If Cigna changes the form, re-run
   `cigna_discover.py` to recapture, update the "CLAIM FORM" helpers, and
   re-test with `--dry-run`. The end-to-end form fill is verified; the final
   "Submit claim" click is exercised on the first real superbill.
6. The claim name format is `<provider> MM/DD` (the session date).
7. **Probe before touching selectors.** Every selector in production code traces
   to a captured probe output. The maintenance probes are:
   `cigna_discover.py` (claim form controls), `cigna_login_probe.py` (login
   flow: identifier → password → 2FA), `cigna_session_probe.py` (what a saved
   `sessions/cigna.json` actually lands on), `cigna_onetrust_probe.py`
   (cookie banner), and `cigna_network_probe.py` (error pages: compares
   browser/stealth configs, dumps console errors and failed requests), and
   `jane_login_probe.py` (Jane sign-in: identifier → password; `--fresh`
   ignores the saved session, `--home` checks whether it is still alive). When Cigna breaks something, re-run the matching probe and
   look at its screenshots + JSON in `logs/` before editing code — don't patch
   from error messages. Probes are permanent tools; if a new flow needs
   observing, add a new probe alongside it.
8. **Rule out our own code before blaming Cigna.** If a run fails on an
   error page or a missing control, check whether my.cigna.com works outside
   the automation (plain configs in `cigna_network_probe.py`, and the owner's
   own browser) before saying Cigna is down. If it works there, the bug is
   ours — keep digging.
9. **All browser scripts use `_common.stealth()`**, never `Stealth()`
   directly, so evasion fixes apply everywhere at once.

## Changes & known risks

A running log of behavioral changes that aren't obvious from the code, plus
failure modes worth knowing about. Append, don't rewrite.

### 2026-05-27 — soft-expiry detection in `cigna_submit.py`

**Why:** Four consecutive polls (5/25–5/27) failed at the upload step with
`Locator.set_input_files: Timeout 30000ms exceeded`. Root cause: Cigna's saved
session was "soft-expired" — Cigna kept the browser on the form URL (no
redirect to login, no visible password field, so `_on_login_page` returned
False) but never rendered the form, so `input[type=file]` never appeared.

**What changed:** After the existing login probe, `submit()` now calls
`_form_ready(page, timeout_ms=5000)`. If the file input doesn't attach within
5s, the tool closes the browser context, deletes `sessions/cigna.json`, opens
a fresh context (forcing Cigna to redirect to login), runs the normal login
flow, and probes once more. If that *also* fails, it raises and the
orchestrator fires the existing Telegram failure alert.

Net effect: the most likely future failure of this pipeline — Cigna deciding
not to trust the saved session — self-heals on the first retry instead of
spamming Telegram on every poll.

**Future risks to watch:**

- **Real form change looks identical to soft expiry.** If Cigna actually
  changes or renames the upload control, every run will (a) re-login
  unnecessarily, then (b) still fail. The alert text will mention "after a
  fresh sign-in" — that's the tell. Fix is to re-run `cigna_discover.py`
  and update the selector.
- **2FA storm on a Cigna outage.** With ~12 polls/week, if Cigna's site is
  broken for a day, the tool re-logs-in on each poll. Could trigger Cigna's
  fraud detection. Low risk; worth knowing.
- **Burned 2FA on a transient blip.** The fresh-login path needs a 2FA code
  every ~30 days (when "remember this device" expires) or whenever Cigna
  decides a re-login looks new. If the hardened path triggers on a slow CDN
  load instead of a real session issue, you may get a Telegram "send the
  code" prompt for nothing.
- **2FA code fetcher has a gap.** `from_gmail` is still one-shot. If a
  Cigna code lands via email instead of SMS, and the email hasn't arrived
  by the time the Messages window closes, the run falls through to the
  Telegram prompt needlessly. Acceptable; not common.
- **Detection heuristic is `input[type=file]`.** Same selector the upload
  uses. Brittle by design — if Cigna swaps in a custom uploader without a
  hidden file input, the heuristic falsely fires "soft expiry" forever.

### 2026-05-27 — Messages polling window in `get_2fa_code.py`

**Why:** `from_messages()` was one-shot. iPhone->Mac SMS forwarding can lag
10–30s, so a run that asked Cigna for a 2FA code milliseconds before the
text actually arrived would miss it and bounce straight to the Telegram
prompt, requiring manual reply.

**What changed:** `get_code()` now polls Messages three times — immediately,
+15s, +30s — before falling through to Gmail and Telegram. Total added
latency: up to 30s on a real 2FA challenge, zero on the common case (the
text is already there at t=0).

**Future risks to watch:**

- **30s isn't always enough.** If Cigna's SMS or Apple's relay is unusually
  slow, the run still falls through to Telegram. The Telegram fallback is
  the real safety net; this just reduces how often it gets hit.
- **Stale codes.** `from_messages` looks at codes received in the last 10
  minutes. If a previous Cigna 2FA challenge fired recently and that older
  code is still in Messages, the polling loop will pick it up immediately
  and try to use a stale code. Hasn't happened in practice (2FA is rare)
  but is a real failure mode if the cadence ever changes.

### 2026-05-27 — handle Cigna `/guest` landing page in `cigna_submit.py`

**Why:** The soft-expiry self-heal (earlier same day) fired on the 18:00
launchd run but then still failed. Manual diagnosis: when Cigna receives an
unauthenticated request for the secure form URL, it doesn't redirect to a
login form — it serves a public landing page at
`/web/public/guest/...` whose only relevant control is a "Log in" button.
`_on_login_page` only detected (a) a visible password field or (b)
`login`/`logon` in the URL. Neither matched, so the orchestrator plowed
into the form check and timed out.

**What changed:**

- `_on_login_page` now also returns True when `/guest` is in the URL.
- New `_click_login_on_guest_page` clicks the "Log in" button and waits for
  the actual sign-in page to load. It's called at the top of `_login`, so
  every login path (initial, post-soft-expiry retry, etc.) handles /guest
  identically.

Captured selector: `get_by_role("button", name=re.compile(r"^\s*log ?in\s*$"))`.
The /guest page has exactly four visible buttons (Español, Log in, Register,
Feedback) so the locator is safe and unique. Source: `cigna_discover.py` run
at 2026-05-27 19:40 with no saved session — see
`logs/discover-20260527-194004-controls.json`.

**Future risks to watch:**

- **Cigna could rename the button.** "Log in" → "Sign in" → "Member sign in"
  etc. The regex tolerates spacing ("Log in" / "Login") but not a different
  verb. If Cigna renames it, every run lands on /guest, fails to click,
  raises, and alerts. Fix is to re-run `cigna_discover.py` and update the
  regex.
- **Login loops if credentials are wrong.** If `_login` clicks Log in →
  enters bad credentials → Cigna bounces back to /guest, the next run
  starts at /guest, clicks Log in, fails again. The existing post-`_login`
  check (`if _on_login_page(page): raise`) still catches this — but now
  /guest counts as a login page, so the error message reads as a sign-in
  failure rather than a generic form failure. Good.
- **Discover script can't reach the form unauthenticated anymore.**
  `cigna_discover.py` doesn't know about /guest — running it without a
  saved session now dumps the /guest page's controls (4 buttons) instead
  of the claim form. Workaround when re-capturing claim-form selectors:
  run `/cigna-login` first so discover starts authenticated.

### 2026-05-27 — recognize "Next" as a sign-in submit button

**Why:** After landing the /guest fix, the first attempt got further but
still failed — the existing `_login` regex (`log ?in|sign ?in|continue`)
didn't match Cigna's actual button label on the username page. They use an
identifier-first flow where the submit button is just **"Next"**.

**What changed:** Widened the button regex in `_login` to
`log ?in|sign ?in|continue|next`. Applied via `replace_all` so all three
occurrences (post-username, post-password second step, post-2FA) match
consistently — Cigna may use "Next" on any of those pages.

Source of the selector: new `scripts/cigna_login_probe.py`, which walks the
flow up to identifier entry and dumps every visible button. Logs from the
2026-05-27 19:58 run show one submit button labeled "Next" with attribute
`name="action"`.

**New tool:** `scripts/cigna_login_probe.py` is a maintenance script
parallel to `cigna_discover.py` — discover captures the *form* state,
probe captures the *login* state. Use probe whenever the login flow
itself changes (button rename, layout overhaul, added pre-login captcha).

**Future risks to watch:**

- **"Next" is generic.** If Cigna adds an unrelated "Next" button to a
  pre-login page (e.g. a marketing carousel "Next slide" control above
  the form), `.first` might match that instead of the submit button.
  Mitigation if it happens: tighten to
  `page.locator('button[type="submit"]').get_by_text(re.compile(...))`
  or match on `name="action"`.
- **Single-page password form is unhandled now.** The previous regex
  handled the case where username + password appear on the same page
  with a single "Sign in" button. The new regex still does — but if
  Cigna ever reverts to that flow *and* labels its single button "Next",
  the code will still click it (and succeed). No regression. Worth
  knowing only if you're chasing a different bug.

### 2026-05-27 — recognize Cigna's MFA-SMS challenge page

**Why:** After fixing /guest and the "Next" button, the flow finally
reached Cigna's 2FA page — and we still failed there. `_looks_like_2fa`
was matching phrases like "verification code" / "enter the code"; Cigna's
page actually says "**Verify your identity**" / "We've sent a text message
to..." / "**Enter 6-digit code**" (no "the"). None matched, so the 2FA
branch never fired, the post-`_login` check saw we were still on a login
URL, and the run raised. Cigna texted a real code — we just never knew to
look for it.

**What changed in `cigna_submit.py`:**

- `_looks_like_2fa` now checks three signals in order:
  1. **Code input by name** — `input[name="code"]` or `input[autocomplete="one-time-code"]`. Cigna uses `name="code"`.
  2. **URL pattern** — `/mfa-` or `challenge` in the URL. Cigna's MFA URL is
     `https://p.universal-login.cigna.com/u/mfa-sms-challenge?...`.
  3. **Body text** — widened to include Cigna's current wording ("verify
     your identity", "we've sent a text message", "6-digit code") plus
     the generic phrases that were already there.
- The 2FA code-field locator now prefers `input[name="code"]` over the
  generic `input[type=text]`, so we can't accidentally fill the code into
  the wrong input on a page that has multiple text fields.

Source: `scripts/cigna_login_probe.py` was extended to walk the full flow
(username → password → 2FA challenge) and snapshot each step. Run at
2026-05-27 20:03 — see `logs/probe-20260527-200351-3-post-password.png`
and `.json`.

**Future risks to watch:**

- **Cigna swaps SMS for a different MFA method by default.** TOTP, email
  code, push notification — each gets a different URL fragment and page
  wording. The URL check (`/mfa-` / `challenge`) is broad enough to catch
  most variants, but the code-entry selector + button regex may not.
  Re-run `cigna_login_probe.py` if 2FA detection ever stops working.
- **Body-text inner_text() returns empty on slow renders.** The 3s timeout
  on `body.inner_text(timeout=3000)` is tight — on a slow page the text
  fallback may miss. The input-by-name and URL checks run first and don't
  have this problem.
- **"Resend" and "Use a different verification method" buttons.** Both are
  `type=submit name=action` on the same page as Continue. Our 2FA submit
  uses `name=re.compile("verify|submit|continue|confirm")` which matches
  "Continue" specifically and ignores the other two. If Cigna ever renames
  Continue to something starting with "Resend" we'd click the wrong button
  — unlikely, but logged here so it's findable.

### 2026-05-27 — click-the-label fallback for 'remember this device'

**Why:** First successful 2FA pickup. Code from Messages worked, was
filled into the form, then `cb.check()` on the "Remember this device for
30 days" checkbox timed out 30s with `<label for="rememberBrowser">
intercepts pointer events`. Cigna's checkbox is custom-styled — the real
`<input>` is hidden, the label is what users actually click. Same pattern
as the radios on the claim form, which already had a label-click fallback.

**What changed in `_tick_remember_device`:** try `check()` first; on
failure, click `label[for="<id>"]`; on second failure, try
`check(force=True)`; on third failure, log a warning and continue
(failure to tick "remember" isn't worth aborting the whole submission —
it just means we'll hit 2FA again sooner).

The label-finder JS now walks `document.querySelector('label[for=...]')`
in addition to `closest('label')` and `parentElement`, since on this
page the label is a sibling, not an ancestor.

### 2026-05-27 — truncate long Telegram messages in `notify.py`

**Why:** The failure alert from the failed remember-device click also
failed with `HTTP Error 400: Bad Request`. Cause: Playwright's full
traceback is multi-kilobyte; embedded in the alert text, it exceeded
Telegram's 4096-char message limit, and Telegram rejected the API call
silently (only the `WARNING: Telegram alert failed` line in the log
hinted at it). Net effect: when a Cigna submission failed with a verbose
error, the user got *no* alert at all — exactly the wrong direction.

**What changed:** `notify.send` truncates anything over 3800 chars and
appends `[...truncated; see logs/ for the full message]`. The full
traceback still lands in the per-day log file as before — we just don't
try to push the whole thing through Telegram.

**Future risks to watch:**

- **HTML escaping in long error strings.** Messages are sent with
  `parse_mode=HTML`. If a Playwright error ever contains literal `<` or
  `>`, Telegram could reject the message before the truncation even
  matters. Hasn't happened yet — Playwright errors are mostly plain text.
- **The 3800 cutoff is conservative.** Real limit is 4096. We left
  ~300 chars of headroom in case Telegram counts emoji or HTML entities
  differently than character length. Safe but not tight.

### 2026-05-29 — skip the identifier step when auth0 remembers us

**Why:** The 15:00 run on 2026-05-29 failed with
`Locator.fill: Timeout 20000ms exceeded` against the username locator,
even though the prior login had succeeded two days earlier. Root cause
captured by a new probe (`cigna_session_probe.py`): when the orchestrator
loads `sessions/cigna.json` and follows /guest → Log in, Cigna's auth0
sometimes skips `/u/login/identifier` and sends the browser straight to
`/u/login/password` — partial-session cookies remember who the user is
but the password is still required. `_login` unconditionally tried to
fill an identifier first, so it waited 20s for a text field that didn't
exist and raised.

This is distinct from the existing soft-expiry self-heal: that path only
triggers when the *form* fails to render, not when login does. Here
login was failing well before the form was reached.

**What changed in `cigna_submit.py`:** `_login` now checks whether an
identifier input is present before trying to fill it. If it's missing,
the function logs "auth0 remembered us; jumping straight to password"
and falls through to the password-page handler (which already existed
as the "second-step password page" branch). The post-`_login` check at
the end of the function still catches a real failure to authenticate.

**New tool:** `scripts/cigna_session_probe.py` — parallel to
`cigna_login_probe.py` but reads `sessions/cigna.json` as
`storage_state` and snapshots whatever page comes out the other side of
/guest → Log in. Use this when the orchestrator fails inside `_login`
but a fresh `cigna_login_probe.py` run succeeds — that's the signature
of a partial-session-cookie issue. The script does NOT mutate
`sessions/cigna.json`.

**Future risks to watch:**

- **Three Cigna login flows now in play.** (a) fresh: identifier → password
  → 2FA; (b) partial: password → 2FA (this fix); (c) full: form renders
  directly with no login. If Cigna adds a fourth (e.g. password-skipped
  but 2FA still required because device-trust expired separately),
  `_login` will fall through both `if ident.count()` and
  `if pwd.count()` blocks and arrive at the 2FA check. If 2FA is required
  it'll handle correctly; if not it'll proceed and the post-`_login`
  check will catch any failure. Probably fine, but worth knowing.
- **Stale session cookies could trap us in a login loop.** If the
  partial session is valid enough to skip the identifier but the
  password we have is wrong, every run will: load form → /guest → Log in
  → password page → fill bad password → bounce → form → /guest → …
  The post-`_login` `_on_login_page(page)` check still catches this on
  the next round-trip and raises, so the alert text will read as a
  sign-in failure. Good.
- **Probe must be re-run if the failure recurs.** The fix assumes
  Cigna's "remembered identifier" cookie behaves consistently. If a
  future failure has the same surface symptom (20s timeout on the
  username locator), run `cigna_session_probe.py` to see what URL we
  actually landed on before assuming this fix still covers it.

### 2026-05-29 — dismiss Cigna's OneTrust cookie banner on /guest

**Why:** After landing the identifier-skip fix earlier the same day, the
first `--dry-run` got further but still failed — this time at the /guest
"Log in" click, with Playwright timing out for 30s because
`<div class="onetrust-pc-dark-filter ot-fade-in">` was intercepting all
pointer events on the page. Cigna's OneTrust cookie consent banner had
appeared and its dark-filter overlay sat on top of the login button.
The banner is intermittent: two probes ~5 minutes earlier never
triggered it; the `--dry-run` at 15:15 did. Cause is the consent-cookie
state of the saved session — sometimes the cookie isn't there or has
expired.

**What changed in `cigna_submit.py`:** new `_dismiss_onetrust_banner`
helper, called at the top of `_click_login_on_guest_page` before the
"Log in" click. Looks for
`#onetrust-consent-sdk button.onetrust-close-btn-handler.banner-close-button`
(the close X — the only dismiss control Cigna's OneTrust config
exposes; there is **no** Accept All or Reject All button in this banner)
and clicks it if present. Silent no-op if the banner isn't there.

Source: new `scripts/cigna_onetrust_probe.py` ran with a cookie-less
context to force the banner to render, then dumped every control under
`#onetrust-consent-sdk`. Output:
`logs/onetrust-20260529-151809.json`. The visible controls are exactly
three: Privacy Policy link, "Cookie Preferences" (opens a modal, not a
dismiss), and the unlabeled close X.

**New tool:** `scripts/cigna_onetrust_probe.py` — parallel to the other
diagnostic probes. Use it if the OneTrust banner ever changes shape
(re-skin, new buttons, different SDK version).

**Future risks to watch:**

- **OneTrust banner could fire on the form page too.** Same domain
  (my.cigna.com), same SDK. If Cigna ever serves the banner on the
  form page directly, the form's file-upload click could fail the
  same way. Easy fix when it happens: also call
  `_dismiss_onetrust_banner(page)` right after `page.goto(form_url)`
  in `_open_form`.
- **OneTrust SDK version change.** Cigna could swap to a newer
  OneTrust deployment that includes Accept/Reject buttons and removes
  the close X, or renames the close-button class. If the dismiss
  silently fails the click times out and we'd see the same
  "intercepts pointer events" error in the next failure. Fix: re-run
  `cigna_onetrust_probe.py` and update the selector.
- **Closing the banner without consent.** Clicking the X dismisses the
  banner without setting any preference, which on the next page load
  means the banner *could* reappear. In practice OneTrust SDKs set a
  short-lived "dismissed" flag and don't re-render in the same
  session. If we ever see the banner re-appear on the password page
  after dismissing it on /guest, we'd need to call the dismiss helper
  again post-navigation.

### 2026-06-14 — watchdog timeout in `run_weekly.sh` (hung-run wedge)

**Why:** The pipeline silently stopped running after 2026-06-08. Diagnosis:
the 12:00 run that day hung on the Gmail IMAP call (it logged "Known sessions
since start_date" — the line right before the email search — and never got to
"Found N superbill emails" or "finished"). The Python process (and its parent
`run_weekly.sh`) stayed alive, blocked in a network read with no socket
timeout, for ~6 days. Because this is a single-instance launchd LaunchAgent,
launchd treats a still-running invocation as in-progress and **skips every
subsequent scheduled slot**. So one hung run wedged the whole job for 6 days
with no alert — every Tue–Fri run 6/9–6/12 was silently never started.
Confirmed via `ps -p <pid> -o etime` (elapsed `05-22:10:00`) and
`launchctl list` showing a live `"PID"`. Fix that unstuck it: `kill` the hung
PIDs, after which launchd's `"PID"` key cleared and the next slot was free.

**What changed in `run_weekly.sh`:** `orchestrate.py` now runs in the
background with a `MAX_RUN_SECS=900` bash watchdog (macOS ships no
`timeout`/`gtimeout`). If the run is still alive after 15 min the watchdog
sends TERM, then KILL 10s later, so the script exits and frees the launchd
slot. 900s clears the worst-case legitimate run (Cigna 2FA Telegram wait is
300s — see `get_2fa_code.py` `from_telegram` default — plus browser work)
with headroom. A watchdog kill is logged as `exit 143/137 — killed by
watchdog`.

**Future risks to watch:**

- **Watchdog kills a legit slow run.** If Cigna 2FA + browser work ever
  legitimately exceeds 15 min, the watchdog will kill it mid-flight. The
  `— killed by watchdog` tell in `launchd.log` distinguishes this from a
  clean exit. Bump `MAX_RUN_SECS` if it ever trips on a real run.
- **Orphaned chromium children.** The watchdog kills the Python PID, not its
  Playwright-spawned browser children; those reparent to init and exit on
  their own. They don't keep `run_weekly.sh` alive, so launchd is freed
  regardless — but a wedged browser could leave stray processes. Not seen yet.
- **Root cause (no IMAP socket timeout) still unaddressed.** The watchdog is a
  catch-all backstop, not a fix for the specific hang. If hangs become
  frequent, set a socket timeout in `gmail_client`'s IMAP connect/login so the
  run fails fast (and alerts) instead of burning the full 15 min every time.
- **A watchdog kill produces no Telegram alert.** The killed Python process
  never reaches `notify.py`, so a chronically-hung pipeline now recovers
  silently rather than spamming — but also without telling you it's
  struggling. Watch `launchd.log` for repeated `killed by watchdog` lines.

### 2026-09-15 — stealth's iframe evasion crashed Cigna; don't nudge for failed submissions

**Why:** The 11:00 run failed at the /guest "Log in" click (30s timeout; the
Playwright call log only said "waiting for", so the button never existed —
not the OneTrust overlay, which reports "intercepts pointer events").
`cigna_session_probe.py` showed `/web/public/guest` rendering Cigna's
**"Unknown error — Please retry or check again later"** page (controls: "Try
again", "Feedback"). A cookie-less Playwright context got the same page.

That was first misdiagnosed as a Cigna outage. It wasn't: my.cigna.com worked
fine in the owner's normal browser. Both failing checks used the same
`Stealth()` patch, so neither could tell an outage from our own bug.
`cigna_network_probe.py` (new) compared four configs on my.cigna.com:

| Config | Result |
|---|---|
| bundled Chromium + Stealth (production) | Unknown error |
| Google Chrome + Stealth | Unknown error |
| bundled Chromium, no Stealth | normal /guest page with "Log in" |
| Google Chrome, no Stealth | normal /guest page with "Log in" |

With Stealth on, the console logged `RangeError: Maximum call stack size
exceeded at HTMLIFrameElement.get` recursing forever — playwright-stealth
2.0.3's `iframe_content_window` evasion patches the iframe getter, and on
Cigna's page that patch calls itself, crashing Cigna's app into its generic
error screen. Why it started on 9/15 after working 9/14 13:57 is unverified;
the likely guess is a Cigna front-end deploy that newly touches iframes. The
one failed request (`ets.f5apis.com`, ERR_ABORTED) appears in all four
configs, so it is not the cause.

**What changed:**

- New `_common.stealth()` returns `Stealth(iframe_content_window=False)`.
  Every browser script (`cigna_submit`, `setup_login`, and all four Cigna
  probes) now uses it instead of `Stealth()`. Probe config
  `bundled+stealth-noiframe` confirmed the normal page, no recursion error,
  and `navigator.webdriver` still hidden.
- Verified end to end: `cigna_session_probe.py` reached
  `/u/login/password`, and `orchestrate.py --dry-run` logged in, uploaded
  the pending superbill, filled every field, and stopped at the review screen
  (`logs/cigna-review-20260915-122731.png`).
- `orchestrate.py` `check_missing`: the same failed run drafted a bogus "no
  superbill for <date>" nudge to the provider although the superbill was downloaded,
  because it only skipped dates with status `submitted`. It now skips any
  date with a claim row (`claim_status != "none"`) — a row is only written
  after a superbill is fetched and matched to a session.

**New tool:** `scripts/cigna_network_probe.py` — loads my.cigna.com under
several browser/stealth configs with no saved session and records the
page, buttons, console errors, and failed requests for each. Run it whenever
Cigna shows an error page, before concluding anything.

**Future risks to watch:**

- **Don't call it an outage from inside Playwright.** Any check that shares
  the production browser config shares its bugs. Before reporting that
  Cigna is down, run `cigna_network_probe.py` (plain configs) and ask
  the owner to load my.cigna.com in their own browser. If a plain browser
  works, the problem is ours.
- **Another stealth evasion could break next.** If a different patch starts
  recursing or crashing, the symptom will look identical ("Unknown error").
  The probe's console dump names the patched getter; disable that one
  evasion in `_common.stealth()` rather than dropping Stealth entirely.
- **Less iframe camouflage.** With the iframe evasion off, a bot check that
  inspects `iframe.contentWindow` could flag us. Not seen; auth0 login and
  the claim form both passed on 9/15.

### 2026-10-04 — Jane session check on every run; honest Jane sign-in error

**Why:** Jane sign-in alerts fired on every run 9/25–10/2 until the owner
signed in by hand. New `jane_login_probe.py --fresh` showed what the
automatic login actually hits: after the password is submitted, Jane lands on
`/auth/failure` — "We couldn't find your profile. This could mean that your
username or password is incorrect." The password page carries an invisible
reCAPTCHA (`recaptcha_challenge_token`). The old error text ("it may be
asking for a verification code") was a guess; no code prompt appears.
Source: `logs/janeprobe-20261004-212113-*`.

The logs also show the automatic Jane login has **never** succeeded: 41
attempts since June, 41 failures. Every recovery was a manual
`setup_login.py jane`. Whether the cause is a wrong Keychain password or
Jane refusing the automated browser is **not established** — the page is
the same either way.

Days a superbill was fetched, from the logs: sessions die somewhere between
7 and 14 days after a manual sign-in (three such cycles in the logs). Until now the session was only
used when a new superbill arrived, about weekly. That pattern fits an idle
timeout of about a week *or* a fixed lifetime of about two weeks; the data
can't tell them apart.

**What changed:**

- `fetch_superbill.keepalive()` loads the portal with the saved session and
  re-saves it. Signed in = `a[href="/logout"]` present on `/account`; signed
  out = lands on `/login`. `orchestrate.check_jane_session()` calls it at
  the start of every run. If the timeout is idle-based, this keeps the
  session alive indefinitely. If Jane has signed us out, the run summary
  says so **once** (marker file `sessions/jane.expired-warned`, cleared on
  the next signed-in check) — before a superbill is waiting, not after.
- `_jane_login` recognises `/auth/failure`, says "Jane rejected the
  automatic sign-in", and saves `logs/jane-signin-<stamp>.png`. The
  Telegram alert now includes that reason.
- Tested by forcing each path with temp session files: signed-out check,
  warn-once, marker clearing, and the rejected sign-in.

**Future risks to watch:**

- **The keepalive may not help.** If Jane's lifetime is fixed, sessions
  will still die about two weeks after each manual sign-in. Check around
  2026-10-18: `grep "Jane session check" logs/*.log`. If it flips to
  SIGNED OUT with daily use, the timeout is absolute.
- **`_needs_signin` counts password fields**, and the signed-in `/account`
  page has three (change-password settings). Not reached today because
  superbill links download directly when signed in; don't reuse it on
  `/account`.
- **`fetch_superbill.py` does not use `_common.stealth()`** (rule 9). Left
  as is: it is the config the saved session has always worked under.

### 2026-10-04 (later) — removed the automatic Jane login

**Why:** The owner confirmed the Keychain Jane password matches the one they
sign in with by hand (hidden-input compare in Terminal; the value was never
shown). So the 41 `/auth/failure` rejections were not a wrong password: Jane
refuses the sign-in from the automated browser. The invisible reCAPTCHA on
the password page is the likely reason, but that part is inferred, not
observed.

**What changed:** `fetch_superbill.fetch()` no longer attempts a login. If
the superbill link lands on the sign-in page it raises "Jane has signed the
tool out. Run the /jane-login command…" straight away. `_jane_login` and
`_looks_like_2fa` are deleted. Tested: signed-out raises with no login
attempt; signed-in still downloads.

Jane recovery is therefore always manual (`setup_login.py jane`, at the
Mac). The every-run session check is what keeps that rare, if it works.

**Future risks to watch:**

- **Do not re-add an automated Jane login or disguise the browser to get
  past the reCAPTCHA.** It is Jane's bot check on a health-records portal;
  the answer is a longer-lived session or a manual sign-in, not evasion.
- **`oon-claims/jane-username` and `jane-password` are now read only by
  `jane_login_probe.py`.** `doctor.py` may still report them as required.

### 2026-10-04 (later still) — observed: Jane's bot check rejects the login

Network capture of a fresh headless sign-in (field names and lengths only):
clicking "Sign In" on `/sign_in` makes the page fetch a reCAPTCHA token and
POST it to `/sessions/assess`; the browser then goes to `/auth/failure`
**without ever POSTing the `sign-in-form` to `/sessions`**. The password is
never sent, so the rejection is the assessment of the browser, not the
credentials. This upgrades the reCAPTCHA explanation from inferred to
observed. The `/sessions/assess` response body was not captured.

The account's Sign-in Settings (`/account#identity_login`) offer only
Username / Password and "Manage Sessions" — no Google sign-in, passkey, or
emailed sign-in link to use as an alternative.

