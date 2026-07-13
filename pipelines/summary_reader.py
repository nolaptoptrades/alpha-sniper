#!/usr/bin/env python3
"""
summary_reader.py — NLT Alpha Sniper

Normalized trade_summary.jsonl reader with configurable display depth.

Usage:
    python3 summary_reader.py                    # full view (default)
    python3 summary_reader.py --view standard    # mid view
    python3 summary_reader.py --view compact     # basic view
    python3 summary_reader.py --json             # machine-readable output
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

# ─────────────────────────────────────────────────────────────
# FIELD GROUPS BY VIEW MODE
# ─────────────────────────────────────────────────────────────

# full:     everything
# standard: trading metrics + basic wallet, no raw tick data
# compact:  outcome only

VIEW_FIELDS = {
    "full": {
        "identity":     ["id", "mint", "launchpad", "dex", "discovery_source", "pipeline_version"],
        "timing":       ["entry_ts", "exit_ts", "exit_date", "duration_sec"],
        "entry":        ["entry_liq_usd", "entry_mom_pct", "entry_bsr", "entry_lp_ratio", "entry_vol_5m"],
        "brain":        ["brain_score", "brain_score_raw", "brain_hard_blocks", "brain_reasons", "trigger"],
        "exit":         ["exit_reason", "exit_roi_pct", "entry_price_usd", "exit_price_usd", "is_loss"],
        "ticks":        ["tick_count", "ath_pct", "atl_pct", "mfe_pct", "mae_pct",
                         "age_at_ath_sec", "age_at_atl_sec", "max_drawdown_pct",
                         "avg_bsr_during_hold", "avg_liq_during_hold",
                         "liq_start_usd", "liq_end_usd", "price_volatility",
                         "dump_spike_count", "tsl_activated"],
        "rug":          ["rug_signature"],
        "shadow":       ["shadow_complete", "peak_roi_pct", "time_to_peak_sec",
                         "exit_vs_peak_pct", "shadow_duration_sec", "ghost_tick_count"],
        "wallet":       ["wallet_risk_score", "concentration_flag", "fresh_wallet_count", "dev_wallet_age_days"],
        "validity":     ["is_valid"],
    },
    "standard": {
        "identity":     ["id", "mint", "launchpad", "dex", "discovery_source"],
        "timing":       ["entry_ts", "exit_ts", "duration_sec"],
        "entry":        ["entry_liq_usd", "entry_mom_pct", "entry_bsr"],
        "brain":        ["brain_score", "trigger"],
        "exit":         ["exit_reason", "exit_roi_pct", "is_loss"],
        "ticks":        ["ath_pct", "mfe_pct", "mae_pct", "max_drawdown_pct", "tick_count", "tsl_activated"],
        "shadow":       ["peak_roi_pct", "exit_vs_peak_pct", "shadow_duration_sec"],
        "wallet":       ["concentration_flag", "wallet_risk_score"],
        "validity":     ["is_valid"],
    },
    "compact": {
        "identity":     ["id"],
        "timing":       ["duration_sec"],
        "exit":         ["exit_reason", "exit_roi_pct", "is_loss"],
        "ticks":        ["ath_pct", "mfe_pct", "tick_count"],
        "shadow":       ["peak_roi_pct"],
        "validity":     ["is_valid"],
    },
}

# ─────────────────────────────────────────────────────────────
# DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────

def _fmt_duration(sec: Optional[float]) -> str:
    if sec is None:
        return "—"
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec/60:.1f}m"
    return f"{sec/3600:.1f}h"


def _fmt_roi(pct: Optional[float]) -> str:
    if pct is None:
        return "—"
    color = "\033[92m" if pct > 0 else "\033[91m" if pct < 0 else ""
    reset = "\033[0m" if color else ""
    return f"{color}{pct:+.2f}%{reset}"


def _fmt_usd(val: Optional[float]) -> str:
    if val is None:
        return "—"
    if val >= 1_000_000:
        return f"${val/1e6:.2f}M"
    if val >= 1_000:
        return f"${val/1e3:.0f}K"
    return f"${val:.2f}"


def _fmt_reason(reason: str) -> str:
    colors = {
        "TP": "\033[92m", "TSL": "\033[93m", "SL": "\033[91m",
        "RUG": "\033[91m", "TIMEOUT": "\033[93m", "PRICE_MISS": "\033[91m",
    }
    c = colors.get(reason, "")
    r = "\033[0m" if c else ""
    return f"{c}{reason}{r}"


def _fmt_brain(score: Optional[int], trigger: str) -> str:
    if score is None:
        return "—"
    if score >= 85:
        return f"\033[92m{score}\033[0m"
    if score >= 65:
        return f"\033[93m{score}\033[0m"
    return f"{score}"


# ─────────────────────────────────────────────────────────────
# NORMALIZED RECORD BUILDER
# ─────────────────────────────────────────────────────────────

def normalize_record(raw: Dict[str, Any], view: str) -> Dict[str, Any]:
    """Extract and format a summary record for the given view mode."""
    fields = VIEW_FIELDS.get(view, VIEW_FIELDS["full"])
    out: Dict[str, Any] = {}
    for group, keys in fields.items():
        for key in keys:
            out[key] = raw.get(key)
    return out


def filter_summary(path: str, view: str) -> List[Dict[str, Any]]:
    """Read trade_summary.jsonl and return normalized records."""
    records = []
    if not os.path.exists(path):
        return records
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                records.append(normalize_record(raw, view))
            except Exception:
                pass
    return records


# ─────────────────────────────────────────────────────────────
# TERMINAL DISPLAY
# ─────────────────────────────────────────────────────────────

def display_trade(idx: int, rec: Dict[str, Any], view: str) -> None:
    """Pretty-print one trade record based on view mode."""
    tid = (rec.get("id") or "")[:8]
    roi = _fmt_roi(rec.get("exit_roi_pct"))
    reason = _fmt_reason(rec.get("exit_reason") or "?")
    duration = _fmt_duration(rec.get("duration_sec"))
    mfe = rec.get("mfe_pct")
    mae = rec.get("mae_pct")

    # ── Main line ────────────────────────────────────────────
    parts = [
        f"#{idx}",
        f"{tid}…",
        f"{reason}",
        f"roi={roi}",
        f"dur={duration}",
    ]
    if mfe is not None:
        parts.append(f"mfe={mfe:+.2f}%")
    if mae is not None and mae > 0:
        parts.append(f"mae={mae:.2f}%")

    # Hobbyist extras
    if view in ("full", "standard"):
        score = rec.get("brain_score")
        if score is not None:
            parts.append(f"brain={_fmt_brain(score, rec.get('trigger', ''))}")
        peak = rec.get("peak_roi_pct")
        if peak is not None:
            parts.append(f"peak={peak:+.2f}%")
        left = rec.get("exit_vs_peak_pct")
        if left is not None:
            parts.append(f"left={left:+.2f}%")

    # Pro extras
    if view == "full":
        liq = rec.get("entry_liq_usd")
        if liq is not None:
            parts.append(f"liq={_fmt_usd(liq)}")
        source = rec.get("discovery_source")
        if source:
            parts.append(f"via={source}")
        risk = rec.get("wallet_risk_score")
        if risk is not None:
            parts.append(f"risk={risk}")

    print("  ".join(parts))

    # ── Expanded details ─────────────────────────────────────
    if view == "compact":
        return

    # Hobbyist & Pro: entry conditions
    if view in ("full", "standard"):
        liq = rec.get("entry_liq_usd")
        mom = rec.get("entry_mom_pct")
        bsr = rec.get("entry_bsr")
        if any(v is not None for v in [liq, mom, bsr]):
            details = []
            if liq is not None:
                details.append(f"liq={_fmt_usd(liq)}")
            if mom is not None:
                details.append(f"mom={mom:.1f}%")
            if bsr is not None:
                details.append(f"bsr={bsr:.2f}")
            print(f"     entry:  {', '.join(details)}")

    # Pro: full breakdown
    if view != "full":
        return

    # Tick metrics
    tick_parts = []
    for k in ["tick_count", "ath_pct", "atl_pct", "price_volatility", "tsl_activated"]:
        v = rec.get(k)
        if v is not None:
            if k == "tsl_activated":
                tick_parts.append(f"tsl={'yes' if v else 'no'}")
            elif k == "price_volatility":
                tick_parts.append(f"volatility={v:.8f}")
            else:
                tick_parts.append(f"{k}={v}")
    if tick_parts:
        print(f"     ticks:  {', '.join(tick_parts)}")

    # Brain reasons (first 3)
    reasons = rec.get("brain_reasons")
    if reasons:
        reason_strs = [r.split(" (")[0] for r in reasons[:3]]
        print(f"     brain:  {', '.join(reason_strs)}")

    # Wallet
    wallet_parts = []
    for k in ["concentration_flag", "fresh_wallet_count", "dev_wallet_age_days"]:
        v = rec.get(k)
        if v is not None:
            wallet_parts.append(f"{k}={v}")
    if wallet_parts:
        print(f"     wallet: {', '.join(wallet_parts)}")

    # Rug signature
    rug = rec.get("rug_signature")
    if rug:
        rug_parts = []
        for k, v in rug.items():
            if v is not None:
                rug_parts.append(f"{k}={v}")
        if rug_parts:
            print(f"     rug:    {', '.join(rug_parts)}")

    print()  # blank line between trades


def display_summary(records: List[Dict[str, Any]], view: str) -> None:
    """Display all records with the given view depth."""
    if not records:
        print("No trade records found.")
        return

    print(f"\n{'═' * 70}")
    print(f"  Alpha Sniper — Trade Summary ({view.upper()} view)")
    print(f"{'═' * 70}\n")

    for i, rec in enumerate(records, 1):
        display_trade(i, rec, view)

    # ── Aggregate stats ──────────────────────────────────────
    total = len(records)
    wins = sum(1 for r in records if (r.get("exit_roi_pct") or 0) > 0)
    losses = total - wins
    tp = sum(1 for r in records if r.get("exit_reason") == "TP")
    rug = sum(1 for r in records if r.get("exit_reason") == "RUG")
    tsl = sum(1 for r in records if r.get("exit_reason") == "TSL")
    timeout = sum(1 for r in records if r.get("exit_reason") == "TIMEOUT")

    rois = [r.get("exit_roi_pct") for r in records if r.get("exit_roi_pct") is not None]
    avg_roi = sum(rois) / len(rois) if rois else 0

    print(f"{'─' * 70}")
    print(f"  Total: {total}  |  Wins: {wins}  |  Losses: {losses}")
    print(f"  TP: {tp}  |  TSL: {tsl}  |  RUG: {rug}  |  TIMEOUT: {timeout}")
    print(f"  Avg ROI: {avg_roi:+.2f}%")
    print(f"{'═' * 70}\n")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Alpha Sniper — Trade Summary Reader")
    parser.add_argument(
        "--view", choices=["full", "standard", "compact"], default="full",
        help="Display depth: full (default), standard, compact"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output as JSON instead of formatted text"
    )
    parser.add_argument(
        "--path", type=str,
        help="Path to trade_summary.jsonl (default: from config)"
    )
    args = parser.parse_args()

    # Resolve path
    if args.path:
        summary_path = args.path
    else:
        try:
            from paths import get_path
            summary_path = get_path("master_summary")
        except ImportError:
            print("Error: cannot resolve path. Use --path or run from pipeline directory.")
            sys.exit(1)

    records = filter_summary(summary_path, args.view)

    if args.json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
    else:
        display_summary(records, args.view)


if __name__ == "__main__":
    main()
