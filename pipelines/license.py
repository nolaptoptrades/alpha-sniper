"""
license.py — NLT Alpha Sniper License Validator
Ships inside Nuitka binary. Offline validation only.
Never raises exceptions — always falls back to free tier.
"""

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict

# ── Constants ────────────────────────────────────────────────
_SECRET = "MwLf68e78TmsUtt7GmCv8e1mZ6YsqC5m-SPGLGQaS8M"
if not _SECRET:
    # falls back to free tier silently — license.py never hard stops
    pass

GRACE_PERIOD_SEC = 3 * 86400   # 3 days
VALID_TIERS = {"free", "hobbyist", "pro"}


def _free(reason: str, days_remaining: int = 0) -> Dict[str, Any]:
    """Return a free-tier result."""
    return {
        "valid":          False,
        "tier":           "free",
        "key":            "",
        "reason":         reason,
        "days_remaining": days_remaining,
        "expiry_ts":      0,
        "trial":          False,  # free tier is never a trial
        "user_id":        "",
    }


def validate_license(license_path: str) -> Dict[str, Any]:
    """
    Validate a license.key file.
    Returns dict with tier, validity, and reason.
    Never raises — failures return tier="free".
    """
    try:
        # Step 1 — File exists
        if not os.path.exists(license_path):
            return _free("no_license_file")

        # Step 2 — Parse JSON
        try:
            with open(license_path) as f:
                lic = json.load(f)
        except Exception:
            return _free("invalid_format")

        # Step 3 — Required fields (added "trial")
        required = ["key", "tier", "user_id", "issued_ts", "expiry_ts", "trial", "signature"]
        if not all(k in lic for k in required):
            return _free("missing_fields")

        # Step 4 — Valid tier
        if lic["tier"] not in VALID_TIERS:
            return _free("invalid_tier")

        # Step 5 — Check expiry
        try:
            expiry_sec = int(lic["expiry_ts"], 16)
        except Exception:
            return _free("invalid_expiry")

        now = int(time.time())
        days_remaining = round((expiry_sec - now) / 86400)
        in_grace = False

        if now > expiry_sec + GRACE_PERIOD_SEC:
            return _free("expired", days_remaining=days_remaining)

        if now > expiry_sec:
            in_grace = True

        # Step 6 — Verify HMAC signature (added "trial" to payload)
        payload = {
            "key":       lic["key"],
            "tier":      lic["tier"],
            "user_id":   lic["user_id"],
            "issued_ts": lic["issued_ts"],
            "expiry_ts": lic["expiry_ts"],
            "trial":     lic["trial"],
        }
        message = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        expected = hmac.new(
            _SECRET.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected, lic.get("signature", "")):
            return _free("invalid_signature")

        # Step 7 — All checks passed (added "trial" and "user_id" to return dict)
        reason = "valid_grace" if in_grace else "valid"
        return {
            "valid":          True,
            "tier":           lic["tier"],
            "key":            lic["key"],
            "reason":         reason,
            "days_remaining": days_remaining,
            "expiry_ts":      expiry_sec,
            "trial":          lic["trial"],
            "user_id":        lic.get("user_id", ""),
        }

    except Exception:
        return _free("unexpected_error")
