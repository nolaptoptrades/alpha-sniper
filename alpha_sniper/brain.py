#!/usr/bin/env python3
"""
brain.py — NLT Alpha Sniper v0.4.0

Rule-based decision engine. Ported from entry_worker.py.

RESPONSIBILITIES
────────────────
  compute_score_and_verdict()  — 12-signal rule scorer + hard blocks
  check_fast_track()           — early-entry gate, all guards must pass
  main()                       — poll loop: reads entry_queue.jsonl,
                                 scores each mint, writes verdicts.jsonl

DATA FLOW
─────────
  safety.py  →  entry_queue.jsonl
  brain.py   reads entry_queue.jsonl
  brain.py   →  verdicts.jsonl
  simulator.py reads verdicts.jsonl

DEDUP STRATEGY
──────────────
  brain_state.json stores two things:
    queue_offset  — byte position in entry_queue.jsonl so we never re-read
                    the whole file on every poll tick
    processed     — {mint: iso_ts} map with TTL so that even if offset
                    resets on crash-restart, or safety.py writes a duplicate
                    line, the mint is skipped cleanly

HARD BLOCKS
───────────
  Brain has its own entry gates independent from safety.py.
  Controlled by brain.hard_blocks.enabled in config.json.
  When disabled, brain fully trusts safety's filtering and only scores.

FUTURE UPDATES
──────────────
  ML/KNN scoring, wallet brain (transaction pattern analysis),
  shadow mode (silent scoring alongside rule bot) — not part of
  current shipping pipeline. See _brain_upgrade_roadmap in config.json.

All tuneable values live in config.json. Never bury magic numbers in logic.
"""

import json
import os
import time
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from paths import get_config, get_path, log_error


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS  — all values sourced from config.json
# ─────────────────────────────────────────────────────────────────────────────

cfg = get_config()

# ── Brain hard-block thresholds (brain's own gates, independent from safety) ──
_hb = cfg.get("brain", {}).get("hard_blocks", {})

HARD_BLOCKS_ENABLED       = bool(_hb.get("enabled", True))
MIN_LIQ_USD               = float(_hb.get("min_liq_usd", 20000.0))
MIN_MOM_HARD              = float(_hb.get("min_mom_pct", -5.0))
MAX_MOM_HARD              = float(_hb.get("max_mom_pct", 50.0))
MAX_TOP10_PRIVATE_PCT     = float(_hb.get("max_top10_private_pct", 35.0))
WHALE_ABSOLUTE_CEILING    = float(_hb.get("whale_absolute_ceiling_pct", 60.0))

# ── Scoring constants ─────────────────────────────────────────────────────────
_sc = cfg["scoring"]

BUY_SCORE_MIN             = int(_sc["buy_score_min"])
WATCH_SCORE_MIN           = int(_sc["watch_score_min"])
MIN_BSR                   = float(_sc["min_bsr"])
MIN_MOM_M5                = float(_sc["min_mom_m5"])
MAX_MOM_M5                = float(_sc["max_mom_m5"])
LATE_ENTRY_HARD_BLOCK_PCT = float(_sc["late_entry_hard_block_pct"])
LATE_ENTRY_WARN_PCT       = float(_sc["late_entry_warn_pct"])

LP_TREND_STRONG_UP   = float(_sc["lp_trend_strong_up"])
LP_TREND_FLAT        = float(_sc["lp_trend_flat"])
LP_TREND_FALLING     = float(_sc["lp_trend_falling"])
LP_TREND_STRONG_DOWN = float(_sc["lp_trend_strong_down"])

VOL_TREND_ACCEL     = float(_sc["vol_trend_accel"])
VOL_TREND_DECEL     = float(_sc["vol_trend_decel"])
LIQ_TREND_WARN_DROP = float(_sc["liq_trend_warn_drop_usd"])
LIQ_TREND_GOOD_RISE = float(_sc["liq_trend_good_rise_usd"])

FLOW_STRONG_POS = float(_sc["flow_strong_pos"])
FLOW_POS        = float(_sc["flow_pos"])
FLOW_NEG        = float(_sc["flow_neg"])
FLOW_STRONG_NEG = float(_sc["flow_strong_neg"])

