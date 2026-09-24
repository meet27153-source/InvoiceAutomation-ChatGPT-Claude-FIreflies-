from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import time
from pathlib import Path

from app.config import DATA_DIR
from app.crypto import encrypt
from app.db import set_account_needs_reauth, update_account_session
from app.logging_setup import log

SESSION_NAME = "invoice-automation-auth"
AUTH_TIMEOUT_SECONDS = 300
POLL_SECONDS = 4


def _prepare_node_environment() -> dict[str, str]:
    """Make npm/npx usable on Windows when the user npm directory is missing."""
    env = os.environ.copy()
    if os.name == "nt":
        appdata = Path(env.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        user_npm = appdata / "npm"
        user_npm.mkdir(parents=True, exist_ok=True)
        env.setdefault("NPM_CONFIG_PREFIX", str(user_npm))
        env.setdefault("npm_config_prefix", str(user_npm))
        parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
        if str(user_npm) not in parts:
            env["PATH"] = str(user_npm) + os.pathsep + env.get("PATH", "")
    return env


def _cli_command() -> list[str]:
    env = _prepare_node_environment()
    env["PLAYWRIGHT_MCP_PROFILE_DIR_NAME"] = "Default"
    env["PLAYWRIGHT_MCP_BROWSER"] = "chrome"
    for candidate in ("playwright-cli", "playwright-cli.cmd"):
        exe = shutil.which(candidate, path=env.get("PATH"))
        if exe:
            return [exe]
    npx = shutil.which("npx", path=env.get("PATH")) or shutil.which("npx.cmd", path=env.get("PATH"))
    if npx:
        return [npx, "--yes", "@playwright/cli@0.1.20"]
    raise RuntimeError("Node.js/npm was not found. Install Node.js 20+ and restart the app.")


def _run_cli(*args: str, timeout: int = 60) -> str:
    """Run one Playwright CLI command with a hard timeout and process-tree cleanup.

    The previous implementation used subprocess.run(timeout=...), but on Windows
    an npx/node child could survive the timeout and leave the automation looking
    permanently "Running". We explicitly kill the whole process tree now.
    """
    env = _prepare_node_environment()
    cmd = _cli_command() + [f"-s={SESSION_NAME}"] + list(args)
    log.info("Running Playwright browser command: %s", " ".join(cmd[:5]))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(DATA_DIR),
        creationflags=creationflags,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    creationflags=creationflags,
                )
            except Exception:
                pass
        else:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        raise RuntimeError(
            f"Playwright CLI command timed out after {timeout}s: "
            f"{' '.join(cmd[:4])}"
        )
    if proc.returncode != 0:
        detail = (stderr or stdout or "Playwright CLI command failed").strip()
        raise RuntimeError(detail[-2000:])
    return (stdout or "").strip()


def _probe_page() -> tuple[str, str]:
    code = '''async page => JSON.stringify({url: page.url(), text: (await page.locator("body").innerText()).slice(0, 12000)})'''
    output = _run_cli("--raw", "run-code", code, timeout=30)
    try:
        data = json.loads(output)
        return str(data.get("url", "")), str(data.get("text", ""))
    except Exception:
        return "", output


_LOGIN_LINE = re.compile(
    r"^(log in|sign in|sign up|create account|continue with (google|email))$", re.I | re.M
)


def _is_authenticated(url: str, text: str) -> bool:
    # Empty url means _probe_page failed -- never treat that as logged in.
    # Match login prompts only as whole lines, so logged-in pages that merely
    # mention "sign in" somewhere are not rejected.
    lower_url = url.lower()
    blocked_url = any(x in lower_url for x in ("auth.openai.com", "accounts.google.com", "/login", "/log-in", "/signup", "/sign-up", "/sign-in"))
    return bool(url) and not blocked_url and not _LOGIN_LINE.search(text)


def _save_state(path: Path) -> dict:
    _run_cli("state-save", str(path), timeout=60)
    if not path.exists():
        raise RuntimeError("Authentication state was not created by Playwright CLI.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Saved browser state could not be read: {type(exc).__name__}") from exc


PLAYWRIGHT_EXTENSION_URL = (
    "https://chromewebstore.google.com/detail/playwright-extension/"
    "mmlmfjhmonkocbjadbfplnigmagldckm"
)


