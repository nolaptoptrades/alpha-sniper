"""
sources/dexscreener.py — Alpha Sniper V2

DexScreener discovery source.
Available to: free + paid tiers.
Auth: none required.

Polls PumpSwap pairs from DexScreener public API, sorted by newest first.
Age-filtered to surface only fresh graduations.
Lower signal latency than Helius (~30-60s behind graduation),
but requires no API key and never fails on auth errors.

Exposes:
    validate_config(cfg) -> (ok: bool, reason: str)
    fetch(cfg)           -> List[Dict]
"""

import platform
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple
from paths import log_error

# ============================================================
# CONSTANTS
# All overridable from config.json "discovery.sources.dexscreener"
# ============================================================

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/search?q=pumpswap&sort=created&order=desc"
MIN_LIQ_USD     = 10_000.0
MIN_VOL_5M      = 500.0
MAX_AGE_MIN     = 180
REQUEST_TIMEOUT = 10

STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "So11111111111111111111111111111111111111112",     # wSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # JitoSOL
}


# ============================================================
# HELPERS
# ============================================================

def _now_local() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _build_headers() -> Dict[str, str]:
    """
    Build User-Agent from the user's actual system to blend in
    with normal traffic. DexScreener sometimes 403s on generic
    python-requests UA strings under heavy load.
    """
    system = platform.system()
    arch   = platform.machine()
    py_ver = platform.python_version()
    return {
        "User-Agent": f"AlphaSniper/0.9 ({system}; {arch}) Python/{py_ver}"
    }


# ============================================================
# VALIDATE
# ============================================================

def validate_config(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """DexScreener needs no API key — always valid."""
    return True, "ok"


# ============================================================
# FETCH
# ============================================================

def fetch(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Fetch recent PumpSwap graduation pairs from DexScreener,
    sorted by creation time (newest first).

    Filters:
      - dexId must be pumpswap
      - liquidity >= min_liq_usd
      - volume_5m >= min_vol_5m
      - age <= max_age_min

    Returns normalized rows:
        source, mint, symbol, dex, usd, block_time, age_sec, sig
    """
    import requests

    min_liq     = float(cfg.get("min_liq_usd", MIN_LIQ_USD))
    min_vol     = float(cfg.get("min_vol_5m",  MIN_VOL_5M))
    max_age_min = float(cfg.get("max_age_min", MAX_AGE_MIN))
    max_age_sec = max_age_min * 60.0

    rows: List[Dict[str, Any]] = []
    now_ms = time.time() * 1000.0

    try:
        r = requests.get(DEXSCREENER_URL, headers=_build_headers(), timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data  = r.json()
        pairs = data.get("pairs") or []
    except Exception as e:
        print(f"[dexscreener] {_now_local()}  fetch error — {e}")
        return rows

    for p in pairs:
        try:
            if p.get("dexId") != "pumpswap":
                continue

            liq   = float((p.get("liquidity") or {}).get("usd") or 0)
            vol5m = float((p.get("volume")    or {}).get("m5")  or 0)

            if liq   < min_liq: continue
            if vol5m < min_vol: continue

            base = p.get("baseToken") or {}
            mint = base.get("address")
            if not mint or mint in STABLE_MINTS:
                continue

            pair_created = p.get("pairCreatedAt")   # epoch ms
            age_sec      = None
            if pair_created:
                age_sec = (now_ms - pair_created) / 1000.0
                if age_sec > max_age_sec:
                    continue
                if age_sec < 0:
                    age_sec = 0

            rows.append({
                "source":     "dexscreener",
                "launchpad":  "pumpswap",
                "mint":       mint,
                "symbol":     base.get("symbol"),
                "dex":        "pumpswap",
                "usd":        vol5m,
                "block_time": int(pair_created / 1000.0) if pair_created else None,
                "age_sec":    round(age_sec) if age_sec is not None else None,
                "sig":        None,
            })

        except Exception:
            continue

    return rows
