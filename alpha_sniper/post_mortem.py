#!/usr/bin/env python3
"""
post_mortem.py — Alpha Sniper V2

Post-trade data compiler. Runs continuously alongside the pipeline.

RESPONSIBILITIES
  Phase 1 (immediate, on CLOSE detect):
    - Tails paper_trades.jsonl for new CLOSE records
    - Reads matching OPEN + paper_ticks + entry_verdicts
    - Compiles master_summary record
    - Registers trade_id for Phase 2 shadow watch
    - Calls sync.py (Phase 1 upload)

  Phase 2 (deferred, on shadow complete):
    - Monitors shadow_ticks.jsonl for pending trade_ids
    - Updates master_summary record with ceiling fields in-place
    - Calls sync.py (Phase 2 update)

  Timeout:
    - After SHADOW_WAIT_SEC, closes pending records with shadow_complete=false

DEDUP STRATEGY (reliable, output-file-as-truth):
  - On startup: load all existing IDs from master_summary into hot sets
    (processed_ids, completed_shadows) — one full read, never repeated
  - During loop: O(1) set lookups — no file reads for dedup
  - On write: add to hot set immediately after append
  - State file (post_mortem_state.json) is a performance hint only:
    offsets, pending_shadow map. Never trusted for dedup authority.
  - completed_shadows persisted in state so Phase 2 isn't re-applied
    on restart even if master_summary update already happened

FILES TOUCHED
    Reads:  paper_trades.jsonl, paper_ticks.jsonl, entry_verdicts.jsonl,
            shadow_ticks.jsonl, sniper_events.jsonl, wallet_ticks.jsonl
    Writes: master_summary.jsonl, post_mortem_state.json

Author: NoLaptopTrades
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from paths import get_config, get_path, get_setting, log_error


# ============================================================
# CONSTANTS
# ============================================================

SNIPER_EVENTS_PATH  = get_path("sniper_events")
PAPER_TRADES_PATH   = get_path("paper_trades")
PAPER_TICKS_PATH    = get_path("paper_ticks")
VERDICTS_PATH       = get_path("verdicts")
SHADOW_TICKS_PATH   = get_path("shadow_ticks")
TRADE_SUMMARY_PATH  = get_path("master_summary")
STATE_PATH          = os.path.join(get_path("state"), "post_mortem_state.json")

CFG             = get_config()
SHADOW_WAIT_SEC = float(get_setting("post_mortem", "shadow_wait_sec", 1800))
MAX_HOLD_SEC    = float(get_setting("post_mortem", "max_hold_sec",    3600))
LOOP_SLEEP_SEC  = 0.5

NO_SHADOW_REASONS = {"RUG", "SL", "PRICE_MISS", "STALE", "UNKNOWN"}


# ============================================================
# HELPERS — I/O
# ============================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_local() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _fmt_mint(mint: str) -> str:
    return f"{mint[:5]}…{mint[-3:]}" if len(mint) > 8 else mint


def _now_ts() -> float:
    return time.time()


def _parse_ts(ts_str: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _safe_float(val: Any) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except Exception:
        return None


def _load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _append_jsonl(path: str, obj: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    records = []
    if not os.path.exists(path):
        return records
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
    return records


def _update_jsonl_record(path: str, trade_id: str, updates: Dict[str, Any]) -> bool:
    """
    Find record by trade_id and update fields in place.
    Reads entire file, rewrites with update applied.
    Returns True if record found and updated.
    """
    if not os.path.exists(path):
        return False

    lines: List[str] = []
    found = False

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                lines.append(line)
                continue
            try:
                rec = json.loads(stripped)
                if rec.get("id") == trade_id:
                    rec.update(updates)
                    lines.append(json.dumps(rec, ensure_ascii=False) + "\n")
                    found = True
                else:
                    lines.append(line)
            except Exception:
                lines.append(line)

    if found:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp, path)

    return found


# ============================================================
# FILE TAILER
# ============================================================

def _tail_new_lines(path: str, offset: int) -> Tuple[List[Dict[str, Any]], int]:
    """
    Read new lines from a JSONL file starting at byte offset.
    Returns (list of parsed dicts, new offset).
    Validates offset against current file size — resets to 0 if stale.
    """
    if not os.path.exists(path):
        return [], offset

    try:
        current_size = os.path.getsize(path)
    except OSError:
        return [], offset

    if offset > current_size:
        offset = 0

    out: List[Dict[str, Any]] = []
    new_offset = offset

    with open(path, "r", encoding="utf-8") as f:
        f.seek(offset)
        while True:
            line = f.readline()
            if not line:
                break
            new_offset = f.tell()
            stripped = line.strip()
            if not stripped:
                continue
            try:
                out.append(json.loads(stripped))
            except Exception:
                pass

    return out, new_offset


# ============================================================
# HOT SETS — built once at startup, O(1) lookups during loop
# ============================================================

def _build_hot_sets() -> Tuple[Set[str], Set[str]]:
    """
    Read master_summary.jsonl ONCE at startup.
    Returns:
      processed_ids     — all trade IDs ever written to master_summary
      completed_shadows — trade IDs where shadow_complete=True
    These sets are the dedup authority. State file is never trusted for this.
    """
    processed_ids: Set[str]     = set()
    completed_shadows: Set[str] = set()

    if not os.path.exists(TRADE_SUMMARY_PATH):
        return processed_ids, completed_shadows

    count = 0
    for rec in _read_jsonl(TRADE_SUMMARY_PATH):
        tid = rec.get("id")
        if not tid:
            continue
        processed_ids.add(tid)
        count += 1
        if rec.get("shadow_complete"):
            completed_shadows.add(tid)

    return processed_ids, completed_shadows


# ============================================================
# STATE MANAGEMENT
# ============================================================

def _load_state() -> Dict[str, Any]:
    default: Dict[str, Any] = {
        "trade_offset":   0,
        "pending_shadow": {},
    }
    state = _load_json(STATE_PATH, default)
    if not isinstance(state, dict):
        state = default

    state.setdefault("trade_offset",   0)
    state.setdefault("pending_shadow", {})

    for tid, info in list(state["pending_shadow"].items()):
        if isinstance(info, (int, float)):
            state["pending_shadow"][tid] = {
                "deadline_ts": float(info),
                "entry_ts":    "",
                "exit_reason": "UNKNOWN",
            }

    try:
        current_size = os.path.getsize(PAPER_TRADES_PATH)
        if state["trade_offset"] > current_size:
            state["trade_offset"] = 0
    except FileNotFoundError:
        state["trade_offset"] = 0

    return state


def _save_state(state: Dict[str, Any]) -> None:
    _save_json(STATE_PATH, state)


# ============================================================
# DATA LOADERS
# ============================================================

def _load_open_record(trade_id: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(PAPER_TRADES_PATH):
        return None
    with open(PAPER_TRADES_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("id") == trade_id and rec.get("type") == "OPEN":
                    return rec
            except Exception:
                continue
    return None


def _load_ticks_for_trade(trade_id: str) -> List[Dict[str, Any]]:
    ticks: List[Dict[str, Any]] = []
    if not os.path.exists(PAPER_TICKS_PATH):
        return ticks
    with open(PAPER_TICKS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("id") == trade_id:
                    ticks.append(rec)
            except Exception:
                continue
    return ticks


def _load_verdict_for_mint(mint: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(VERDICTS_PATH):
        return None
    lines: List[str] = []
    with open(VERDICTS_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("mint") == mint:
                return rec
        except Exception:
            continue
    return None


def _load_discovery_source(mint: str) -> Optional[str]:
    if not os.path.exists(SNIPER_EVENTS_PATH):
        return None
    lines: List[str] = []
    with open(SNIPER_EVENTS_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("mint") == mint:
                return rec.get("source")
        except Exception:
            continue
    return None


def _load_wallet_data(mint: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "wallet_risk_score":  None,
        "concentration_flag": None,
        "fresh_wallet_count": None,
        "dev_wallet_age_days": None,
    }
    try:
        wallet_path = get_path("wallet_ticks")
        if not wallet_path or not os.path.exists(wallet_path):
            return result
        with open(wallet_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    w = json.loads(line)
                    if w.get("mint") == mint:
                        result["wallet_risk_score"]   = w.get("wallet_risk_score")
                        result["concentration_flag"]  = w.get("concentration_flag")
                        result["fresh_wallet_count"]  = w.get("fresh_wallet_count")
                        result["dev_wallet_age_days"] = w.get("dev_wallet_age_days")
                        return result
                except Exception:
                    continue
    except Exception:
        pass
    return result


# ============================================================
# TICK ANALYSIS
# ============================================================

def _analyze_ticks(ticks: List[Dict[str, Any]], entry_price: float) -> Dict[str, Any]:
    empty = {
        "tick_count": len(ticks), "ath_pct": None, "atl_pct": None,
        "mfe_pct": None, "mae_pct": None, "age_at_ath_sec": None,
        "age_at_atl_sec": None, "max_drawdown_pct": None,
        "avg_bsr_during_hold": None, "avg_liq_during_hold": None,
        "liq_start_usd": None, "liq_end_usd": None,
        "price_volatility": None, "dump_spike_count": 0, "tsl_activated": False,
    }
    if not ticks or entry_price <= 0:
        return empty

    ticks_sorted = sorted(ticks, key=lambda t: _safe_float(t.get("pos_age_sec")) or 0.0)

    prices, bsr_vals, liq_vals = [], [], []
    tsl_flags: List[bool] = []
    dump_spikes = 0
    prev_price = None

    for t in ticks_sorted:
        p = _safe_float(t.get("price_usd"))
        b = _safe_float(t.get("bsr"))
        l = _safe_float(t.get("liq_usd"))
        if p and p > 0:  prices.append(p)
        if b is not None: bsr_vals.append(b)
        if l is not None: liq_vals.append(l)
        if t.get("tsl_active") is True: tsl_flags.append(True)
        if prev_price and p and p < prev_price and b is not None and b <= 0.85:
            dump_spikes += 1
        if p: prev_price = p

    if not prices:
        return empty

    max_p, min_p = max(prices), min(prices)
    ath_pct = round((max_p - entry_price) / entry_price * 100, 2)
    atl_pct = round((min_p - entry_price) / entry_price * 100, 2)
    mfe_pct = ath_pct
    mae_pct = round((entry_price - min_p) / entry_price * 100, 2)

    age_at_ath = age_at_atl = None
    for t in ticks_sorted:
        p   = _safe_float(t.get("price_usd"))
        age = _safe_float(t.get("pos_age_sec"))
        if p == max_p and age is not None and age_at_ath is None:
            age_at_ath = round(age, 1)
        if p == min_p and age is not None and age_at_atl is None:
            age_at_atl = round(age, 1)

    avg_bsr  = round(sum(bsr_vals) / len(bsr_vals), 4) if bsr_vals else None
    avg_liq  = round(sum(liq_vals) / len(liq_vals), 2) if liq_vals else None
    liq_start = round(liq_vals[0],  2) if liq_vals else None
    liq_end   = round(liq_vals[-1], 2) if liq_vals else None

    price_vol = None
    if len(prices) >= 2:
        mean_p   = sum(prices) / len(prices)
        variance = sum((p - mean_p) ** 2 for p in prices) / (len(prices) - 1)
        price_vol = round(variance ** 0.5, 8)

    return {
        "tick_count":         len(ticks_sorted),
        "ath_pct":            ath_pct,
        "atl_pct":            atl_pct,
        "mfe_pct":            mfe_pct,
        "mae_pct":            mae_pct,
        "age_at_ath_sec":     age_at_ath,
        "age_at_atl_sec":     age_at_atl,
        "max_drawdown_pct":   atl_pct,
        "avg_bsr_during_hold": avg_bsr,
        "avg_liq_during_hold": avg_liq,
        "liq_start_usd":      liq_start,
        "liq_end_usd":        liq_end,
        "price_volatility":   price_vol,
        "dump_spike_count":   dump_spikes,
        "tsl_activated":      any(tsl_flags),
    }


def _extract_rug_signature(ticks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not ticks:
        return None
    ticks_sorted = sorted(ticks, key=lambda t: _safe_float(t.get("pos_age_sec")) or 0.0)
    rug_ticks = [t for t in ticks_sorted if t.get("rug_flag") is True]
    if not rug_ticks:
        return None
    rug_tick  = rug_ticks[0]
    rug_age   = _safe_float(rug_tick.get("pos_age_sec"))
    rug_liq   = _safe_float(rug_tick.get("liq_usd"))
    rug_price = _safe_float(rug_tick.get("price_usd"))
    pre_rug   = [t for t in ticks_sorted
                 if (_safe_float(t.get("pos_age_sec")) or 0) < (rug_age or 0)]
    liq_drop_speed = price_drop_before = None
    if pre_rug:
        last     = pre_rug[-1]
        last_liq = _safe_float(last.get("liq_usd"))
        last_age = _safe_float(last.get("pos_age_sec"))
        if last_liq and rug_liq and last_age and rug_age:
            delta_t = rug_age - last_age
            if delta_t > 0:
                liq_drop_speed = round((last_liq - rug_liq) / delta_t, 2)
        first_price = _safe_float(pre_rug[0].get("price_usd"))
        if first_price and first_price > 0 and rug_price:
            price_drop_before = round((first_price - rug_price) / first_price * 100, 2)
    return {
        "liq_at_rug_usd":       round(rug_liq, 2) if rug_liq else None,
        "price_at_rug":         rug_price,
        "ticks_before_rug":     len(pre_rug),
        "liq_drop_speed":       liq_drop_speed,
        "price_drop_before_rug": price_drop_before,
    }


# ============================================================
# PHASE 1 — compile summary from CLOSE event
# ============================================================

def _compile_phase1(close_rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    trade_id = close_rec.get("id")
    mint     = close_rec.get("mint")

    if not trade_id or not mint:
        return None

    open_rec = _load_open_record(trade_id)
    if not open_rec:
        return None

    ticks   = _load_ticks_for_trade(trade_id)
    verdict = _load_verdict_for_mint(mint)

    entry_ts  = open_rec.get("entry_ts", "")
    exit_ts   = close_rec.get("exit_ts", "")
    entry_dt  = _parse_ts(entry_ts)
    exit_dt   = _parse_ts(exit_ts)
    duration_sec = None
    if entry_dt and exit_dt:
        duration_sec = round((exit_dt - entry_dt).total_seconds(), 1)

    entry_price  = _safe_float(open_rec.get("entry_price_usd")) or 0.0
    exit_price   = _safe_float(close_rec.get("exit_price_usd"))
    roi_pct      = _safe_float(close_rec.get("roi_pct"))
    exit_reason  = close_rec.get("reason", "UNKNOWN")
    is_loss      = close_rec.get("is_loss", roi_pct is not None and roi_pct < 0)

    discovery_source = _load_discovery_source(mint)

    context  = open_rec.get("context") or {}
    entry_liq = _safe_float(context.get("liq_usd"))
    entry_mom = _safe_float(context.get("mom_pct_m5"))
    entry_bsr = _safe_float(context.get("bsr"))

    brain_score = brain_score_raw = brain_hard_blocks = brain_reasons = None
    entry_lp_ratio = entry_vol_5m = None

    if verdict:
        brain_score       = verdict.get("score")
        brain_score_raw   = verdict.get("score_raw")
        brain_hard_blocks = verdict.get("hard_blocks")
        brain_reasons     = verdict.get("reasons")
        diag              = verdict.get("diagnostics") or {}
        if entry_liq is None: entry_liq = _safe_float(diag.get("liq_end"))
        if entry_mom is None: entry_mom = _safe_float(diag.get("mom_end"))
        if entry_bsr is None: entry_bsr = _safe_float(diag.get("bsr_end"))

    if brain_reasons:
        for reason in brain_reasons:
            if reason.startswith("lp_ratio=") and entry_lp_ratio is None:
                try:
                    entry_lp_ratio = float(reason.split("=")[1].split(" ")[0])
                except Exception:
                    pass
            if reason.startswith("vol=") and entry_vol_5m is None:
                try:
                    entry_vol_5m = float(reason.split("=")[1].split(" ")[0].replace(",", ""))
                except Exception:
                    pass

    wallet       = _load_wallet_data(mint)
    tick_metrics = _analyze_ticks(ticks, entry_price)
    rug_sig      = _extract_rug_signature(ticks) if exit_reason == "RUG" else None

    summary = {
        "id":               trade_id,
        "mint":             mint,
        "launchpad":        open_rec.get("launchpad", "unknown"),
        "dex":              open_rec.get("dexId", "pumpswap"),
        "discovery_source": discovery_source,
        "pipeline_version": get_config().get("_meta", {}).get("version", "unknown"),
        "entry_ts":         entry_ts,
        "exit_ts":          exit_ts,
        "exit_date":        exit_ts[:10] if exit_ts else None,
        "duration_sec":     duration_sec,
        "entry_liq_usd":    round(entry_liq) if entry_liq else None,
        "entry_mom_pct":    round(entry_mom, 2) if entry_mom else None,
        "entry_bsr":        round(entry_bsr, 4) if entry_bsr else None,
        "entry_lp_ratio":   round(entry_lp_ratio, 4) if entry_lp_ratio else None,
        "entry_vol_5m":     round(entry_vol_5m, 2) if entry_vol_5m else None,
        "brain_score":      brain_score,
        "brain_score_raw":  brain_score_raw,
        "brain_hard_blocks": brain_hard_blocks,
        "brain_reasons":    brain_reasons,
        "trigger":          open_rec.get("trigger", "NORMAL"),
        "exit_reason":      exit_reason,
        "exit_roi_pct":     round(roi_pct, 4) if roi_pct is not None else None,
        "entry_price_usd":  entry_price,
        "exit_price_usd":   exit_price,
        "is_loss":          is_loss,
        "tick_count":       tick_metrics["tick_count"],
        "ath_pct":          tick_metrics["ath_pct"],
        "atl_pct":          tick_metrics["atl_pct"],
        "mfe_pct":          tick_metrics["mfe_pct"],
        "mae_pct":          tick_metrics["mae_pct"],
        "age_at_ath_sec":   tick_metrics["age_at_ath_sec"],
        "age_at_atl_sec":   tick_metrics["age_at_atl_sec"],
        "max_drawdown_pct": tick_metrics["max_drawdown_pct"],
        "avg_bsr_during_hold": tick_metrics["avg_bsr_during_hold"],
        "avg_liq_during_hold": tick_metrics["avg_liq_during_hold"],
        "liq_start_usd":    tick_metrics["liq_start_usd"],
        "liq_end_usd":      tick_metrics["liq_end_usd"],
        "price_volatility": tick_metrics["price_volatility"],
        "dump_spike_count": tick_metrics["dump_spike_count"],
        "tsl_activated":    tick_metrics["tsl_activated"],
        "rug_signature":    rug_sig,
        "shadow_complete":  False,
        "peak_roi_pct":     None,
        "time_to_peak_sec": None,
        "exit_vs_peak_pct": None,
        "shadow_duration_sec": None,
        "ghost_tick_count": None,
        "wallet_risk_score":    wallet["wallet_risk_score"],
        "concentration_flag":   wallet["concentration_flag"],
        "fresh_wallet_count":   wallet["fresh_wallet_count"],
        "dev_wallet_age_days":  wallet["dev_wallet_age_days"],
        "synced":   False,
        "is_valid": _validate_trade(duration_sec, roi_pct,
                                    tick_metrics["tick_count"], exit_reason,
                                    _safe_float((open_rec.get("rules") or {}).get("max_hold_sec"))),
    }
    return summary


def _validate_trade(duration_sec, roi_pct, tick_count, exit_reason="", max_hold_sec=2700.0) -> bool:
    if duration_sec is not None and max_hold_sec is not None and duration_sec > max_hold_sec:
        return False
    if exit_reason == "RUG":
        return True
    if duration_sec is not None and duration_sec < 2:
        return False
    if roi_pct is not None and roi_pct > 500:
        return False
    if roi_pct is not None and roi_pct <= -100:
        return False
    if tick_count < 2:
        return False
    return True


# ============================================================
# PHASE 2 — apply shadow ceiling data
# ============================================================

def _compute_phase2_from_ticks(
    trade_id: str,
    pending: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Read shadow_ticks.jsonl for this trade_id, compute ceiling fields.
    Returns update dict or None if not enough data yet.
    """
    if not os.path.exists(SHADOW_TICKS_PATH):
        return None

    ticks: List[Dict[str, Any]] = []
    with open(SHADOW_TICKS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
                if t.get("id") == trade_id:
                    ticks.append(t)
            except Exception:
                continue

    if not ticks:
        return None

    ticks.sort(key=lambda t: float(t.get("ghost_age_sec", 0)))
    last_tick  = ticks[-1]
    ghost_age  = float(last_tick.get("ghost_age_sec", 0))

    deadline = pending.get("deadline_ts", 0)
    if ghost_age < SHADOW_WAIT_SEC - 30 and _now_ts() < deadline:
        return None

    peak_tick = max(ticks, key=lambda t: float(t.get("ghost_roi_pct", 0)))
    peak_roi  = float(peak_tick.get("ghost_roi_pct", 0))
    exit_roi  = float(ticks[0].get("exit_roi_pct", 0))

    return {
        "shadow_complete":    True,
        "peak_roi_pct":       round(peak_roi, 4),
        "time_to_peak_sec":   round(float(peak_tick.get("ghost_age_sec", 0)), 1),
        "exit_vs_peak_pct":   round(peak_roi - exit_roi, 4),
        "shadow_duration_sec": round(ghost_age, 1),
        "ghost_tick_count":   len(ticks),
    }


# ============================================================
# SYNC
# ============================================================

def _call_sync(phase: int, trade_id: str, shared: bool, complete: bool = False) -> bool:
    """Call sync.py subprocess. Returns True if exit code 0."""
    try:
        sync_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "sync.py"
        )
        result = subprocess.run([
            sys.executable, sync_path,
            "--phase",    str(phase),
            "--trade-id", trade_id,
            "--shared",   str(shared).lower(),
            "--complete", str(complete).lower(),
        ], check=False, timeout=15)
        return result.returncode == 0
    except Exception as e:
        log_error("post_mortem", f"sync error: {e}")
        return False


