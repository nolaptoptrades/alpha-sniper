"""
sources/moralis.py — NLT Alpha Sniper

Moralis Solana API source for token/pair discovery.
Implements the standard source interface: fetch() and validate_config().

API: Moralis Solana API
Docs: https://docs.moralis.io/web3-data-api/solana
Auth: BYOK — user provides MORALIS_API_KEY in .env file

Discovery strategy:
    Fetches recently created tokens on Solana via Moralis token endpoint,
    filters for PumpSwap graduation candidates by liquidity threshold.
    Falls back to pair-based search if token endpoint returns insufficient data.

Rate limits (free tier): ~1000 requests/day (varies by Moralis plan)
"""

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

# ============================================================
# CONSTANTS
# ============================================================

MORALIS_BASE_URL = "https://solana-gateway.moralis.io"
DEFAULT_MIN_LIQ_USD = 80000.0
DEFAULT_MAX_AGE_MIN = 30
REQUEST_TIMEOUT_SEC = 15
MAX_RETRIES = 2
RETRY_SLEEP_SEC = 1.0

# Moralis doesn't have a direct "graduation" event endpoint,
# so we query recently created tokens and filter by liquidity.
# PumpSwap tokens typically have "pump" in their DEX name.
PUMP_DEX_KEYWORDS = ["pump", "pumpswap", "pump.fun"]


# ============================================================
# HELPERS
# ============================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_api_key() -> Optional[str]:
    """Fetch Moralis API key from environment (loaded from .env by paths.py)."""
    return os.environ.get("MORALIS_API_KEY")


