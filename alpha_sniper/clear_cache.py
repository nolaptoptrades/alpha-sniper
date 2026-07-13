#!/usr/bin/env python3
"""
clear_cache.py — NLT Alpha Sniper
Deletes pipeline log and handshake files. Safe to run anytime — no trade data touched.
REMOVES:
    *.log files (brain, bridge_bot, discovery, safety, simulator, post_mortem, sensor,
                 brain_w, error)
    handshake jsonl (safety, entry_verdicts, sniper_events, entry_queue, live_ticks)
NEVER TOUCHES:
    master_summary.jsonl, paper_trades.jsonl, paper_ticks.jsonl,
    shadow_ticks.jsonl, rug_ticks.jsonl, wallet_ticks.jsonl
    (use clear_trades.py for those)
Usage:
    python3 clear_cache.py
    python3 clear_cache.py --yes   # skip confirmation
"""
import argparse
import os
import sys
from paths import get_path

# ============================================================
# CONSTANTS
# ============================================================

LOG_FILES = [
    "brain.log",
    "brain_w.log",
    "bridge_bot.log",
    "discovery.log",
    "safety.log",
    "simulator.log",
    "post_mortem.log",
    "sensor.log",
    "error.log",
]

HANDSHAKE_FILES = [
    "safety.jsonl",
    "entry_verdicts.jsonl",
    "sniper_events.jsonl",
    "entry_queue.jsonl",
    "live_ticks.jsonl",
]

# ============================================================
# HELPERS
# ============================================================

def _collect_targets() -> list:
    try:
        logs_dir = get_path("logs")
    except Exception as e:
        print(f"ERROR: could not resolve logs dir — {e}")
        sys.exit(1)

    targets = []
    for filename in LOG_FILES + HANDSHAKE_FILES:
        full_path = os.path.join(logs_dir, filename)
        if os.path.exists(full_path):
            size = os.path.getsize(full_path)
            targets.append((full_path, filename, size))
    return targets

# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="NLT Alpha Sniper — Clear Cache")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    targets = _collect_targets()

    if not targets:
        print("nothing to clear — all cache files already empty")
        sys.exit(0)

    print("⚠  the following files will be permanently deleted:\n")
    total_bytes = 0
    for path, label, size in targets:
        kb = size / 1024
        print(f"   {label:<35}  {kb:,.1f} KB")
        total_bytes += size

    total_mb = total_bytes / (1024 * 1024)
    print(f"\n   total: {total_mb:,.2f} MB")
    print("\n   master_summary, paper_trades, paper_ticks, shadow_ticks not touched.")

    if not args.yes:
        print()
        try:
            confirm = input("   delete all? [y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\ncancelled")
            sys.exit(0)
        if confirm != "y":
            print("cancelled")
            sys.exit(0)

    removed = 0
    for path, label, size in targets:
        try:
            os.remove(path)
            print(f"   ✓ removed {label}")
            removed += 1
        except Exception as e:
            print(f"   ✗ failed to remove {label}: {e}")

    print(f"\ndone — {removed}/{len(targets)} files cleared")

if __name__ == "__main__":
    main()
