"""
Funnelish Authentication Manager
==================================
Handles JWT token acquisition and refresh for the Funnelish API.

Token Strategy:
  1. Check FUNNELISH_TOKEN env var
  2. Check .funnelish_token file (and validate expiry)
  3. Try to refresh via Playwright (headless browser) if available
  4. Raise AuthError with instructions if all else fails
"""

import json
import os
import time
import base64
import urllib.request
import urllib.parse
import urllib.error
from config import FUNNELISH_EMAIL, FUNNELISH_PASSWORD, FUNNELISH_TOKEN, FUNNELISH_TOKEN_FILE


class FunnelishAuthError(Exception):
    pass


def decode_jwt_expiry(token: str) -> int:
    """Extract expiry timestamp from JWT payload without verifying signature."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return 0
        payload = parts[1]
        # Pad base64
        payload += "=" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("expiry", data.get("exp", 0))
    except Exception:
        return 0


def is_token_valid(token: str, buffer_seconds: int = 300) -> bool:
    """Returns True if token is present and not expiring within buffer_seconds."""
    if not token:
        return False
    expiry = decode_jwt_expiry(token)
    if expiry == 0:
        return True  # No expiry field, assume valid
    return time.time() < (expiry - buffer_seconds)


def load_stored_token() -> str:
    """Load token from file if valid."""
    if os.path.exists(FUNNELISH_TOKEN_FILE):
        try:
            token = open(FUNNELISH_TOKEN_FILE).read().strip()
            if is_token_valid(token):
                return token
        except Exception:
            pass
    return ""


def save_token(token: str) -> None:
    """Persist token to file."""
    with open(FUNNELISH_TOKEN_FILE, "w") as f:
        f.write(token)
    os.chmod(FUNNELISH_TOKEN_FILE, 0o600)


FUNNELISH_REQUIRED_ACCOUNT_ID = 77440  # "mark" account — main FB funnels


def refresh_token_via_raw_cdp(cdp_url: str = "http://127.0.0.1:18800") -> str:
    """
    Extract token from existing Funnelish tab via raw CDP websocket.
    Fast (~2s), no Playwright needed. Returns token or raises RuntimeError.
    Only accepts tokens for account_id=77440 (mark) — rejects Trybello (5245).
    """
    import asyncio
    try:
        import websockets
    except ImportError:
        raise RuntimeError("websockets not installed")

    async def _fetch():
        resp = urllib.request.urlopen(f"{cdp_url}/json", timeout=4)
        pages = json.loads(resp.read())
        for pg in pages:
            url = pg.get("url", "")
            if "app.funnelish.com" in url and "blob:" not in url:
                ws_url = pg["webSocketDebuggerUrl"]
                async with websockets.connect(ws_url) as ws:
                    await ws.send(json.dumps({
                        "id": 1, "method": "Runtime.evaluate",
                        "params": {"expression": "localStorage.getItem('user-token')", "returnByValue": True}
                    }))
                    result = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                    token = result.get("result", {}).get("result", {}).get("value")
                    if token:
                        # Verify it's the correct account before accepting
                        account_id = _get_account_id_from_token(token)
                        if account_id != FUNNELISH_REQUIRED_ACCOUNT_ID:
                            raise RuntimeError(
                                f"CDP tab has wrong Funnelish account (id={account_id}). "
                                f"Need account {FUNNELISH_REQUIRED_ACCOUNT_ID} (mark). "
                                "Falling back to Playwright login."
                            )
                    return token
        raise RuntimeError("No Funnelish tab found in CDP browser")

    return asyncio.run(_fetch())


def _get_account_id_from_token(token: str) -> int:
    """Extract account_id from JWT payload."""
    try:
        parts = token.split(".")
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("account_id", 0)
    except Exception:
        return 0


def refresh_token_via_playwright() -> str:
    """
    Use Playwright to log in and capture the JWT token.
    Always selects the "mark" account (id=77440) — the main FB funnels account.
    Requires: pip install playwright && playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise FunnelishAuthError(
            "playwright not installed. Run: pip install playwright && playwright install chromium"
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto("https://app.funnelish.com/log-in")
        page.fill('input[placeholder="Your email address"]', FUNNELISH_EMAIL)
        page.fill('input[placeholder="Your password"]', FUNNELISH_PASSWORD)
        page.press('input[placeholder="Your password"]', "Enter")
        page.wait_for_url("**/select-account**", timeout=12000)
        time.sleep(2)
        # Always select the "mark" account (id=77440) — main FB funnels (get.trybello.com).
        # "Trybello" (id=5245) is the Google funnels account (shop.trybello.com) — do NOT use for nightly sync.
        # NOTE: Funnelish v2 changed the UI — "div.account_div" no longer exists.
        # Use the API-based account switch approach: POST to switch-account endpoint after login.
        # Strategy: look for any clickable element containing "mark" text, trying multiple selectors.
        target = None
        # Try v2 selectors (text-based search across common card containers)
        for selector in [
            "div.account_div",
            "[class*='account']",
            "[class*='workspace']",
            "[class*='Account']",
            "button",
            "div[role='button']",
        ]:
            try:
                elements = page.locator(selector).all()
                for el in elements:
                    txt = el.text_content() or ""
                    if "mark" in txt.strip().lower() and len(txt.strip()) < 50:
                        target = el
                        break
                if target:
                    break
            except Exception:
                continue

        if target is None:
            # Last resort: use JS to click the element containing "mark" text
            clicked = page.evaluate("""
                () => {
                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        if (el.children.length === 0 && el.textContent.trim().toLowerCase() === 'mark') {
                            el.closest('button, div[role="button"], div[tabindex], li, a') 
                                ? el.closest('button, div[role="button"], div[tabindex], li, a').click()
                                : el.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if not clicked:
                raise FunnelishAuthError(
                    "Could not find 'mark' account selector on Funnelish select-account page. "
                    "UI may have changed. Manual token refresh needed."
                )
        else:
            target.click()

        # Wait for any post-login page (root or dashboard)
        page.wait_for_function(
            "() => !window.location.pathname.includes('select-account') && !window.location.pathname.includes('log-in')",
            timeout=15000
        )
        time.sleep(1)
        # Verify we got the right account token
        token_check = page.evaluate("() => localStorage.getItem('user-token')")
        if token_check:
            account_id = _get_account_id_from_token(token_check)
            if account_id != FUNNELISH_REQUIRED_ACCOUNT_ID:
                raise FunnelishAuthError(
                    f"Playwright selected wrong account (id={account_id}). "
                    f"Need {FUNNELISH_REQUIRED_ACCOUNT_ID} (mark/get.trybello.com). "
                    "Manual token refresh needed."
                )
        time.sleep(2)
        token = page.evaluate("() => localStorage.getItem('user-token')")
        browser.close()
        return token


def get_token(force_refresh: bool = False) -> str:
    """
    Get a valid Funnelish JWT token for mark account (77440).
    Order of precedence: env var → file → playwright refresh.
    """
    # 1. Env var (always wins if set)
    if FUNNELISH_TOKEN and not force_refresh:
        if is_token_valid(FUNNELISH_TOKEN):
            return FUNNELISH_TOKEN
        print("⚠️  FUNNELISH_TOKEN env var is expired.")

    # 2. Stored file token
    if not force_refresh:
        stored = load_stored_token()
        if stored:
            return stored

    # 3. Try raw CDP (fast, no Playwright)
    print("🔄 Refreshing Funnelish token via raw CDP...")
    try:
        token = refresh_token_via_raw_cdp()
        if token and is_token_valid(token):
            save_token(token)
            print("✅ Token refreshed via CDP.")
            return token
    except Exception as e:
        print(f"⚠️  Raw CDP refresh failed: {e}")

    # 4. Try Playwright refresh (login flow, slower)
    print("🔄 Refreshing Funnelish token via Playwright...")
    try:
        token = refresh_token_via_playwright()
        if token and is_token_valid(token):
            save_token(token)
            print("✅ Token refreshed successfully.")
            return token
    except Exception as e:
        print(f"⚠️  Playwright refresh failed: {e}")

    # All strategies exhausted — send Telegram alert before raising
    _send_auth_failure_alert()
    raise FunnelishAuthError(
        "Could not obtain a valid Funnelish token.\n"
        "To fix:\n"
        "  1. Log into https://app.funnelish.com\n"
        "  2. Open browser console and run: copy(localStorage.getItem('user-token'))\n"
        f"  3. Save the token: echo '<TOKEN>' > {FUNNELISH_TOKEN_FILE}\n"
        "  OR set the FUNNELISH_TOKEN environment variable."
    )


def _send_auth_failure_alert():
    """Send a Telegram alert when all token refresh strategies fail."""
    try:
        TELEGRAM_BOT_TOKEN="877809...NzJw"
        TELEGRAM_CHAT_ID = "341129660"
        msg = (
            "⚠️ *Funnelish token refresh failed* — all strategies exhausted.\n"
            "CDP unavailable, Playwright login failed.\n"
            "Nightly sync will NOT run. Manual token refresh needed:\n"
            "`cd ~/Projects/funnelish-sync && python3 refresh_token.py`"
        )
        data = json.dumps({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass  # Don't let alert failure mask the real error


# ─── Trybello account (5245) token ──────────────────────────────────────────

TRYBELLO_TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".funnelish_trybello_token")
FUNNELISH_TRYBELLO_TOKEN = os.getenv("FUNNELISH_TRYBELLO_TOKEN", "")


def get_trybello_token():  # -> Optional[str]
    """
    Get a valid Funnelish JWT token for the Trybello account (5245 / Google+Klaviyo funnels).
    Order of precedence: env var → local file → bello-inventory Railway API.
    Returns None if unavailable (non-fatal — sync continues with mark account only).
    """
    # 1. Env var
    if FUNNELISH_TRYBELLO_TOKEN and is_token_valid(FUNNELISH_TRYBELLO_TOKEN):
        return FUNNELISH_TRYBELLO_TOKEN

    # 2. Local file (written by refresh_funnelish_tokens.py or manually)
    if os.path.exists(TRYBELLO_TOKEN_FILE):
        try:
            token = open(TRYBELLO_TOKEN_FILE).read().strip()
            if token and is_token_valid(token):
                return token
        except Exception:
            pass

    # 3. Pull from bello-inventory Railway volume (POST /api/funnelish-tokens/status doesn't expose token)
    # Use the stored token file path on Railway — not accessible remotely.
    # Fallback: return None and log warning.
    print("⚠️  Trybello Funnelish token (5245) unavailable — skipping account 5245 in sync.")
    print("   To fix: run python3 refresh_funnelish_tokens.py or set FUNNELISH_TRYBELLO_TOKEN env var.")
    return None


if __name__ == "__main__":
    # Save current token from env/arg to file
    import sys
    if len(sys.argv) > 1:
        token = sys.argv[1]
        save_token(token)
        expiry = decode_jwt_expiry(token)
        if expiry:
            exp_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(expiry))
            print(f"✅ Token saved. Expires: {exp_str}")
        else:
            print("✅ Token saved.")
    else:
        token = get_token()
        print(f"Token valid: {is_token_valid(token)}")
        expiry = decode_jwt_expiry(token)
        if expiry:
            print(f"Expires: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(expiry))}")
