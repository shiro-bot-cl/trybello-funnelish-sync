#!/usr/bin/env python3
"""
Funnelish Token Refresher
==========================
Refreshes the Funnelish JWT via Playwright, saves it locally,
and POSTs it to the Railway slack_command_server so it doesn't need a redeploy.

Run via cron: 06:00 Warsaw daily (before the 07:00 sync cron)
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent
TOKEN_FILE = BASE_DIR / ".funnelish_token"

RAILWAY_URL = os.getenv("RAILWAY_URL", "https://trybello-funnelish-sync-production.up.railway.app")
TOKEN_UPDATE_SECRET = os.getenv("TOKEN_UPDATE_SECRET", "")


def refresh_via_playwright() -> str:
    from playwright.sync_api import sync_playwright
    from config import FUNNELISH_EMAIL, FUNNELISH_PASSWORD

    print("🔄 Launching Playwright to refresh Funnelish token...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        page.goto("https://app.funnelish.com/log-in")
        page.fill('input[placeholder="Your email address"]', FUNNELISH_EMAIL)
        page.fill('input[placeholder="Your password"]', FUNNELISH_PASSWORD)
        page.click('button:text("Log in")')
        page.wait_for_url("**/select-account", timeout=15000)

        # Select first account
        page.locator("li").first.click()
        page.wait_for_url("**/dashboard", timeout=15000)

        token = page.evaluate("() => localStorage.getItem('user-token')")
        browser.close()

    if not token:
        raise RuntimeError("Playwright login succeeded but token not found in localStorage")
    return token


def save_token_locally(token: str) -> None:
    TOKEN_FILE.write_text(token)
    TOKEN_FILE.chmod(0o600)
    print(f"✅ Token saved to {TOKEN_FILE}")


def push_token_to_railway(token: str) -> bool:
    if not TOKEN_UPDATE_SECRET:
        print("⚠️  TOKEN_UPDATE_SECRET not set — skipping Railway push")
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
                print(f"✅ Token pushed to Railway service successfully")
                return True
    except Exception as e:
        print(f"❌ Failed to push token to Railway: {e}")
    return False


def main():
    try:
        token = refresh_via_playwright()
    except Exception as e:
        print(f"❌ Token refresh failed: {e}")
        sys.exit(1)

    save_token_locally(token)
    push_token_to_railway(token)

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
