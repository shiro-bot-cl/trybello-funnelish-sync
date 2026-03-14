# TryBello Funnelish-Shopify OTO Sync

Automated daily checker that finds OTO orders captured in Funnelish but missing from Shopify.

## Overview

When customers complete a funnel on Funnelish, OTO (Order To Offer) upsells are sometimes not synced to Shopify. This tool:
1. Fetches Funnelish orders for a given date
2. Fetches Shopify orders for the same date
3. Finds OTO items in Funnelish that are missing from Shopify
4. Generates a CSV and sends a Slack notification for review
5. Provides a separate script to push missing orders to Shopify

## Quick Start

```bash
# Install requirements (minimal - uses stdlib)
pip install playwright && playwright install chromium  # Optional: for auto token refresh

# Save your Funnelish JWT token (get from browser, valid 24h)
python3 funnelish_auth.py "YOUR_JWT_TOKEN_HERE"

# Run sync checker for yesterday
python3 daily_sync.py

# Run for specific date
python3 daily_sync.py 2026-03-13

# Dry run (no Slack notification)
python3 daily_sync.py --dry-run
```

## Pushing Missing Orders to Shopify

```bash
# Always dry-run first!
python3 push_orders_to_shopify.py output/missing_orders_2026-03-13.csv --dry-run

# Then push for real (requires "YES" confirmation)
python3 push_orders_to_shopify.py output/missing_orders_2026-03-13.csv

# Lookup Shopify variant IDs (run once to fill SKU_VARIANT_CACHE)
python3 push_orders_to_shopify.py --lookup-variants
```

## Product Classification Rules

### Funnelish → Category
| Product name contains | Category |
|---|---|
| "hair growth shampoo" | OTO1_Shampoo |
| "hair growth conditioner" | OTO2_Conditioner |
| "daily hair booster" | OTO3_Booster |
| "porch pirate" | OTO4_PorchPirate |
| Everything else | MAIN |

### Shopify SKU → Category
| SKU prefix | Category |
|---|---|
| HH-NF, HH-PL | MAIN |
| HS-GR | OTO1_Shampoo |
| HC-GR | OTO2_Conditioner |
| HB-GR | OTO3_Booster |
| PPP-01 | OTO4_PorchPirate |

## SKU Recovery Mappings
| OTO Category | Shopify SKU | Price |
|---|---|---|
| OTO1_Shampoo | HS-GR-06 | $90 |
| OTO2_Conditioner | HC-GR-03 | $90 |
| OTO3_Booster | HB-GR-03 | $75 |
| OTO4_PorchPirate | PPP-01 | $9.95 |

## Token Management

The Funnelish JWT token expires every 24 hours.

**To refresh:**
1. Log into https://app.funnelish.com
2. Open Dev Tools → Console
3. Run: `copy(localStorage.getItem('user-token'))`
4. Run: `python3 funnelish_auth.py "<PASTE_TOKEN>"`

**With Playwright installed**, the scripts will auto-refresh the token.

## Cron Setup

To run daily at 6 AM:
```bash
# crontab -e
0 6 * * * cd /Users/serhiibiletskyi/Projects/funnelish-sync && python3 daily_sync.py >> logs/cron.log 2>&1
```

## Needs From Serge
- [ ] **Slack webhook URL** for notifications (https://api.slack.com/apps → Incoming Webhooks)
- [ ] **Confirm product classifications** - are the product name rules correct? Check against real Funnelish data.
- [ ] **Shopify variant IDs** - run `python3 push_orders_to_shopify.py --lookup-variants` and confirm SKU matches
- [ ] **Token refresh method** - install Playwright, or set up daily token refresh cronjob

## File Structure
```
funnelish-sync/
├── daily_sync.py          # Main sync checker script
├── push_orders_to_shopify.py  # Recovery script (push missing orders)
├── funnelish_auth.py      # Token management
├── config.py              # Configuration
├── .env.example           # Environment variable template
├── .funnelish_token        # JWT token (auto-saved, gitignored)
├── csv_imports/           # Drop Funnelish CSV exports here (fallback)
├── output/                # Generated CSVs with missing orders
└── logs/                  # Cron logs
```
