#!/usr/bin/env python3
"""
TryBello Daily Funnelish ↔ Shopify Sync Checker (v5)
=====================================================
Finds MAIN and OTO orders captured in Funnelish that were NOT created in Shopify.

Usage:
    python3 daily_sync.py                    # check yesterday
    python3 daily_sync.py 2026-03-13         # check specific date
    python3 daily_sync.py --dry-run          # dry run, no notifications
    python3 daily_sync.py --help

Output:
    missing_orders_YYYY-MM-DD.csv   (always saved, includes Order Type column)
    Slack notification               (if SLACK_WEBHOOK_URL set)

Changelog:
    v5 (2026-03-17): Also check MAIN/front-end orders, not just OTOs.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import List, Dict, Tuple, Optional

# ─── Bootstrap path ────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from config import (
    SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, SHOPIFY_SHOP, SHOPIFY_API_VERSION,
    FUNNELISH_ORDERS_API, SLACK_WEBHOOK_URL, SLACK_CHANNEL, SLACK_CS_USER_ID,
    GOOGLE_SHEET_URL, APPS_SCRIPT_WEB_APP, FUNNELISH_CSV_DIR, OTO_SKU_MAP,
    OTO_SKU_PREFIX, OTO_PRICE_BY_SUPPLY, OTO_PORCH_PIRATE
)
from funnelish_auth import get_token

# ─── Product Classification ─────────────────────────────────────────────────────

def classify_funnelish_product(name: str) -> str:
    """Classify a Funnelish product name into OTO category."""
    p = name.lower()
    if "hair growth shampoo" in p:
        return "OTO1_Shampoo"
    if "hair growth conditioner" in p:
        return "OTO2_Conditioner"
    if "daily hair booster" in p or "hair booster" in p:
        return "OTO3_Booster"
    if "porch pirate" in p:
        return "OTO4_PorchPirate"
    # Everything else is MAIN
    # (months supply, hair helper, buy X, e-book, eyebrow serum, derma roller, popup variants)
    return "MAIN"


def classify_shopify_sku(sku: str, title: str) -> str:
    """Classify a Shopify order line item into OTO category."""
    sku = (sku or "").upper()
    t = (title or "").lower()
    if sku.startswith("HH-") or "hair helper" in t:
        return "MAIN"
    if sku.startswith("HS-GR") or "shampoo" in t:
        return "OTO1_Shampoo"
    if sku.startswith("HC-GR") or "conditioner" in t:
        return "OTO2_Conditioner"
    if sku.startswith("HB-GR") or "booster" in t:
        return "OTO3_Booster"
    if sku == "PPP-01" or "porch pirate" in t:
        return "OTO4_PorchPirate"
    return "MAIN"

# ─── Variant Resolver ──────────────────────────────────────────────────────────

def resolve_shopify_variant(product_name: str, oto_category: str, funnelish_amount: float) -> Dict:
    """
    Determine the correct Shopify SKU and price from the Funnelish product name.
    SKU suffix: 03 = 3-month, 06 = 6-month, 09 = 9-month
    """
    if oto_category == "OTO4_PorchPirate":
        return {
            "sku": OTO_PORCH_PIRATE["sku"],
            "price": funnelish_amount or OTO_PORCH_PIRATE["price"],
            "supply": "single"
        }

    name = (product_name or "").lower()

    # Detect supply size from product name
    if "9-month" in name or "9 month" in name or "9month" in name:
        suffix = "09"
    elif "6-month" in name or "6 month" in name or "6month" in name:
        suffix = "06"
    else:
        suffix = "03"  # default: 3-month

    prefix = OTO_SKU_PREFIX.get(oto_category, "UNKNOWN")
    sku = f"{prefix}-{suffix}"
    price = funnelish_amount if funnelish_amount and funnelish_amount > 0 else OTO_PRICE_BY_SUPPLY.get(suffix, 90.00)

    supply_label = {"03": "3-month", "06": "6-month", "09": "9-month"}.get(suffix, suffix)
    return {"sku": sku, "price": price, "supply": supply_label}

# ─── Shopify API ────────────────────────────────────────────────────────────────

def get_shopify_token() -> str:
    """Get Shopify access token via client credentials OAuth."""
    data = json.dumps({
        "client_id": SHOPIFY_CLIENT_ID,
        "client_secret": SHOPIFY_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }).encode()
    req = urllib.request.Request(
        f"https://{SHOPIFY_SHOP}/admin/oauth/access_token",
        data=data,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    return result["access_token"]


def fetch_shopify_orders(token: str, date_from: datetime, date_to: datetime) -> List[Dict]:
    """Fetch all Shopify orders within date range (±1 day buffer applied by caller)."""
    orders = []
    # Use EST boundaries (UTC-5) + 1 day buffer each side for safety
    d_from = (date_from - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00-05:00")
    d_to   = (date_to   + timedelta(days=1)).strftime("%Y-%m-%dT23:59:59-05:00")
    page_info = None
    base_url = f"https://{SHOPIFY_SHOP}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    params = {
        "status": "any",
        "limit": 250,
        "created_at_min": d_from,
        "created_at_max": d_to,
        "fields": "id,email,created_at,line_items,tags,financial_status,name"
    }

    while True:
        if page_info:
            url = f"{base_url}?limit=250&page_info={page_info}"
        else:
            url = f"{base_url}?{urllib.parse.urlencode(params)}"

        req = urllib.request.Request(url, headers={"X-Shopify-Access-Token": token})
        with urllib.request.urlopen(req, timeout=30) as resp:
            link_header = resp.getheader("Link", "")
            result = json.loads(resp.read())

        orders.extend(result.get("orders", []))
        print(f"  Fetched {len(orders)} Shopify orders...", end="\r")

        # Parse next page cursor
        page_info = None
        for part in link_header.split(","):
            if 'rel="next"' in part:
                # Extract page_info from URL
                url_part = part.strip().split(";")[0].strip("<>")
                parsed = urllib.parse.urlparse(url_part)
                qs = urllib.parse.parse_qs(parsed.query)
                page_info = qs.get("page_info", [None])[0]
                break

        if not page_info:
            break

    print(f"  ✅ Total Shopify orders: {len(orders)}")
    return orders


def build_shopify_lookup(orders: List[Dict]) -> Dict[str, List[Dict]]:
    """
    Build lookup: email.lower() → list of {category, created_at, amount} dicts.
    Preserves per-order timestamps so downstream code can do date-aware matching.
    """
    lookup: Dict[str, List[Dict]] = defaultdict(list)
    for order in orders:
        email = (order.get("email") or "").lower().strip()
        if not email:
            continue
        created_at = order.get("created_at", "")
        for item in order.get("line_items", []):
            sku = item.get("sku", "")
            title = item.get("title", "")
            price = float(item.get("price", 0) or 0)
            category = classify_shopify_sku(sku, title)
            lookup[email].append({
                "category": category,
                "created_at": created_at,
                "amount": price,
            })
    return dict(lookup)

# ─── Funnelish API ──────────────────────────────────────────────────────────────

def fetch_funnelish_orders_api(date_from: datetime, date_to: datetime) -> List[Dict]:
    """Fetch Funnelish orders for the given date range via API.
    Uses EST (UTC-5) boundaries since Funnelish account is in EST.
    """
    token = get_token()
    from datetime import timezone as _tz
    EST = _tz(timedelta(hours=-5), name="EST")
    # Mar 13 00:00 EST → Mar 13 05:00 UTC
    ts_from = int(date_from.replace(hour=0, minute=0, second=0, tzinfo=EST).timestamp())
    # Mar 13 23:59:59 EST → Mar 14 04:59:59 UTC
    ts_to   = int(date_to.replace(hour=23, minute=59, second=59, tzinfo=EST).timestamp())

    orders = []
    skip = 0
    limit = 100

    while True:
        url = (f"{FUNNELISH_ORDERS_API}?skip={skip}&limit={limit}"
               f"&date_from={ts_from}&date_to={ts_to}"
               f"&payment_status=succeeded")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("⚠️  Token expired. Refreshing...")
                token = get_token(force_refresh=True)
                continue
            raise

        batch = data.get("orders", [])
        orders.extend(batch)
        total = data.get("meta", {}).get("count", 0)
        print(f"  Fetched {len(orders)}/{total} Funnelish orders...", end="\r")

        if len(orders) >= total or len(batch) < limit:
            break
        skip += limit

    print(f"  ✅ Total Funnelish orders: {len(orders)}")
    return orders


def fetch_funnelish_orders_csv(date: datetime) -> List[Dict]:
    """
    CSV fallback: reads from FUNNELISH_CSV_DIR.
    Expected CSV columns: order_id, email, product_name, created_at, amount, status
    """
    os.makedirs(FUNNELISH_CSV_DIR, exist_ok=True)
    date_str = date.strftime("%Y-%m-%d")
    csv_path = os.path.join(FUNNELISH_CSV_DIR, f"{date_str}.csv")

    if not os.path.exists(csv_path):
        # Also check for any CSV in the directory
        csvs = sorted([f for f in os.listdir(FUNNELISH_CSV_DIR) if f.endswith(".csv")])
        if not csvs:
            raise FileNotFoundError(
                f"No Funnelish CSV found for {date_str}.\n"
                f"Drop a CSV at: {csv_path}\n"
                f"Required columns: order_id, email, product_name, created_at, amount, status"
            )
        csv_path = os.path.join(FUNNELISH_CSV_DIR, csvs[-1])
        print(f"  Using latest CSV: {csvs[-1]}")

    orders = []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            orders.append({
                "order_id": row.get("order_id", ""),
                "name": row.get("product_name", row.get("name", "")),
                "created_at": row.get("created_at", ""),
                "amount": float(row.get("amount", 0)),
                "payment_status": row.get("status", row.get("payment_status", "succeeded")),
                "customer": {
                    "optin_email": row.get("email", ""),
                    "first_name": row.get("first_name", ""),
                    "last_name": row.get("last_name", ""),
                }
            })
    print(f"  ✅ Loaded {len(orders)} orders from CSV: {csv_path}")
    return orders

# ─── Core Sync Logic ────────────────────────────────────────────────────────────

def group_funnelish_sessions(orders: List[Dict]) -> Dict[str, Dict]:
    """
    Group Funnelish orders by (email, date) → customer session.
    Returns: {email: {'MAIN': [orders], 'OTO1_Shampoo': [orders], ...}}
    """
    sessions: Dict[str, Dict] = defaultdict(lambda: defaultdict(list))

    for order in orders:
        customer = order.get("customer", {})
        email = (customer.get("optin_email") or "").lower().strip()
        if not email:
            continue
        status = order.get("payment_status", "")
        if status not in ("succeeded", "paid", ""):
            continue

        category = classify_funnelish_product(order.get("name", ""))
        sessions[email][category].append(order)

    return {k: dict(v) for k, v in sessions.items()}


def _within_48h(shopify_created: str, funnelish_created: str) -> bool:
    """Return True if two ISO-8601 timestamps are within 48 hours of each other."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%S"
        s = datetime.strptime(shopify_created[:19], fmt)
        f = datetime.strptime(funnelish_created[:19], fmt)
        return abs((s - f).total_seconds()) < 172800  # 48 hours
    except Exception:
        return True  # can't parse → assume it matches (safe default)


