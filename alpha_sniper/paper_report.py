#!/usr/bin/env python3
"""
paper_report.py
 
Prints a paper trade performance report from any paper_trades.jsonl file.
Aligned with landing page stat style: capped + normalized, correct EV math.
 
Usage:
  python paper_report.py                  # reads live paper_trades.jsonl
  python paper_report.py --file <path>    # reads any specific file
"""
 
import os
import sys
import json
from datetime import datetime, timezone

from paths import get_config, get_path, get_setting
# ── Config ────────────────────────────────────────────────────
BASE          = os.path.expanduser("~/alpha_sniper/android/abc")
LIVE_FILE     = os.path.join(BASE, "logs", "paper_trades.jsonl")
PAPER_TRADES_PATH   = get_path("paper_trades")
 
DEFAULT_FILE  = PAPER_TRADES_PATH
 
ROI_CAP_PCT   = 500.0
 
NORMALIZED_TP_PCT = 30.0
 
OUTLIER_THRESHOLD = 500.0
 
# ── Helpers ───────────────────────────────────────────────────
 
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
 
def load_trades(path: str) -> list:
    """
    Load fully closed trades (OPEN+CLOSE pairs) from a jsonl file.
    Orphaned CLOSE records without a matching OPEN are ignored.
    Returns list sorted by exit_ts ascending.
    """
    raw = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r   = json.loads(line)
                    tid = r.get("id")
                    if not tid:
                        continue
                    if tid not in raw:
                        raw[tid] = {}
                    raw[tid][r["type"]] = r
                except Exception:
                    pass
    except FileNotFoundError:
        print(f"[report] file not found: {path}")
        sys.exit(1)
 
    trades = []
    for tid, v in raw.items():
        if "OPEN" not in v or "CLOSE" not in v:
            continue
        o = v["OPEN"]
        c = v["CLOSE"]
        trades.append({
            "id":       tid,
            "mint":     o.get("mint", ""),
            "entry_ts": o.get("entry_ts", ""),
            "exit_ts":  c.get("exit_ts", ""),
            "roi_pct":  float(c.get("roi_pct", 0.0)),
            "reason":   c.get("reason", "?"),
        })
 
    trades.sort(key=lambda x: x["exit_ts"])
    return trades
 
def compute_ev(trades: list, mode: str) -> dict:
    """
    Compute EV stats.
 
    mode="capped"     — real ROI, wins capped at ROI_CAP_PCT. Losses uncapped.
    mode="normalized" — TP exits assumed at NORMALIZED_TP_PCT. SL/TO as recorded.
 
    EV formula:
        EV = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)
    Both avg_win and avg_loss already carry their signs, so this is correct.
    """
    rois = []
    for t in trades:
        roi    = t["roi_pct"]
        reason = t["reason"]
        if mode == "capped":
            if roi > 0:
                roi = min(roi, ROI_CAP_PCT)
        elif mode == "normalized":
            if reason == "TP":
                roi = NORMALIZED_TP_PCT
        rois.append(roi)
 
    wins   = [r for r in rois if r > 0]
    losses = [r for r in rois if r <= 0]
    total  = len(rois)
 
    wr     = len(wins) / total if total else 0.0
    avg_w  = sum(wins)   / len(wins)   if wins   else 0.0
    avg_l  = sum(losses) / len(losses) if losses else 0.0
    ev     = (wr * avg_w) + ((1 - wr) * avg_l)
 
    return {
        "wr":     wr,
        "avg_w":  avg_w,
        "avg_l":  avg_l,
        "ev":     ev,
        "wins":   len(wins),
        "losses": len(losses),
    }
 
def sep(char="─", width=44):
    print(char * width)
 
def row(label, value, width=20):
    print(f"  {label:<{width}} {value}")

# ── Main ──────────────────────────────────────────────────────
 
def main():
    # Resolve file path
    target = DEFAULT_FILE
    if "--file" in sys.argv:
        idx = sys.argv.index("--file")
        if idx + 1 < len(sys.argv):
            target = os.path.expanduser(sys.argv[idx + 1])
        else:
            print("Usage: python paper_report.py --file <path>")
            sys.exit(1)
 
    trades = load_trades(target)
 
    if not trades:
        print("[report] no closed trades found.")
        sys.exit(0)
 
    total      = len(trades)
    tp         = sum(1 for t in trades if t["reason"] == "TP")
    sl         = sum(1 for t in trades if t["reason"] == "SL")
    tsl        = sum(1 for t in trades if t["reason"] == "TSL")
    pricemiss  = sum(1 for t in trades if t["reason"] == "PRICE_MISS")
    rug        = sum(1 for t in trades if t["reason"] == "RUG")
    to         = sum(1 for t in trades if t["reason"] == "TIMEOUT")
    first_date = trades[0]["exit_ts"][:10]
    last_date  = trades[-1]["exit_ts"][:10]
    outliers   = [t for t in trades if t["roi_pct"] > OUTLIER_THRESHOLD]
 
    cap  = compute_ev(trades, "capped")
    norm = compute_ev(trades, "normalized")
 
    # ── Header ────────────────────────────────────────────
    print()
    sep("═")
    print(f"  PAPER TRADE REPORT")
    print(f"  {now_iso()} UTC")
    sep("═")
    print(f"  File   : {os.path.basename(target)}")
    print(f"  Period : {first_date} → {last_date}")
    sep()
 
    # ── Summary ───────────────────────────────────────────
    row("Trades",   total)
    row("Win rate", f"{cap['wr']*100:.1f}%")
    row("TP",       tp)
    row("SL",       sl)
    row("TSL",      tsl)
    row("$MISS",       pricemiss)
    row("RUG",      rug)
    row("TO (timeout)", to)
    sep()
 
    # ── Capped stats ──────────────────────────────────────
    print(f"  Stats  ")
    sep("·")
    row("Avg win",  f"{cap['avg_w']:+.2f}%")
    row("Avg loss", f"{cap['avg_l']:+.2f}%")
    row("EV",       f"{cap['ev']:+.2f}%")
    sep()

    # ── All trades ────────────────────────
    for t in trades:
        print(f" {t['exit_ts'][:10]} |{t['roi_pct']:>8.2f}% | {t['reason']:<7} | {t['mint'][:6]}...")

    sep("═")
    print()
 
    # ── Outliers ──────────────────────────────────────────
    if outliers:
        print(f"  Extreme wins > {OUTLIER_THRESHOLD:.0f}%  ({len(outliers)} trades)")
        sep("·")
        for t in outliers:
            print(f"  {t['exit_ts'][:10]}  {t['roi_pct']:>12.2f}%  [{t['reason']}]  {t['mint'][:12]}…")
        sep()
 
    print()
 
if __name__ == "__main__":
    main()