def start_manual_auth(account: dict) -> None:
    """Attach to the user's real Chrome through the official Playwright Extension.

    Chrome 136+ protects the default Chrome profile from traditional remote
    debugging switches. Extension mode is designed for automating already-open
    tabs and reusing the real signed-in browser session without a separate
    automation profile.
    """
    try:
        _run_cli("attach", "--extension=chrome", timeout=90)
    except RuntimeError as exc:
        import webbrowser
        detail = str(exc)
        detail_lower = detail.lower()
        if "extension" in detail_lower or "no browser" in detail_lower or "connect" in detail_lower:
            try:
                webbrowser.open(PLAYWRIGHT_EXTENSION_URL)
            except Exception:
                pass
            raise RuntimeError(
                "Could not connect to Chrome through the Playwright Extension. "
                "Install the official Playwright Extension in Chrome, make sure "
                "it is enabled for this profile, keep Chrome open, and click "
                "Authenticate / Re-authenticate again.\n\n"
                f"Underlying error: {detail}"
            ) from exc
        raise

    if account["service"] == "openai" and account.get("account_type") == "chatgpt":
        start_url = "https://chatgpt.com/"
    else:
        from app.services import get_service_class
        cls = get_service_class(account["service"])
        start_url = cls(None, account.get("username", ""), "", None, account.get("account_type", "default")).billing_url
    _run_cli("tab-new", start_url, timeout=60)
    log.info("Opened authentication tab for account id %s.", account["id"])


def finish_manual_auth(account: dict) -> None:
    state_path = Path(DATA_DIR) / f"auth-state-{account['id']}.json"
    deadline = time.monotonic() + AUTH_TIMEOUT_SECONDS
    try:
        authenticated = False
        while time.monotonic() < deadline:
            try:
                url, text = _probe_page()
                if _is_authenticated(url, text):
                    authenticated = True
                    break
            except Exception as exc:
                log.debug("Auth probe waiting: %s", type(exc).__name__)
            time.sleep(POLL_SECONDS)
        if not authenticated:
            raise RuntimeError("Login was not detected within 5 minutes. Finish login in the new Chrome tab and click Authenticate / Re-authenticate again.")
        state = _save_state(state_path)
        update_account_session(account["id"], encrypt(json.dumps(state)))
        set_account_needs_reauth(account["id"], False)
        log.info("Authentication state saved for account id %s.", account["id"])
    finally:
        try:
            if state_path.exists():
                state_path.unlink()
        except OSError:
            pass
        try:
            _run_cli("tab-close", timeout=10)
        except Exception:
            pass
        try:
            _run_cli("detach", timeout=10)
        except Exception:
            pass