def find_missing_orders(
    funnelish_sessions: Dict[str, Dict],
    shopify_lookup: Dict[str, List[Dict]]
) -> Tuple[List[Dict], List[Dict]]:
    """
    Find MAIN and OTO orders present in Funnelish but missing from Shopify.

    MAIN check: customer has a MAIN order in Funnelish but no MAIN purchase in Shopify
                within 48 h of the Funnelish order timestamp (date-aware).
    OTO check:  customer has OTO in Funnelish but that OTO category is not found in
                Shopify within 48 h of the Funnelish OTO timestamp.
                Only checked for sessions that also have a MAIN order (real customers).

    Using date-aware matching prevents repeat buyers from having an old purchase
    falsely suppress a genuinely missing order on a later date.

    Returns (missing_main, missing_otos) — both sorted by created_at.
    """
    missing_main: List[Dict] = []
    missing_otos: List[Dict] = []

    for email, categories in funnelish_sessions.items():
        shopify_entries = shopify_lookup.get(email, [])
        has_funnelish_main = "MAIN" in categories

        # ── Check MAIN orders ──────────────────────────────────────
        if has_funnelish_main:
            for order in categories["MAIN"]:
                funnelish_ts = order.get("created_at", "")
                found_in_shopify = any(
                    e["category"] == "MAIN" and _within_48h(e["created_at"], funnelish_ts)
                    for e in shopify_entries
                )
                if not found_in_shopify:
                    customer = order.get("customer", {})
                    product_name = order.get("name", "")
                    funnelish_amount = float(order.get("amount", 0) or 0)
                    missing_main.append({
                        "order_type": "MAIN",
                        "email": email,
                        "first_name": customer.get("first_name", ""),
                        "last_name": customer.get("last_name", ""),
                        "customer_id": customer.get("customer_id", ""),
                        "phone": customer.get("phone", ""),
                        "funnelish_order_id": order.get("order_id", ""),
                        "funnelish_order_number": order.get("order_number", ""),
                        "funnelish_product_name": product_name,
                        "oto_category": "MAIN",
                        "supply": "",
                        "amount": funnelish_amount,
                        "created_at": funnelish_ts,
                        "shopify_sku": "",
                        "shopify_price": funnelish_amount,
                        # Address fields — populated by enrich_with_addresses()
                        "shipping_address1": "",
                        "shipping_address2": "",
                        "shipping_city": "",
                        "shipping_state": "",
                        "shipping_zip": "",
                        "shipping_country": "",
                    })

        # ── Check OTO orders ───────────────────────────────────────
        # Skip orphaned OTO-only sessions (no MAIN = likely test/fraud)
        if not has_funnelish_main:
            continue

        for category, orders in categories.items():
            if category == "MAIN":
                continue
            for order in orders:
                funnelish_ts = order.get("created_at", "")
                found_in_shopify = any(
                    e["category"] == category and _within_48h(e["created_at"], funnelish_ts)
                    for e in shopify_entries
                )
                if not found_in_shopify:
                    customer = order.get("customer", {})
                    product_name = order.get("name", "")
                    funnelish_amount = float(order.get("amount", 0) or 0)
                    variant = resolve_shopify_variant(product_name, category, funnelish_amount)
                    missing_otos.append({
                        "order_type": "OTO",
                        "email": email,
                        "first_name": customer.get("first_name", ""),
                        "last_name": customer.get("last_name", ""),
                        "customer_id": customer.get("customer_id", ""),
                        "phone": customer.get("phone", ""),
                        "funnelish_order_id": order.get("order_id", ""),
                        "funnelish_order_number": order.get("order_number", ""),
                        "funnelish_product_name": product_name,
                        "oto_category": category,
                        "supply": variant["supply"],
                        "amount": funnelish_amount,
                        "created_at": order.get("created_at", ""),
                        "shopify_sku": variant["sku"],
                        "shopify_price": variant["price"],
                        # Address fields — populated by enrich_with_addresses()
                        "shipping_address1": "",
                        "shipping_address2": "",
                        "shipping_city": "",
                        "shipping_state": "",
                        "shipping_zip": "",
                        "shipping_country": "",
                    })

    return (
        sorted(missing_main, key=lambda x: x["created_at"]),
        sorted(missing_otos, key=lambda x: x["created_at"]),
    )


