#!/usr/bin/env python3
"""
TryBello Shopify Order Recovery — push_orders_to_shopify.py
============================================================
Reads a CSV of missing OTO orders (output of daily_sync.py) and creates
them in Shopify. Includes a dry-run mode and rate limiting.

Usage:
    python3 push_orders_to_shopify.py missing_orders_2026-03-13.csv
    python3 push_orders_to_shopify.py missing_orders_2026-03-13.csv --dry-run
    python3 push_orders_to_shopify.py missing_orders_2026-03-13.csv --limit 10

⚠️  IMPORTANT: Always run with --dry-run first and review output before pushing!
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(__file__))
from config import (
    SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, SHOPIFY_SHOP,
    SHOPIFY_API_VERSION, OTO_SKU_MAP
)

# ─── Shopify variant lookup cache ───────────────────────────────────────────────
# Pre-populated with known variant IDs (auto-discovered 2026-03-14).
# NOTE: Multiple variants found per SKU (different product listings).
# ⚠️  SERGE: Verify these are the correct "active" variants before live run!
# Run --lookup-variants to re-discover all matches.
SKU_VARIANT_CACHE: Dict[str, Optional[int]] = {
    # Shampoo
    "HS-GR-01": 53649862132022,
    "HS-GR-02": 53649862164790,
    "HS-GR-03": 53649862197558,
    "HS-GR-04": 52022319350070,
    "HS-GR-05": 52950157164854,
    "HS-GR-06": 52022421324086,
    "HS-GR-08": 53284232790326,
    "HS-GR-09": 51727018131766,
    # Conditioner
    "HC-GR-01": 51683644113206,
    "HC-GR-02": 51683644145974,
    "HC-GR-03": 51683644178742,
    "HC-GR-04": 52023443947830,
    "HC-GR-05": 51282358731062,
    "HC-GR-06": 52023497326902,
    "HC-GR-09": 51727025471798,
    # Booster
    "HB-GR-01": 51507428983094,
    "HB-GR-02": 52023564763446,
    "HB-GR-03": 51819958075702,
    "HB-GR-04": 50203254751542,
    "HB-GR-06": 50203077017910,
    "HB-GR-09": 50203086717238,
    # Porch Pirate
    "PPP-01":   52762993492278,
}


def get_shopify_token() -> str:
    """Get Shopify access token.
    If SHOPIFY_ACCESS_TOKEN env var is set, use it directly.
    If SHOPIFY_CLIENT_SECRET starts with 'shpss_' or 'shpat_', it IS the access token.
    Otherwise, exchange client credentials via OAuth.
    """
    # Direct token override
    direct_token = os.getenv("SHOPIFY_ACCESS_TOKEN", "")
    if direct_token:
        return direct_token
    # Only shpat_ / shpca_ are real access tokens — shpss_ is a client secret, must be exchanged
    if SHOPIFY_CLIENT_SECRET.startswith(("shpat_", "shpca_")):
        return SHOPIFY_CLIENT_SECRET
    # OAuth exchange
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
        return json.loads(resp.read())["access_token"]


def lookup_variants(token: str) -> Dict[str, int]:
    """
    Scan Shopify products to find variant IDs matching our SKUs.
    Returns: {sku: variant_id}
    """
    variant_map: Dict[str, int] = {}
    target_skus = set(SKU_VARIANT_CACHE.keys())
    page_info = None
    base_url = f"https://{SHOPIFY_SHOP}/admin/api/{SHOPIFY_API_VERSION}/products.json"

    while len(variant_map) < len(target_skus):
        if page_info:
            url = f"{base_url}?limit=250&page_info={page_info}&fields=variants"
        else:
            url = f"{base_url}?limit=250&fields=variants"

        req = urllib.request.Request(url, headers={"X-Shopify-Access-Token": token})
        with urllib.request.urlopen(req, timeout=30) as resp:
            link_header = resp.getheader("Link", "")
            data = json.loads(resp.read())

        for product in data.get("products", []):
            for variant in product.get("variants", []):
                sku = variant.get("sku", "")
                if sku in target_skus:
                    variant_map[sku] = variant["id"]
                    print(f"  Found: {sku} → variant_id {variant['id']}")

        if len(variant_map) >= len(target_skus):
            break

        # Next page
        page_info = None
        for part in link_header.split(","):
            if 'rel="next"' in part:
                url_part = part.strip().split(";")[0].strip("<>")
                parsed = urllib.parse.urlparse(url_part)
                qs = urllib.parse.parse_qs(parsed.query)
                page_info = qs.get("page_info", [None])[0]
                break
        if not page_info:
            break

    return variant_map


def build_shopify_order(row: Dict, variant_map: Dict[str, int], date_str: str) -> Dict:
    """Build Shopify order payload from a missing-orders CSV row."""
    # Use the SKU recorded in the CSV (already resolved to correct supply size)
    sku = row.get("shopify_sku", "UNKNOWN")
    price = str(row.get("shopify_price") or row.get("amount") or 0)

    # Build line item — always use variant_id if available
    variant_id = variant_map.get(sku)
    if variant_id:
        # Always pass exact Funnelish price — never let Shopify use variant price or apply tax
        line_item = {
            "variant_id": variant_id,
            "quantity": 1,
            "price": price,
            "tax_lines": [],  # Suppress tax — customer already paid exact amount via Funnelish
        }
    else:
        # Fallback: custom line item with SKU (fulfillment will require manual intervention)
        sku_info = OTO_SKU_MAP.get(row.get("oto_category", ""), {})
        title = sku_info.get("title", row.get("funnelish_product_name", sku))
        line_item = {
            "title": title,
            "sku": sku,
            "price": price,
            "quantity": 1,
            "requires_shipping": True,
            "tax_lines": [],  # Suppress tax — customer already paid exact amount via Funnelish
        }
        print(f"  ⚠️  No variant_id for SKU {sku} — using custom line item")

    # Customer info
    email = row.get("email", "")
    first_name = (row.get("first_name", "") or "").strip().title()
    last_name = (row.get("last_name", "") or "").strip().title()
    phone = row.get("phone", "") or ""
    funnelish_id = row.get("funnelish_order_id", "")
    funnelish_num = row.get("funnelish_order_number", funnelish_id)

    order = {
        "email": email,
        "financial_status": "paid",
        "fulfillment_status": None,
        "send_receipt": False,
        "send_fulfillment_receipt": False,
        "taxes_included": True,   # Taxes already included in Funnelish price — do not recalculate
        "shipping_lines": [],     # No shipping charges — customer already paid via Funnelish
        "line_items": [line_item],
        "tags": f"funnelish-recovery,{date_str}",
        "note": f"Recovered from Funnelish sync failure — original Funnelish order ID: {funnelish_id} (order #{funnelish_num})",
        "source_name": "funnelish-recovery",
    }

    # Customer block
    order["customer"] = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
    }

    # Shipping address
    addr1 = row.get("shipping_address1", "") or ""
    city  = row.get("shipping_city", "") or ""
    state = row.get("shipping_state", "") or ""
    zip_  = row.get("shipping_zip", "") or ""
    country = row.get("shipping_country", "US") or "US"

    if addr1 and city:
        order["shipping_address"] = {
            "first_name": first_name,
            "last_name": last_name,
            "address1": addr1,
            "address2": row.get("shipping_address2", "") or "",
            "city": city,
            "province": state,
            "zip": zip_,
            "country": country,
            "phone": phone,
        }
    else:
        print(f"  ⚠️  No shipping address for {email} — order will need manual address")

    return order


def create_shopify_order(token: str, order_payload: Dict, dry_run: bool = False) -> Optional[Dict]:
    """Create a single Shopify order. Returns created order or None."""
    if dry_run:
        print(f"    [DRY RUN] Would create: {order_payload['email']} | "
              f"{order_payload['line_items'][0].get('title', order_payload['line_items'][0].get('variant_id'))} | "
              f"${order_payload['line_items'][0].get('price', 'variant')}")
        return {"id": "DRY_RUN", "name": "#DRY-RUN"}

    data = json.dumps({"order": order_payload}).encode()
    req = urllib.request.Request(
        f"https://{SHOPIFY_SHOP}/admin/api/{SHOPIFY_API_VERSION}/orders.json",
        data=data,
        headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        return result.get("order", {})
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        raise Exception(f"HTTP {e.code}: {error_body[:300]}")


def main():
    parser = argparse.ArgumentParser(
        description="Push missing OTO orders from CSV to Shopify"
    )
    parser.add_argument("csv_file", nargs="?", help="Path to missing_orders CSV")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without creating orders")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max orders to create (safety cap)")
    parser.add_argument("--lookup-variants", action="store_true",
                        help="Scan Shopify products to find variant IDs and print them")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="Seconds between API calls (default: 0.5)")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  TryBello Shopify Order Recovery")
    print(f"  Mode: {'DRY RUN' if args.dry_run else '🔴 LIVE - orders WILL be created'}")
    print(f"{'='*60}\n")

    # Get Shopify token
    print("🔑 Getting Shopify token...")
    token = get_shopify_token()
    print("  ✅ Token acquired")

    # Variant lookup mode
    if args.lookup_variants:
        print("\n🔍 Looking up variant IDs...")
        variant_map = lookup_variants(token)
        print("\n✅ Variant map:")
        for sku, vid in variant_map.items():
            print(f"  {sku}: {vid}")
        print("\nAdd these to SKU_VARIANT_CACHE in push_orders_to_shopify.py")
        return

    if not args.csv_file:
        parser.print_help()
        sys.exit(1)

    # Load variant map (what we know)
    print("\n🔍 Looking up Shopify variant IDs...")
    try:
        variant_map = lookup_variants(token)
    except Exception as e:
        print(f"  ⚠️  Variant lookup failed: {e}")
        print("  Will use title-based line items instead.")
        variant_map = {}

    # Read CSV
    print(f"\n📄 Reading CSV: {args.csv_file}")
    rows = []
    with open(args.csv_file, "r") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    print(f"  Found {len(rows)} orders to process")

    if args.limit:
        rows = rows[:args.limit]
        print(f"  Limiting to {args.limit} orders (--limit flag)")

    # Confirm before live run
    if not args.dry_run:
        print(f"\n⚠️  About to create {len(rows)} REAL Shopify orders!")
        print("   This CANNOT be easily undone.")
        # Allow non-interactive confirmation via env var
        auto_confirm = os.getenv("SHOPIFY_PUSH_CONFIRM", "").strip().upper()
        if auto_confirm == "YES":
            print("   Auto-confirmed via SHOPIFY_PUSH_CONFIRM env var.")
            confirm = "YES"
        else:
            confirm = input("   Type 'YES' to confirm: ")
        if confirm.strip() != "YES":
            print("   Aborted.")
            sys.exit(0)

    # Process orders
    print(f"\n🚀 Processing {len(rows)} orders...")
    results = []
    errors = []
    date_str = datetime.utcnow().strftime("%Y-%m-%d")

    for i, row in enumerate(rows):
        try:
            order_payload = build_shopify_order(row, variant_map, date_str)
            created = create_shopify_order(token, order_payload, dry_run=args.dry_run)
            order_id = created.get("id", "?")
            order_name = created.get("name", "?")
            print(f"  [{i+1}/{len(rows)}] ✅ Created {order_name} (id:{order_id}) — "
                  f"{row['email']} | {row['oto_category']}")
            results.append({"row": row, "shopify_id": order_id, "shopify_name": order_name})
        except Exception as e:
            print(f"  [{i+1}/{len(rows)}] ❌ FAILED — {row.get('email')} | {row.get('oto_category')}: {e}")
            errors.append({"row": row, "error": str(e)})

        if args.delay > 0 and i < len(rows) - 1:
            time.sleep(args.delay)

    # Summary
    print(f"\n{'='*60}")
    print(f"  Results: {len(results)} created, {len(errors)} failed")
    if errors:
        print(f"\n  ❌ Failed orders:")
        for err in errors:
            print(f"    - {err['row'].get('email')} | {err['row'].get('oto_category')}: {err['error'][:100]}")
    print(f"{'='*60}\n")

    # Save results CSV
    if not args.dry_run and results:
        out_path = args.csv_file.replace(".csv", "_pushed.csv")
        with open(out_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["email", "oto_category", "shopify_order_id", "shopify_order_name", "funnelish_id"])
            for r in results:
                writer.writerow([
                    r["row"].get("email"), r["row"].get("oto_category"),
                    r["shopify_id"], r["shopify_name"], r["row"].get("funnelish_order_id")
                ])
        print(f"  📄 Results saved: {out_path}")


if __name__ == "__main__":
    main()
