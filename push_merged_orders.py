#!/usr/bin/env python3
"""
Push missing OTO orders to Shopify — pre-merged by customer+date.

Groups multiple OTO line items from the same customer on the same day into
a single Shopify order (mimics what the merger app would do, but up-front
so the merger app has nothing to do).

Usage:
    python3 push_merged_orders.py [--dry-run] CSVFILE [CSVFILE ...]

    # Push Mar 7-12 (all at once):
    python3 push_merged_orders.py --dry-run output/missing_orders_2026-03-{07,08,09,10,11,12}.csv
"""

import csv
import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).parent))
from config import SHOPIFY_SHOP, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET
from shopify_utils import safe_to_delete

# Full SKU → variant_id map (from push_orders_to_shopify.py)
SKU_VARIANT_CACHE: Dict[str, int] = {
    "HS-GR-01": 53649862132022,
    "HS-GR-02": 53649862164790,
    "HS-GR-03": 53649862197558,
    "HS-GR-04": 52022319350070,
    "HS-GR-05": 52950157164854,
    "HS-GR-06": 52022421324086,
    "HS-GR-08": 53284232790326,
    "HS-GR-09": 51727018131766,
    "HC-GR-01": 51683644113206,
    "HC-GR-02": 51683644145974,
    "HC-GR-03": 51683644178742,
    "HC-GR-04": 52023443947830,
    "HC-GR-05": 51282358731062,
    "HC-GR-06": 52023497326902,
    "HC-GR-09": 51727025471798,
    "HB-GR-01": 51507428983094,
    "HB-GR-02": 52023564763446,
    "HB-GR-03": 51819958075702,
    "HB-GR-04": 50203254751542,
    "HB-GR-06": 50203077017910,
    "HB-GR-09": 50203086717238,
    "PPP-01":   52762993492278,
}