# ============================================================
# MAIN LOOP
# ============================================================

def main() -> None:
    print(f"{_now_local()} starting — shadow_wait={SHADOW_WAIT_SEC}s")

    os.makedirs(os.path.dirname(TRADE_SUMMARY_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(STATE_PATH),          exist_ok=True)

    # ── Sharing ────────────────────────────────────────────────────
    data_cfg        = get_config().get("data") or {}
    sharing_enabled = bool(data_cfg.get("sharing_enabled", False))
    shared          = sharing_enabled

    # ── Build hot sets from output file — single startup read ─────
    processed_ids:     Set[str] = set()
    completed_shadows: Set[str] = set()
    processed_ids, completed_shadows = _build_hot_sets()

    # ── Load state (offsets + pending_shadow only) ────────────────
    state = _load_state()

    # ── Reconcile pending_shadow against hot sets ─────────────────
    stale_pending = [tid for tid in state["pending_shadow"]
                     if tid in completed_shadows]
    for tid in stale_pending:
        del state["pending_shadow"][tid]

    # Add any processed_ids that should be in pending_shadow but got lost
    if os.path.exists(TRADE_SUMMARY_PATH):
        for rec in _read_jsonl(TRADE_SUMMARY_PATH):
            tid = rec.get("id")
            if not tid:
                continue
            if (tid not in completed_shadows
                    and tid not in state["pending_shadow"]
                    and rec.get("exit_reason") not in NO_SHADOW_REASONS
                    and not rec.get("shadow_complete")):
                deadline = _now_ts() + 60
                state["pending_shadow"][tid] = {
                    "deadline_ts": deadline,
                    "entry_ts":    rec.get("entry_ts", ""),
                    "exit_reason": rec.get("exit_reason", "UNKNOWN"),
                }


    print(f"{_now_local()} {len(processed_ids)} processed  {len(completed_shadows)} shadow-complete  {len(state['pending_shadow'])} pending")

    # ── Main loop ─────────────────────────────────────────────────
    while True:
        try:
            now = _now_ts()

            # ── Phase 1: process new CLOSE records ───────────────
            new_trades, new_offset = _tail_new_lines(
                PAPER_TRADES_PATH, state["trade_offset"]
            )
            if new_trades:
                state["trade_offset"] = new_offset

            for rec in new_trades:
                if rec.get("type") != "CLOSE":
                    continue

                trade_id = rec.get("id")
                if not trade_id:
                    continue

                # ── DEDUP: hot set check — O(1), no file read ────
                if trade_id in processed_ids:
                    continue



                summary = _compile_phase1(rec)
                if not summary:
                    print(f"{_now_local()} ✗ P1  {_fmt_mint(trade_id)}  failed")
                    continue

                _append_jsonl(TRADE_SUMMARY_PATH, summary)

                processed_ids.add(trade_id)

                exit_reason = summary.get("exit_reason", "UNKNOWN")
                if exit_reason not in NO_SHADOW_REASONS:
                    deadline = now + SHADOW_WAIT_SEC
                    state["pending_shadow"][trade_id] = {
                        "deadline_ts": deadline,
                        "entry_ts":    summary["entry_ts"],
                        "exit_reason": exit_reason,
                    }

                # Phase 1 sync
                sync_ok = _call_sync(1, trade_id, shared, complete=False)
                if sync_ok:
                    _update_jsonl_record(TRADE_SUMMARY_PATH, trade_id, {"synced": True})

                shadow_tag = "pending" if exit_reason not in NO_SHADOW_REASONS else "n/a"
                print(f"{_now_local()} ✓ P1  {_fmt_mint(trade_id)}  {exit_reason:<8} roi={summary['exit_roi_pct']:+}%  shadow={shadow_tag}")

            # ── Phase 2: process pending shadow trades ────────────
            completed_now: List[str] = []

            for trade_id, pending in list(state["pending_shadow"].items()):

                # ── DEDUP: skip if already shadow-complete ────────
                if trade_id in completed_shadows:
                    completed_now.append(trade_id)
                    continue

                updates = _compute_phase2_from_ticks(trade_id, pending)

                if updates:
                    ok = _update_jsonl_record(TRADE_SUMMARY_PATH, trade_id, updates)
                    if ok:
                        completed_shadows.add(trade_id)
                        completed_now.append(trade_id)

                        sync_ok = _call_sync(2, trade_id, shared, complete=True)
                        if sync_ok:
                            _update_jsonl_record(TRADE_SUMMARY_PATH, trade_id, {"synced": True})

                        print(f"{_now_local()} ✓ P2  {_fmt_mint(trade_id)}  peak={updates.get('peak_roi_pct'):+}%")
                    else:
                        print(f"{_now_local()} ⚠ P2  {_fmt_mint(trade_id)}  record missing")
                        completed_now.append(trade_id)

                elif now >= pending["deadline_ts"]:
                    print(f"{_now_local()} ⏱ P2  {_fmt_mint(trade_id)}  timeout")
                    _update_jsonl_record(TRADE_SUMMARY_PATH, trade_id,
                                         {"shadow_complete": False})
                    completed_shadows.add(trade_id)
                    completed_now.append(trade_id)

                    sync_ok = _call_sync(2, trade_id, shared, complete=False)
                    if sync_ok:
                        _update_jsonl_record(TRADE_SUMMARY_PATH, trade_id, {"synced": True})

            for tid in completed_now:
                state["pending_shadow"].pop(tid, None)

            for tid, pending in list(state["pending_shadow"].items()):
                entry_ts = pending.get("entry_ts")
                if not entry_ts:
                    continue
                entry_dt = _parse_ts(entry_ts)
                if not entry_dt:
                    continue
                age_sec = now - entry_dt.timestamp()
                if age_sec > MAX_HOLD_SEC:
                    exit_reason = pending.get("exit_reason", "UNKNOWN")
                    if exit_reason not in ("UNKNOWN", ""):
                        updates = _compute_phase2_from_ticks(tid, pending)
                        if updates:
                            ok = _update_jsonl_record(TRADE_SUMMARY_PATH, tid, updates)
                            if ok:
                                completed_shadows.add(tid)
                                _call_sync(2, tid, shared, complete=True)
                                print(f"{_now_local()} ✓ P2  {_fmt_mint(tid)}  peak={updates.get('peak_roi_pct'):+}%  (recovered)")
                        else:
                            _update_jsonl_record(TRADE_SUMMARY_PATH, tid, {"shadow_complete": False})
                        completed_shadows.add(tid)
                        state["pending_shadow"].pop(tid, None)
                    else:
                        print(f"{_now_local()} ⚠ STALE {_fmt_mint(tid)}  age={age_sec:.0f}s")
                        _update_jsonl_record(TRADE_SUMMARY_PATH, tid, {
                            "exit_reason":    "STALE",
                            "is_valid":       False,
                            "shadow_complete": False,
                        })
                        completed_shadows.add(tid)
                        state["pending_shadow"].pop(tid, None)

            _save_state(state)

        except Exception as e:
            log_error("post_mortem", f"loop error: {e}")

        time.sleep(LOOP_SLEEP_SEC)


if __name__ == "__main__":
    main()
