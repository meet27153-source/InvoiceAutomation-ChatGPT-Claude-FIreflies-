# Automated Billing Invoice System

A local, single-user invoice automation application designed to satisfy the supplied SOW:

**Configure accounts once → authenticate once → configure recipients and schedule → automatically find, download, email, and de-duplicate invoices.**

## Important provider note

Billing systems differ by provider and account type. In particular, ChatGPT billing and OpenAI API billing are separate systems. The application therefore requires an account type for OpenAI instead of assuming that every OpenAI account uses the API billing page.

For ChatGPT web subscriptions, OpenAI's current help documentation says past invoices are available through the billing portal reached from ChatGPT Settings/Account/Payment, while API customers use the API Platform billing/history area. Subscriptions purchased through Apple/Google can have a different receipt source.

The browser integrations intentionally do **not** bypass MFA, CAPTCHA, SSO, or other security controls. Initial authentication/re-authentication is performed manually in your existing Chrome session; scheduled runs reuse that signed-in session. An encrypted storage-state backup is also kept locally.

## Architecture

```text
Flask UI
  ├── Accounts
  ├── Settings
  ├── Dashboard
  └── Logs
       │
       ▼
   APScheduler
       │
       ▼
   Orchestrator
       │
       ├── OpenAI / ChatGPT plugin
       ├── Fireflies plugin
       └── Claude / Anthropic plugin
              │
              ▼
       Find latest invoice
              │
              ▼
       Download PDF
              │
              ▼
          SMTP email
              │
              ▼
     Record processed invoice
```

## Project structure

```text
invoice_automation/
  app/
    main.py
    config.py
    db.py
    crypto.py
    email_sender.py
    logging_setup.py
    orchestrator.py
    scheduler.py
    services/
      base.py
      openai_service.py
      fireflies_service.py
      claude_service.py
    web/
      routes.py
      templates/
      static/style.css
  data/       # runtime-only: DB, keys, downloaded invoices
  logs/       # runtime-only: app.log
  requirements.txt
```

## Security

- Passwords and browser session state are encrypted locally with Fernet.
- Encryption keys remain in the local `data/` directory and are gitignored.
- The Flask UI binds to `127.0.0.1` by default.
- Logs redact common password/token/cookie/authorization patterns.
- MFA/CAPTCHA is never bypassed.
- Re-authentication saves a session only after billing access is successfully verified.
- SMTP passwords are encrypted before being stored.

## Installation on Windows

Use Python 3.12 for this project if that is the environment selected for deployment.

```powershell
cd C:\invoice_automation\invoice_automation
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
playwright install chromium
npx --yes @playwright/cli@latest
python -m app.main
```

Open:

```text
http://127.0.0.1:5000
```

## First-time setup

### 1. Add an account

Go to **Accounts → Add account**.

Select the provider and the correct billing/account type. For example:

- OpenAI → ChatGPT web subscription
- OpenAI → OpenAI API / Platform
- Claude → Claude.ai Pro/Team
- Claude → Anthropic API / Console
- Fireflies → Fireflies account

Save the account.

### 2. Authenticate

Click **Authenticate / Re-authenticate**. The application opens a **new tab in your already-running Chrome**. Complete the provider's normal login, including any MFA/SSO steps. The application does not solve or bypass these controls.

The session is saved only after the application verifies that the billing area is accessible.

### 3. Configure email

For Gmail:

```text
SMTP host:       smtp.gmail.com
SMTP port:       587
SMTP username:   your Gmail address
SMTP password:   Google App Password
STARTTLS:        enabled
Sender email:    your Gmail address
```

Use a Google App Password where required; do not use your normal account password in the application.

### 4. Configure recipients

Enter one or more accountant/finance addresses separated by commas.

### 5. Configure schedule

Supported schedules:

- Every N days
- Monthly, day 1–28 at 06:00 local time
- Custom cron expression

### 6. Test

Use **Run now** from the Dashboard. Confirm:

1. Authentication/session works.
2. The billing page is accessible.
3. A new invoice is identified.
4. The PDF is downloaded.
5. SMTP sends the email.
6. The invoice appears under Processed invoices.

## Reliability behavior

- Download and SMTP failures are retried up to `MAX_RETRIES`.
- MFA/CAPTCHA/session problems become `needs_reauth` instead of being bypassed.
- One account failure does not stop other enabled accounts.
- The SQLite unique constraint protects against duplicate processed-invoice records.
- The process lock prevents a manual run and scheduled run from executing simultaneously in the same process.
- The scheduler uses `max_instances=1` and coalescing to avoid overlapping scheduled jobs.

## Running automatically on Windows

The application must be running for APScheduler to execute jobs. To satisfy the SOW's unattended requirement, configure Windows Task Scheduler to start:

