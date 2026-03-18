#!/usr/bin/env python3
"""
TryBello Funnelish Sync — Slack Slash Command Server
=====================================================
Handles /approve-otos <date> from Slack.

Flow:
  1. /approve-otos YYYY-MM-DD  → runs sync, shows order summary + Confirm/Cancel buttons
  2. Mubashir clicks "Confirm Push" button → triggers actual Shopify push
  3. /approve-otos YYYY-MM-DD confirm  → skip preview, push directly (for scripted use)

Environment:
    SLACK_SIGNING_SECRET   — from Slack App settings
    SLACK_BOT_TOKEN        — Bot token (xoxb-...) for posting Block Kit messages
    PORT                   — defaults to 8080
"""

import csv
import hashlib
import hmac
import io
import json
import os
import subprocess
import sys
import time
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).parent

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
PORT = int(os.getenv("PORT", 8080))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#trybello-cs-alert")
TOKEN_UPDATE_SECRET = os.getenv("TOKEN_UPDATE_SECRET", "")
MUBASHIR_ID = "U0ABB3AV2E8"

# In-memory Funnelish token
_funnelish_token: str = os.getenv("FUNNELISH_TOKEN", "")


# ─── Slack helpers ──────────────────────────────────────────────────────────────

def verify_slack_signature(headers: dict, body: bytes) -> bool:
    if not SLACK_SIGNING_SECRET:
        print("⚠️  SLACK_SIGNING_SECRET not set — skipping verification")
        return True
    timestamp = headers.get("X-Slack-Request-Timestamp", "")
    try:
        if not timestamp or abs(time.time() - float(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False
    sig_basestring = f"v0:{timestamp}:{body.decode()}".encode()
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), sig_basestring, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, headers.get("X-Slack-Signature", ""))


def post_to_slack(text: str, channel: str = None, blocks=None) -> None:
    payload = {"text": text, "channel": channel or SLACK_CHANNEL}
    if blocks:
        payload["blocks"] = blocks

    # Prefer Bot token for Block Kit; fall back to webhook
    if SLACK_BOT_TOKEN and (blocks or not SLACK_WEBHOOK_URL):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            },
        )
    else:
        payload.pop("channel", None)
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            SLACK_WEBHOOK_URL, data=data,
            headers={"Content-Type": "application/json"},
        )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"Slack post error: {e}")


def post_to_url(url: str, payload: dict) -> None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"Response URL post error: {e}")


# ─── Sync helper: build CSV, return (rows, csv_path) ───────────────────────────

def try_refresh_token() -> bool:
    """Attempt to refresh the Funnelish token via refresh_token.py. Returns True on success."""
    global _funnelish_token
    try:
        print("🔄 Attempting token refresh via refresh_token.py...")
        result = subprocess.run(
            [sys.executable, str(BASE_DIR / "refresh_token.py")],
            capture_output=True, text=True, cwd=str(BASE_DIR), timeout=120,
        )
        print(result.stdout[-500:])
        if result.returncode == 0:
            # Reload the token file
            token_file = BASE_DIR / ".funnelish_token"
            if token_file.exists():
                _funnelish_token = token_file.read_text().strip()
                print(f"✅ Token refreshed successfully")
                return True
        else:
            print(f"❌ Token refresh failed: {result.stderr[-300:]}")
    except Exception as e:
        print(f"❌ Token refresh exception: {e}")
    return False


def run_sync(date_str: str, retry_on_auth_error: bool = True) -> tuple:
    """Run daily_sync for date_str, return (rows_list, csv_path).
    Auto-retries once if auth error detected (token expired)."""
    csv_path = BASE_DIR / "output" / f"missing_orders_{date_str}.csv"
    sub_env = os.environ.copy()
    if _funnelish_token:
        sub_env["FUNNELISH_TOKEN"] = _funnelish_token

    result = subprocess.run(
        [sys.executable, str(BASE_DIR / "daily_sync.py"), date_str, "--dry-run"],
        capture_output=True, text=True, cwd=str(BASE_DIR), env=sub_env,
    )

    # Detect auth error and auto-retry with refreshed token
    auth_error_signals = ["FunnelishAuthError", "Could not obtain a valid Funnelish token", "401", "invalid token"]
    output_combined = (result.stdout + result.stderr).lower()
    is_auth_error = any(sig.lower() in output_combined for sig in auth_error_signals)

    if result.returncode != 0 and is_auth_error and retry_on_auth_error:
        print("⚠️  Auth error detected — refreshing token and retrying...")
        if try_refresh_token():
            return run_sync(date_str, retry_on_auth_error=False)  # Retry once

    if result.returncode != 0:
        # Before giving up, try reading pre-synced data from Google Sheet
        print("  ⚠️  Sync failed — trying Google Sheet fallback...")
        sheet_rows = read_orders_from_sheet(date_str)
        if sheet_rows:
            # Write to CSV so push_merged_orders.py can use it
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            fieldnames = list(sheet_rows[0].keys())
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(sheet_rows)
            print(f"  ✅ Wrote {len(sheet_rows)} rows from Sheet to {csv_path}")
            return sheet_rows, csv_path
        raise RuntimeError(result.stderr[-500:])
    if not csv_path.exists():
        # Sync succeeded but no CSV (e.g. 0 missing orders)
        sheet_rows = read_orders_from_sheet(date_str)
        if sheet_rows:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            fieldnames = list(sheet_rows[0].keys())
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(sheet_rows)
            return sheet_rows, csv_path
        return [], csv_path

    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    return rows, csv_path


