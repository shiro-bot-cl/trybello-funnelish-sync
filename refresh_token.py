#!/usr/bin/env python3
"""
Funnelish Token Refresher
==========================
Connects to the running OpenClaw browser (CDP on port 18800),
finds the logged-in Funnelish session, extracts the JWT, saves it locally,
and POSTs it to Railway.

If no active Funnelish session is found, opens a new page and logs in
using the OpenClaw browser (which handles Vue forms correctly).

Run via cron: 06:00 Warsaw daily (before the 07:00 sync cron)
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# Auto-load .env from project root
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

BASE_DIR = Path(__file__).parent
TOKEN_FILE = BASE_DIR / ".funnelish_token"

RAILWAY_URL = os.getenv("RAILWAY_URL", "https://trybello-funnelish-sync-production.up.railway.app")
TOKEN_UPDATE_SECRET = os.getenv("TOKEN_UPDATE_SECRET", "")
OPENCLAW_CDP = "http://127.0.0.1:18800"


def get_token_from_openclaw_browser() -> str:
    """
    Connect to OpenClaw's browser via CDP (port 18800).
    Try to find an existing Funnelish session first.
    If none, open a new page and log in.
    """
    from playwright.sync_api import sync_playwright
    from config import FUNNELISH_EMAIL, FUNNELISH_PASSWORD

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(OPENCLAW_CDP)
        context = browser.contexts[0]

        # Look for an existing logged-in Funnelish page
        funnelish_page = None
        for pg in context.pages:
            if "app.funnelish.com" in pg.url and "/log-in" not in pg.url:
                token = pg.evaluate("() => localStorage.getItem('user-token')")
                if token:
                    funnelish_page = pg
                    print(f"✅ Found existing Funnelish session at {pg.url}")
                    return token

        # Also check login page tabs (might be stuck on /log-in)
        for pg in context.pages:
            if "app.funnelish.com" in pg.url:
                token = pg.evaluate("() => localStorage.getItem('user-token')")
                if token:
                    print(f"✅ Found Funnelish token in tab at {pg.url}")
                    return token

        # No live session — open login page and authenticate
        print("🔄 No active session found — logging in via OpenClaw browser...")
        page = context.new_page()
        page.goto("https://app.funnelish.com/log-in")
        page.wait_for_load_state("domcontentloaded", timeout=20000)
        time.sleep(2)  # Extra wait for Vue to render

        # Fill email — try multiple placeholder variants
        email_sel = 'input[placeholder="Your email address"], input[type="email"], input[name="email"]'
        page.wait_for_selector(email_sel, timeout=15000)
        page.locator(email_sel).first.click()
        page.locator(email_sel).first.type(FUNNELISH_EMAIL, delay=60)

        # Fill password
        pw_sel = 'input[type="password"], input[placeholder="Your password"], input[name="password"]'
        page.locator(pw_sel).first.click()
        page.locator(pw_sel).first.type(FUNNELISH_PASSWORD, delay=60)

        time.sleep(1)  # Allow Vue validation to fire

        # Try to click login button — try multiple selectors
        LOGIN_SELECTORS = [
            'button:text("Log in to Funnelish")',
            'button:text("Log in")',
            'button[type="submit"]',
            'form button',
        ]
        btn = None
        for _ in range(20):  # up to 10s
            for sel in LOGIN_SELECTORS:
                try:
                    b = page.query_selector(sel)
                    if b and b.is_enabled():
                        btn = b
                        break
                except Exception:
                    pass
            if btn:
                break
            time.sleep(0.5)

        if btn:
            btn.click()
        else:
            # Last resort: press Enter on password field
            print("⚠️  Login button not found — submitting via Enter key")
            page.locator(pw_sel).first.press("Enter")

        # Wait for post-login redirect (either select-account or dashboard)
        try:
            page.wait_for_url("**/select-account", timeout=12000)
            page.locator("li").first.click()
            page.wait_for_url("**/dashboard", timeout=12000)
        except Exception:
            # Some accounts go straight to dashboard
            try:
                page.wait_for_url("**/dashboard", timeout=12000)
            except Exception:
                pass

        token = page.evaluate("() => localStorage.getItem('user-token')")
        page.close()

        if not token:
            raise RuntimeError("Login completed but no token found in localStorage")
        return token


def save_token_locally(token: str) -> None:
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    print(f"✅ Token saved to {TOKEN_FILE}")


def push_token_to_railway(token: str) -> bool:
    """Push token to the running service via /set-token (fast, in-memory update)."""
    if not TOKEN_UPDATE_SECRET:
        print("⚠️  TOKEN_UPDATE_SECRET not set — skipping Railway service push")
        return False

    url = f"{RAILWAY_URL}/set-token"
    data = json.dumps({"token": token}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN_UPDATE_SECRET}"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
            if resp.get("ok"):
                print("✅ Token pushed to Railway service (in-memory) successfully")
                return True
    except Exception as e:
        print(f"❌ Failed to push token to Railway service: {e}")
    return False


def update_railway_env_var(token: str) -> bool:
    """
    Persistently update the FUNNELISH_TOKEN env var on Railway via GraphQL API.
    This survives service restarts — fixes the ephemeral filesystem problem.
    """
    RAILWAY_API_TOKEN = os.getenv("RAILWAY_API_TOKEN", "9598b7d4-45f6-4e49-959f-14da2fdb256d")
    PROJECT_ID = "0e155348-881d-41d5-a0ad-5f302e7a9e0c"
    ENV_ID = "bfa3c1f1-7fce-4bce-8b9e-4829953dfa70"
    SERVICE_ID = "ee483ebe-5675-402a-ae99-9357ae1a491b"

    query = """
    mutation UpsertVar($input: VariableUpsertInput!) {
      variableUpsert(input: $input)
    }
    """
    variables = {
        "input": {
            "projectId": PROJECT_ID,
            "environmentId": ENV_ID,
            "serviceId": SERVICE_ID,
            "name": "FUNNELISH_TOKEN",
            "value": token
        }
    }
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        "https://backboard.railway.app/graphql/v2",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {RAILWAY_API_TOKEN}"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
            if "errors" not in resp:
                print("✅ FUNNELISH_TOKEN env var updated on Railway (persistent)")
                return True
            else:
                print(f"❌ Railway API error: {resp['errors']}")
    except Exception as e:
        print(f"❌ Failed to update Railway env var: {e}")
    return False


def main():
    try:
        token = get_token_from_openclaw_browser()
    except Exception as e:
        print(f"❌ Token refresh failed: {e}")
        sys.exit(1)

    save_token_locally(token)
    push_token_to_railway(token)       # Fast: in-memory update of running service
    update_railway_env_var(token)      # Persistent: survives service restarts

    # Decode and show expiry
    import base64
    try:
        parts = token.split(".")
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        expiry = data.get("expiry", data.get("exp", 0))
        if expiry:
            exp_str = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(expiry))
            print(f"🔑 New token expires: {exp_str}")
    except Exception:
        pass

    print("✅ Token refresh complete")


if __name__ == "__main__":
    main()