BSR_RECENCY_WEIGHT   = int(_sc["bsr_recency_weight"])
DUMP_SPIKE_HEAVY_BSR = float(_sc["dump_spike_heavy_bsr"])
DUMP_SPIKE_MILD_BSR  = float(_sc["dump_spike_mild_bsr"])
VOL_HIGH_USD         = float(_sc["vol_high_usd"])
VOL_MODERATE_USD     = float(_sc["vol_moderate_usd"])

# ── Fast-track constants ──────────────────────────────────────────────────────
_ft = cfg["fast_track"]

MIN_FAST_TRACK_SAMPLES = int(_ft["min_samples"])
FAST_TRACK_SCORE_MIN   = int(_ft["score_min"])
FAST_TRACK_BSR_MIN     = float(_ft["bsr_min"])
FAST_TRACK_PRICE_MIN   = float(_ft["price_min_pct"])
FAST_TRACK_PRICE_MAX   = float(_ft["price_max_pct"])
FAST_TRACK_LIQ_RATIO   = float(_ft["liq_ratio"])

# ── Brain loop constants ──────────────────────────────────────────────────────
_br = cfg["brain"]

LOOP_SLEEP_SEC    = float(_br["loop_sleep_sec"])
PROCESSED_TTL_SEC = int(_br["processed_ttl_sec"])

# ── Paths ─────────────────────────────────────────────────────────────────────
ENTRY_QUEUE_PATH = get_path("entry_queue")
VERDICTS_PATH    = get_path("verdicts")
BRAIN_STATE_PATH = get_path("brain_state")


# ─────────────────────────────────────────────────────────────────────────────
# UTILITY
# ─────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _safe_float(val: Any) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except Exception:
        return None