SCRIPT_TEMPLATE = r"""async page => {
  const sleep = ms => page.waitForTimeout(ms);

  await page.waitForLoadState('domcontentloaded', {timeout:10000}).catch(() => {});
  await sleep(1800);

  // ---- Auth check (browser-side; document only exists inside evaluate) ----
  const authCheck = await page.evaluate(() => {
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    return {url: location.href, text: clean(document.body ? document.body.innerText : '')};
  });
  if (/auth\.openai\.com|accounts\.google\.com|\/log-?in|\/sign-?up|\/sign-?in/i.test(authCheck.url) ||
      /^(log in|sign in|sign up|create account)$/im.test(authCheck.text)) {
    return {ok:false, kind:'reauth', message:'The attached Chrome session is not authenticated for this service.', url:authCheck.url};
  }

  // ---- Find the newest dated invoice/transaction row (browser-side, polls
  // internally via setTimeout so this is a single Node<->browser round trip) ----
  const found = await page.evaluate(async () => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    const visible = el => {
      if (!el) return false;
      const s = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
    };
    const dateRe = /\b(?:\d{1,2}\/\d{1,2}\/20\d{2}|[A-Z][a-z]{2,8}\s+\d{1,2},\s+20\d{2})\b/;
    const end = Date.now() + 15000;
    while (Date.now() < end) {
      for (const el of [...document.querySelectorAll('a,button,[role="button"],tr,li,div')]) {
        if (!visible(el)) continue;
        const text = clean(el.innerText || '');
        if (!dateRe.test(text) || text.length > 500) continue;
        if (!/paid|invoice|receipt|\$|usd|transaction/i.test(text)) continue;
        el.setAttribute('data-automation-row', '1');
        return {text};
      }
      await sleep(400);
    }
    return null;
  });

  if (!found) {
    const bodyText = await page.evaluate(() => {
      const clean = s => (s || '').replace(/\s+/g, ' ').trim();
      return clean(document.body ? document.body.innerText : '');
    });
    return {ok:false, kind:'transaction_not_found', message:'Billing page loaded, but no dated invoice/transaction row was found.', url: page.url(), text: bodyText.slice(-6000)};
  }

  await page.locator('[data-automation-row="1"]').first().click({timeout: 5000}).catch(() => {});
  await sleep(1200);

  // ---- Find the download control (browser-side); either return a direct
  // PDF href (Node fetches it) or tag the element for a Node-side click
  // (needed so page.waitForEvent('download') can be armed before clicking) ----
  const control = await page.evaluate(() => {
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    const visible = el => {
      if (!el) return false;
      const s = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
    };
    const dateRe = /\b(?:\d{1,2}\/\d{1,2}\/20\d{2}|[A-Z][a-z]{2,8}\s+\d{1,2},\s+20\d{2})\b/;
    let el = document.querySelector('[data-testid="download-invoice-receipt-pdf-button"]');
    let source = 'testid';
    if (!el || !visible(el)) {
      // Row-scoped match first: Claude.ai invoice rows expose a bare "View"
      // button (no href) that opens the Stripe hosted invoice via JS. The
      // generic matcher below never matches plain "View", so look inside the
      // tagged row for it (or for a Stripe anchor) before falling back.
      const row = document.querySelector('[data-automation-row="1"]');
      const pool = row ? [row, ...row.querySelectorAll('a,button,[role="button"]')] : [];
      const rowEl = pool.filter(e => e.matches('a,button,[role="button"]') && visible(e)).find(e => {
        const t = clean(e.innerText || e.getAttribute('aria-label') || '');
        const h = e.getAttribute('href') || '';
        return /(?:invoice|pay)\.stripe\.com/i.test(h) || /^view(?: invoice| receipt)?$/i.test(t);
      });
      if (rowEl) { el = rowEl; source = 'row'; }
    }
    if (!el || !visible(el)) {
      source = 'fallback';
      // Scope the fallback to inside the Settings/Billing dialog only. An
      // unscoped document-wide search can match unrelated page chrome --
      // in practice it matched a sidebar chat conversation link titled
      // "Invoice automation tools" (ChatGPT's own conversation URLs look
      // like /c/<uuid>), because its title text happened to start with
      // "Invoice". Restricting the search root avoids that class of match
      // entirely, and the href check below is a second, explicit guard.
      const scopeRoot = document.querySelector('[role="dialog"], [aria-modal="true"]') || document;
      const els = [...scopeRoot.querySelectorAll('a,button,[role="button"]')].filter(visible);
      el = els.find(e => {
        const text = clean(e.innerText || e.getAttribute('aria-label') || '');
        const href = e.getAttribute('href') || '';
        if (/^\/c\/[0-9a-f-]{10,}/i.test(href)) return false; // never a ChatGPT conversation link
        return /\.pdf(?:$|[?#])/i.test(href) ||
               (/download/i.test(text) && /invoice|receipt|pdf/i.test(text)) ||
               /^(invoice|receipt|download)/i.test(text) ||
               /invoice\.stripe\.com/i.test(href) ||
               /^open invoice|^view invoice|^download invoice/i.test(text);
      });
    }
    if (!el) return null;
    const bodyText = clean(document.body ? document.body.innerText : '');
    const href = el.getAttribute('href') || '';
    // Stripe hosted-invoice URLs end in a per-invoice token; use it as a
    // stable id (same scheme as ClaudeService: "stripe:<last path segment>").
    const stripeSeg = (href.match(/(?:invoice|pay)\.stripe\.com\/[^?#]*\/([^/?#]+)/) || [])[1];
    // Require a digit: the old pattern (/inv[-_ ]?\w{3,}/i) matched the plain
    // word "Invoice"/"Invoices", giving every invoice the same external_id,
    // so dedupe reported "already emailed" forever after the first success.
    const external = (bodyText.match(/\b(?:in_[A-Za-z0-9]{8,}|inv[-_ ]?(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{3,})\b/i) || [])[0]
      || (stripeSeg ? 'stripe:' + stripeSeg : null);
    const date = (bodyText.match(dateRe) || [])[0] || null;
    const debugInfo = {
      debugTag: el.tagName,
      debugHref: href.slice(0, 200),
      debugText: clean(el.innerText || el.getAttribute('aria-label') || '').slice(0, 120),
      debugSource: source,
      debugTestid: el.getAttribute('data-testid') || null
    };
    if (href && !/^javascript:/i.test(href) && /\.pdf(?:$|[?#])/i.test(href)) {
      return {mode:'href', href, external, date, ...debugInfo};
    }
    if (el.tagName === 'A' && /invoice\.stripe\.com/i.test(href) && href) {
      // Handle this by opening a fresh tab directly (see below) rather than
      // clicking this link and hoping window.open()'s popup survives
      // Chrome's popup blocker -- opening our own tab in the same
      // authenticated context is simpler and can't be blocked, since it
      // isn't a window.open() call triggered from page script at all.
      return {mode:'stripe_invoice', href, external, date, ...debugInfo};
    }
    el.setAttribute('data-automation-download', '1');
    return {mode:'click', external, date, ...debugInfo};
  });

  if (!control) {
    const elementDump = await page.evaluate(() => {
      const clean = s => (s || '').replace(/\s+/g, ' ').trim();
      const visible = el => {
        if (!el) return false;
        const s = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
      };
      const scopeRoot = document.querySelector('[role="dialog"], [aria-modal="true"]') || document;
      const els = [...scopeRoot.querySelectorAll('a,button,[role="button"]')].filter(visible);
      return els.slice(0, 40).map(e => {
        const text = clean(e.innerText || '');
        const aria = clean(e.getAttribute('aria-label') || '');
        const href = e.getAttribute('href') || '';
        const testid = e.getAttribute('data-testid') || '';
        return `[${e.tagName}${testid ? ' testid='+testid : ''}] text="${text.slice(0,40)}" aria="${aria.slice(0,40)}" href="${href.slice(0,60)}"`;
      });
    }).catch(() => []);
    return {
      ok:false,
      kind:'download_not_found',
      message:'Transaction opened, but no invoice/receipt download control was found. '
        + `Visible clickable elements in scope: ${elementDump.join(' || ') || 'none found'}`,
      url: page.url()
    };
  }

  // ---- Actual download (Node-side: page.request / page.waitForEvent only
  // exist here, never inside evaluate) ----
  // ---- Claude.ai: a bare "View" button (no href) opens the Stripe hosted
  // invoice via window.open. Click it, read the URL the new tab lands on,
  // close that tab, and hand the URL to the stripe_invoice branch below,
  // which opens it in a tab we control (so no popup-blocker dependence). ----
  if (control.mode === 'click' && control.debugSource === 'row') {
    const before = page.url();
    const newTab = page.context().waitForEvent('page', {timeout: 15000}).catch(() => null);
    await page.locator('[data-automation-download="1"]').first().click({timeout: 5000}).catch(() => {});
    const tab = await newTab;
    let landed = '';
    if (tab) {
      await tab.waitForLoadState('domcontentloaded', {timeout: 15000}).catch(() => {});
      if (!/^https?:/i.test(tab.url())) {
        await tab.waitForURL(/^https?:/i, {timeout: 10000}).catch(() => {});
      }
      landed = tab.url();
      await tab.close().catch(() => {});
    } else {
      await sleep(2000);
      if (page.url() !== before) landed = page.url();
    }
    if (!/(?:invoice|pay)\.stripe\.com/i.test(landed)) {
      return {
        ok:false,
        kind:'download_failed',
        message:`Clicked the invoice row's "View" control but it did not open a Stripe invoice (landed on "${landed || 'nothing'}").`,
        url: page.url()
      };
    }
    const seg = (landed.match(/(?:invoice|pay)\.stripe\.com\/[^?#]*\/([^/?#]+)/) || [])[1];
    control.mode = 'stripe_invoice';
    control.href = landed;
    control.external = control.external || (seg ? 'stripe:' + seg : null);
    control.date = control.date || ((found.text.match(/\b(?:\d{1,2}\/\d{1,2}\/20\d{2}|[A-Z][a-z]{2,8}\s+\d{1,2},\s+20\d{2})\b/) || [])[0] || null);
  }

  let pdf_b64 = null;

  // Hoisted so both the 'href' fast-path and the two click-based branches
  // below can all use it. Watches network responses for a PDF payload
  // rather than trusting a URL to contain ".pdf" literally -- Stripe-hosted
  // PDFs commonly serve from paths like ".../pdf?s=ap" (a path *segment*,
  // no dot), which a naive URL check misses. It also matches PDFs served
  // through S3 presigned URLs with content-type "application/octet-stream"
  // (the real filename only shows up in Content-Disposition or the query
  // string's "filename=...pdf"), which a bare "application/pdf" check
  // misses -- that mismatch was the root cause of the invoice-download
  // failures where the S3 response for the PDF bytes flew right by
  // unnoticed and the workflow timed out despite the file already having
  // been fetched.
  const capturePdfResponse = (pg, timeoutMs) => {
    let settled = false;
    let onResponse;
    const responsePromise = new Promise(resolve => {
      onResponse = async (response) => {
        if (settled) return;
        try {
          const headers = response.headers();
          const ct = (headers['content-type'] || '').toLowerCase();
          const cd = (headers['content-disposition'] || '').toLowerCase();
          const url = response.url();
          const looksLikePdf =
            ct.includes('application/pdf') ||
            ((ct.includes('application/octet-stream') || ct.includes('binary/octet-stream')) &&
              (/\.pdf(?:["'?]|$)/i.test(cd) ||
               /\.pdf(?:$|[?#])/i.test(url) ||
               /filename[^;]*=.*\.pdf/i.test(decodeURIComponent(url))));
          if (looksLikePdf) {
            let body;
            try {
              body = await response.body();
            } catch (e) {
              return; // response body was already gone; let another response try
            }
            if (!settled) { settled = true; resolve(body); }
          }
        } catch (e) { /* ignore and keep listening */ }
      };
      pg.on('response', onResponse);
    });
    const timeoutPromise = pg.waitForTimeout(timeoutMs).then(() => null);
    return Promise.race([responsePromise, timeoutPromise]).finally(() => {
      settled = true;
      pg.off('response', onResponse);
    });
  };

  // Some "download" buttons trigger a native browser file download (e.g. an
  // <a download> element) instead of, or in addition to, a plain network
  // response Playwright can see as a "response" event with a PDF content
  // type. This captures that case by reading the downloaded file's bytes.
  const captureDownloadBytes = (pg, timeoutMs) => {
    return pg.waitForEvent('download', {timeout: timeoutMs}).then(async (download) => {
      try {
        const stream = await download.createReadStream();
        if (!stream) return null;
        const chunks = [];
        for await (const chunk of stream) chunks.push(chunk);
        return Buffer.concat(chunks);
      } catch (e) {
        return null;
      }
    }).catch(() => null);
  };

  // Waits on several "did we get the file" signals at once (a PDF-typed
  // network response, a native download event) and resolves as soon as any
  // of them actually produces bytes, rather than racing them naively (a
  // naive Promise.race can resolve to null just because one signal happened
  // to time out before the other one succeeded).
  const firstNonNull = (promises) => new Promise((resolve) => {
    let remaining = promises.length;
    for (const p of promises) {
      p.then((value) => {
        remaining -= 1;
        if (value) resolve(value);
        else if (remaining === 0) resolve(null);
      }).catch(() => {
        remaining -= 1;
        if (remaining === 0) resolve(null);
      });
    }
  });

  if (control.mode === 'href') {
    const response = await page.request.get(new URL(control.href, page.url()).href);
    if (!response.ok()) return {ok:false, kind:'download_failed', message:`Invoice PDF request returned HTTP ${response.status()}.`, url: page.url()};
    const bytes = await response.body();
    pdf_b64 = Buffer.from(bytes).toString('base64');

  } else if (control.mode === 'stripe_invoice') {
    // Open a fresh tab directly in Node, in the SAME authenticated browser
    // context (so ChatGPT/Stripe cookies still apply), and navigate straight
    // to the Stripe hosted invoice URL. This sidesteps Chrome's popup
    // blocker entirely -- we are not triggering window.open() from page
    // script and hoping it survives, we are opening the tab ourselves via
    // Playwright's own API, which is not subject to popup blocking at all.
    const stripePage = await page.context().newPage();
    const seenResponses = [];
    const diagListener = (response) => {
      try {
        seenResponses.push({
          url: response.url().slice(0, 200),
          status: response.status(),
          contentType: (response.headers()['content-type'] || '').slice(0, 60)
        });
      } catch (e) { /* ignore */ }
    };
    stripePage.on('response', diagListener);

    let pdfBytes = null;
    let popupDiag = 'no click attempt was made';
    try {
      await stripePage.goto(control.href, {waitUntil:'load', timeout:20000}).catch(() => {});
      // Give the page's own initial load a moment to already be the PDF
      // (some Stripe invoice links serve the PDF directly on navigation).
      pdfBytes = await capturePdfResponse(stripePage, 8000);
      if (!pdfBytes) {
        // Otherwise this is Stripe's interactive hosted invoice viewer (a
        // client-rendered SPA built from custom elements / shadow DOM).
        // A plain `document.querySelectorAll` inside page.evaluate() does
        // NOT pierce shadow roots, so it can silently fail to find controls
        // like "Download invoice" that live inside a component's shadow
        // tree -- the search finds nothing, nothing gets clicked, and the
        // whole wait window elapses for no reason. Playwright's own
        // locators (getByText, locator().filter()) pierce open shadow DOM
        // automatically, so use those instead to find and click the control.
        const pdfPromise = capturePdfResponse(stripePage, 18000);
        const downloadPromise = captureDownloadBytes(stripePage, 18000);
        // The click commonly opens the PDF in a NEW TAB using Chrome's
        // built-in PDF viewer. That viewer does its own fetching on the
        // popup page object, not on stripePage -- so listeners attached
        // only to stripePage (above) never see that traffic at all, no
        // matter how long they wait. Watch for the popup separately.
        let popupPage = null;
        const popupWaiter = stripePage.waitForEvent('popup', {timeout: 20000})
          .then(p => { popupPage = p; return p; })
          .catch(() => null);

        const candidates = [
          stripePage.getByText('Download invoice', {exact: false}),
          stripePage.getByText('Download receipt', {exact: false}),
          stripePage.locator('button, a, [role="button"]').filter({hasText: /download|invoice|receipt|pdf/i}),
        ];
        let clicked = false;
        for (const loc of candidates) {
          try {
            const first = loc.first();
            // waitFor gives the SPA time to finish rendering the control
            // (it may not exist in the DOM/shadow tree yet on first check).
            await first.waitFor({state: 'visible', timeout: 8000});
            await first.click({timeout: 5000});
            clicked = true;
            break;
          } catch (e) {
            // control not found/visible within the wait window; try the next candidate
          }
        }

        if (clicked) {
          // Chrome's PDF viewer, once navigated, shows the *actual* PDF
          // resource URL on that popup page (Playwright sees the real
          // navigated URL, not an internal viewer/extension URL), even
          // though it may redirect once or twice getting there. Rather
          // than trying to sniff the viewer's own internal fetch traffic,
          // just wait for it to settle on a URL and GET that URL directly
          // with an authenticated request -- the same approach already
          // used for the plain 'href' fast-path above.
          const popupFetchPromise = (async () => {
            const popup = popupPage || await popupWaiter;
            if (!popup) {
              popupDiag = 'no popup tab was opened within 20s of the click';
              return null;
            }
            popupDiag = `popup opened, url="${popup.url().slice(0,200)}"`;
            try {
              await popup.waitForLoadState('load', {timeout: 10000}).catch(() => {});
              await popup.waitForTimeout(1500); // let any redirect chain finish
              popupDiag = `popup settled at url="${popup.url().slice(0,200)}"`;
              const resp = await popup.request.get(popup.url());
              popupDiag += `, direct GET returned HTTP ${resp.status()}`;
              if (resp.ok()) return await resp.body();
            } catch (e) {
              popupDiag += `, direct GET threw: ${String(e && e.message || e).slice(0,200)}`;
            } finally {
              await popup.close().catch(() => {});
            }
            return null;
          })();

          pdfBytes = await firstNonNull([pdfPromise, downloadPromise, popupFetchPromise]);
        }
      }
    } finally {
      stripePage.off('response', diagListener);
    }

    if (!pdfBytes) {
      // Scoped to just this Stripe tab's own text -- never the ChatGPT
      // page's sidebar/chat history, unlike an earlier version of this
      // diagnostic which accidentally dumped the whole document.body.
      const stripePageText = await stripePage.evaluate(() => {
        const clean = s => (s || '').replace(/\s+/g, ' ').trim();
        return clean(document.body ? document.body.innerText : '');
      }).catch(() => '');
      const respSummary = seenResponses.slice(-10)
        .map(r => `${r.status} ${r.contentType || '(no content-type)'} ${r.url}`)
        .join(' | ') || 'no responses observed';
      await stripePage.close().catch(() => {});
      return {
        ok:false,
        kind:'download_failed',
        message:'Opened the Stripe hosted invoice page directly, but no PDF response was detected. '
          + `Popup/download-tab diagnostics: ${popupDiag}. `
          + `Stripe page text (first 800 chars): "${stripePageText.slice(0,800)}". `
          + `Recent responses on that tab: ${respSummary}`,
        url: control.href
      };
    }
    await stripePage.close().catch(() => {});
    pdf_b64 = Buffer.from(pdfBytes).toString('base64');

  } else {
    // Generic fallback for a plain in-page download button/link that isn't
    // a Stripe hosted invoice (kept for other providers).
    const seenResponses = [];
    const diagListener = (response) => {
      try {
        seenResponses.push({
          url: response.url().slice(0, 200),
          status: response.status(),
          contentType: (response.headers()['content-type'] || '').slice(0, 60)
        });
      } catch (e) { /* ignore */ }
    };
    page.on('response', diagListener);

    await page.evaluate(() => { window.__printCalled = false; window.print = () => { window.__printCalled = true; }; });

    const downloadPromise = page.waitForEvent('download', {timeout:15000}).catch(() => null);
    const popupPromise = page.waitForEvent('popup', {timeout:15000}).catch(() => null);
    const mainPagePdfPromise = capturePdfResponse(page, 15000);

    await page.evaluate(() => { window.__navMarker = 'still-here'; });

    await page.waitForTimeout(600); // let any modal backdrop fade-out transition finish
    const controlLocator = page.locator('[data-automation-download="1"]').first();
    if (!(await controlLocator.count())) {
      return {ok:false, kind:'download_failed', message:'The tagged download control disappeared from the DOM before it could be clicked.', url: page.url()};
    }
    await controlLocator.scrollIntoViewIfNeeded().catch(() => {});
    await controlLocator.click({ timeout: 10000 });

    const download = await downloadPromise;
    if (download) {
      page.off('response', diagListener);
      await download.saveAs(%DEST%);
    } else {
      const [popup, mainPagePdf] = await Promise.all([popupPromise, mainPagePdfPromise]);
      let pdfBytes = mainPagePdf;

      if (!pdfBytes && popup) {
        await popup.waitForLoadState('load', {timeout:15000}).catch(() => {});
        pdfBytes = await capturePdfResponse(popup, 4000);
        if (!pdfBytes) {
          const popupPdfPromise = capturePdfResponse(popup, 18000);
          const clickedInPopup = await popup.evaluate(async () => {
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            const clean = s => (s || '').replace(/\s+/g, ' ').trim();
            const visible = el => {
              if (!el) return false;
              const s = getComputedStyle(el);
              const r = el.getBoundingClientRect();
              return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
            };
            const looksLikeDownload = e => {
              const text = clean(e.innerText || '');
              const aria = clean(e.getAttribute('aria-label') || '');
              const title = clean(e.getAttribute('title') || '');
              const href = e.getAttribute('href') || '';
              return /download|invoice|receipt|pdf|print/i.test(text) ||
                     /download|invoice|receipt|pdf/i.test(aria) ||
                     /download|invoice|receipt|pdf/i.test(title) ||
                     /\.pdf(?:$|[?#])/i.test(href);
            };
            const end = Date.now() + 15000;
            while (Date.now() < end) {
              const els = [...document.querySelectorAll('a,button,[role="button"]')].filter(visible);
              const el = els.find(looksLikeDownload);
              if (el) { el.click(); return true; }
              await sleep(500);
            }
            return false;
          }).catch(() => false);
          if (clickedInPopup) {
            pdfBytes = await popupPdfPromise;
          }
        }
        await popup.close().catch(() => {});
      }

      if (!pdfBytes) {
        const printWasCalled = await page.evaluate(() => window.__printCalled).catch(() => false);
        if (printWasCalled) {
          try {
            const cdp = await page.context().newCDPSession(page);
            const { data } = await cdp.send('Page.printToPDF', { printBackground: true });
            pdfBytes = Buffer.from(data, 'base64');
          } catch (e) { /* fall through to normal failure reporting below */ }
        }
      }

      page.off('response', diagListener);

      if (!pdfBytes) {
        // Scoped to the dialog/modal text only (not the whole page) so this
        // never leaks unrelated page content like a sidebar's chat history.
        const pageState = await page.evaluate(() => {
          const clean = s => (s || '').replace(/\s+/g, ' ').trim();
          const dlg = document.querySelector('[role="dialog"], [aria-modal="true"]');
          return {
            dialogText: dlg ? clean(dlg.innerText).slice(0, 800) : null,
            navMarkerSurvived: window.__navMarker === 'still-here'
          };
        }).catch(() => ({ dialogText: null, navMarkerSurvived: false }));
        const respSummary = seenResponses.slice(-10)
          .map(r => `${r.status} ${r.contentType || '(no content-type)'} ${r.url}`)
          .join(' | ') || 'no responses observed';
        const ctrl = `tag=${control.debugTag || '?'} testid=${control.debugTestid || 'none'} source=${control.debugSource || '?'} text="${control.debugText || ''}" href="${control.debugHref || ''}"`;
        return {
          ok:false,
          kind:'download_failed',
          message:'Clicked the download control, but no download event or PDF response (application/pdf) was detected within 15s. '
            + `Matched control: ${ctrl}. `
            + (pageState.navMarkerSurvived ? '' : 'The page appears to have fully reloaded/navigated after the click. ')
            + (pageState.dialogText ? `Dialog/modal text: "${pageState.dialogText}". ` : 'No dialog/modal element found. ')
            + `Recent responses: ${respSummary}`,
          url: page.url()
        };
      }
      pdf_b64 = Buffer.from(pdfBytes).toString('base64');
    }
  }
  return {
    ok:true,
    pdf_b64,
    external_id: control.external || ('entry:' + found.text.replace(/[^A-Za-z0-9]+/g,'-').replace(/^-|-$/g,'').slice(0,100)),
    date: control.date,
    transaction: found.text.slice(0,500),
    url: page.url()
  };
}"""