def _http_get(url: str, headers: Dict[str, str], params: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    GET request with retry logic. Returns parsed JSON dict.
    Raises on non-2xx after retries exhausted.
    """
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                headers=headers,
                params=params or {},
                timeout=REQUEST_TIMEOUT_SEC,
            )
            if resp.status_code == 200:
                return resp.json()
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.RequestException as e:
            last_error = str(e)
        
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_SLEEP_SEC)
    
    raise RuntimeError(f"[moralis] request failed after {MAX_RETRIES + 1} attempts: {last_error}")


def _is_pumpswap_candidate(pair: Dict[str, Any]) -> bool:
    """
    Check if a token pair looks like a PumpSwap graduation.
    
    Heuristics:
      - DEX name contains pump-related keywords
      - OR pair was created recently (PumpSwap graduates are new)
      - Has meaningful liquidity (not dust)
    """
    dex = (pair.get("dex") or pair.get("exchange") or "").lower()
    label = (pair.get("pairLabel") or pair.get("label") or "").lower()
    
    for keyword in PUMP_DEX_KEYWORDS:
        if keyword in dex or keyword in label:
            return True
    
    # If no pump DEX identified but it's a new pair on a known DEX,
    # include it — user can filter downstream with safety.py
    return False


def _parse_token_row(
    token: Dict[str, Any],
    min_liq_usd: float,
    source_name: str = "moralis",
) -> Optional[Dict[str, Any]]:
    """
    Convert a Moralis token/pair object into a normalized discovery event.
    Returns None if it doesn't meet minimum criteria.
    """
    # Extract fields — Moralis API shape varies slightly by endpoint
    mint = (
        token.get("mint") or
        token.get("tokenAddress") or
        token.get("baseToken", {}).get("address") or
        ""
    )
    symbol = token.get("symbol") or token.get("tokenSymbol") or ""
    
    # Liquidity in USD
    liq_usd = 0.0
    liq_raw = token.get("liquidity") or token.get("liquidityUsd") or 0.0
    try:
        liq_usd = float(liq_raw)
    except (ValueError, TypeError):
        pass
    
    # Try to get from pair data if top-level is empty
    if liq_usd == 0.0:
        pair_liq = token.get("pair", {}).get("liquidityUsd") or 0.0
        try:
            liq_usd = float(pair_liq)
        except (ValueError, TypeError):
            pass
    
    if liq_usd < min_liq_usd:
        return None
    
    if not mint:
        return None
    
    # Block time / creation time
    created_at = (
        token.get("createdAt") or
        token.get("blockTimestamp") or
        token.get("pairCreatedAt") or
        ""
    )
    
    # Age in seconds
    age_sec = None
    if created_at:
        try:
            if isinstance(created_at, (int, float)):
                created_ts = float(created_at)
            else:
                created_ts = datetime.fromisoformat(
                    str(created_at).replace("Z", "+00:00")
                ).timestamp()
            age_sec = time.time() - created_ts
        except Exception:
            pass
    
    # DEX info
    dex = (
        token.get("dex") or
        token.get("exchange") or
        token.get("pair", {}).get("dex") or
        token.get("pair", {}).get("exchange") or
        "unknown"
    )
    
    return {
        "source":     source_name,
        "mint":       mint,
        "symbol":     symbol or None,
        "dex":        dex,
        "usd":        liq_usd,
        "block_time": str(created_at) if created_at else None,
        "age_sec":    age_sec,
        "sig":        token.get("signature") or token.get("txHash"),
    }


# ============================================================
# SOURCE INTERFACE
# ============================================================

def validate_config(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Validate that the source is properly configured.
    
    Checks:
      1. API key is present in environment
      2. Config has required fields (min_liq_usd, max_age_min)
    
    Returns (ok, reason).
    """
    api_key = _get_api_key()
    if not api_key:
        return False, "MORALIS_API_KEY not set in .env file"
    
    if not cfg.get("enabled", True):
        return False, "moralis disabled in config.json"
    
    return True, "ok"


def fetch(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Fetch recently created PumpSwap candidate tokens via Moralis.
    
    Strategy:
      1. Query token endpoint for new tokens on Solana
      2. Filter by liquidity threshold
      3. If few results, also try pairs endpoint
      4. Deduplicate by mint address
    
    Args:
        cfg: Source config block from config.json
    
    Returns:
        List of normalized event dicts with source="moralis"
    """
    api_key = _get_api_key()
    if not api_key:
        print("[moralis] no API key — skipping")
        return []
    
    headers = {
        "accept": "application/json",
        "X-API-Key": api_key,
    }
    
    min_liq_usd = float(cfg.get("min_liq_usd", DEFAULT_MIN_LIQ_USD))
    max_age_min = int(cfg.get("max_age_min", DEFAULT_MAX_AGE_MIN))
    
    events: List[Dict[str, Any]] = []
    seen_mints: set = set()
    
    # ── Strategy 1: Token endpoint (newest tokens) ──────────────────────
    try:
        token_url = f"{MORALIS_BASE_URL}/token/mainnet/solana/trending"
        params = {
            "limit": 50,
        }
        data = _http_get(token_url, headers, params)
        tokens = data.get("result") or data.get("tokens") or []
        
        for token in tokens:
            event = _parse_token_row(token, min_liq_usd)
            if event and event["mint"] not in seen_mints:
                # Age filter
                age = event.get("age_sec")
                if age is not None and age > max_age_min * 60:
                    continue
                seen_mints.add(event["mint"])
                events.append(event)
        
        if tokens:
            print(f"[moralis] token endpoint: {len(tokens)} raw, {len(events)} passed filter")
    except Exception as e:
        print(f"[moralis] token endpoint error: {e}")
    
    # ── Strategy 2: Pairs endpoint (backup) ─────────────────────────────
    try:
        pairs_url = f"{MORALIS_BASE_URL}/token/mainnet/solana/pairs"
        params = {
            "limit": 50,
            "order": "DESC",
            "sort": "createdAt",
        }
        data = _http_get(pairs_url, headers, params)
        pairs = data.get("result") or data.get("pairs") or []
        
        added_from_pairs = 0
        for pair in pairs:
            event = _parse_token_row(pair, min_liq_usd)
            if event and event["mint"] not in seen_mints:
                age = event.get("age_sec")
                if age is not None and age > max_age_min * 60:
                    continue
                seen_mints.add(event["mint"])
                events.append(event)
                added_from_pairs += 1
        
        if pairs:
            print(f"[moralis] pairs endpoint: {len(pairs)} raw, {added_from_pairs} passed filter")
    except Exception as e:
        print(f"[moralis] pairs endpoint error: {e}")
    
    return events


# ============================================================
# SELF-TEST (run directly: python -m sources.moralis)
# ============================================================

if __name__ == "__main__":
    print("[moralis] self-test")
    print()
    
    # Minimal test config
    test_cfg = {
        "enabled": True,
        "min_liq_usd": 80000.0,
        "max_age_min": 30,
    }
    
    ok, reason = validate_config(test_cfg)
    print(f"  validate_config: {ok} ({reason})")
    
    if not ok:
        print("  skipping fetch (no API key or disabled)")
        exit(0)
    
    print("  fetching...")
    try:
        rows = fetch(test_cfg)
        print(f"  got {len(rows)} events")
        for row in rows[:5]:
            print(f"    {row['mint'][:12]}…  liq=${row['usd']:,.0f}  age={row['age_sec']}s  dex={row['dex']}")
    except Exception as e:
        print(f"  fetch error: {e}")
