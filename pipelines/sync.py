#!/usr/bin/env python3
"""
sync.py — NLT Alpha Sniper

Data contribution uploader — DaaS foundation.
Called by post_mortem.py after Phase 1 and Phase 2.

Phase 1: partial trade_summary, shadow_complete=false
Phase 2: updated trade_summary with ceiling data, shadow_complete=true

Uploads anonymized trade records to NLT aggregate API.
Sync is opt-in — set data.sharing_enabled=true in config.json.

Usage (called by post_mortem.py):
    python3 sync.py --phase 1 --trade-id <uuid> --shared true --complete false
"""

import argparse
import hashlib
import json
import os
import socket
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

from paths import get_path, log_error

TRADE_SUMMARY_PATH = get_path("master_summary")

SYNC_KEY      = os.environ.get("NLT_SYNC_KEY", "NLT-0qiuzKHoeFcxBZv40igfc04oAZYFqj3d")
SYNC_ENDPOINT = os.environ.get("NLT_SYNC_ENDPOINT", "https://nlt-trades.frankykho1.workers.dev")


def _load_trade_record(trade_id: str) -> dict | None:
    """Load a single trade record from master_summary.jsonl by trade_id."""
    if not os.path.exists(TRADE_SUMMARY_PATH):
        return None

    with open(TRADE_SUMMARY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("id") == trade_id:
                    return rec
            except Exception:
                continue

    return None


def _post_to_worker(payload: dict) -> bool:
    """POST payload to the sync worker endpoint. Returns True on 200, False otherwise."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            SYNC_ENDPOINT,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-nlt-key": SYNC_KEY,
                "User-Agent": "NLT-AlphaSniper/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception as e:
        log_error("sync", f"upload error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="NLT Alpha Sniper — Sync")
    parser.add_argument("--phase",    type=int, required=True, choices=[1, 2])
    parser.add_argument("--trade-id", type=str, required=True)
    parser.add_argument("--shared",   type=str, required=True)
    parser.add_argument("--complete", type=str, required=True)
    args = parser.parse_args()

    shared = args.shared.lower() == "true"

    if not shared:
        print(f"[sync] sharing disabled — skipping {args.trade_id[:8]}…")
        sys.exit(0)

    # ── Load trade record ──────────────────────────────────
    trade_record = _load_trade_record(args.trade_id)
    if not trade_record:
        log_error("sync", f"trade record not found: {args.trade_id[:8]}")
        sys.exit(1)

    # Block stale and invalid trades
    if not trade_record.get("is_valid", True) or trade_record.get("exit_reason") == "STALE":
        sys.exit(0)

    # ── Machine identifier — anonymous, hashed hostname ────
    user_id = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16]

    # ── Build payload ──────────────────────────────────────
    payload = {
        "id":               args.trade_id,
        "user_id":          user_id,
        "tier":             "free",
        "is_trial":         False,
        "phase":            args.phase,
        "pipeline_version": trade_record.get("pipeline_version", "unknown"),
        "payload":          trade_record,
    }

    print(f"[sync] Phase {args.phase} — {args.trade_id[:8]}…")

    success = _post_to_worker(payload)

    if success:
        print(f"[sync] ✓ uploaded")
        sys.exit(0)
    else:
        print(f"[sync] ✗ upload failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