def _downloads_dir() -> Path:
    """Best-effort guess at the OS default Downloads folder for this user."""
    return Path.home() / "Downloads"


def _snapshot_pdfs(dir_path: Path) -> set[str]:
    try:
        return {p.name for p in dir_path.iterdir() if p.suffix.lower() == ".pdf"}
    except OSError:
        return set()


def _wait_for_new_pdf(dir_path: Path, before: set[str], timeout: float = 15.0) -> Path | None:
    """Poll the Downloads folder for a PDF that wasn't there before.

    Playwright is only ATTACHED to the user's real, already-running Chrome
    for this workflow (via the extension), not launched/controlled by it.
    In that mode Chrome's native download manager frequently completes a
    click's resulting download entirely on its own -- saving straight to
    disk -- without ever surfacing as a Playwright "download" event or as
    an interceptable network response inside the browser-automation
    script at all. So even when that script reports failure, the file may
    already be sitting in Downloads; this checks for that before giving up.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = _snapshot_pdfs(dir_path)
        fresh = current - before
        if fresh:
            candidate = dir_path / sorted(fresh)[-1]
            last_size = -1
            for _ in range(20):
                try:
                    size = candidate.stat().st_size
                except OSError:
                    size = -1
                if size > 0 and size == last_size:
                    break
                last_size = size
                time.sleep(0.3)
            return candidate
        time.sleep(0.5)
    return None


def run_invoice_cli(account: dict, dest_path: str) -> dict:
    """Download the latest invoice for OpenAI/ChatGPT, Claude, or Fireflies
    using the already authenticated Chrome tab through Playwright Extension mode.
    """
    from pathlib import Path as _Path
    from app.services import get_service_class

    dest = _Path(dest_path).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    service_cls = get_service_class(account["service"])
    billing_url = service_cls(
        None, account.get("username", ""), "", None, account.get("account_type", "default")
    ).billing_url

    try:
        _run_cli("attach", "--extension=chrome", timeout=20)
    except RuntimeError as exc:
        raise RuntimeError(
            "Could not attach to your existing Chrome. Keep normal Chrome open, "
            "make sure the Playwright Extension is installed/enabled, and run "
            "Authenticate / Re-authenticate once."
        ) from exc

    try:
        downloads_dir = _downloads_dir()
        downloads_before = _snapshot_pdfs(downloads_dir)

        _run_cli("tab-new", billing_url, timeout=20)

        script = SCRIPT_TEMPLATE.replace("%DEST%", json.dumps(str(dest)))

        script_path = Path(DATA_DIR) / f"invoice_workflow_{account['id']}.js"
        script_path.write_text(script, encoding="utf-8")
        try:
            raw = _run_cli("--raw", "run-code", f"--filename={script_path}", timeout=70)
        finally:
            try:
                script_path.unlink()
            except OSError:
                pass
        try:
            result = json.loads(raw)
        except Exception as exc:
            raise RuntimeError(f"Browser workflow returned invalid diagnostic output: {raw[-2000:]}") from exc

        if not result.get("ok"):
            # The browser-side script may report failure even though Chrome
            # already completed the download natively straight to disk (see
            # _wait_for_new_pdf's docstring). Check Downloads before giving up.
            recovered = _wait_for_new_pdf(downloads_dir, downloads_before, timeout=15.0)
            if recovered is not None:
                try:
                    recovered_name = recovered.name
                    data = recovered.read_bytes()
                    dest.write_bytes(data)
                    try:
                        recovered.unlink()
                    except OSError:
                        pass
                    log.info(
                        "Recovered invoice PDF for account id %s from the Downloads folder "
                        "after the browser workflow reported failure.", account["id"],
                    )
                    # Match the shape of the normal success result (external_id,
                    # date, transaction) so callers that key off those fields
                    # don't KeyError -- we don't have the page-scraped values
                    # from a failed run, so fall back to the recovered
                    # filename, which for these invoices already encodes the
                    # invoice number (e.g. "Invoice-ENDPX7FE-0010.pdf").
                    stem = Path(recovered_name).stem
                    result = {
                        "ok": True,
                        "url": result.get("url"),
                        "external_id": f"recovered-{stem}",
                        "date": None,
                        "transaction": stem,
                        "recovered_from_downloads_folder": True,
                    }
                except OSError:
                    recovered = None

            if recovered is None:
                kind = result.get("kind", "failed")
                message = result.get("message", "Invoice workflow failed.")
                if kind == "reauth":
                    raise RuntimeError(
                        f"{service_cls.display_name} needs re-authentication in your normal Chrome. "
                        "Open the site, sign in manually, then run Authenticate / Re-authenticate."
                    )
                raise RuntimeError(message + (f" Current URL: {result.get('url','')}" if result.get('url') else ""))

        if result.get("pdf_b64"):
            import base64
            dest.write_bytes(base64.b64decode(result["pdf_b64"]))

        if not dest.exists() or dest.stat().st_size < 1000:
            raise RuntimeError("The workflow reported a download, but no valid PDF was created.")
        return result
    finally:
        try:
            _run_cli("tab-close", timeout=10)
        except Exception:
            pass
        try:
            _run_cli("detach", timeout=10)
        except Exception:
            pass

# Backward-compatible name: older orchestrator versions import this.
run_chatgpt_invoice_cli = run_invoice_cli