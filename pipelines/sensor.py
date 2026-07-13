#!/usr/bin/env python3
"""
sensor.py — NLT Alpha Sniper v0.7.0

RESPONSIBILITY: Raw market data collection and tick generation.
  - Tails entry_queue.jsonl for mints that passed safety checks
  - Fetches price, liquidity, volume, and BSR per mint via dex_api
  - Computes price and liq velocity (delta from last seen)
  - Writes structured ticks to live_ticks.jsonl for Brain

DOES NOT:
  - Make any trading decisions
  - Score or filter mints (that is Brain's job)
  - Track open positions (that is Simulator's job)

COMMUNICATION:
  Input:  entry_queue.jsonl  (safety.py → sensor.py)
  Output: live_ticks.jsonl   (sensor.py → brain.py)

All tuneable values live in config.json under "sensor".
Never hardcode constants here.
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from paths import get_config, get_path, get_setting
from dex_api import fetch_dex_data

# ─────────────────────────────────────────────────────────────
# CONFIG — all values from config.json
# ─────────────────────────────────────────────────────────────

_CFG             = get_config()

INBOX_PATH       = get_path("entry_queue")
OUTPUT_PATH      = get_path("live_ticks")
STATE_PATH       = get_path("sensor_state")

POLL_INTERVAL    = float(get_setting("settings", "poll_interval_sec", 2))
MIN_MOM_HARD     = float(get_setting("sensor",   "min_mom_hard",      3.0))
MAX_MOM_HARD     = float(get_setting("sensor",   "max_mom_hard",      20.0))
STATE_SAVE_EVERY = int(  get_setting("sensor",   "state_save_every",  10))


# ─────────────────────────────────────────────────────────────
# VELOCITY STATE
# Persisted across restarts so velocity doesn't jump on first tick.
# ─────────────────────────────────────────────────────────────

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


# ─────────────────────────────────────────────────────────────
# TICK FETCHER
# ─────────────────────────────────────────────────────────────

def fetch_tick_data(
    mint:  str,
    state: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Fetch current market data for a mint via dex_api.
    Returns structured tick dict or None if fetch fails.
    """
    try:
        dex = fetch_dex_data(mint)

        if dex.get("reason") != "ok" or dex.get("price_usd") is None:
            return None

        price = float(dex.get("price_usd")     or 0.0)
        liq   = float(dex.get("liquidity_usd") or 0.0)
        buys  = int(  dex.get("buys_5m")        or 0)
        sells = int(  dex.get("sells_5m")       or 0)
        bsr   = buys / max(1, sells)

        # Velocity — delta from last known state
        prev      = state.get(mint, {"price": price, "liq": liq})
        price_vel = price - float(prev.get("price") or price)
        liq_vel   = liq   - float(prev.get("liq")   or liq)

        # Update state for next tick
        state[mint] = {"price": price, "liq": liq}

        return {
            "ts":             datetime.now(timezone.utc).isoformat(),
            "mint":           mint,
            "pair":           dex.get("pair_address"),
            "dex":            dex.get("dex_id"),
            "priceUsd":       price,
            "liquidity_usd":  liq,
            "price_vel":      price_vel,
            "liq_vel":        liq_vel,
            "bsr":            round(bsr, 4),
            "buys_5m":        buys,
            "sells_5m":       sells,
            "volume_m5":      dex.get("vol_5m"),
            "lp_ratio":       dex.get("lp_ratio"),
            "priceChange_m5": dex.get("priceChange_m5"),
            "priceChange_h1": dex.get("priceChange_h1"),
            "flow_ratio":     None,   # future — not computed by dex_api
            "raw":            dex.get("raw"),
        }

    except Exception as e:
        print(f"[sensor] fetch failed for {mint[:12]}...: {e}")
        return None


# ─────────────────────────────────────────────────────────────
# TICK WRITER
# ─────────────────────────────────────────────────────────────

def write_tick(tick: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(tick, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────

def main() -> None:
    print("[sensor] Alpha Sniper Sensor starting...")
    print(f"[sensor] inbox:            {INBOX_PATH}")
    print(f"[sensor] output:           {OUTPUT_PATH}")
    print(f"[sensor] poll:             {POLL_INTERVAL}s")
    print(f"[sensor] mom gate:         [{MIN_MOM_HARD}, {MAX_MOM_HARD}]%")
    print(f"[sensor] state save every: {STATE_SAVE_EVERY} mints")

    if not os.path.exists(INBOX_PATH):
        print(f"[sensor] waiting for entry_queue: {INBOX_PATH}")
        while not os.path.exists(INBOX_PATH):
            time.sleep(5)
        print("[sensor] entry_queue found, starting...")

    state = load_state()
    print(f"[sensor] loaded velocity state for {len(state)} known mint(s)")

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    state_dirty        = False
    state_save_counter = 0

    with open(INBOX_PATH, "r") as f:
        f.seek(0, os.SEEK_END)
        print("[sensor] positioned at end of entry_queue, watching for new mints...")

        while True:
            line = f.readline()

            if not line:
                if state_dirty:
                    save_state(state)
                    state_dirty = False
                time.sleep(POLL_INTERVAL)
                continue

            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
            except Exception as e:
                print(f"[sensor] bad JSON in entry_queue: {e}")
                continue

            mint = data.get("mint")
            if not mint:
                print("[sensor] entry_queue line missing mint — skipping")
                continue

            print(f"[sensor] new mint: {mint[:12]}...")

            tick = fetch_tick_data(mint, state)
            if tick is None:
                print(f"[sensor] fetch returned nothing for {mint[:12]}... skipping")
                continue

            # ── Momentum hard gate ────────────────────────────────────────────
            # Re-checks with live data at fetch time — safety fetch may be stale
            mom = tick.get("priceChange_m5")
            if mom is None:
                print(f"[sensor] no m5 momentum for {mint[:12]}... skipping")
                continue
            mom = float(mom)
            if not (MIN_MOM_HARD <= mom <= MAX_MOM_HARD):
                print(
                    f"[sensor] GATE mom={mom:.2f}% out of range "
                    f"[{MIN_MOM_HARD},{MAX_MOM_HARD}] "
                    f"mint={mint[:12]}... skipping"
                )
                continue
            # ─────────────────────────────────────────────────────────────────

            write_tick(tick)
            state_dirty         = True
            state_save_counter += 1

            if state_save_counter >= STATE_SAVE_EVERY:
                save_state(state)
                state_dirty        = False
                state_save_counter = 0

            print(
                f"[sensor] tick written — "
                f"price={tick['priceUsd']:.8f} "
                f"liq={tick['liquidity_usd']:,.0f} "
                f"bsr={tick['bsr']:.2f} "
                f"vel={tick['price_vel']:+.8f}"
            )


if __name__ == "__main__":
    main()
