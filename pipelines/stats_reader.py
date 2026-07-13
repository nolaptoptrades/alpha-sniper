"""
stats_reader.py — NLT Alpha Sniper

Reads entry_verdicts.jsonl and paper_trades.jsonl to produce
live stats for the TUI display. Called by manager.py every 10s.

No tuneable values here — paths are passed in by manager_config.py
which sources them from config.json via paths.py.
"""

import json
import os
from datetime import datetime, timezone


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _parse_ts(ts: str) -> str:
    """Extract date portion from ISO timestamp."""
    try:
        return ts[:10]
    except Exception:
        return ""


def read_verdict_stats(verdicts_path: str) -> dict:
    """
    Read entry_verdicts.jsonl and return today's counts.
    Returns dict with keys: evaluated, buy, skip, watch
    """
    stats = {"evaluated": 0, "buy": 0, "skip": 0, "watch": 0}
    today = _today_iso()

    try:
        with open(verdicts_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = rec.get("ts", "")
                    if _parse_ts(ts) != today:
                        continue
                    stats["evaluated"] += 1
                    verdict = rec.get("verdict", "SKIP")
                    if verdict == "BUY":
                        stats["buy"] += 1
                    elif verdict == "WATCH":
                        stats["watch"] += 1
                    else:
                        stats["skip"] += 1
                except Exception:
                    continue
    except FileNotFoundError:
        pass

    return stats


def read_trade_stats(trades_path: str) -> dict:
    """
    Read paper_trades.jsonl and return today's closed trade counts.
    Returns dict with keys: tp, sl, tsl, rug, timeout, pricemiss, open, close
    """
    stats = {
        "tp": 0, "sl": 0, "tsl": 0, "rug": 0,
        "timeout": 0, "pricemiss": 0, "open": 0, "close": 0,
    }
    today = _today_iso()

    trades = {}
    try:
        with open(trades_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    tid = rec.get("id")
                    if not tid:
                        continue
                    if tid not in trades:
                        trades[tid] = {}
                    if rec.get("type") == "OPEN":
                        trades[tid]["open"] = rec
                    elif rec.get("type") == "CLOSE":
                        trades[tid]["close"] = rec
                except Exception:
                    continue
    except FileNotFoundError:
        pass

    for tid, v in trades.items():
        # open with no close = still open position
        if "open" in v and "close" not in v:
            stats["open"] += 1
            continue

        if "open" not in v or "close" not in v:
            continue

        # only count today's closed trades
        close_ts = v["close"].get("exit_ts") or v["close"].get("ts", "")
        if _parse_ts(close_ts) != today:
            continue

        stats["close"] += 1

        reason = v["close"].get("reason", "")
        if reason == "TP":
            stats["tp"] += 1
        elif reason == "SL":
            stats["sl"] += 1
        elif reason == "TSL":
            stats["tsl"] += 1
        elif reason == "RUG":
            stats["rug"] += 1
        elif reason == "TIMEOUT":
            stats["timeout"] += 1
        elif reason == "PRICE_MISS":
            stats["pricemiss"] += 1

    return stats


def read_all_stats(verdicts_path: str, trades_path: str) -> dict:
    """
    Combined stats for TUI display.
    Returns single dict with all counts.
    """
    v = read_verdict_stats(verdicts_path)
    t = read_trade_stats(trades_path)
    return {
        "evaluated": v["evaluated"],
        "buy":       v["buy"],
        "skip":      v["skip"],
        "watch":     v["watch"],
        "tp":        t["tp"],
        "sl":        t["sl"],
        "tsl":       t["tsl"],
        "rug":       t["rug"],
        "timeout":   t["timeout"],
        "pricemiss": t["pricemiss"],
        "open":      t["open"],
        "close":     t["close"],
    }


# ── Self-test ─────────────────────────────────────────────────
# Run directly to verify stats are reading correctly:
#   python stats_reader.py

if __name__ == "__main__":
    from paths import get_path

    vpath = get_path("verdicts")
    tpath = get_path("paper_trades")

    stats = read_all_stats(vpath, tpath)
    print("=== STATS TODAY ===")
    print(f"Evaluated : {stats['evaluated']}")
    print(f"BUY       : {stats['buy']}")
    print(f"SKIP      : {stats['skip']}")
    print(f"WATCH     : {stats['watch']}")
    print(f"TP        : {stats['tp']}")
    print(f"SL        : {stats['sl']}")
    print(f"TSL       : {stats['tsl']}")
    print(f"RUG       : {stats['rug']}")
    print(f"TIMEOUT   : {stats['timeout']}")
    print(f"$MISS     : {stats['pricemiss']}")
    print(f"Open pos  : {stats['open']}")
    print(f"Close pos : {stats['close']}")
