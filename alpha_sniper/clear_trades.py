#!/usr/bin/env python3
"""
clear_trades.py — NLT Alpha Sniper
Removes compiled trade records from paper_trades.jsonl and paper_ticks.jsonl.
Uses master_summary.jsonl as source of truth.

RULE: if trade id exists in master_summary → safe to remove from raw files.
      if trade id NOT in master_summary   → keep (not yet compiled).

NEVER TOUCHES:
    master_summary.jsonl  (source of truth, never modified)

Usage:
    python3 clear_trades.py
    python3 clear_trades.py --yes   # skip confirmation
"""

import argparse
import json
import os
import sys

from paths import get_path

# ============================================================
# CONSTANTS
# ============================================================

PAPER_TRADES_PATH   = get_path("paper_trades")
PAPER_TICKS_PATH    = get_path("paper_ticks")
SHADOW_TICKS_PATH   = get_path("shadow_ticks")
WALLET_TICKS_PATH   = get_path("wallet_ticks")
MASTER_SUMMARY_PATH = get_path("master_summary")


# ============================================================
# HELPERS
# ============================================================

def _read_compiled_ids() -> set:
    """Load all trade IDs from master_summary.jsonl."""
    ids = set()
    if not os.path.exists(MASTER_SUMMARY_PATH):
        return ids
    with open(MASTER_SUMMARY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                tid = rec.get("id")
                if tid:
                    ids.add(tid)
            except Exception:
                continue
    return ids


def _read_compiled_mints() -> set:
    """Load all mints from master_summary.jsonl."""
    mints = set()
    if not os.path.exists(MASTER_SUMMARY_PATH):
        return mints
    with open(MASTER_SUMMARY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                mint = rec.get("mint")
                if mint:
                    mints.add(mint)
            except Exception:
                continue
    return mints


def _filter_jsonl_by_mint(path: str, compiled_mints: set) -> tuple:
    """Filter JSONL by mint field instead of id field."""
    kept      = []
    removed   = 0
    kept_count = 0

    if not os.path.exists(path):
        return [], 0, 0

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec  = json.loads(stripped)
                mint = rec.get("mint")
                if mint and mint in compiled_mints:
                    removed += 1
                else:
                    kept.append(stripped)
                    kept_count += 1
            except Exception:
                kept.append(stripped)
                kept_count += 1

    return kept, removed, kept_count


def _filter_jsonl(path: str, compiled_ids: set) -> tuple:
    """
    Read a JSONL file, split into keep/remove by compiled_ids.
    Returns (kept_lines, removed_count, kept_count).
    """
    kept   = []
    removed = 0
    kept_count = 0

    if not os.path.exists(path):
        return [], 0, 0

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
                tid = rec.get("id")
                if tid and tid in compiled_ids:
                    removed += 1
                else:
                    kept.append(stripped)
                    kept_count += 1
            except Exception:
                # Keep malformed lines — don't silently drop them
                kept.append(stripped)
                kept_count += 1

    return kept, removed, kept_count


def _rewrite_jsonl(path: str, lines: list) -> None:
    """Rewrite a JSONL file with the given lines."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    os.replace(tmp, path)


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="NLT Alpha Sniper — Clear Trades")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    # Step 1 — load compiled IDs
    if not os.path.exists(MASTER_SUMMARY_PATH):
        print("master_summary.jsonl not found — nothing to do")
        sys.exit(0)

    compiled_ids = _read_compiled_ids()
    if not compiled_ids:
        print("master_summary.jsonl is empty — nothing to do")
        sys.exit(0)

    print(f"master_summary: {len(compiled_ids)} compiled trade(s) found\n")

    # Step 2 — dry scan both files
    trades_kept,  trades_removed,  _ = _filter_jsonl(PAPER_TRADES_PATH,  compiled_ids)
    ticks_kept,   ticks_removed,   _ = _filter_jsonl(PAPER_TICKS_PATH,   compiled_ids)
    shadow_kept,  shadow_removed,  _ = _filter_jsonl(SHADOW_TICKS_PATH,  compiled_ids)
    compiled_mints                   = _read_compiled_mints()
    wallet_kept,  wallet_removed,  _ = _filter_jsonl_by_mint(WALLET_TICKS_PATH, compiled_mints)

    total_removed = trades_removed + ticks_removed + shadow_removed + wallet_removed
    if total_removed == 0:
        print("nothing to remove — raw files already clean")
        sys.exit(0)

    print("⚠  the following records will be permanently removed:\n")
    print(f"   paper_trades.jsonl   {trades_removed} records removed  {len(trades_kept)} kept")
    print(f"   paper_ticks.jsonl    {ticks_removed} records removed  {len(ticks_kept)} kept")
    print(f"   shadow_ticks.jsonl   {shadow_removed} records removed  {len(shadow_kept)} kept")
    print(f"   wallet_ticks.jsonl   {wallet_removed} records removed  {len(wallet_kept)} kept")
    print(f"\n   master_summary.jsonl is NOT modified.")

    if len(trades_kept) > 0:
        print(f"\n   {len(trades_kept)} trade record(s) kept — not yet in master_summary.")

    if not args.yes:
        print()
        try:
            confirm = input("   proceed? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\ncancelled")
            sys.exit(0)
        if confirm != "y":
            print("cancelled")
            sys.exit(0)

    # Step 4 — rewrite files
    errors = 0

    for path, kept, removed, label in [
        (PAPER_TRADES_PATH,  trades_kept,  trades_removed,  "paper_trades.jsonl"),
        (PAPER_TICKS_PATH,   ticks_kept,   ticks_removed,   "paper_ticks.jsonl"),
        (SHADOW_TICKS_PATH,  shadow_kept,  shadow_removed,  "shadow_ticks.jsonl"),
        (WALLET_TICKS_PATH,  wallet_kept,  wallet_removed,  "wallet_ticks.jsonl"),
    ]:
        try:
            _rewrite_jsonl(path, kept)
            print(f"   ✓ {label:<25} {removed} removed  {len(kept)} kept")
        except Exception as e:
            print(f"   ✗ {label} failed: {e}")
            errors += 1

    if errors:
        print(f"\n⚠  {errors} error(s) — check output above")
        sys.exit(1)
    else:
        print(f"\ndone — raw trade data cleaned")


if __name__ == "__main__":
    main()
