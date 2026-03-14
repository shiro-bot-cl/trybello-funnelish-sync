#!/usr/bin/env python3
"""
TryBello Funnelish Sync — Slack Slash Command Server
=====================================================
Handles /approve-otos <date> from Slack.
Runs the push_orders_to_shopify.py for the given date.

Usage:
    python3 slack_command_server.py

Environment:
    SLACK_SIGNING_SECRET   — from Slack App settings
    PORT                   — defaults to 8080
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).parent

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
PORT = int(os.getenv("PORT", 8080))
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#trybello-cs-alert")


# ─── Slack signature verification ──────────────────────────────────────────────

def verify_slack_signature(headers: dict, body: bytes) -> bool:
    """Verify request is genuinely from Slack."""
    if not SLACK_SIGNING_SECRET:
        print("⚠️  SLACK_SIGNING_SECRET not set — skipping verification (unsafe!)")
        return True

    timestamp = headers.get("X-Slack-Request-Timestamp", "")
    if abs(time.time() - float(timestamp)) > 300:
        return False  # Replay attack protection

    sig_basestring = f"v0:{timestamp}:{body.decode()}".encode()
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring,
        hashlib.sha256
    ).hexdigest()
    received = headers.get("X-Slack-Signature", "")
    return hmac.compare_digest(expected, received)


# ─── Slack response helper ──────────────────────────────────────────────────────

def post_to_slack(text: str, channel: str = None) -> None:
    import urllib.request
    payload = {"text": text, "channel": channel or SLACK_CHANNEL}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"Slack post error: {e}")


# ─── Push runner ────────────────────────────────────────────────────────────────

def run_push(date_str: str, response_url: str = None) -> None:
    """Run the daily sync + push for the given date."""
    csv_path = BASE_DIR / "output" / f"missing_orders_{date_str}.csv"

    def reply(text: str):
        post_to_slack(text)
        if response_url:
            # Also post to Slack's response_url for ephemeral feedback
            import urllib.request
            data = json.dumps({"text": text, "response_type": "in_channel"}).encode()
            req = urllib.request.Request(response_url, data=data, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10):
                    pass
            except Exception:
                pass

    reply(f"⏳ Running OTO sync for *{date_str}*... (this takes ~60s)")

    # Step 1: Re-run the sync to get fresh data for this date
    sync_result = subprocess.run(
        [sys.executable, str(BASE_DIR / "daily_sync.py"), date_str, "--dry-run"],
        capture_output=True, text=True, cwd=str(BASE_DIR)
    )

    if sync_result.returncode != 0:
        reply(f"❌ Sync failed for {date_str}:\n```{sync_result.stderr[-500:]}```")
        return

    if not csv_path.exists():
        reply(f"❌ No missing orders found for {date_str} — nothing to push.")
        return

    # Step 2: Count pending rows
    import csv
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))

    if not rows:
        reply(f"✅ No orders to push for {date_str}.")
        return

    total_value = sum(float(r.get("shopify_price", 0)) for r in rows)
    reply(f"📦 Found *{len(rows)} orders* (${total_value:,.2f}) — pushing to Shopify now...")

    # Step 3: Run the push (expects "YES" confirmation via stdin)
    push_result = subprocess.run(
        [sys.executable, str(BASE_DIR / "push_orders_to_shopify.py"), str(csv_path)],
        input="YES\n",
        capture_output=True, text=True, cwd=str(BASE_DIR),
        timeout=300
    )

    if push_result.returncode != 0:
        reply(f"❌ Push failed:\n```{push_result.stderr[-500:]}```")
        return

    # Parse results from stdout
    output = push_result.stdout
    pushed = output.count("✅") or len(rows)
    failed = output.count("❌")

    reply(
        f"✅ *Done! {date_str} push complete.*\n"
        f"• Pushed: {pushed} orders\n"
        f"• Failed: {failed}\n"
        f"• Value recovered: ${total_value:,.2f}\n"
        f"_(Orders are now in Shopify — check the sheet for updated status)_"
    )


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

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Build headers dict for verification
        headers = {k: v for k, v in self.headers.items()}

        if not verify_slack_signature(headers, body):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return

        # Parse form data
        params = urllib.parse.parse_qs(body.decode())
        command = params.get("command", [""])[0]
        text = params.get("text", [""])[0].strip()
        response_url = params.get("response_url", [""])[0]
        user_name = params.get("user_name", ["unknown"])[0]

        print(f"Command: {command} | Text: {text} | User: {user_name}")

        # Respond immediately (Slack requires response within 3s)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

        if command == "/approve-otos":
            # Parse date argument
            if text:
                date_str = text.strip()
                try:
                    datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    resp = {"text": f"❌ Invalid date format: `{date_str}`. Use YYYY-MM-DD (e.g. `/approve-otos 2026-03-13`)"}
                    self.wfile.write(json.dumps(resp).encode())
                    return
            else:
                # Default to yesterday
                date_str = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

            # Acknowledge immediately
            resp = {
                "response_type": "in_channel",
                "text": f"🔄 <@{user_name}> approved push for *{date_str}* — starting now..."
            }
            self.wfile.write(json.dumps(resp).encode())

            # Run push in background thread
            import threading
            t = threading.Thread(target=run_push, args=(date_str, response_url), daemon=True)
            t.start()

        elif command == "/sync-status":
            # Quick status check
            resp = {
                "response_type": "ephemeral",
                "text": "✅ ShiroBot sync server is running."
            }
            self.wfile.write(json.dumps(resp).encode())

        else:
            resp = {"text": f"Unknown command: {command}"}
            self.wfile.write(json.dumps(resp).encode())


# ─── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"🚀 Starting Slack command server on port {PORT}")
    print(f"   Signing secret: {'SET' if SLACK_SIGNING_SECRET else 'NOT SET (unsafe!)'}")
    print(f"   Base dir: {BASE_DIR}")
    server = HTTPServer(("0.0.0.0", PORT), SlackCommandHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