# Backwards-compat alias kept for any external callers
def find_missing_otos(funnelish_sessions, shopify_lookup):
    """Deprecated: use find_missing_orders() instead."""
    _, otos = find_missing_orders(funnelish_sessions, shopify_lookup)
    return otos

def enrich_with_addresses(missing: List[Dict], token: str) -> None:
    """
    Fetch shipping address for each missing order from Funnelish customer API.
    Mutates each row in-place, adding shipping_address1/city/state/zip/country/phone.
    """
    CUSTOMER_API = "https://customers.v2.api.funnelish.com/api/v1/customers"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    seen: Dict[str, Dict] = {}  # customer_id → address fields

    for row in missing:
        cid = str(row.get("customer_id", "")).strip()
        if not cid:
            continue
        if cid not in seen:
            try:
                req = urllib.request.Request(f"{CUSTOMER_API}/{cid}", headers=headers)
                with urllib.request.urlopen(req, timeout=10) as r:
                    c = json.loads(r.read())
                seen[cid] = {
                    "shipping_address1": c.get("shipping_address", "") or "",
                    "shipping_address2": c.get("shipping_address2", "") or "",
                    "shipping_city":     c.get("shipping_city", "") or "",
                    "shipping_state":    c.get("shipping_state", "") or "",
                    "shipping_zip":      c.get("shipping_zip", "") or "",
                    "shipping_country":  c.get("shipping_country", "US") or "US",
                    "phone":             c.get("phone", "") or "",
                }
            except Exception as e:
                print(f"  ⚠️  Could not fetch address for customer {cid}: {e}")
                seen[cid] = {}
        row.update(seen[cid])


