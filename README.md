# Out-of-Network Claim Auto-Submitter

Files out-of-network insurance claims with Cigna automatically. When a provider
emails a superbill, the system downloads the PDF, fills in Cigna's online claim
form, submits it, records the result, and sends a summary by Telegram.

It is a pure-Python pipeline that runs on a macOS launchd schedule with no one
at the keyboard. All names and addresses in this repository are placeholders.

## What it does

1. Finds new **superbill** emails from Jane (the provider's billing system) in
   Gmail.
2. Downloads each superbill PDF from the practice's Jane portal.
3. Logs into Cigna, including SMS two-factor authentication, and submits each
   superbill as an out-of-network claim.
4. If a session is 5 or more days old with no superbill, drafts a reminder to
   the provider in Gmail Drafts.
5. Sends a Telegram summary of the run, plus an immediate alert for any failure.

## Pipeline

```
Gmail (IMAP)
  ├─ superbill emails ──────► fetch_superbill.py ──► superbills/*.pdf
  └─ appointment reminders                               │
            │                                            ▼
            │                                    cigna_submit.py ──► my.cigna.com
            ▼                                            │
     check for sessions                                  ▼
     with no superbill ──► Gmail draft               state.py (SQLite)
                                                         │
                                                         ▼
                                                 notify.py (Telegram)
```

`scripts/orchestrate.py` is the entry point and runs the whole flow.
`--dry-run` downloads superbills and fills the Cigna form to the review screen
without submitting anything or saving drafts.

## Components

| Script | Role |
|---|---|
| `orchestrate.py` | Runs the workflow end to end. |
| `gmail_client.py` | Gmail over IMAP: finds superbill emails and appointment reminders, saves drafts. |
| `fetch_superbill.py` | Opens a Jane "View Superbill" link with a saved session, downloads the PDF, reads the service date from it. |
| `cigna_submit.py` | Logs into Cigna, uploads the PDF, answers the form, names and submits the claim. |
| `get_2fa_code.py` | Resolves a Cigna verification code. |
| `state.py` | SQLite store of claims, reminders and runs. |
| `notify.py` | Sends Telegram messages. |
| `setup_login.py` | Interactive sign-in that saves a browser session for Jane or Cigna. |
| `doctor.py` | Health check: Keychain items, session age, scheduled job, recent activity. |
| `regen_launchd.py`, `run_weekly.sh` | Install and run the scheduled job. |
| `_common.py` | Paths, config loading, Keychain access, logging, browser stealth config. |

## How it works

**Matching superbills to sessions.** Session dates come from Jane's
appointment-reminder emails. Each superbill's service date is read from the PDF
and matched to a known session. A session with no claim after the configured
number of days triggers a reminder draft.

**No duplicate claims.** `state.db` is keyed by session date. A session marked
`submitted` is skipped on every later run, and already-processed superbill
emails are skipped before a browser is started.

**Cigna sign-in.** A saved browser session is reused. When Cigna requires a
sign-in, the system handles each path it can land on: the public guest page,
the cookie-consent banner, the username step (skipped when Cigna remembers the
account), the password step, and the SMS challenge, where it ticks "remember
this device" so later runs skip two-factor for about 30 days.

**Two-factor codes.** The code is read from the macOS Messages database, which
is polled for 30 seconds to allow for SMS forwarding lag. If none arrives, the
system checks recent email, then asks for the code through the Telegram bot and
waits for a reply.

**Expired sessions.** Cigna can leave an expired session on the form URL
without rendering the form. The system detects the missing upload control,
discards the saved session, signs in fresh and retries once.

**Jane sessions.** Jane's bot check rejects automated sign-ins, so the Jane
session is created by hand with `setup_login.py`. Every run loads the portal
with that session to keep it active, and reports once if Jane has signed it
out.

**Failure handling.** A failed download or submission is recorded, alerted by
Telegram and retried on the next run. Long error messages are truncated to fit
Telegram's message limit. `run_weekly.sh` kills any run that exceeds 15 minutes
so a hung process cannot block later scheduled runs.

**Browser.** Playwright drives Chromium. Cigna pages are loaded with
`playwright-stealth`, configured in one place (`_common.stealth()`) with its
iframe evasion disabled because that patch crashes Cigna's page.

## Maintenance probes

Every selector in the pipeline was captured from the live pages by a probe
script. When a portal changes, the matching probe is re-run and its screenshots
and control dumps (written to `logs/`) are used to update the code.

| Probe | Captures |
|---|---|
| `cigna_discover.py` | The claim form's controls, including those inside shadow DOM. |
| `cigna_login_probe.py` | The Cigna sign-in flow: username, password, two-factor page. |
| `cigna_session_probe.py` | Where a saved Cigna session actually lands. |
| `cigna_onetrust_probe.py` | The cookie-consent banner. |
| `cigna_network_probe.py` | Cigna's home page under several browser and stealth configurations, with console errors and failed requests. |
| `jane_login_probe.py` | The Jane sign-in flow, and whether the saved session is still signed in. |

`CLAUDE.md` records the project's rules and a dated log of each behavioural
change, the failure that prompted it, and the risks it leaves open.

## Setup

Requires macOS (Keychain, launchd, Messages) and Python 3.

```
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp config/settings.example.yml config/settings.yml
```

Edit `config/settings.yml` with your own addresses, portal URL and start date.
It is gitignored.

### 1. Gmail app password

Enable IMAP in Gmail, then create an app password at
<https://myaccount.google.com/apppasswords> (requires 2-Step Verification).
Store it:

```
security add-generic-password -s "oon-claims/gmail-app-password" \
  -a "oon-claims/gmail-app-password" -w 'THE_16_CHAR_APP_PASSWORD'
```

### 2. Cigna and Jane credentials

```
security add-generic-password -s "oon-claims/cigna-username" \
  -a "oon-claims/cigna-username" -w 'YOUR_CIGNA_USERNAME'
security add-generic-password -s "oon-claims/cigna-password" \
  -a "oon-claims/cigna-password" -w 'YOUR_CIGNA_PASSWORD'
security add-generic-password -s "oon-claims/jane-username" \
  -a "oon-claims/jane-username" -w 'YOUR_JANE_USERNAME'
security add-generic-password -s "oon-claims/jane-password" \
  -a "oon-claims/jane-password" -w 'YOUR_JANE_PASSWORD'
```

### 3. Telegram bot

In Telegram, message **@BotFather**, send `/newbot`, and follow the prompts.
Copy the bot token. Send your new bot any message, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the
`chat.id` number. Store both:

```
security add-generic-password -s "oon-claims/telegram-token" \
  -a "oon-claims/telegram-token" -w 'BOT_TOKEN'
security add-generic-password -s "oon-claims/telegram-chat-id" \
  -a "oon-claims/telegram-chat-id" -w 'CHAT_ID'
```

### 4. Two-factor codes from Messages (optional)

- On your iPhone: Settings → Messages → Text Message Forwarding → enable for
  this Mac.
- Grant **Full Disk Access** to your terminal app so the system can read
  `~/Library/Messages`.

Without this, the Telegram bot asks for the code instead.

### 5. Save the browser sessions

Run these in a terminal. Each opens a browser and waits for you to sign in:

```
.venv/bin/python scripts/setup_login.py jane
.venv/bin/python scripts/setup_login.py cigna
```

For Cigna, complete two-factor and tick **"remember this device for 30 days"**.

### 6. Install the schedule

```
.venv/bin/python scripts/regen_launchd.py
```

Run times are set under `schedule` in `config/settings.yml`.

## Usage

```
.venv/bin/python scripts/orchestrate.py            # run now
.venv/bin/python scripts/orchestrate.py --dry-run  # stop at the review screen
.venv/bin/python scripts/doctor.py                 # health check
```

## Privacy and scope

Superbills are protected health information. `superbills/`, `sessions/`,
`state.db`, `logs/` and `config/settings.yml` are gitignored and stay on the
machine. Credentials live in the macOS Keychain, never in the repository.

The system is built for one person automating their own accounts to submit
their own claims.
