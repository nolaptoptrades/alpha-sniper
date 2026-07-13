"""
dex_api.py — NLT Alpha Sniper

Single DexScreener client for the full pipeline.
Replaces lp_api.py and volume_api.py — one HTTP call per mint, all data returned.

Returns everything safety.py, sensor.py, and simulator.py need:
  liquidity, lp_ratio, volume, buys/sells, momentum, price, age.

No API key required. DexScreener is public.
On any error, all fields return None — callers degrade gracefully.
"""

import json
import time
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────

DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
REQUEST_TIMEOUT_SEC   = 8
TARGET_CHAIN          = "solana"


# ─────────────────────────────────────────────────────────────
# NEUTRAL RESPONSE — returned on any failure
# ─────────────────────────────────────────────────────────────

def _neutral(reason: str = "unknown", raw: Any = None) -> Dict[str, Any]:
    return {
        # lp
        "liquidity_usd":    None,
        "lp_ratio":         None,
        "lp_ratio_basis":   None,
        "fdv_usd":          None,
        "market_cap_usd":   None,
        # volume
        "vol_5m":           None,
        "vol_1h":           None,
        "buys_5m":          None,
        "sells_5m":         None,
        # momentum
        "priceChange_m5":   None,
        "priceChange_h1":   None,
        # price
        "price_usd":        None,
        # age
        "pair_created_at":  None,
        # meta
        "pair_address":     None,
        "dex_id":           None,
        "reason":           reason,
        "raw":              raw,
    }


# ─────────────────────────────────────────────────────────────
# HTTP HELPER
# ─────────────────────────────────────────────────────────────

