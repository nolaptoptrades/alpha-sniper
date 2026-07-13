"""
activate.py — NLT Alpha Sniper License Activation
Called by cli.py when user runs: nlt --license

Decodes the NLT-[base64] key string the user received,
validates it, and writes license.key to the correct path.
"""

import base64
import json
import os
import time
from datetime import datetime, timezone

from paths import get_config, get_path

# ============================================================
# CONSTANTS
# ============================================================

KEY_PREFIX = "NLT-"


# ============================================================
# DECODE
# ============================================================

def decode_key_string(key_string: str) -> dict:
    """
    Decode a NLT-[base64] key string back to license dict.
    Raises ValueError on invalid format.
    """
    key_string = key_string.strip()
    if not key_string.startswith(KEY_PREFIX):
        raise ValueError("not a valid NLT key — should start with 'NLT-'")

    b64 = key_string[len(KEY_PREFIX):]
    # Restore padding
    b64 += "=" * (4 - len(b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(b64).decode()
        return json.loads(raw)
    except Exception as e:
        raise ValueError(f"could not decode key: {e}")


# ============================================================
# VALIDATE (pre-write sanity check)
# ============================================================

def _validate_decoded(lic: dict) -> tuple:
    """
    Basic sanity checks before writing to disk.
    Returns (ok: bool, reason: str).
    Full HMAC validation happens in license.py on startup.
    """
    required = ["key", "tier", "user_id", "issued_ts", "expiry_ts", "trial", "signature"]
    if not all(k in lic for k in required):
        return False, "key is missing required fields — may be corrupted"

    valid_tiers = {"free", "hobbyist", "pro"}
    if lic.get("tier") not in valid_tiers:
        return False, f"unknown tier '{lic.get('tier')}'"

    try:
        expiry_sec = int(lic["expiry_ts"], 16)
    except Exception:
        return False, "key has invalid expiry format"

    now = int(time.time())
    if expiry_sec < now:
        days_ago = round((now - expiry_sec) / 86400)
        return False, f"key expired {days_ago} day(s) ago"

    return True, "ok"


# ============================================================
# WRITE
# ============================================================

def _license_path() -> str:
    """Resolve license.key path — lives next to config.json at base_dir."""
    try:
        cfg = get_config()
        return os.path.join(cfg["base_dir"], "license.key")
    except Exception:
        return os.path.expanduser("~/nolaptoptrades/license.key")


def _write_license(lic: dict, key_string: str = "") -> str:
    """Write decoded license dict to license.key. Returns path written."""
    path = _license_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    data = {**lic, "key_string": key_string}
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    return path


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def run_activation():
    """
    Interactive license activation.
    Called by cli.py when --license flag is passed.
    """
    print()
    print("  NLT Alpha Sniper — License Activation")
    print("  ──────────────────────────────────────")
    print("  Paste your license key below.")
    print("  Keys start with NLT- and were sent to you by @nolaptoptrades.")
    print()

    try:
        raw = input("  Key: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n  Cancelled.")
        return

    if not raw:
        print("  No key entered. Run 'nlt --license' to try again.")
        return

    # Decode
    try:
        lic = decode_key_string(raw)
    except ValueError as e:
        print(f"\n  ✗ Invalid key — {e}")
        print(f"  Contact @nolaptoptrades if you believe this is an error.")
        return

    # Validate
    ok, reason = _validate_decoded(lic)
    if not ok:
        print(f"\n  ✗ Key rejected — {reason}")
        print(f"  Contact @nolaptoptrades if you believe this is an error.")
        return

    # Write
    try:
        path = _write_license(lic, key_string=raw)
    except Exception as e:
        print(f"\n  ✗ Could not write license file — {e}")
        return

    # Confirmation
    tier      = lic.get("tier", "unknown")
    trial     = lic.get("trial", False)
    try:
        expiry_sec  = int(lic["expiry_ts"], 16)
        days        = max(0, round((expiry_sec - time.time()) / 86400))
        expiry_str  = datetime.fromtimestamp(expiry_sec, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        days       = 0
        expiry_str = "unknown"

    trial_tag = " (trial)" if trial else ""
    secs_left = max(0, expiry_sec - int(time.time()))
    if secs_left < 86400:
        hours_left = round(secs_left / 3600, 1)
        time_left  = f"{hours_left}h remaining"
    else:
        time_left  = f"{days} days remaining"

    print()
    print(f"  ✓ License activated")
    print(f"    Tier:    {tier}{trial_tag}")
    print(f"    Expires: {expiry_str} ({time_left})")
    print(f"    Saved:   {path}")
    print()
    print(f"  Run 'nlt' to start.")
    print()