def _window_thirds(
    samples: List[Dict[str, Any]], key: str
) -> Tuple[Optional[float], Optional[float]]:
    """Return (first-third avg, last-third avg) for a sample key."""
    n = len(samples)
    if n < 3:
        return None, None
    third = max(1, n // 3)
    fv = [_safe_float(s.get(key)) for s in samples[:third]]
    lv = [_safe_float(s.get(key)) for s in samples[n - third:]]
    fv = [v for v in fv if v is not None]
    lv = [v for v in lv if v is not None]
    if not fv or not lv:
        return None, None
    return sum(fv) / len(fv), sum(lv) / len(lv)


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


# ─────────────────────────────────────────────────────────────────────────────
# SCORING ENGINE  — 12 signals + hard blocks
# Ported from entry_worker.py compute_score_and_verdict()
# ─────────────────────────────────────────────────────────────────────────────

def compute_score_and_verdict(
    meta:         Dict[str, Any],
    samples:      List[Dict[str, Any]],
    pair_changed: bool,
    trigger:      str = "NORMAL",
) -> Dict[str, Any]:
    """
    Score a mint across 12 signals and return a verdict.

    Works on any samples list >= 1. When called for fast-track,
    samples may be only 10-29 items — all signals degrade gracefully.

    trigger: "NORMAL" | "FAST_TRACK"

    Returns:
        score        int    0-100 capped
        score_raw    int    uncapped pre-cap value
        verdict      str    BUY | WATCH | SKIP
        trigger      str
        hard_blocks  list[str]   non-empty → verdict forced to SKIP
        reasons      list[str]   capped at 15 entries
        diagnostics  dict
    """
    hard_blocks: List[str] = []
    reasons:     List[str] = []
    n = len(samples)

    start_snap = samples[0]
    end_snap   = samples[-1]

    price_start = _safe_float(start_snap.get("priceUsd"))
    price_end   = _safe_float(end_snap.get("priceUsd"))

    window_price_chg: Optional[float] = None
    if price_start and price_start > 0 and price_end is not None:
        window_price_chg = (price_end / price_start - 1.0) * 100.0

    liq_start = _safe_float(start_snap.get("liquidity_usd"))
    liq_end_v = _safe_float(end_snap.get("liquidity_usd"))
    vol_start = _safe_float(start_snap.get("volume_m5"))
    vol_end_v = _safe_float(end_snap.get("volume_m5"))
    lp_first_avg, lp_last_avg = _window_thirds(samples, "lp_ratio")

    # ── Hard blocks (brain's own gates, toggled via config) ───────────────────

    if HARD_BLOCKS_ENABLED:
        # Whale — high-confidence hard block
        whale_pct  = meta.get("whale_top10_private_percent")
        whale_conf = meta.get("whale_confidence")
        if whale_pct is not None and whale_conf == "high":
            try:
                if float(whale_pct) > MAX_TOP10_PRIVATE_PCT:
                    hard_blocks.append(
                        f"whale_top10>{MAX_TOP10_PRIVATE_PCT:.0f}% (confidence=high)"
                    )
            except Exception:
                pass

        # Whale — absolute ceiling, always active regardless of confidence
        if whale_pct is not None:
            try:
                if float(whale_pct) > WHALE_ABSOLUTE_CEILING:
                    hard_blocks.append(
                        f"whale_top10>{WHALE_ABSOLUTE_CEILING:.0f}% (absolute ceiling)"
                    )
            except Exception:
                pass

        # Liquidity floor
        try:
            liq_end = float(end_snap.get("liquidity_usd") or 0.0)
            if liq_end < MIN_LIQ_USD:
                hard_blocks.append(f"liq<{MIN_LIQ_USD:g}")
        except Exception:
            hard_blocks.append("liq_missing_or_invalid")

        # Momentum bounds
        mom_end: Optional[float] = None
        try:
            mom_raw = end_snap.get("priceChange_m5")
            if mom_raw is not None:
                mom_end = float(mom_raw)
                if mom_end < 0:
                    hard_blocks.append(f"mom_negative:{mom_end:.2f}%")
                elif mom_end < MIN_MOM_HARD:
                    hard_blocks.append(f"mom_too_low:{mom_end:.1f}%<{MIN_MOM_HARD}%")
                elif mom_end > MAX_MOM_HARD:
                    hard_blocks.append(f"mom_too_high:{mom_end:.1f}%>{MAX_MOM_HARD}%")
            else:
                reasons.append("mom_pct_m5 missing")
        except Exception:
            reasons.append("mom_pct_m5 invalid")
    else:
        mom_end = None
        try:
            mom_raw = end_snap.get("priceChange_m5")
            if mom_raw is not None:
                mom_end = float(mom_raw)
        except Exception:
            pass

    # ── Always-active blocks (regardless of hard_blocks toggle) ───────────────

    # Late entry — window price already ran too far
    if window_price_chg is not None and window_price_chg > LATE_ENTRY_HARD_BLOCK_PCT:
        hard_blocks.append(
            f"late_entry: window_price_chg={window_price_chg:.1f}%"
            f">{LATE_ENTRY_HARD_BLOCK_PCT}%"
        )

    # Pair changed mid-window — data is unreliable
    if pair_changed:
        hard_blocks.append("pair changed during window")

    # ── Scoring signals ───────────────────────────────────────────────────────
    score    = 0
    flow_end: Optional[float] = None

    # [1] LP RATIO — end snapshot
    try:
        lp_end = float(end_snap.get("lp_ratio") or 0.0)
        if lp_end >= 1.0:
            score += 30; reasons.append(f"lp_ratio={lp_end:.3f} (strong)")
        elif lp_end >= 0.5:
            score += 15; reasons.append(f"lp_ratio={lp_end:.3f} (moderate)")
        elif lp_end >= 0.15:
            score += 5;  reasons.append(f"lp_ratio={lp_end:.3f} (weak)")
        else:
            score -= 10; reasons.append(f"lp_ratio={lp_end:.3f} (very low)")
    except Exception:
        reasons.append("lp_ratio missing")

    # [2] LP RATIO TREND — first-third vs last-third avg
    if lp_first_avg is not None and lp_last_avg is not None:
        lp_delta = lp_last_avg - lp_first_avg
        if lp_delta >= LP_TREND_STRONG_UP:
            score += 10; reasons.append(f"lp_trend=+{lp_delta:.3f} (rising)")
        elif lp_delta >= LP_TREND_FLAT:
            score += 3;  reasons.append(f"lp_trend=+{lp_delta:.3f} (stable)")
        elif lp_delta >= LP_TREND_FALLING:
            reasons.append(f"lp_trend={lp_delta:.3f} (slight drop, neutral)")
        elif lp_delta >= LP_TREND_STRONG_DOWN:
            score -= 8;  reasons.append(f"lp_trend={lp_delta:.3f} (falling)")
        else:
            score -= 15; reasons.append(f"lp_trend={lp_delta:.3f} (hard drop)")
    else:
        reasons.append("lp_trend: insufficient samples")

    # [3] BSR END — buy/sell ratio at last sample
    bsr_end: float = 0.0
    try:
        bsr_end = float(end_snap.get("bsr") or 0.0)
        if 1.5 <= bsr_end < 2.0:
            score += 25; reasons.append(f"bsr_end={bsr_end:.3f} (best band 1.5-2.0)")
        elif bsr_end >= 2.0:
            score += 20; reasons.append(f"bsr_end={bsr_end:.3f} (high, watch for spike)")
        elif 1.3 <= bsr_end < 1.5:
            score += 10; reasons.append(f"bsr_end={bsr_end:.3f} (solid 1.3-1.5)")
        elif 1.0 <= bsr_end < 1.3:
            score += 5;  reasons.append(f"bsr_end={bsr_end:.3f} (mild buy pressure)")
        else:
            score -= 15; reasons.append(f"bsr_end={bsr_end:.3f} (sell pressure)")
    except Exception:
        reasons.append("bsr_end missing")

    # [4] RECENCY-WEIGHTED SUSTAINED BSR
    if n >= 3:
        third    = max(1, n // 3)
        early_s  = samples[:n - third]
        recent_s = samples[n - third:]

        def _bsr_hits(sl: List[Dict[str, Any]]) -> int:
            return sum(
                1 for s in sl
                if (_safe_float(s.get("bsr")) or 0.0) >= MIN_BSR
            )

        eh = _bsr_hits(early_s);  et = len(early_s)
        rh = _bsr_hits(recent_s); rt = len(recent_s)
        w_rate = (eh + BSR_RECENCY_WEIGHT * rh) / max(1, et + BSR_RECENCY_WEIGHT * rt)

        if w_rate >= 0.70:
            score += 10
            reasons.append(
                f"bsr_sustained: {eh}/{et} early, {rh}/{rt} recent "
                f"→ {w_rate:.2f} (strong)"
            )
        elif w_rate >= 0.45:
            score += 5; reasons.append(f"bsr_sustained: rate={w_rate:.2f} (partial)")
        else:
            reasons.append(f"bsr_sustained: rate={w_rate:.2f} (weak)")
    else:
        reasons.append("bsr_sustained: too few samples")

    # [5] MOMENTUM — DexScreener 5m rolling candle
    if mom_end is not None:
        if MIN_MOM_M5 <= mom_end <= MAX_MOM_M5:
            score += 20; reasons.append(f"mom_5m={mom_end:.2f}% (target band)")
        elif 10.0 < mom_end <= 20.0:
            score += 15; reasons.append(f"mom_5m={mom_end:.2f}% (above band, ok)")
        elif mom_end > 20.0:
            score += 10; reasons.append(f"mom_5m={mom_end:.2f}% (high, SL risk)")
        else:
            score += 5;  reasons.append(f"mom_5m={mom_end:.2f}% (below band, weak)")

    # [6] WINDOW PRICE VELOCITY — price change across entire sample window
    if window_price_chg is not None:
        if 5.0 <= window_price_chg <= LATE_ENTRY_WARN_PCT:
            score += 8;  reasons.append(f"window_vel=+{window_price_chg:.1f}% (healthy)")
        elif 0.0 < window_price_chg < 5.0:
            score += 3;  reasons.append(f"window_vel=+{window_price_chg:.1f}% (slow pos)")
        elif LATE_ENTRY_WARN_PCT < window_price_chg <= LATE_ENTRY_HARD_BLOCK_PCT:
            score -= 10; reasons.append(f"window_vel=+{window_price_chg:.1f}% (late entry warn)")
        elif window_price_chg < 0:
            score -= 8;  reasons.append(f"window_vel={window_price_chg:.1f}% (falling)")
    else:
        reasons.append("window_vel: price data missing")

    # [7] VOLUME absolute — end snapshot
    try:
        ve = end_snap.get("volume_m5")
        if ve is not None:
            ve = float(ve)
            if ve >= VOL_HIGH_USD:
                score += 15; reasons.append(f"vol={ve:,.0f} (high)")
            elif ve >= VOL_MODERATE_USD:
                score += 8;  reasons.append(f"vol={ve:,.0f} (moderate)")
            else:
                reasons.append(f"vol={ve:,.0f} (low, neutral)")
        else:
            reasons.append("vol missing")
    except Exception:
        reasons.append("vol invalid")

    # [8] VOLUME TREND — start vs end
    if vol_start is not None and vol_start > 0 and vol_end_v is not None:
        vt = (vol_end_v - vol_start) / vol_start
        if vt >= VOL_TREND_ACCEL:
            score += 8;  reasons.append(f"vol_trend=+{vt*100:.0f}% (accel)")
        elif vt <= VOL_TREND_DECEL:
            score -= 8;  reasons.append(f"vol_trend={vt*100:.0f}% (decel)")
        else:
            reasons.append(f"vol_trend={vt*100:.0f}% (flat)")
    else:
        reasons.append("vol_trend: insufficient data")

    # [9] FLOW RATIO — net buy/sell flow pressure
    try:
        fr = end_snap.get("flow_ratio")
        if fr is not None:
            flow_end = float(fr)
            if flow_end >= FLOW_STRONG_POS:
                score += 8;  reasons.append(f"flow={flow_end:.3f} (strong inflows)")
            elif flow_end >= FLOW_POS:
                score += 4;  reasons.append(f"flow={flow_end:.3f} (pos flow)")
            elif flow_end >= FLOW_NEG:
                reasons.append(f"flow={flow_end:.3f} (neutral)")
            elif flow_end >= FLOW_STRONG_NEG:
                score -= 5;  reasons.append(f"flow={flow_end:.3f} (outflows)")
            else:
                score -= 12; reasons.append(f"flow={flow_end:.3f} (heavy outflows)")
        else:
            reasons.append("flow_ratio missing")
    except Exception:
        reasons.append("flow_ratio invalid")

    # [10] LIQUIDITY TREND — pool growing or leaking
    if liq_start is not None and liq_end_v is not None:
        ld = liq_end_v - liq_start
        if ld >= LIQ_TREND_GOOD_RISE:
            score += 5;  reasons.append(f"liq_trend=+${ld:,.0f} (pool growing)")
        elif ld <= -LIQ_TREND_WARN_DROP:
            score -= 5;  reasons.append(f"liq_trend=-${abs(ld):,.0f} (liq leaking)")
        else:
            reasons.append(f"liq_trend=${ld:+,.0f} (stable)")
    else:
        reasons.append("liq_trend: insufficient data")

    # [11] DUMP SPIKES — price drops with low BSR = sell-side aggression
    heavy = mild = 0
    for i in range(1, n):
        p0 = _safe_float(samples[i - 1].get("priceUsd"))
        p1 = _safe_float(samples[i].get("priceUsd"))
        b1 = _safe_float(samples[i].get("bsr")) or 0.0
        if p0 is None or p1 is None:
            continue
        if p1 < p0:
            if b1 <= DUMP_SPIKE_HEAVY_BSR:  heavy += 1
            elif b1 <= DUMP_SPIKE_MILD_BSR: mild  += 1

    if heavy >= 2:
        score -= 15; reasons.append(f"dump_spikes: {heavy} heavy")
    elif heavy == 1:
        score -= 8;  reasons.append(f"dump_spikes: 1 heavy")
    elif mild >= 3:
        score -= 8;  reasons.append(f"dump_spikes: {mild} mild (recurring)")
    elif mild >= 1:
        score -= 4;  reasons.append(f"dump_spikes: {mild} mild")
    else:
        reasons.append("dump_spikes: none")

    # [12] LATE ENTRY WARN — scored penalty already applied in [6], logged here
    if window_price_chg is not None and LATE_ENTRY_WARN_PCT < window_price_chg <= LATE_ENTRY_HARD_BLOCK_PCT:
        reasons.append(
            f"late_entry_warn: window_price_chg={window_price_chg:.1f}% "
            f"(>{LATE_ENTRY_WARN_PCT}%)"
        )

    # ── Verdict ───────────────────────────────────────────────────────────────
    score_capped = max(0, min(100, int(round(score))))

    if hard_blocks:
        verdict = "SKIP"
    elif score_capped >= BUY_SCORE_MIN:
        verdict = "BUY"
    elif score_capped >= WATCH_SCORE_MIN:
        verdict = "WATCH"
    else:
        verdict = "SKIP"

    return {
        "score":       score_capped,
        "score_raw":   score,
        "verdict":     verdict,
        "trigger":     trigger,
        "hard_blocks": hard_blocks,
        "reasons":     reasons[:15],
        "diagnostics": {
            "window_price_chg_pct": window_price_chg,
            "lp_trend_delta":       (lp_last_avg - lp_first_avg)
                                    if lp_first_avg is not None and lp_last_avg is not None
                                    else None,
            "flow_ratio_end":       flow_end,
            "liq_delta_usd":        (liq_end_v - liq_start)
                                    if liq_start is not None and liq_end_v is not None
                                    else None,
            "vol_trend_pct":        ((vol_end_v - vol_start) / vol_start * 100)
                                    if vol_start and vol_start > 0 and vol_end_v is not None
                                    else None,
            "samples_used":         n,
            "bsr_end":              bsr_end,
            "mom_end":              mom_end,
            "liq_end":              liq_end,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# FAST-TRACK GATE
# ─────────────────────────────────────────────────────────────────────────────

def check_fast_track(
    meta:    Dict[str, Any],
    samples: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Evaluate whether a partial sample set qualifies for early entry.

    Runs every tick after MIN_FAST_TRACK_SAMPLES are collected.
    Returns the scorepack (trigger="FAST_TRACK") if ALL guards pass, else None.

    Guards (all must pass simultaneously):
      1. len(samples) >= MIN_FAST_TRACK_SAMPLES  (default 10 × 2s = 20s)
      2. No hard blocks in scorepack
      3. score >= FAST_TRACK_SCORE_MIN  (default 80)
      4. BSR at last sample >= FAST_TRACK_BSR_MIN  (default 2.0)
      5. window_price_chg in [FAST_TRACK_PRICE_MIN, FAST_TRACK_PRICE_MAX]
      6. liq_end >= liq_start * FAST_TRACK_LIQ_RATIO  (not falling >2%)

    WHY ALL GUARDS: score alone can be high on lp_ratio + flow while BSR
    is weak. The explicit BSR + price velocity guards confirm genuine
    momentum rather than a structurally healthy but stagnant pool.
    """
    if len(samples) < MIN_FAST_TRACK_SAMPLES:
        return None

    scorepack = compute_score_and_verdict(
        meta, samples, pair_changed=False, trigger="FAST_TRACK"
    )

    if scorepack["hard_blocks"]:
        return None
    if scorepack["score"] < FAST_TRACK_SCORE_MIN:
        return None

    bsr_now = _safe_float(samples[-1].get("bsr")) or 0.0
    if bsr_now < FAST_TRACK_BSR_MIN:
        return None

    wpc = scorepack["diagnostics"].get("window_price_chg_pct")
    if wpc is None:
        return None
    if not (FAST_TRACK_PRICE_MIN <= wpc <= FAST_TRACK_PRICE_MAX):
        return None

    liq_s = _safe_float(samples[0].get("liquidity_usd"))
    liq_e = _safe_float(samples[-1].get("liquidity_usd"))
    if liq_s is not None and liq_s > 0 and liq_e is not None:
        if liq_e < liq_s * FAST_TRACK_LIQ_RATIO:
            return None

    return scorepack


# ─────────────────────────────────────────────────────────────────────────────
# BRAIN STATE  — offset + processed dedup
# ─────────────────────────────────────────────────────────────────────────────

def _validate_offset(path: str, offset: int, known_size: int = 0) -> int:
    """
    Validate that the stored offset is still valid for this file.
    Returns 0 if the file was rotated/truncated, otherwise returns the offset.
    """
    try:
        stat = os.stat(path)
        current_size = stat.st_size
        current_inode = stat.st_ino
    except FileNotFoundError:
        return 0
    
    if current_size < offset:
        return 0
    
    if known_size > 0 and current_size < known_size:
        return 0
    
    return offset


def _state_load() -> Dict[str, Any]:
    raw = _load_json(BRAIN_STATE_PATH, {})
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("queue_offset", 0)
    raw.setdefault("processed", {})
    raw.setdefault("entry_queue_size", 0)
    raw.setdefault("entry_queue_inode", 0)
    if not isinstance(raw["processed"], dict):
        raw["processed"] = {}
    
    raw["queue_offset"] = _validate_offset(
        ENTRY_QUEUE_PATH,
        raw["queue_offset"],
        raw.get("entry_queue_size", 0)
    )
    
    return raw


def _state_save(state: Dict[str, Any]) -> None:
    try:
        stat = os.stat(ENTRY_QUEUE_PATH)
        state["entry_queue_size"] = stat.st_size
        state["entry_queue_inode"] = stat.st_ino
    except FileNotFoundError:
        state["entry_queue_size"] = 0
        state["entry_queue_inode"] = 0
    
    _save_json(BRAIN_STATE_PATH, state)


def _prune_processed(processed: Dict[str, str]) -> Dict[str, str]:
    """Remove mints older than PROCESSED_TTL_SEC."""
    now_ts = time.time()
    result = {}
    for mint, ts in processed.items():
        t = _parse_iso(ts)
        if t is not None and (now_ts - t) <= PROCESSED_TTL_SEC:
            result[mint] = ts
    return result


def _already_processed(processed: Dict[str, str], mint: str) -> bool:
    ts = processed.get(mint)
    if not ts:
        return False
    t = _parse_iso(ts)
    if t is None:
        return False
    return (time.time() - t) <= PROCESSED_TTL_SEC


def _mark_processed(processed: Dict[str, str], mint: str) -> None:
    processed[mint] = _now_iso()


# ─────────────────────────────────────────────────────────────────────────────
# QUEUE READER
# ─────────────────────────────────────────────────────────────────────────────

def _read_new_queue_lines(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Seek to state["queue_offset"], read any new lines, update offset in place.
    Returns list of valid queue objects (must have "mint" and "samples").
    """
    if not os.path.exists(ENTRY_QUEUE_PATH):
        return []

    out: List[Dict[str, Any]] = []
    with open(ENTRY_QUEUE_PATH, "r", encoding="utf-8") as f:
        f.seek(int(state.get("queue_offset", 0)))
        while True:
            line = f.readline()
            if not line:
                break
            state["queue_offset"] = f.tell()
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("mint"):
                out.append(obj)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# VERDICT EMITTER
# ─────────────────────────────────────────────────────────────────────────────

def _emit_verdict(record: Dict[str, Any]) -> None:
    """Score one queue record and append result to verdicts.jsonl."""
    mint         = record["mint"]

    # Track entry context during sample collection
    entry_price = None
    entry_pair  = None
    entry_dex   = None

    if not record.get("samples"):
        print(f"[brain] collecting samples for {mint[:8]}…")
        from dex_api import fetch_dex_data
        collected = []
        sample_count    = int(cfg.get("brain", {}).get("sample_count", 30))
        sample_interval = float(cfg.get("brain", {}).get("sample_interval_sec", 2.0))
        first_pair = None
        pair_changed = False
        for i in range(sample_count):
            try:
                d    = fetch_dex_data(mint) or {}
                pair = d.get("pair_address")
                if first_pair is None and pair:
                    first_pair = pair
                elif pair and pair != first_pair:
                    pair_changed = True
                # Capture entry context from last sample
                entry_price = d.get("price_usd")
                entry_pair  = d.get("pair_address") or entry_pair
                entry_dex   = d.get("dex_id") or entry_dex
                buys  = int(d.get("buys_5m") or 0)
                sells = int(d.get("sells_5m") or 0)
                bsr   = buys / max(1, sells)
                snap = {
                    "priceUsd":       d.get("price_usd"),
                    "liquidity_usd":  d.get("liquidity_usd"),
                    "volume_m5":      d.get("vol_5m"),
                    "lp_ratio":       d.get("lp_ratio"),
                    "bsr":            round(buys / max(1, sells), 4),
                    "buys_5m":        buys,
                    "sells_5m":       sells,
                    "priceChange_m5": d.get("priceChange_m5"),
                    "flow_ratio":     None,
                }
                collected.append(snap)
                # fast-track check after min samples
                if i + 1 >= MIN_FAST_TRACK_SAMPLES:
                    ft = check_fast_track({}, collected)
                    if ft:
                        print(f"[brain] FAST_TRACK triggered at sample {i+1} for {mint[:8]}…")
                        record = {
                            **record,
                            "samples": collected,
                            "trigger": "FAST_TRACK",
                            "pair_changed": pair_changed,
                            "entry_price_usd": entry_price,
                            "pairAddress": entry_pair,
                            "dexId": entry_dex,
                        }
                        break
            except Exception as e:
                log_error("brain", f"sample fetch error mint={mint[:8]} i={i}: {e}")
            if i < sample_count - 1:
                time.sleep(sample_interval)
        if not record.get("samples"):
            record = {
                **record,
                "samples": collected,
                "pair_changed": pair_changed,
                "entry_price_usd": entry_price,
                "pairAddress": entry_pair,
                "dexId": entry_dex,
            }
    
    samples      = record.get("samples") or []
    if not samples:
        print(f"[brain] no samples collected for {mint[:8]}… skipping")
        return

    # Fallback: if entry_price not captured during collection, use last sample
    entry_price = record.get("entry_price_usd")
    entry_pair  = record.get("pairAddress")
    entry_dex   = record.get("dexId")
    if entry_price is None and samples:
        entry_price = samples[-1].get("priceUsd")

    meta         = record.get("meta") or {}
    pair_changed = bool(record.get("pair_changed", False))
    trigger      = record.get("trigger", "NORMAL")

    scorepack = compute_score_and_verdict(meta, samples, pair_changed, trigger)

    verdict_rec = {
        "ts":              _now_iso(),
        "mint":            mint,
        "launchpad":       record.get("launchpad", "unknown"),
        "entry_price_usd": entry_price,
        "pairAddress":     entry_pair,
        "dexId":           entry_dex,
        "trigger":         scorepack["trigger"],
        "samples_n":       len(samples),
        "score":           scorepack["score"],
        "score_raw":       scorepack["score_raw"],
        "verdict":         scorepack["verdict"],
        "hard_blocks":     scorepack["hard_blocks"],
        "reasons":         scorepack["reasons"],
        "diagnostics":     scorepack["diagnostics"],
    }
    _append_jsonl(VERDICTS_PATH, verdict_rec)
    print(
        f"[brain] {mint[:8]}… "
        f"score={scorepack['score']} "
        f"verdict={scorepack['verdict']} "
        f"trigger={scorepack['trigger']} "
        f"entry_price={entry_price} "
        f"blocks={scorepack['hard_blocks'] or 'none'}"
    )

    
    if scorepack["verdict"] == "BUY":
        try:
            from brain_w import capture_wallet_snapshot
            WALLET_TICKS_PATH = get_path("wallet_ticks")
        except ImportError:
            capture_wallet_snapshot = None
            WALLET_TICKS_PATH = None
        
        if capture_wallet_snapshot and WALLET_TICKS_PATH:
            def _capture():
                try:
                    wallet_snap = capture_wallet_snapshot(mint)
                    if wallet_snap:
                        _append_jsonl(WALLET_TICKS_PATH, wallet_snap)
                except Exception as e:
                    log_error("brain", f"wallet snapshot failed mint={mint[:8]}: {e}")
            threading.Thread(target=_capture, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    os.makedirs(os.path.dirname(BRAIN_STATE_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(VERDICTS_PATH),    exist_ok=True)

    state = _state_load()
    print(
        f"[brain] started — "
        f"offset={state['queue_offset']} "
        f"processed={len(state['processed'])} mints in dedup"
    )

    while True:
        try:
            # Prune expired dedup entries
            state["processed"] = _prune_processed(state["processed"])

            # Read any new lines from entry_queue.jsonl
            new_records = _read_new_queue_lines(state)

            for record in new_records:
                mint = record.get("mint", "")
                if not mint:
                    continue

                # Skip if already scored within TTL
                if _already_processed(state["processed"], mint):
                    continue

                _emit_verdict(record)
                _mark_processed(state["processed"], mint)

            # Persist state after every batch
            if new_records:
                _state_save(state)

        except Exception as e:
            log_error("brain", f"main loop error: {e}")

        time.sleep(LOOP_SLEEP_SEC)


if __name__ == "__main__":
    main()