def _http_get(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "accept":          "application/json",
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://dexscreener.com/",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"http_{e.code}") from e
    except Exception as e:
        raise RuntimeError(str(e)) from e


# ─────────────────────────────────────────────────────────────
# BEST PAIR SELECTOR
# Picks the highest-liquidity pair on the target chain.
# ─────────────────────────────────────────────────────────────

def _pick_best_pair(pairs: list) -> Optional[Dict[str, Any]]:
    best     = None
    best_liq = -1.0
    for p in pairs or []:
        if (p.get("chainId") or "").lower() != TARGET_CHAIN:
            continue
        try:
            liq = float((p.get("liquidity") or {}).get("usd") or 0.0)
        except Exception:
            liq = 0.0
        if liq > best_liq:
            best_liq = liq
            best     = p
    return best


# ─────────────────────────────────────────────────────────────
# SAFE CAST HELPERS
# ─────────────────────────────────────────────────────────────

def _f(val: Any) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except Exception:
        return None


def _i(val: Any) -> Optional[int]:
    try:
        return int(val) if val is not None else None
    except Exception:
        return None


def _pair_created_at(pair: Dict[str, Any]) -> Optional[float]:
    """Normalize pairCreatedAt to epoch seconds."""
    raw = pair.get("pairCreatedAt")
    if raw is None:
        return None
    try:
        ms = float(raw)
        # DexScreener returns ms epoch
        return ms / 1000.0 if ms > 1_000_000_000_000 else ms
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# MAIN FETCH
# ─────────────────────────────────────────────────────────────

def fetch_dex_data(mint: str) -> Dict[str, Any]:
    """
    Fetch all DexScreener data for a mint in one HTTP call.

    Returns a flat dict with all fields safety.py, sensor.py,
    and simulator.py need. All fields are None on failure.

    Fields:
        liquidity_usd    float   pool liquidity in USD
        lp_ratio         float   liquidity / market_cap (or fdv)
        lp_ratio_basis   str     "marketCap" | "fdv" | "fdv_over_marketCap_garbage"
        fdv_usd          float   fully diluted valuation
        market_cap_usd   float   circulating market cap
        vol_5m           float   5m volume USD
        vol_1h           float   1h volume USD
        buys_5m          int     buy transaction count (NOT unique wallets)
        sells_5m         int     sell transaction count
        priceChange_m5   float   5m price change %
        priceChange_h1   float   1h price change %
        price_usd        float   current price USD
        pair_created_at  float   epoch seconds (None if missing)
        pair_address     str     DexScreener pair address
        dex_id           str     dex identifier (e.g. "pumpswap")
        reason           str     "ok" or error description
        raw              dict    full pair object for debugging
    """
    url = DEXSCREENER_TOKEN_URL.format(mint=mint)

    try:
        data = _http_get(url)
    except RuntimeError as e:
        return _neutral(reason=f"http_error:{e}")
    except Exception as e:
        return _neutral(reason=f"exception:{e}")

    pairs = data.get("pairs") or []
    if not pairs:
        return _neutral(reason="no_pairs", raw=data)

    pair = _pick_best_pair(pairs)
    if not pair:
        return _neutral(reason="no_pairs_on_chain", raw=data)

    # ── LP / market cap ──────────────────────────────────────
    liq      = pair.get("liquidity") or {}
    liq_usd  = _f(liq.get("usd"))
    fdv_usd  = _f(pair.get("fdv"))
    mcap_usd = _f(pair.get("marketCap"))

    # lp_ratio denominator: prefer marketCap, fall back to fdv
    ratio_denom = mcap_usd
    ratio_basis = "marketCap"
    if ratio_denom in (None, 0.0):
        ratio_denom = fdv_usd
        ratio_basis = "fdv"
    elif fdv_usd not in (None, 0.0) and ratio_denom > (fdv_usd * 10.0):
        ratio_denom = fdv_usd
        ratio_basis = "fdv_over_marketCap_garbage"

    lp_ratio = None
    if liq_usd is not None and ratio_denom not in (None, 0.0):
        lp_ratio = liq_usd / ratio_denom

    # ── Volume + txns ────────────────────────────────────────
    volume = pair.get("volume") or {}
    txns   = pair.get("txns") or {}
    m5     = txns.get("m5") or {}

    vol_5m   = _f(volume.get("m5"))
    vol_1h   = _f(volume.get("h1"))
    buys_5m  = _i(m5.get("buys"))
    sells_5m = _i(m5.get("sells"))

    # ── Momentum ─────────────────────────────────────────────
    pc             = pair.get("priceChange") or {}
    price_change_m5 = _f(pc.get("m5"))
    price_change_h1 = _f(pc.get("h1"))

    # ── Price ────────────────────────────────────────────────
    price_usd = _f(pair.get("priceUsd"))

    # ── Age ──────────────────────────────────────────────────
    created_at = _pair_created_at(pair)

    return {
        "liquidity_usd":   liq_usd,
        "lp_ratio":        lp_ratio,
        "lp_ratio_basis":  ratio_basis,
        "fdv_usd":         fdv_usd,
        "market_cap_usd":  mcap_usd,
        "vol_5m":          vol_5m,
        "vol_1h":          vol_1h,
        "buys_5m":         buys_5m,
        "sells_5m":        sells_5m,
        "priceChange_m5":  price_change_m5,
        "priceChange_h1":  price_change_h1,
        "price_usd":       price_usd,
        "pair_created_at": created_at,
        "pair_address":    pair.get("pairAddress"),
        "dex_id":          pair.get("dexId"),
        "reason":          "ok",
        "raw":             pair,
    }


# ─────────────────────────────────────────────────────────────
# SELF-TEST
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    mint = sys.argv[1] if len(sys.argv) > 1 else ""
    if not mint:
        print("Usage: python dex_api.py <mint_address>")
        sys.exit(1)

    print(f"[dex_api] fetching {mint}...")
    result = fetch_dex_data(mint)
    print(f"  reason:         {result['reason']}")
    print(f"  liquidity_usd:  {result['liquidity_usd']}")
    print(f"  lp_ratio:       {result['lp_ratio']} ({result['lp_ratio_basis']})")
    print(f"  vol_5m:         {result['vol_5m']}")
    print(f"  buys_5m:        {result['buys_5m']}")
    print(f"  sells_5m:       {result['sells_5m']}")
    print(f"  priceChange_m5: {result['priceChange_m5']}")
    print(f"  price_usd:      {result['price_usd']}")
    print(f"  pair_created_at:{result['pair_created_at']}")
    print(f"  dex_id:         {result['dex_id']}")
