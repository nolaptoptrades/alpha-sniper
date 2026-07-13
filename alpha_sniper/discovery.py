#!/usr/bin/env python3
"""
discovery.py — Alpha Sniper V2

Sources live in sources/ — each exposes fetch(cfg) and validate_config(cfg).
Adding a new source = drop a file in sources/, register it in SOURCES below.
No changes to this file needed.

Usage:
    python3 discovery.py            # run loop forever
    python3 discovery.py --once     # one cycle then exit
    python3 discovery.py --dry-run  # fetch but do not write

Author: NoLaptopTrades
"""

import argparse
import importlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from paths import get_config, get_path, get_setting, log_error

# ============================================================
# CONSTANTS
# ============================================================

LOOP_INTERVAL_SEC = 10   # default loop interval if not in config

# ── Source registry ──────────────────────────────────────────
# Maps source name -> module path inside sources/
# To add a new source: add an entry here + drop the file in sources/
# Tier access is controlled in config.json, not here.
SOURCES = {
    "helius":       "sources.helius",
    "moralis":      "sources.moralis",
    "dexscreener":  "sources.dexscreener",
}

STABLE_SYMBOLS: Set[str] = {
    "USDC", "USDT", "USDt", "USDe",
    "USDC.E", "USDT.E", "WUSDC", "WUSDT",
    "SOL", "WSOL", "JITOSOL",
}

STABLE_MINTS: Set[str] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "So11111111111111111111111111111111111111112",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
}

# ============================================================
# CONFIG HELPERS
# ============================================================

def get_discovery_cfg() -> Dict[str, Any]:
    return get_config().get("discovery") or {}


def get_source_cfg(source_name: str) -> Dict[str, Any]:
    """Return per-source config block from config.json."""
    cfg = get_discovery_cfg()
    return (cfg.get("sources") or {}).get(source_name) or {}


def get_priority() -> List[str]:
    """
    Return ordered list of sources to try.
    Reads from config.json discovery.priority if present,
    falls back to all registered sources.
    """
    cfg           = get_discovery_cfg()
    user_priority = cfg.get("priority") or list(SOURCES.keys())
    return [s for s in user_priority if s in SOURCES]


# ============================================================
# SOURCE LOADER
# ============================================================

def _load_source(name: str):
    """Dynamically import a source module from sources/."""
    module_path = SOURCES.get(name)
    if not module_path:
        raise ImportError(f"[discovery] unknown source: {name}")
    return importlib.import_module(module_path)


# ============================================================
# OUTPUT WRITER
# ============================================================

_written_this_session: Set[str] = set()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_local() -> str:
    """Local time string for user-facing logs."""
    return datetime.now().strftime("%H:%M:%S")