# ─── Google Sheet fallback reader ───────────────────────────────────────────────

APPS_SCRIPT_WEB_APP = os.getenv(
    "APPS_SCRIPT_WEB_APP",
    "https://script.google.com/macros/s/AKfycbwJfDtSnwvh8E96z6WOvNttc2pBS2r876Qyc0z5E3fVPZCzOYzywKTIGTdcR-QaSCX5/exec"
)

def read_orders_from_sheet(date_str: str) -> list:
    """Read already-synced orders from the Google Sheet tab for date_str.
    Returns list of row dicts (same format as CSV), or [] if tab not found."""
    try:
        url = f"{APPS_SCRIPT_WEB_APP}?action=read&date={date_str}"
        req = urllib.request.Request(url, headers={"User-Agent": "ShiroBot/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        if isinstance(data, list) and data:
            print(f"  📊 Read {len(data)} rows from Google Sheet tab {date_str}")
            return data
        print(f"  ℹ️  Google Sheet tab {date_str} is empty or not found.")
        return []
    except Exception as e:
        print(f"  ⚠️  Google Sheet read failed: {e}")
        return []


# ─── Push runner ────────────────────────────────────────────────────────────────

def run_push(date_str: str, response_url: str = None) -> None:
    """Execute the actual Shopify push and report results."""
    csv_path = BASE_DIR / "output" / f"missing_orders_{date_str}.csv"
    sub_env = os.environ.copy()
    if _funnelish_token:
        sub_env["FUNNELISH_TOKEN"] = _funnelish_token

    def reply(text: str):
        post_to_slack(text)
        if response_url:
            post_to_url(response_url, {"text": text, "response_type": "in_channel"})

    if not csv_path.exists():
        reply(f"❌ No CSV found for {date_str}. Run `/approve-otos {date_str}` to re-sync first.")
        return

    with open(csv_path) as f:
        rows = list(csv.DictReader(f))

    if not rows:
        reply(f"✅ No orders to push for {date_str}.")
        return

    total_value = sum(float(r.get("shopify_price", 0)) for r in rows)
    reply(f"⏳ Pushing *{len(rows)} orders* (${total_value:,.2f}) to Shopify for *{date_str}*...")

    push_result = subprocess.run(
        [sys.executable, str(BASE_DIR / "push_merged_orders.py"), str(csv_path)],
        input="YES\n",
        capture_output=True, text=True, cwd=str(BASE_DIR),
        timeout=300, env=sub_env,
    )

    if push_result.returncode != 0:
        reply(f"❌ Push failed:\n```{push_result.stderr[-500:]}```")
        return

    output = push_result.stdout
    # Parse final summary line
    pushed_line = [l for l in output.splitlines() if l.startswith("PUSHED:")]
    if pushed_line:
        reply(f"✅ *{date_str} push complete!*\n{pushed_line[0]}\n_Orders are live in Shopify._")
    else:
        pushed = output.count("✅")
        failed = output.count("❌")
        reply(
            f"✅ *{date_str} push complete!*\n"
            f"• Pushed: {pushed} orders / ${total_value:,.2f}\n"
            f"• Failed: {failed}\n"
            f"_Orders are live in Shopify._"
        )


# ─── Preview flow ────────────────────────────────────────────────────────────────

def run_preview(date_str: str, response_url: str, user_name: str) -> None:
    """Sync date, build preview, post Block Kit message with Confirm/Cancel buttons."""
    from collections import Counter

    def reply_url(text: str):
        post_to_url(response_url, {"text": text, "response_type": "in_channel"})

    reply_url(f"⏳ <@{user_name}> requested OTO sync for *{date_str}* — fetching data...")

    try:
        rows, csv_path = run_sync(date_str)
    except RuntimeError as e:
        reply_url(f"❌ Sync failed for {date_str}:\n```{e}```")
        return

    if not rows:
        reply_url(f"✅ No missing OTO orders found for *{date_str}* — nothing to push.")
        return

    total_value = sum(float(r.get("shopify_price", 0)) for r in rows)
    sku_counts = Counter(r.get("shopify_sku", "unknown") for r in rows)
    sku_lines = "\n".join(f"  • {sku}: {count}" for sku, count in sorted(sku_counts.items()))

    # Build Block Kit message with confirm/cancel buttons
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📦 OTO Orders Ready — {date_str}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"<@{MUBASHIR_ID}> — *{len(rows)} orders* totalling *${total_value:,.2f}* "
                    f"are missing from Shopify and ready to push.\n\n"
                    f"*SKU breakdown:*\n{sku_lines}"
                ),
            },
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ Confirm Push"},
                    "style": "primary",
                    "action_id": "confirm_push",
                    "value": date_str,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ Cancel"},
                    "style": "danger",
                    "action_id": "cancel_push",
                    "value": date_str,
                },
            ],
        },
    ]

    fallback_text = f"📦 {len(rows)} OTO orders (${total_value:,.2f}) ready for {date_str}. Reply with `/approve-otos {date_str} confirm` to push."
    post_to_slack(fallback_text, blocks=blocks)