def get_shopify_token() -> str:
    data = json.dumps({
        "client_id": SHOPIFY_CLIENT_ID,
        "client_secret": SHOPIFY_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(
        f"https://{SHOPIFY_SHOP}/admin/oauth/access_token",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())["access_token"]


def load_csvs(paths: List[str]) -> List[Dict]:
    rows = []
    for path in paths:
        try:
            with open(path) as f:
                file_date = Path(path).stem.split("missing_orders_")[-1][:10]
                for row in csv.DictReader(f):
                    row["_file_date"] = file_date
                    rows.append(row)
            print(f"  Loaded {path}")
        except FileNotFoundError:
            print(f"  ⚠️  Not found: {path} — skipping")
    return rows


def group_rows(rows: List[Dict]) -> Dict[Tuple, List[Dict]]:
    """Group by (email, file_date) — same customer, same day = one order."""
    groups: Dict[Tuple, List[Dict]] = defaultdict(list)
    for row in rows:
        key = (row["email"].lower().strip(), row["_file_date"])
        groups[key].append(row)
    return dict(groups)


def build_merged_order(rows: List[Dict], date_str: str, shopify_token: str = "") -> Dict:
    """Build a single Shopify order with multiple line items."""
    ref = rows[0]  # use first row for customer info
    email = ref["email"].strip()
    first_name = (ref.get("first_name") or "").strip().title()
    last_name = (ref.get("last_name") or "").strip().title()
    phone = ref.get("phone") or ""

    # Build line items
    line_items = []
    funnelish_ids = []
    for row in rows:
        sku = row.get("shopify_sku", "")
        variant_id = SKU_VARIANT_CACHE.get(sku)
        if variant_id:
            line_items.append({"variant_id": variant_id, "quantity": 1})
        else:
            price = str(row.get("shopify_price") or row.get("amount") or 0)
            line_items.append({
                "title": row.get("funnelish_product_name", sku),
                "sku": sku,
                "price": price,
                "quantity": 1,
                "requires_shipping": True,
            })
            print(f"  ⚠️  No variant_id for SKU {sku} ({email}) — custom line item")
        fid = row.get("funnelish_order_id", "")
        fnum = row.get("funnelish_order_number", fid)
        funnelish_ids.append(f"{fid} (#{fnum})")

    note = f"Recovered from Funnelish sync failure — Funnelish order IDs: {', '.join(funnelish_ids)}"

    order = {
        "email": email,
        "financial_status": "paid",
        "fulfillment_status": None,
        "send_receipt": False,
        "send_fulfillment_receipt": False,
        "taxes_included": True,   # Customer already paid exact amount via Funnelish
        "shipping_lines": [],     # No shipping charges
        "line_items": line_items,
        "tags": f"funnelish-recovery,{date_str}",
        "note": note,
        "source_name": "funnelish-recovery",
        "customer": {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
        },
    }

    # Shipping address
    addr1 = ref.get("shipping_address1") or ""
    city = ref.get("shipping_city") or ""
    state = ref.get("shipping_state") or ""
    zip_ = ref.get("shipping_zip") or ""
    country = ref.get("shipping_country") or "US"

    if addr1 and city:
        order["shipping_address"] = {
            "first_name": first_name,
            "last_name": last_name,
            "address1": addr1,
            "address2": ref.get("shipping_address2") or "",
            "city": city,
            "province": state,
            "zip": zip_,
            "country": country,
            "phone": phone,
        }
    else:
        # ── FALLBACK: look up from Shopify customer's existing orders ──
        print(f"  ⚠️  No address in CSV for {email} — looking up from Shopify...")
        if shopify_token:
            import urllib.parse as _up
            shop = os.getenv("SHOPIFY_SHOP", "trybello.myshopify.com")
            url = (f"https://{shop}/admin/api/2024-01/orders.json"
                   f"?email={_up.quote(email)}&status=any&limit=10")
            req = urllib.request.Request(url, headers={"X-Shopify-Access-Token": shopify_token})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    prev_orders = json.loads(r.read()).get("orders", [])
                for po in prev_orders:
                    sa = po.get("shipping_address") or {}
                    if sa.get("address1") and sa.get("city"):
                        order["shipping_address"] = {
                            "first_name": first_name,
                            "last_name": last_name,
                            "address1": sa.get("address1", ""),
                            "address2": sa.get("address2", "") or "",
                            "city":     sa.get("city", ""),
                            "province": sa.get("province", ""),
                            "zip":      sa.get("zip", ""),
                            "country":  sa.get("country", "US") or "US",
                            "phone":    sa.get("phone", "") or phone,
                        }
                        print(f"  ✅  Got address from Shopify: {sa.get('address1')}, {sa.get('city')}")
                        break
                else:
                    print(f"  ❌  No address found in Shopify for {email}")
            except Exception as e:
                print(f"  ❌  Shopify address lookup failed for {email}: {e}")
        else:
            print(f"  ❌  No shopify_token — cannot look up address for {email}")

    return order


def create_shopify_order(token: str, order: Dict, dry_run: bool) -> Dict:
    if dry_run:
        skus = [li.get("sku") or f"variant:{li.get('variant_id')}" for li in order["line_items"]]
        return {"id": "DRY_RUN", "name": "#DRY-RUN", "skus": skus}

    url = f"https://{SHOPIFY_SHOP}/admin/api/2024-01/orders.json"
    data = json.dumps({"order": order}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["order"]


def main():
    dry_run = "--dry-run" in sys.argv
    csv_paths = [a for a in sys.argv[1:] if not a.startswith("--")]

    if not csv_paths:
        print("Usage: python3 push_merged_orders.py [--dry-run] CSV [CSV ...]")
        sys.exit(1)

    print(f"{'[DRY RUN] ' if dry_run else ''}Loading CSVs...")
    rows = load_csvs(csv_paths)
    print(f"Total rows: {len(rows)}")

    groups = group_rows(rows)
    print(f"Grouped into {len(groups)} orders ({sum(1 for v in groups.values() if len(v)>1)} merged, {sum(1 for v in groups.values() if len(v)==1)} single)")

    if not dry_run:
        confirm = input(f"\nPush {len(groups)} orders to Shopify? Type YES to confirm: ")
        if confirm.strip() != "YES":
            print("Aborted.")
            sys.exit(0)

    token = None if dry_run else get_shopify_token()

    created = 0
    failed = 0
    total_value = 0.0

    for i, ((email, date_str), order_rows) in enumerate(groups.items(), 1):
        order_payload = build_merged_order(order_rows, date_str, shopify_token=token or "")
        skus = [r["shopify_sku"] for r in order_rows]
        value = sum(float(r.get("shopify_price") or r.get("amount") or 0) for r in order_rows)

        try:
            result = create_shopify_order(token, order_payload, dry_run)
            if dry_run:
                print(f"  [{i}/{len(groups)}] ✅ DRY RUN — {email} | {skus} | ${value:.2f}")
            else:
                print(f"  [{i}/{len(groups)}] ✅ Created {result['name']} (id:{result['id']}) — {email} | {skus} | ${value:.2f}")
            created += 1
            total_value += value
        except urllib.error.HTTPError as e:
            body = e.read()[:200]
            print(f"  [{i}/{len(groups)}] ❌ FAILED — {email}: HTTP {e.code}: {body}")
            failed += 1

        if not dry_run and i % 10 == 0:
            time.sleep(0.5)  # rate limit

    print(f"\n{'==='*20}")
    mode = "DRY RUN" if dry_run else "PUSHED"
    print(f"{mode}: {created}/{len(groups)} orders | ${total_value:,.2f} total | {failed} failures")

    if dry_run and failed == 0:
        print(f"\nDry run clean. Run without --dry-run to push live.")


if __name__ == "__main__":
    main()