def append_events(rows: List[Dict[str, Any]], dry_run: bool = False) -> tuple:
    """
    Normalize and append graduation events to sniper_events.jsonl.

    Filters:
      - Drop stables by symbol and mint
      - Session dedup by mint (one event per mint per process lifetime)

    Returns (total_written, launchpad_counts) where launchpad_counts is
    a dict of {launchpad: count} for user-facing log breakdown.
    """
    if not rows:
        return 0, {}

    out_path = get_path("sniper_events")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    lines: List[str]             = []
    launchpad_counts: Dict[str, int] = {}

    for row in rows:
        mint   = row.get("mint")
        symbol = (row.get("symbol") or "").strip()

        if not mint:
            continue
        if symbol.upper() in STABLE_SYMBOLS:
            continue
        if mint in STABLE_MINTS:
            continue
        if mint in _written_this_session:
            continue

        _written_this_session.add(mint)

        launchpad = row.get("launchpad", "unknown")
        launchpad_counts[launchpad] = launchpad_counts.get(launchpad, 0) + 1

        event = {
            "ts":         _now_iso(),
            "source":     row.get("source", "unknown"),
            "launchpad":  launchpad,
            "mint":       mint,
            "symbol":     symbol or None,
            "dex":        row.get("dex"),
            "usd":        float(row["usd"]) if row.get("usd") is not None else None,
            "block_time": row.get("block_time"),
            "age_sec":    row.get("age_sec"),
            "sig":        row.get("sig"),
        }
        lines.append(json.dumps(event, separators=(",", ":")))

    if not dry_run and lines:
        with open(out_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    return len(lines), launchpad_counts


# ============================================================
# DISCOVERY CYCLE
# ============================================================

def run_discovery_cycle(
    tier: str,
    priority: List[str],
    cycle: int,
    dry_run: bool = False,
) -> int:
    """
    Run one discovery cycle.

    Tries sources in priority order. If a source returns rows,
    writes them and stops (no double-writing from multiple sources).
    If it returns nothing, tries the next source.

    Returns total rows written.
    """
    ts = _now_local()
    print(f"[discovery] {ts}  cycle {cycle} — scanning...")

    for source_name in priority:
        source_cfg = get_source_cfg(source_name)

        if not source_cfg.get("enabled", True):
            continue  # silently skip disabled sources

        # Inject cursor path for helius
        if source_name == "helius":
            source_cfg = dict(source_cfg)
            source_cfg["_cursor_path"] = get_path("helius_cursor")

        try:
            mod = _load_source(source_name)
        except ImportError as e:
            print(f"[discovery] {_now_local()}  {source_name} — failed to load: {e}")
            continue

        ok, reason = mod.validate_config(source_cfg)
        if not ok:
            print(f"[discovery] {_now_local()}  {source_name} — config error: {reason}")
            continue

        try:
            rows = mod.fetch(source_cfg)
        except Exception as e:
            print(f"[discovery] {_now_local()}  {source_name} — fetch error: {e}")
            rows = []

        if rows:
            wrote, lp_counts = append_events(rows, dry_run=dry_run)
            if wrote:
                # Format launchpad breakdown for helius, plain count for others
                if source_name == "helius" and lp_counts:
                    breakdown = "  ".join(f"{lp}: {n}" for lp, n in sorted(lp_counts.items()))
                    print(f"[discovery] {_now_local()}  helius — {breakdown}  ({wrote} sent to pipeline)")
                else:
                    print(f"[discovery] {_now_local()}  {source_name} — {wrote} sent to pipeline")
            else:
                print(f"[discovery] {_now_local()}  {source_name} — no new graduations")
            return wrote
        else:
            print(f"[discovery] {_now_local()}  {source_name} — no new graduations")

    return 0


# ============================================================
# MAIN LOOP
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Alpha Sniper — Discovery")
    parser.add_argument("--once",    action="store_true", help="Run one cycle then exit")
    parser.add_argument("--dry-run", action="store_true", help="Fetch but do not write")
    args = parser.parse_args()

    cfg      = get_discovery_cfg()
    priority = get_priority()
    interval = float(cfg.get("loop_interval_sec", LOOP_INTERVAL_SEC))
    out_path = get_path("sniper_events")

    # Build readable source list for startup line
    active_sources = [s for s in priority if get_source_cfg(s).get("enabled", True)]
    sources_str    = " → ".join(active_sources) if active_sources else "none"
    dry_tag        = "  [dry-run]" if args.dry_run else ""
    print(f"[discovery] ready — sources: {sources_str}  interval: {interval}s{dry_tag}")
    print(f"[discovery] starting...")

    # PWA server hook — no-op if server.py not present
    try:
        from api.server import start_server_in_thread
        start_server_in_thread(port=8080)
    except ImportError:
        pass

    cycle = 0
    while True:
        cycle      += 1
        cycle_start = time.time()

        try:
            run_discovery_cycle(tier, priority, cycle=cycle, dry_run=args.dry_run)
        except Exception as e:
            log_error("discovery", f"cycle {cycle} error: {e}")

        if args.once:
            break

        elapsed = time.time() - cycle_start
        sleep   = max(0.0, interval - elapsed)
        print(f"[discovery] {_now_local()}  sleeping {sleep:.0f}s")
        time.sleep(sleep)


if __name__ == "__main__":
    main()