# ─── Output: CSV ────────────────────────────────────────────────────────────────

def save_missing_csv(missing: List[Dict], date: datetime) -> str:
    """Save missing orders to CSV. Returns file path."""
    date_str = date.strftime("%Y-%m-%d")
    output_dir = os.path.join(os.path.dirname(__file__), "output")
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"missing_orders_{date_str}.csv")

    fieldnames = [
        "order_type",  # MAIN or OTO
        "email", "first_name", "last_name", "customer_id", "phone",
        "funnelish_order_id", "funnelish_order_number",
        "funnelish_product_name", "oto_category", "supply",
        "amount", "created_at",
        "shopify_sku", "shopify_price",
        "shipping_address1", "shipping_address2",
        "shipping_city", "shipping_state", "shipping_zip", "shipping_country",
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(missing)

    print(f"  ✅ Saved: {path}")
    return path

# ─── Output: Slack ──────────────────────────────────────────────────────────────

def write_to_sheet(missing: List[Dict], date_str: str, dry_run: bool = False) -> str:
    """
    POST missing orders to the Apps Script web app, which writes them
    to a new tab named date_str in the Google Sheet.
    Returns the direct sheet tab URL, or the base sheet URL on failure.
    """
    sheet_tab_url = f"{GOOGLE_SHEET_URL}#gid=0"  # fallback

    if dry_run:
        print("  [DRY RUN] Would write to Google Sheet.")
        return sheet_tab_url

    if not APPS_SCRIPT_WEB_APP:
        print("  ⚠️  APPS_SCRIPT_WEB_APP not set. Skipping sheet write.")
        return sheet_tab_url

    orders_payload = []
    for o in missing:
        orders_payload.append({
            "order_type":            o.get("order_type", "OTO"),
            "order_number":          o.get("order_number", ""),
            "email":                 o.get("email", ""),
            "first_name":            o.get("first_name", ""),
            "last_name":             o.get("last_name", ""),
            "oto_category":          o.get("oto_category", ""),
            "funnelish_product_name": o.get("funnelish_product_name", ""),
            "shopify_sku":           o.get("shopify_sku", ""),
            "shopify_price":         o.get("shopify_price", 0),
            "status":                "pending",
        })

    payload = json.dumps({"date": date_str, "orders": orders_payload}).encode()
    # Google Apps Script POST redirects (302) — must follow redirect as GET
    class _RedirectAsGet(urllib.request.HTTPRedirectHandler):
        def http_error_302(self, req, fp, code, msg, headers):
            new_url = headers.get("Location")
            return urllib.request.urlopen(urllib.request.Request(new_url, method="GET"), timeout=30)
        http_error_301 = http_error_302

    opener = urllib.request.build_opener(_RedirectAsGet())
    req = urllib.request.Request(
        APPS_SCRIPT_WEB_APP,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = opener.open(req, timeout=30)
        result = json.loads(resp.read())
        if result.get("ok"):
            rows = result.get("rows", len(missing))
            print(f"  ✅ Google Sheet updated: {rows} rows written to tab '{date_str}'")
            sheet_tab_url = f"https://docs.google.com/spreadsheets/d/1ItfhBGTuV8dfsres3j2YE0H7lIutd0brSrW-Uak-f2E/edit"
        else:
            print(f"  ❌ Sheet write failed: {result.get('error', 'unknown error')}")
    except Exception as e:
        print(f"  ❌ Sheet write error: {e}")

    return sheet_tab_url


def send_slack_notification(
    missing_main: List[Dict],
    missing_otos: List[Dict],
    date_str: str,
    csv_path: str,
    dry_run: bool = False,
    sheet_url: str = None,
) -> None:
    """Send Slack notification with missing orders summary (MAIN + OTO breakdown)."""
    if not SLACK_WEBHOOK_URL:
        print("  ⚠️  SLACK_WEBHOOK_URL not set. Skipping Slack notification.")
        print("     Set it in .env or environment: export SLACK_WEBHOOK_URL='https://hooks.slack.com/...'")
        return

    if dry_run:
        print("  [DRY RUN] Would send Slack notification.")
        return

    sheet_link = sheet_url or GOOGLE_SHEET_URL
    tag = f"<@{SLACK_CS_USER_ID}>" if SLACK_CS_USER_ID else ""

    total_missing = len(missing_main) + len(missing_otos)
    main_value = sum(float(o.get("shopify_price", 0)) for o in missing_main)
    oto_value  = sum(float(o.get("shopify_price", 0)) for o in missing_otos)
    total_value = main_value + oto_value

    # OTO breakdown by category
    by_cat: Dict[str, int] = defaultdict(int)
    for o in missing_otos:
        by_cat[o["oto_category"]] += 1
    oto_lines = "\n".join(
        f"      ◦ {cat}: {count}" for cat, count in sorted(by_cat.items())
    )

    summary_lines = []
    summary_lines.append(f"  • Missing MAIN orders: *{len(missing_main)}* — ${main_value:,.2f}")
    if missing_otos:
        summary_lines.append(f"  • Missing OTO orders: *{len(missing_otos)}* — ${oto_value:,.2f}")
        if oto_lines:
            summary_lines.append(oto_lines)
    summary_lines.append(f"  • *Total missing: {total_missing} — ${total_value:,.2f}*")

    msg = {
        "channel": SLACK_CHANNEL,
        "text": (
            f":rotating_light: *TryBello Sync Alert — {date_str}*\n\n"
            f"{tag} Found *{total_missing} order(s)* missing from Shopify:\n"
            + "\n".join(summary_lines) + "\n\n"
            f"📊 *<{sheet_link}|View sheet — {date_str} tab>*\n\n"
            f"📋 Review the sheet tab, then run the Slack slash command:\n"
            f">`/approve-otos {date_str}`"
        )
    }

    data = json.dumps(msg).encode()
    req = urllib.request.Request(SLACK_WEBHOOK_URL, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"  ✅ Slack notification sent.")
    except Exception as e:
        print(f"  ❌ Slack notification failed: {e}")

# ─── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TryBello Funnelish-Shopify OTO sync checker")
    parser.add_argument("date", nargs="?", help="Date to check (YYYY-MM-DD). Default: yesterday.")
    parser.add_argument("--dry-run", action="store_true", help="Don't write CSV or send Slack notifications.")
    parser.add_argument("--no-slack", action="store_true", help="Write CSV + Sheet but skip Slack notification (used by approval flow).")
    parser.add_argument("--csv-fallback", action="store_true", help="Use CSV input instead of API.")
    parser.add_argument("--no-funnelish", action="store_true", help="Skip Funnelish fetch (use cached CSV).")
    args = parser.parse_args()

    # Determine target date
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        target_date = datetime.utcnow().replace(tzinfo=timezone.utc) - timedelta(days=1)
        target_date = target_date.replace(tzinfo=None)

    date_str = target_date.strftime("%Y-%m-%d")
    print(f"\n{'='*60}")
    print(f"  TryBello OTO Sync Checker — {date_str}")
    print(f"{'='*60}\n")

    # ── Step 1: Fetch Funnelish orders ──────────────────────────────
    print("📦 Fetching Funnelish orders...")
    if args.csv_fallback or args.no_funnelish:
        funnelish_orders = fetch_funnelish_orders_csv(target_date)
    else:
        try:
            funnelish_orders = fetch_funnelish_orders_api(target_date, target_date)
        except Exception as e:
            print(f"  ❌ Funnelish API error: {e}")
            print("  🔄 Trying CSV fallback...")
            funnelish_orders = fetch_funnelish_orders_csv(target_date)

    # ── Step 2: Group into customer sessions ───────────────────────
    print("\n🔍 Grouping Funnelish sessions...")
    sessions = group_funnelish_sessions(funnelish_orders)
    total_sessions = len(sessions)
    sessions_with_oto = sum(1 for cats in sessions.values() if any(k != "MAIN" for k in cats))
    print(f"  Total customers: {total_sessions}")
    print(f"  Customers with OTOs: {sessions_with_oto}")

    # ── Step 3: Fetch Shopify orders ───────────────────────────────
    print("\n🛒 Fetching Shopify orders...")
    shopify_token = get_shopify_token()
    shopify_orders = fetch_shopify_orders(shopify_token, target_date, target_date)
    shopify_lookup = build_shopify_lookup(shopify_orders)
    print(f"  Unique Shopify customer emails: {len(shopify_lookup)}")

    # ── Step 4: Find missing MAIN + OTO orders ────────────────────
    print("\n🔎 Running sync check (MAIN + OTO)...")
    missing_main, missing_otos = find_missing_orders(sessions, shopify_lookup)
    all_missing = missing_main + missing_otos

    # ── Step 5: Report ─────────────────────────────────────────────
    print(f"\n{'='*60}")
    if not all_missing:
        print(f"  ✅ NO MISSING ORDERS for {date_str}. All synced!")
        print(f"{'='*60}\n")
        return

    main_value = sum(float(o.get("shopify_price", 0)) for o in missing_main)
    oto_value  = sum(float(o.get("shopify_price", 0)) for o in missing_otos)
    total_value = main_value + oto_value

    print(f"  🚨 FOUND {len(all_missing)} MISSING ORDER(S) for {date_str}:")
    print(f"    • Missing MAIN orders: {len(missing_main)} — ${main_value:,.2f}")
    if missing_main:
        for o in missing_main[:3]:
            print(f"        - {o['email']} | {o['funnelish_product_name']} | ${o['amount']:.2f}")
        if len(missing_main) > 3:
            print(f"        ... and {len(missing_main)-3} more")

    print(f"    • Missing OTO orders:  {len(missing_otos)} — ${oto_value:,.2f}")
    if missing_otos:
        from collections import Counter
        cat_counts = Counter(o["oto_category"] for o in missing_otos)
        for cat, count in sorted(cat_counts.items()):
            print(f"        ◦ {cat}: {count}")
        for o in missing_otos[:3]:
            print(f"        - {o['email']} | {o['funnelish_product_name']} | ${o['amount']:.2f} | {o['shopify_sku']}")
        if len(missing_otos) > 3:
            print(f"        ... and {len(missing_otos)-3} more")

    print(f"    ─────────────────────────────────────────")
    print(f"    • Total missing: {len(all_missing)} — ${total_value:,.2f}")
    print(f"{'='*60}\n")

    # ── Step 6: Enrich with shipping addresses ─────────────────────
    print("\n📦 Fetching shipping addresses from Funnelish...")
    funnelish_token = get_token()
    enrich_with_addresses(all_missing, funnelish_token)

    # ── Step 7: Save CSV ───────────────────────────────────────────
    print("\n💾 Saving CSV...")
    csv_path = save_missing_csv(all_missing, target_date)

    # ── Step 8: Write to Google Sheet ──────────────────────────────
    print("\n📊 Writing to Google Sheet...")
    sheet_url = write_to_sheet(all_missing, date_str, dry_run=args.dry_run)

    # ── Step 9: Slack notification ─────────────────────────────────
    skip_slack = args.dry_run or args.no_slack
    print("\n📢 Sending notification...")
    send_slack_notification(
        missing_main, missing_otos, date_str, csv_path,
        dry_run=skip_slack, sheet_url=sheet_url
    )

    print(f"\n✅ Done! Next step: review {csv_path}")
    print(f"   Then run: python3 push_orders_to_shopify.py {csv_path}")
    print()


if __name__ == "__main__":
    main()
