#!/usr/bin/env python3
"""
simulator.py — NLT Alpha Sniper v0.7.0

RESPONSIBILITY: Paper trade simulation only.
  - Reads verdicts.jsonl for BUY signals
  - Opens and tracks paper positions
  - Polls price/liq per position via dex_api
  - Closes positions on TP / SL / TSL / TIMEOUT / RUG / PRICE_MISS
  - Writes paper_trades.jsonl (OPEN + CLOSE records)
  - Writes paper_ticks.jsonl (per-tick state)
  - Writes shadow_ticks.jsonl (ghost tracking after TP/TSL for ceiling data)

DOES NOT:
  - Score or filter entry candidates (that is Brain's job)
  - Calculate MFE/MAE or summaries (that is Curator's job)
  - Make any trading decisions beyond the exit rules in config.json

COMMUNICATION:
  Input:  verdicts.jsonl         (Brain → Simulator)
  Output: paper_trades.jsonl     (Simulator → Curator)
          paper_ticks.jsonl      (Simulator → Curator)
          shadow_ticks.jsonl     (Simulator → Curator)

All tuneable values live in config.json. Never hardcode constants here.
"""

import os
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from paths import get_config, get_path, get_setting, log_error
from dex_api import fetch_dex_data

# ─────────────────────────────────────────────────────────────
# CONFIG — all values from config.json via paths.py
# ─────────────────────────────────────────────────────────────

VERDICTS_PATH     = get_path("verdicts")
PAPER_PATH        = get_path("paper_trades")
TICKS_PATH        = get_path("paper_ticks")
SIM_STATE_PATH    = get_path("sim_state")
SHADOW_TICKS_PATH = get_path("shadow_ticks")
SHADOW_STATE_PATH = get_path("shadow_state")

# Simulator tuning
POLL_SEC       = float(get_setting("simulator", "paper_poll_sec",       2))
PRICE_MISS_MAX = int(  get_setting("simulator", "price_miss_max",       6))
MAX_ACTIVE     = int(  get_setting("simulator", "max_active_positions", 4))
TICKS_ENABLED  = bool( get_setting("simulator", "paper_ticks_enabled",  True))
LOOP_SLEEP     = float(get_setting("simulator", "loop_sleep_sec",       0.5))

# Trade exit rules
TP_PCT       = float(get_setting("trade_rules", "tp_pct",           30.0))
SL_PCT       = float(get_setting("trade_rules", "sl_pct",          -22.0))
MAX_HOLD_SEC = float(get_setting("trade_rules", "max_hold_sec",     2700))
TSL_ACTIVATE = float(get_setting("trade_rules", "tsl_activate_roi", 10.0))
TSL_LOCK     = float(get_setting("trade_rules", "tsl_lock_roi",      1.0))

# Rug detection
RUG_LIQ_FLOOR = float(get_setting("rug_detection", "liq_floor_usd",    1000.0))
RUG_LIQ_WARN  = float(get_setting("rug_detection", "liq_warn_usd",    10000.0))
MAX_SANE_ROI  = float(get_setting("rug_detection", "max_sane_roi_pct",  500.0))

# Shadow trailing
SHADOW_ENABLED  = bool( get_setting("shadow_trailing", "enabled",       True))
SHADOW_POLL_SEC = float(get_setting("shadow_trailing", "poll_sec",      5))
SHADOW_MAX_SEC  = float(get_setting("shadow_trailing", "max_track_sec", 1800))


# ─────────────────────────────────────────────────────────────
# UTILITY
# ─────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_local() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _fmt_mint(mint: str) -> str:
    return f"{mint[:5]}…{mint[-3:]}" if len(mint) > 8 else mint


