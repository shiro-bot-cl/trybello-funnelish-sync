"""
Shopify utility helpers for TryBello funnelish-sync.
"""

# Tags that must NEVER be deleted without explicit human approval.
# These belong to other apps' workflows and deleting them breaks fulfillment.
PROTECTED_TAGS = {
    "merged-order",       # Created by merger app — consolidated order
    "original-merged",    # Original order archived after merge — do not delete
}


def has_protected_tag(order: dict) -> bool:
    """Return True if the order has any tag that should never be auto-deleted."""
    tags = set(t.strip().lower() for t in (order.get("tags") or "").split(",") if t.strip())
    return bool(tags & PROTECTED_TAGS)


def safe_to_delete(order: dict, required_tag: str = None) -> tuple[bool, str]:
    """
    Check if an order is safe to auto-delete.
    Returns (ok, reason).
    
    Rules:
    - Must have required_tag if specified
    - Must NOT have any PROTECTED_TAGS
    """
    tags = order.get("tags") or ""
    
    if required_tag and required_tag not in tags:
        return False, f"missing required tag '{required_tag}'"
    
    if has_protected_tag(order):
        found = [t for t in PROTECTED_TAGS if t in tags.lower()]
        return False, f"has protected tag(s): {found} — do not auto-delete"
    
    return True, "ok"