# ─── HTTP Handler ───────────────────────────────────────────────────────────────

class SlackCommandHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"[{self.address_string()}] {format % args}")

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def _handle_set_token(self, body: bytes) -> None:
        global _funnelish_token
        auth = self.headers.get("Authorization", "")
        if not TOKEN_UPDATE_SECRET or auth != f"Bearer {TOKEN_UPDATE_SECRET}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return
        try:
            payload = json.loads(body)
            new_token = payload.get("token", "").strip()
            if not new_token:
                raise ValueError("missing token")
        except Exception:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Bad request")
            return

        _funnelish_token = new_token
        print("✅ Funnelish token updated in memory.")
        self._send_json(200, {"ok": True})

    def _handle_slack_action(self, body: bytes) -> None:
        """Handle Slack interactive component callbacks (button clicks)."""
        params = urllib.parse.parse_qs(body.decode())
        payload_str = params.get("payload", ["{}"])[0]
        payload = json.loads(payload_str)

        action = payload.get("actions", [{}])[0]
        action_id = action.get("action_id", "")
        date_str = action.get("value", "")
        response_url = payload.get("response_url", "")
        user = payload.get("user", {})
        user_name = user.get("name", "unknown")

        # Immediately ack
        self._send_json(200, {"text": "Got it!"})

        if action_id == "confirm_push":
            post_to_url(response_url, {
                "text": f"✅ <@{user_name}> confirmed — pushing *{date_str}* orders to Shopify now...",
                "response_type": "in_channel",
                "replace_original": True,
            })
            t = threading.Thread(target=run_push, args=(date_str, response_url), daemon=True)
            t.start()

        elif action_id == "cancel_push":
            post_to_url(response_url, {
                "text": f"❌ <@{user_name}> cancelled push for *{date_str}*.",
                "response_type": "in_channel",
                "replace_original": True,
            })

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        if self.path == "/set-token":
            self._handle_set_token(body)
            return

        # Verify Slack signature for all other endpoints
        headers = dict(self.headers)
        if not verify_slack_signature(headers, body):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return

        # Interactive component callback (button clicks)
        if self.path == "/slack/actions":
            self._handle_slack_action(body)
            return

        # Slash command
        params = urllib.parse.parse_qs(body.decode())
        command = params.get("command", [""])[0]
        text = params.get("text", [""])[0].strip()
        response_url = params.get("response_url", [""])[0]
        user_name = params.get("user_name", ["unknown"])[0]

        print(f"Command: {command} | Text: {text} | User: {user_name}")

        if command == "/approve-otos":
            # Parse: /approve-otos [YYYY-MM-DD] [confirm]
            parts = text.split()
            force_confirm = "confirm" in parts
            date_parts = [p for p in parts if p != "confirm"]
            date_str = date_parts[0] if date_parts else (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                self._send_json(200, {"text": f"❌ Invalid date: `{date_str}`. Use YYYY-MM-DD."})
                return

            if force_confirm:
                # Direct push — skip preview (for scripted/automated use)
                self._send_json(200, {
                    "response_type": "in_channel",
                    "text": f"🔄 <@{user_name}> confirmed push for *{date_str}* — starting now...",
                })
                t = threading.Thread(target=run_push, args=(date_str, response_url), daemon=True)
                t.start()
            else:
                # Preview mode — show summary + buttons
                self._send_json(200, {
                    "response_type": "in_channel",
                    "text": f"🔄 <@{user_name}> requested OTO review for *{date_str}*...",
                })
                t = threading.Thread(target=run_preview, args=(date_str, response_url, user_name), daemon=True)
                t.start()

        elif command == "/sync-status":
            self._send_json(200, {"response_type": "ephemeral", "text": "✅ ShiroBot sync server is running."})

        else:
            self._send_json(200, {"text": f"Unknown command: {command}"})


# ─── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"🚀 Starting Slack command server on port {PORT}")
    print(f"   Signing secret: {'SET' if SLACK_SIGNING_SECRET else 'NOT SET (unsafe!)'}")
    print(f"   Bot token: {'SET' if SLACK_BOT_TOKEN else 'NOT SET — Block Kit buttons need xoxb token'}")
    server = HTTPServer(("0.0.0.0", PORT), SlackCommandHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