def parse_iso(s: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_jsonl(path: str, obj: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────
# VERDICT READER — tail verdicts.jsonl for new BUY signals
# ─────────────────────────────────────────────────────────────

def read_new_verdicts(state: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Reads new lines from verdicts.jsonl since last offset.
    Returns only BUY verdicts.
    """
    st = dict(state)
    st.setdefault("verdict_offset", 0)

    if not os.path.exists(VERDICTS_PATH):
        return [], st

    current_size = os.path.getsize(VERDICTS_PATH)
    if current_size < st["verdict_offset"]:
        print(f"{_now_local()} [sim] verdicts.jsonl truncated — resetting offset")
        st["verdict_offset"] = 0

    buys: List[Dict[str, Any]] = []
    with open(VERDICTS_PATH, "r", encoding="utf-8") as f:
        f.seek(st["verdict_offset"])
        while True:
            line = f.readline()
            if not line:
                break
            st["verdict_offset"] = f.tell()
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("verdict") == "BUY":
                buys.append(obj)

    return buys, st


# ─────────────────────────────────────────────────────────────
# PRICE FETCHER
# ─────────────────────────────────────────────────────────────

def fetch_price(mint: str, pair: Optional[str], dex: Optional[str]) -> Dict[str, Any]:
    """
    Fetch current price and liq for an open position via dex_api.
    Returns dict with priceUsd, liq_usd, pairAddress, dexId.
    All values may be None if API fails.
    """
    try:
        data = fetch_dex_data(mint)
    except Exception:
        return {"priceUsd": None, "liq_usd": None, "bsr": None, "pairAddress": pair, "dexId": dex}

    buys = data.get("buys_5m") or 0
    sells = data.get("sells_5m") or 0
    bsr = buys / max(1, sells) if (buys or sells) else None

    return {
        "priceUsd":    data.get("price_usd"),
        "liq_usd":     data.get("liquidity_usd"),
        "bsr":         bsr,
        "pairAddress": data.get("pair_address") or pair,
        "dexId":       data.get("dex_id") or dex,
    }


# ─────────────────────────────────────────────────────────────
# POSITION OPEN / CLOSE
# ─────────────────────────────────────────────────────────────

def open_position(verdict: Dict[str, Any], open_mints: set) -> Optional[Dict[str, Any]]:
    """
    Create an in-memory position from a BUY verdict and write OPEN record.
    Returns position dict or None if mint already open or price invalid.
    """
    mint = verdict.get("mint")
    if not mint or mint in open_mints:
        return None

    launchpad = verdict.get("launchpad", "unknown")

    entry_price = verdict.get("entry_price_usd")
    if not entry_price or float(entry_price) <= 0:
        print(f"{_now_local()} [sim] ✗ SKIP    {_fmt_mint(mint)}  no valid entry_price")
        return None

    pos_id = str(uuid.uuid4())
    pos = {
        "id":              pos_id,
        "mint":            mint,
        "launchpad":       launchpad,
        "pairAddress":     verdict.get("pairAddress"),
        "dexId":           verdict.get("dexId"),
        "entry_ts":        now_iso(),
        "entry_price_usd": float(entry_price),
        "trigger":         verdict.get("trigger", "NORMAL"),
        "context":         verdict.get("context", {}),
        "_tsl_active":     False,
        "_price_misses":   0,
    }

    record = {
        "type":            "OPEN",
        "id":              pos_id,
        "mint":            mint,
        "launchpad":       launchpad,
        "pairAddress":     pos["pairAddress"],
        "dexId":           pos["dexId"],
        "entry_ts":        pos["entry_ts"],
        "entry_price_usd": pos["entry_price_usd"],
        "trigger":         pos["trigger"],
        "context":         pos["context"],
        "rules": {
            "tp_pct":       TP_PCT,
            "sl_pct":       SL_PCT,
            "max_hold_sec": MAX_HOLD_SEC,
            "tsl_activate": TSL_ACTIVATE,
            "tsl_lock":     TSL_LOCK,
        },
    }
    append_jsonl(PAPER_PATH, record)
    print(f"{_now_local()} [sim] ● OPEN    {_fmt_mint(mint)}  entry={entry_price:.7f}  trigger={pos['trigger']}")
    return pos


def close_position(
    pos:        Dict[str, Any],
    exit_price: float,
    reason:     str,
    open_mints: set,
) -> Tuple[str, float]:
    """
    Write CLOSE record and remove mint from open set.
    Applies ROI sanity gate — anything above MAX_SANE_ROI is relabeled RUG.
    """
    entry_price = float(pos["entry_price_usd"])
    roi         = (exit_price / entry_price - 1.0) * 100.0

    if roi > MAX_SANE_ROI:
        print(f"{_now_local()} [sim] ⚠ SANITY  {_fmt_mint(pos['mint'])}  roi={roi:+.1f}% → RUG")
        reason     = "RUG"
        exit_price = 0.0
        roi        = -100.0

    _LOSS_REASONS = {"SL", "TSL", "RUG", "PRICE_MISS"}
    record = {
        "type":           "CLOSE",
        "id":             pos["id"],
        "mint":           pos["mint"],
        "launchpad":      pos.get("launchpad", "unknown"),
        "pairAddress":    pos.get("pairAddress"),
        "dexId":          pos.get("dexId"),
        "trigger":        pos.get("trigger", "NORMAL"),
        "exit_ts":        now_iso(),
        "exit_price_usd": exit_price,
        "roi_pct":        roi,
        "reason":         reason,
        "is_loss":        reason in _LOSS_REASONS,
    }
    append_jsonl(PAPER_PATH, record)
    open_mints.discard(pos["mint"])
    _close_icons = {"TP": "✓", "TSL": "✓", "SL": "✗", "RUG": "✗", "TIMEOUT": "✗"}
    _icon = _close_icons.get(reason, "●")
    print(f"{_now_local()} [sim] {_icon} CLOSE/{reason:<7}  {_fmt_mint(pos['mint'])}  roi={roi:+.2f}%")
    return reason, roi


# ─────────────────────────────────────────────────────────────
# TICK WRITER
# ─────────────────────────────────────────────────────────────

def write_tick(
    pos:        Dict[str, Any],
    price:      Optional[float],
    liq:        Optional[float],
    bsr:        Optional[float] = None,
    roi_pct:    float = 0.0,
    age_sec:    float = 0.0,
    rug_flag:   bool  = False,
    tsl_active: bool  = False,
    dynamic_sl: float = SL_PCT,
) -> None:
    if not TICKS_ENABLED:
        return
    try:
        tick = {
            "id":              pos["id"],
            "mint":            pos["mint"],
            "pairAddress":     pos.get("pairAddress"),
            "dexId":           pos.get("dexId"),
            "pos_age_sec":     age_sec,
            "roi_pct":         roi_pct,
            "entry_price_usd": pos["entry_price_usd"],
            "price_usd":       price,
            "liq_usd":         liq,
            "bsr":             bsr,
            "rug_flag":        rug_flag,
            "tsl_active":      tsl_active,
            "dynamic_sl_pct":  dynamic_sl,
            "ts":              now_iso(),
        }
        append_jsonl(TICKS_PATH, tick)
    except Exception as e:
        log_error("sim", f"tick write failed: {e}")


# ─────────────────────────────────────────────────────────────
# POSITION TICK — core exit logic
# ─────────────────────────────────────────────────────────────

def tick_position(
    pos:        Dict[str, Any],
    open_mints: set,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Single monitoring tick for one open position.
    Returns (closed, shadow_ghost).

    Exit priority:
      1. PRICE_MISS  — consecutive API failures
      2. RUG         — liq below floor OR phantom price with low liq
      3. TP          — roi >= tp_pct
      4. TSL         — tsl active and roi dropped below tsl_lock
      5. SL          — roi <= sl_pct (only when TSL not active)
      6. TIMEOUT     — age > max_hold_sec

    Ticks are only written if no exit condition fires — prevents
    writing glitched price spikes from phantom/RUG events.
    """
    mint = pos["mint"]
    pair = pos.get("pairAddress")
    dex  = pos.get("dexId")

    fetched = fetch_price(mint, pair, dex)
    price   = fetched.get("priceUsd")
    liq     = fetched.get("liq_usd")
    bsr     = fetched.get("bsr")

    # ── 1. Price miss guard ───────────────────────────────────────────
    if price is None:
        pos["_price_misses"] = int(pos.get("_price_misses", 0)) + 1
        if pos["_price_misses"] >= PRICE_MISS_MAX:
            close_position(pos, float(pos["entry_price_usd"]), "PRICE_MISS", open_mints)
            return True, None
        return False, None
    pos["_price_misses"] = 0

    # ── 2. Rug detection (liq floor) ──────────────────────────────────
    if liq is None:
        # No liquidity data at all — skip this tick, don't write
        return False, None

    if liq < RUG_LIQ_FLOOR:
        write_tick(pos, price, liq, bsr, rug_flag=True)
        close_position(pos, 0.0, "RUG", open_mints)
        return True, None

    # ── Core calculations ─────────────────────────────────────────────
    entry_price = float(pos["entry_price_usd"])
    entry_ts    = datetime.fromisoformat(pos["entry_ts"])
    age_sec     = (datetime.now(timezone.utc) - entry_ts).total_seconds()
    roi_pct     = (price / entry_price - 1.0) * 100.0
    rug_flag    = liq < RUG_LIQ_WARN

    # ── Phantom price detection (both roi AND liq must agree) ────────
    if roi_pct > MAX_SANE_ROI and rug_flag:
        print(f"{_now_local()} [sim] ⚠ PHANTOM {_fmt_mint(mint)}  roi={roi_pct:+.1f}%  liq=${liq:,.0f} → RUG")
        write_tick(pos, price, liq, bsr, roi_pct, age_sec, True,
                   pos["_tsl_active"], SL_PCT)
        close_position(pos, 0.0, "RUG", open_mints)
        return True, None

    # ── TSL activation ────────────────────────────────────────────────
    if not pos["_tsl_active"] and roi_pct >= TSL_ACTIVATE:
        pos["_tsl_active"] = True
        print(f"{_now_local()} [sim] ↕ TSL ARM  {_fmt_mint(mint)}  roi={roi_pct:+.2f}%  (still open)")

    dynamic_sl = TSL_LOCK if pos["_tsl_active"] else SL_PCT

    # ── 3. TP ─────────────────────────────────────────────────────────
    if roi_pct >= TP_PCT:
        write_tick(pos, price, liq, bsr, roi_pct, age_sec, rug_flag,
                   pos["_tsl_active"], dynamic_sl)
        close_position(pos, price, "TP", open_mints)
        ghost = spawn_shadow(pos, price, roi_pct, "TP") if SHADOW_ENABLED else None
        return True, ghost

    # ── 4. TSL ────────────────────────────────────────────────────────
    if pos["_tsl_active"] and roi_pct <= TSL_LOCK:
        write_tick(pos, price, liq, bsr, roi_pct, age_sec, rug_flag,
                   pos["_tsl_active"], dynamic_sl)
        close_position(pos, price, "TSL", open_mints)
        ghost = spawn_shadow(pos, price, roi_pct, "TSL") if SHADOW_ENABLED else None
        return True, ghost

    # ── 5. SL (only reachable when TSL not active) ────────────────────
    if roi_pct <= SL_PCT:
        write_tick(pos, price, liq, bsr, roi_pct, age_sec, rug_flag,
                   pos["_tsl_active"], dynamic_sl)
        close_position(pos, price, "SL", open_mints)
        return True, None

    # ── 6. TIMEOUT ────────────────────────────────────────────────────
    if age_sec >= MAX_HOLD_SEC:
        write_tick(pos, price, liq, bsr, roi_pct, age_sec, rug_flag, pos["_tsl_active"], dynamic_sl)
        close_position(pos, price, "TIMEOUT", open_mints)
        track_timeout = get_setting("shadow_trailing", "track_timeout", False)
        ghost = spawn_shadow(pos, price, roi_pct, "TIMEOUT") if SHADOW_ENABLED and track_timeout else None
        return True, ghost

    # ── Normal tick — no exit condition fired ─────────────────────────
    write_tick(pos, price, liq, bsr, roi_pct, age_sec, rug_flag,
               pos["_tsl_active"], dynamic_sl)
    return False, None


# ─────────────────────────────────────────────────────────────
# SHADOW TRAILING — ghost position after TP/TSL close
# Tracks price for up to SHADOW_MAX_SEC to find the real ceiling.
# Output goes to shadow_ticks.jsonl — consumed by analysis scripts.
# ─────────────────────────────────────────────────────────────

def spawn_shadow(
    pos:         Dict[str, Any],
    exit_price:  float,
    exit_roi:    float,
    exit_reason: str,
) -> Dict[str, Any]:
    """Create a ghost tracking record from a just-closed position."""
    return {
        "id":              pos["id"],
        "mint":            pos["mint"],
        "pairAddress":     pos.get("pairAddress"),
        "dexId":           pos.get("dexId"),
        "entry_price_usd": pos["entry_price_usd"],
        "exit_reason":     exit_reason,
        "exit_roi_pct":    exit_roi,
        "exit_price_usd":  exit_price,
        "spawn_ts":        now_iso(),
        "_peak_roi":       exit_roi,
        "_last_poll":      time.time(),
    }


def tick_shadow(ghost: Dict[str, Any]) -> bool:
    """
    Single poll tick for a ghost position.
    Returns True if ghost should be retired.
    """
    mint      = ghost["mint"]
    pair      = ghost.get("pairAddress")
    dex       = ghost.get("dexId")
    spawn_ts  = datetime.fromisoformat(ghost["spawn_ts"])
    ghost_age = (datetime.now(timezone.utc) - spawn_ts).total_seconds()

    if ghost_age >= SHADOW_MAX_SEC:
        print(f"{_now_local()} [sim] ◌ SHADOW  {_fmt_mint(mint)}  retired  peak={ghost['_peak_roi']:+.2f}%")
        return True

    fetched = fetch_price(mint, pair, dex)
    price   = fetched.get("priceUsd")
    liq     = fetched.get("liq_usd")

    if price is None or (liq is not None and liq < RUG_LIQ_FLOOR):
        print(f"{_now_local()} [sim] ◌ SHADOW  {_fmt_mint(mint)}  retired → rug/no-price")
        return True

    entry_price = float(ghost["entry_price_usd"])
    ghost_roi   = (price / entry_price - 1.0) * 100.0
    is_peak     = ghost_roi > ghost["_peak_roi"]

    if is_peak:
        ghost["_peak_roi"] = ghost_roi

    try:
        tick = {
            "id":             ghost["id"],
            "mint":           mint,
            "exit_reason":    ghost["exit_reason"],
            "exit_roi_pct":   ghost["exit_roi_pct"],
            "exit_price_usd": ghost["exit_price_usd"],
            "ghost_age_sec":  round(ghost_age, 2),
            "ghost_roi_pct":  round(ghost_roi, 4),
            "peak_roi_pct":   round(ghost["_peak_roi"], 4),
            "price_usd":      price,
            "liq_usd":        liq,
            "is_peak":        is_peak,
            "ts":             now_iso(),
        }
        append_jsonl(SHADOW_TICKS_PATH, tick)
    except Exception as e:
        log_error("sim", f"shadow tick write failed: {e}")

    return False


# ─────────────────────────────────────────────────────────────
# STARTUP — reload positions open when simulator last stopped
# ─────────────────────────────────────────────────────────────

def reload_open_positions(open_mints: set) -> Dict[str, Dict[str, Any]]:
    """
    Rebuild in-memory position map from paper_trades.jsonl on startup.
    Any OPEN without a matching CLOSE is still active.
    TSL state re-derived from tick log to avoid state-loss on restart.
    """
    positions: Dict[str, Dict[str, Any]] = {}

    if not os.path.exists(PAPER_PATH):
        return positions

    with open(PAPER_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") == "OPEN":
                positions[obj["id"]] = {
                    **obj,
                    "_tsl_active":   False,
                    "_price_misses": 0,
                }
            elif obj.get("type") == "CLOSE":
                positions.pop(obj.get("id"), None)

    # Re-derive TSL state from tick history
    if positions and os.path.exists(TICKS_PATH):
        max_roi: Dict[str, float] = {pid: 0.0 for pid in positions}
        with open(TICKS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    tick = json.loads(line)
                except Exception:
                    continue
                pid = tick.get("id")
                if pid in max_roi:
                    max_roi[pid] = max(max_roi[pid], float(tick.get("roi_pct") or 0.0))

        for pid, pos in positions.items():
            if max_roi.get(pid, 0.0) >= TSL_ACTIVATE:
                pos["_tsl_active"] = True
                print(f"{_now_local()} [sim] ↕ TSL ARM  {_fmt_mint(pos['mint'])}  restored (peak >= {TSL_ACTIVATE}%)  (still open)")

    for pos in positions.values():
        open_mints.add(pos["mint"])
        print(f"{_now_local()} [sim] reloaded  {_fmt_mint(pos['mint'])}  id={pos['id'][:8]}")

    return positions


def reload_ghosts() -> Dict[str, Dict[str, Any]]:
    """
    Rebuild ghost tracking from shadow_ticks.jsonl and shadow_state.json on startup.
    
    Reconciliation pattern:
        1. Load shadow_state.json as a hint (not authority)
        2. For each ghost ID, check actual tick data from shadow_ticks.jsonl
        3. If last tick age > SHADOW_MAX_SEC → ghost expired, drop it
        4. If last tick age < SHADOW_MAX_SEC → ghost still alive, resume
        5. If no ticks exist at all → ghost never started, drop it
        6. Restore _peak_roi from actual tick data, not from state
    
    This ensures ghost state is always derived from truth data (shadow_ticks.jsonl)
    and never blindly trusted from shadow_state.json across restarts.
    """
    ghosts: Dict[str, Dict[str, Any]] = {}
    
    # Step 1: Load state hint if it exists
    state_hint = load_json(SHADOW_STATE_PATH, {})
    ghost_hints = state_hint.get("ghosts", {})
    
    # Step 2: If no shadow_ticks file exists, nothing to reconcile
    if not os.path.exists(SHADOW_TICKS_PATH):
        if ghost_hints:
            log_error("sim", f"shadow_ticks.jsonl missing but shadow_state has {len(ghost_hints)} ghost hint(s) — ignoring")
        return ghosts
    
    # Step 3: Build tick index from shadow_ticks.jsonl (most recent tick per ghost)
    last_tick_time: Dict[str, float] = {}
    peak_roi_from_ticks: Dict[str, float] = {}
    ghost_metadata: Dict[str, Dict[str, Any]] = {}
    
    with open(SHADOW_TICKS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                tick = json.loads(line)
            except Exception:
                continue
            
            gid = tick.get("id")
            if not gid:
                continue
            
            # Track most recent tick timestamp
            ts_str = tick.get("ts")
            if ts_str:
                ts_float = parse_iso(ts_str)
                if ts_float and (gid not in last_tick_time or ts_float > last_tick_time[gid]):
                    last_tick_time[gid] = ts_float
                    
                    # Store full metadata from most recent tick (or any tick really)
                    ghost_metadata[gid] = {
                        "mint": tick.get("mint"),
                        "pairAddress": tick.get("pairAddress"),
                        "dexId": tick.get("dexId"),
                        "entry_price_usd": tick.get("entry_price_usd"),
                        "exit_reason": tick.get("exit_reason"),
                        "exit_roi_pct": tick.get("exit_roi_pct"),
                        "exit_price_usd": tick.get("exit_price_usd"),
                    }
            
            # Track peak ROI seen in ANY tick (not just most recent)
            tick_peak = tick.get("peak_roi_pct")
            if tick_peak is not None:
                if gid not in peak_roi_from_ticks or tick_peak > peak_roi_from_ticks[gid]:
                    peak_roi_from_ticks[gid] = tick_peak
    
    # Step 4: Also check shadow_state.json for ghosts that haven't written ticks yet
    # (rare race condition on shutdown between tick write and ghost spawn)
    for gid, hint in ghost_hints.items():
        if gid not in last_tick_time:
            # Ghost exists in state but not in ticks → check age from spawn timestamp
            spawn_ts_str = hint.get("spawn_ts")
            if spawn_ts_str:
                spawn_ts = parse_iso(spawn_ts_str)
                if spawn_ts:
                    now_ts = time.time()
                    age = now_ts - spawn_ts
                    if age < SHADOW_MAX_SEC:
                        # Ghost still young enough, restore from hint
                        print(f"{_now_local()} [sim] ◌ SHADOW  restoring {gid[:8]}  from state hint")
                        ghosts[gid] = {
                            "id": gid,
                            "mint": hint.get("mint"),
                            "pairAddress": hint.get("pairAddress"),
                            "dexId": hint.get("dexId"),
                            "entry_price_usd": hint.get("entry_price_usd"),
                            "exit_reason": hint.get("exit_reason", "UNKNOWN"),
                            "exit_roi_pct": hint.get("exit_roi_pct", 0.0),
                            "exit_price_usd": hint.get("exit_price_usd", 0.0),
                            "spawn_ts": spawn_ts_str,
                            "_peak_roi": hint.get("_peak_roi", 0.0),
                            "_last_poll": spawn_ts,
                        }
                    else:
                        print(f"{_now_local()} [sim] ◌ SHADOW  dropping stale {gid[:8]}  age={age:.0f}s")
    
    # Step 5: Reconcile each ghost with actual tick data
    now_ts = time.time()
    for gid, last_ts in last_tick_time.items():
        ghost_age = now_ts - last_ts
        
        if ghost_age > SHADOW_MAX_SEC:
            # Ghost expired while pipeline was down
            print(f"{_now_local()} [sim] ◌ SHADOW  expired {gid[:8]}  age={ghost_age:.0f}s")
            continue
        
        # Ghost still alive — resume tracking
        metadata = ghost_metadata.get(gid, {})
        if not metadata:
            log_error("sim", f"ghost {gid[:8]} has ticks but no metadata — skipping")
            continue
        
        # Restore from tick data, NOT from state hint
        spawn_ts_str = None
        if gid in ghost_hints:
            spawn_ts_str = ghost_hints[gid].get("spawn_ts")
        
        # If we don't have spawn_ts from hint, estimate from first tick
        if not spawn_ts_str:
            # Find earliest tick for this ghost
            earliest_ts = None
            try:
                with open(SHADOW_TICKS_PATH, "r", encoding="utf-8") as f:
                    f.seek(0)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            tick = json.loads(line)
                        except Exception:
                            continue
                        if tick.get("id") == gid:
                            ts_str = tick.get("ts")
                            if ts_str:
                                ts_float = parse_iso(ts_str)
                                if earliest_ts is None or ts_float < earliest_ts:
                                    earliest_ts = ts_float
                                    if not spawn_ts_str:
                                        spawn_ts_str = ts_str
            except Exception:
                pass
        
        # Restore ghost with peak ROI derived from tick data
        peak_roi = peak_roi_from_ticks.get(gid, metadata.get("exit_roi_pct", 0.0))
        
        ghosts[gid] = {
            "id": gid,
            "mint": metadata.get("mint"),
            "pairAddress": metadata.get("pairAddress"),
            "dexId": metadata.get("dexId"),
            "entry_price_usd": metadata.get("entry_price_usd"),
            "exit_reason": metadata.get("exit_reason", "UNKNOWN"),
            "exit_roi_pct": metadata.get("exit_roi_pct", 0.0),
            "exit_price_usd": metadata.get("exit_price_usd", 0.0),
            "spawn_ts": spawn_ts_str or now_iso(),
            "_peak_roi": peak_roi,  # Derived from actual tick data, not state hint
            "_last_poll": last_ts,
        }
        
        print(f"{_now_local()} [sim] ◌ SHADOW  resumed {_fmt_mint(metadata.get('mint','?'))}  age={ghost_age:.0f}s  peak={peak_roi:+.2f}%")
    
    # Step 6: Update shadow_state.json with reconciled ghosts (clean persistent state)
    if ghosts:
        state_to_save = {
            "version": "1.0",
            "last_reconcile_ts": now_iso(),
            "ghosts": {
                gid: {
                    "id": ghost["id"],
                    "mint": ghost["mint"],
                    "spawn_ts": ghost["spawn_ts"],
                    "exit_reason": ghost["exit_reason"],
                    "_peak_roi": ghost["_peak_roi"],
                }
                for gid, ghost in ghosts.items()
            }
        }
        save_json(SHADOW_STATE_PATH, state_to_save)
    elif os.path.exists(SHADOW_STATE_PATH):
        # No active ghosts but stale state file exists — clean it up
        state_to_save = {"version": "1.0", "last_reconcile_ts": now_iso(), "ghosts": {}}
        save_json(SHADOW_STATE_PATH, state_to_save)
    
    print(f"{_now_local()} [sim] ◌ SHADOW  reconciled {len(ghosts)} active ghost(s)")
    return ghosts


def save_ghost_state(ghosts: Dict[str, Dict[str, Any]]) -> None:
    """Persist ghost state to shadow_state.json on every spawn/retirement."""
    state = {
        "version": "1.0",
        "last_update_ts": now_iso(),
        "ghosts": {
            gid: {
                "id": ghost["id"],
                "mint": ghost["mint"],
                "spawn_ts": ghost["spawn_ts"],
                "exit_reason": ghost["exit_reason"],
                "exit_roi_pct": ghost.get("exit_roi_pct", 0.0),
                "exit_price_usd": ghost.get("exit_price_usd", 0.0),
                "entry_price_usd": ghost.get("entry_price_usd"),
                "pairAddress": ghost.get("pairAddress"),
                "dexId": ghost.get("dexId"),
                "_peak_roi": ghost["_peak_roi"],
            }
            for gid, ghost in ghosts.items()
        }
    }
    save_json(SHADOW_STATE_PATH, state)

    
# ─────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────

def main() -> None:
    print(f"{_now_local()} [sim] starting — TP={TP_PCT}% SL={SL_PCT}% TSL={TSL_ACTIVATE}% MAX_HOLD={MAX_HOLD_SEC}s")
    print(f"{_now_local()} [sim] verdicts: {VERDICTS_PATH}")
    print(f"{_now_local()} [sim] output:   {PAPER_PATH}")

    os.makedirs(os.path.dirname(PAPER_PATH),     exist_ok=True)
    os.makedirs(os.path.dirname(TICKS_PATH),     exist_ok=True)
    os.makedirs(os.path.dirname(SIM_STATE_PATH), exist_ok=True)

    state       = load_json(SIM_STATE_PATH, {})
    open_mints: set                       = set()
    positions:  Dict[str, Dict[str, Any]] = reload_open_positions(open_mints)
    ghosts:     Dict[str, Dict[str, Any]] = reload_ghosts()
    last_poll        = 0.0
    last_shadow_poll = 0.0

    print(f"{_now_local()} [sim] {len(positions)} position(s) reloaded  {len(ghosts)} ghost(s) resumed")

    while True:
        # ── Read new BUY verdicts ─────────────────────────────────────
        buys, state = read_new_verdicts(state)
        for verdict in buys:
            if len(positions) >= MAX_ACTIVE:
                print(f"{_now_local()} [sim] ✗ SKIP    max positions ({MAX_ACTIVE}) reached — {_fmt_mint(verdict.get('mint','?'))}")
                continue
            pos = open_position(verdict, open_mints)
            if pos:
                positions[pos["id"]] = pos
                open_mints.add(pos["mint"])
        if buys:
            save_json(SIM_STATE_PATH, state)

        # ── Poll open positions ───────────────────────────────────────
        if positions and (time.time() - last_poll) >= POLL_SEC:
            last_poll = time.time()
            for pos_id in list(positions.keys()):
                pos           = positions[pos_id]
                closed, ghost = tick_position(pos, open_mints)
                if closed:
                    del positions[pos_id]
                    if ghost:
                        ghosts[ghost["id"]] = ghost
                        save_ghost_state(ghosts)
                        print(f"{_now_local()} [sim] ◌ SHADOW  {_fmt_mint(ghost['mint'])}  exit={ghost['exit_reason']}  roi={ghost['exit_roi_pct']:+.2f}%")

        # ── Poll ghost positions (shadow trailing) ────────────────────
        if ghosts and (time.time() - last_shadow_poll) >= SHADOW_POLL_SEC:
            last_shadow_poll = time.time()
            for gid in list(ghosts.keys()):
                retired = tick_shadow(ghosts[gid])
                if retired:
                    del ghosts[gid]
                    save_ghost_state(ghosts)

        time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    main()