```text
C:\invoice_automation\invoice_automation\venv\Scripts\python.exe -m app.main
```

at Windows logon or system startup, using the project directory as the working directory.

## Adding another provider

Create a class in `app/services/` that subclasses `BaseService`, then register it in `SERVICE_REGISTRY`.

Implement:

- `login()`
- `get_latest_invoice()`
- `download_invoice()`
- `login_url`
- `billing_url`

The UI and orchestrator automatically use the registered plugin.

## Provider integration maintenance

Third-party billing pages can change without notice. The service plugins use semantic fallbacks and report an actionable error instead of silently selecting an unrelated document. If a provider redesigns its billing page, update only that provider's plugin rather than changing the database, scheduler, email system, or orchestrator.

## Definition-of-done test

The application should not be considered complete until this sequence succeeds:

```text
Install application
    ↓
Configure account(s) once
    ↓
Authenticate once
    ↓
Configure finance recipient(s)
    ↓
Configure schedule
    ↓
Start application automatically at boot/logon
    ↓
Scheduled run starts
    ↓
Provider session is reused
    ↓
Latest unprocessed invoice is found
    ↓
Invoice PDF is downloaded
    ↓
Invoice is emailed
    ↓
Invoice is recorded as processed
    ↓
Next run does not send the same invoice again
```

## ChatGPT subscription invoice browser setup

The **Authenticate / Re-authenticate** button uses the official **Playwright Extension**
connection. This attaches Playwright to your existing Chrome tabs and reuses the
Chrome profile where you are already signed in, instead of trying to read the
protected default Chrome profile through `DevToolsActivePort`. Playwright documents
extension mode specifically for existing tabs, SSO/2FA, cookies, and authenticated sessions.

### One-time setup on Windows

1. Install Node.js 20+ if it is not already installed.
2. In your normal Chrome, install the official **Playwright Extension** from the
   Chrome Web Store.
3. Make sure the extension is enabled in `chrome://extensions/`.
4. Open your normal Chrome and sign in to the correct ChatGPT account.
5. Keep Chrome running.

You do **not** need to start Chrome with `--remote-debugging-port=9222`, and you do
not need to create a separate automation Chrome profile for this authentication flow.

### Authenticate

Go to **Accounts → Authenticate / Re-authenticate**. The app attaches through the
Playwright Extension and opens a **new ChatGPT tab** in your existing Chrome. Complete
any normal login, SSO, or MFA steps yourself. The app never enters OTPs and never
bypasses CAPTCHA/MFA. Once authentication is detected, the browser state is encrypted
and stored locally for scheduled invoice runs.

Official extension:
https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm

Once the login is detected, the app saves the browser storage state, encrypts it
with the local Fernet key, stores it in SQLite, and closes only the temporary
authentication tab.

### Scheduled invoice runs

Scheduled runs do **not** require your everyday Chrome to remain open. They use the
encrypted session captured during manual authentication in a separate headless
Playwright browser. This makes the scheduler suitable for unattended Windows
Task Scheduler execution.

If authentication cannot start, the Accounts page shows the actual startup error
instead of displaying a false success message.


## ChatGPT browser architecture (v11)

ChatGPT subscription invoice retrieval runs through the official Playwright Extension attachment to the user's existing Chrome session. It does not launch a separate/headless Chromium for ChatGPT. The workflow opens a new tab in the attached Chrome session, navigates the current Settings -> Billing UI, opens Transaction history, downloads the newest invoice/receipt, and closes only the automation tab. If the real Chrome session is not authenticated, the run stops and asks for manual re-authentication rather than attempting to enter credentials or bypass MFA/CAPTCHA.


## v12 fix
Pinned Playwright CLI to 0.1.20 and runs the ChatGPT browser workflow from a temporary JavaScript file instead of a long Windows command-line expression. This fixes the command parsing path that produced `SyntaxError: Unexpected token ')'`.


## v14 fix
The ChatGPT browser runner now has hard per-command timeouts and explicitly kills
the complete Playwright/npx process tree when a Windows command times out. This
prevents an attached-Chrome/Playwright child process from leaving a run stuck in
`Running` indefinitely. ChatGPT attach, new-tab, workflow, cleanup, and detach
operations each have bounded time limits, and the browser workflow uses bounded
load-state and billing waits.

## v15 fix
Chrome attachment is explicitly pinned to the user's `Default` Chrome profile.
This prevents extension-mode attachment from selecting a different profile.

## v16 fix
Removed the unsupported `--profile-dir-name` CLI argument that caused
"Unknown option: --profile-dir-name". The Chrome profile is now selected only
through `PLAYWRIGHT_MCP_PROFILE_DIR_NAME=Default`, while Chrome is selected
through `PLAYWRIGHT_MCP_BROWSER=chrome`.
