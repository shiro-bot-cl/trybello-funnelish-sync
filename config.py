"""
TryBello Funnelish-Shopify Sync Configuration
==============================================
"""

import os

# ─── Funnelish ─────────────────────────────────────────────────────────────────
FUNNELISH_EMAIL = os.getenv("FUNNELISH_EMAIL", "")
FUNNELISH_PASSWORD = os.getenv("FUNNELISH_PASSWORD", "")
# Session JWT (expires in ~24h). Refresh via: python3 refresh_token.py
# Or set env var FUNNELISH_TOKEN to override
FUNNELISH_TOKEN = os.getenv("FUNNELISH_TOKEN", "")
FUNNELISH_ORDERS_API = "https://customers.v2.api.funnelish.com/api/v1/orders"
FUNNELISH_TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".funnelish_token")

# ─── Shopify ────────────────────────────────────────────────────────────────────
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID", "")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET", "")
SHOPIFY_SHOP = os.getenv("SHOPIFY_SHOP", "trybello.myshopify.com")
SHOPIFY_API_VERSION = "2024-01"

# ─── Slack ──────────────────────────────────────────────────────────────────────
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#trybello-cs-alert")
SLACK_CS_USER_ID = os.getenv("SLACK_CS_USER_ID", "U0ABB3AV2E8")  # Mubashir Hasan
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL", "https://docs.google.com/spreadsheets/d/1ItfhBGTuV8dfsres3j2YE0H7lIutd0brSrW-Uak-f2E/edit")
APPS_SCRIPT_WEB_APP = os.getenv("APPS_SCRIPT_WEB_APP", "https://script.google.com/macros/s/AKfycbwJfDtSnwvh8E96z6WOvNttc2pBS2r876Qyc0z5E3fVPZCzOYzywKTIGTdcR-QaSCX5/exec")
GOOGLE_SHEET_ID = "1ItfhBGTuV8dfsres3j2YE0H7lIutd0brSrW-Uak-f2E"

# ─── SKU Mappings ───────────────────────────────────────────────────────────────
# SKU suffix by supply size (derived from Funnelish product name)
# HS-GR-03 = 3-month shampoo, HS-GR-06 = 6-month, HS-GR-09 = 9-month
# Same pattern for HC-GR (conditioner) and HB-GR (booster/capsules)
OTO_SKU_PREFIX = {
    "OTO1_Shampoo":     "HS-GR",
    "OTO2_Conditioner": "HC-GR",
    "OTO3_Booster":     "HB-GR",
}

# Prices by supply size (to verify 9-month with Serge)
OTO_PRICE_BY_SUPPLY = {
    "03": 90.00,
    "06": 180.00,
    "09": 225.00,  # TODO: verify with Serge
}

# Porch pirate is a single variant
OTO_PORCH_PIRATE = {"sku": "PPP-01", "price": 9.95, "title": "Porch Pirate Protection"}

# Legacy flat map (fallback only)
OTO_SKU_MAP = {
    "OTO1_Shampoo":     {"sku": "HS-GR-03", "price": 90.00,  "title": "Hair Growth Shampoo 3-Month Supply"},
    "OTO2_Conditioner": {"sku": "HC-GR-03", "price": 90.00,  "title": "Hair Growth Conditioner 3-Month Supply"},
    "OTO3_Booster":     {"sku": "HB-GR-03", "price": 75.00,  "title": "Daily Hair Booster 3-Month Supply"},
    "OTO4_PorchPirate": {"sku": "PPP-01",   "price": 9.95,   "title": "Porch Pirate Protection"},
}

# ─── CSV Fallback ───────────────────────────────────────────────────────────────
# If Funnelish API is unavailable, drop a CSV here:
FUNNELISH_CSV_DIR = os.path.join(os.path.dirname(__file__), "csv_imports")
