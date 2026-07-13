"""
whale_api.py — NLT Alpha Sniper

Top-10 holder concentration check. AMM-filtered via Helius RPC.
Used synchronously by safety.py as a warn/block gate.

Upgraded from legacy supply-only check — now filters known AMM pool
addresses (PumpSwap, Raydium, Meteora, LaunchLab, etc.) from the
holder list before computing concentration. Same interface as before,
so safety.py needs no changes.

CREDENTIAL
──────────
Reads HELIUS_API_KEY from environment (set in .env at base_dir).
Falls back to public Solana RPC if key is missing or call fails.
Key never leaves the local device.
"""

import json
import os
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────

# RPC endpoints — Helius preferred, public as fallback
HELIUS_RPC_URL = "https://mainnet.helius-rpc.com/?api-key={api_key}"
PUBLIC_RPC_URL = "https://api.mainnet-beta.solana.com"

REQUEST_TIMEOUT_SEC = 10

# Banding thresholds (warn-only, not hard blocks at this layer)
# Hard block lives in brain.py via config scoring.whale_absolute_ceiling_pct
BAND_OK_PCT   = 30.0   # <= 30% → ok
BAND_WARN_PCT = 60.0   # 30-60% → warn
# >= 60% → block (logged, passed to brain.py for hard enforcement)

# Known AMM program addresses — filtered from holder concentration
# These are pool accounts, not real holders. Same set as brain_wt.py.
KNOWN_AMM = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",  # LaunchLab
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "boop8hVGQGqehUK2iVEMEnMrL5RbjywRzHKBmBE7ry4",  # Boop.fun
}


# ─────────────────────────────────────────────────────────────
# CREDENTIAL LOADER
# ─────────────────────────────────────────────────────────────

def _get_rpc_url() -> str:
    """
    Return the best available RPC URL.
    Tries HELIUS_API_KEY from environment first.
    Falls back to public RPC if missing.
    """
    api_key = os.getenv("HELIUS_API_KEY", "").strip()
    if api_key:
        return HELIUS_RPC_URL.format(api_key=api_key)
    return PUBLIC_RPC_URL


# ─────────────────────────────────────────────────────────────
# RPC HELPER
# ─────────────────────────────────────────────────────────────

def _rpc_post(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "accept": "application/json"},
        method="POST",
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
# HELIUS-SPECIFIC RPC CALLS (when API key is available)
# ─────────────────────────────────────────────────────────────

def _helius_get_token_accounts(mint: str, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Fetch top holder accounts via Helius getTokenAccounts.
    Returns list of {owner, amount} dicts, sorted by amount descending.
    """
    api_key = os.getenv("HELIUS_API_KEY", "").strip()
    if not api_key:
        return []

    url = HELIUS_RPC_URL.format(api_key=api_key)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccounts",
        "params": {"mint": mint, "limit": limit},
    }
    try:
        result = _rpc_post(url, payload)
        accounts = result.get("token_accounts", [])
        # Sort by amount descending (fix ported from brain_wt)
        accounts.sort(key=lambda x: x.get("amount", 0), reverse=True)
        return accounts
    except Exception:
        return []


def _helius_get_token_supply(mint: str) -> Optional[Dict[str, Any]]:
    """
    Fetch total token supply via Helius getTokenSupply.
    Returns {total_raw, decimals, ui_amount} or None.
    """
    api_key = os.getenv("HELIUS_API_KEY", "").strip()
    if not api_key:
        return None

    url = HELIUS_RPC_URL.format(api_key=api_key)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenSupply",
        "params": [mint],
    }
    try:
        result = _rpc_post(url, payload)
        val = result.get("value", {})
        return {
            "total_raw": int(val.get("amount", 0)),
            "decimals": val.get("decimals", 0),
            "ui_amount": val.get("uiAmount", 0),
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# PUBLIC RPC FALLBACK (when Helius key is missing)
# ─────────────────────────────────────────────────────────────

def _get_mint_info(mint: str, rpc_url: str) -> Dict[str, Any]:
    """Fetch mint supply + decimals via getAccountInfo (public RPC)."""
    payload = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "getAccountInfo",
        "params":  [mint, {"encoding": "jsonParsed"}],
    }
    resp  = _rpc_post(rpc_url, payload)
    value = (resp.get("result") or {}).get("value") or {}
    data  = (value.get("data") or {})
    info  = (data.get("parsed") or {}).get("info") or {}

    supply_str = (info.get("supply") or
                  (info.get("mintAuthority") and None) or
                  info.get("supply"))
    decimals   = info.get("decimals")

    supply = None
    try:
        if supply_str is not None:
            supply = int(supply_str)
    except Exception:
        pass

    return {"supply": supply, "decimals": decimals, "ok": supply is not None}


def _get_largest_accounts(mint: str, rpc_url: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Fetch top holder accounts via getTokenLargestAccounts (public RPC)."""
    payload = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "getTokenLargestAccounts",
        "params":  [mint],
    }
    resp  = _rpc_post(rpc_url, payload)
    value = (resp.get("result") or {}).get("value") or []
    return value[:limit]


# ─────────────────────────────────────────────────────────────
# NEUTRAL RESPONSE
# ─────────────────────────────────────────────────────────────

def _neutral(reason: str, raw: Any = None) -> Dict[str, Any]:
    return {
        "ok":                   None,
        "band":                 None,
        "top10_percent":        None,
        "top1_percent":         None,
        "amm_percent":          None,
        "confidence":           "low",
        "reason":               reason,
        "source":               "rpc_supply_based",
        "raw":                  raw,
    }


# ─────────────────────────────────────────────────────────────
# AMM-FILTERED CONCENTRATION LOGIC (shared with brain_wt)
# ─────────────────────────────────────────────────────────────

def _compute_filtered_concentration(
    holders_raw: List[Dict[str, Any]],
    supply_total: int,
    topn: int = 10,
) -> Dict[str, Any]:
    """
    Given a sorted holder list and total supply, filter out AMM pools
    and compute top-N concentration of real holders.

    Returns dict with:
        topn_sum_raw, topn_pct, top1_pct, real_holder_count, amm_holder_count
    """
    real_amounts = []
    amm_amounts = []

    for entry in holders_raw:
        owner = entry.get("owner", "")
        amount = entry.get("amount", 0)
        if not owner or amount <= 0:
            continue
        if owner in KNOWN_AMM:
            amm_amounts.append(amount)
        else:
            real_amounts.append(amount)

    n = min(topn, len(real_amounts))
    if n == 0 or supply_total <= 0:
        return {
            "topn_sum_raw": 0,
            "topn_pct": 0.0,
            "top1_pct": 0.0,
            "real_holder_count": len(real_amounts),
            "amm_holder_count": len(amm_amounts),
        }

    topn_sum = sum(real_amounts[:n])
    top1_pct = (real_amounts[0] / supply_total) * 100.0 if real_amounts else 0.0
    topn_pct = (topn_sum / supply_total) * 100.0

    return {
        "topn_sum_raw": topn_sum,
        "topn_pct": topn_pct,
        "top1_pct": top1_pct,
        "real_holder_count": len(real_amounts),
        "amm_holder_count": len(amm_amounts),
    }


# ─────────────────────────────────────────────────────────────
# MAIN CHECK — upgraded with AMM filtering
# ─────────────────────────────────────────────────────────────

def check_whale_concentration(
    mint:  str,
    topn:  int = 10,
    limit: int = 20,
) -> Dict[str, Any]:
    """
    AMM-filtered top-N holder concentration check.

    Tries Helius getTokenAccounts first (AMM-aware). Falls back
    to public RPC getTokenLargestAccounts if Helius is unavailable.

    Returns:
        ok               bool|None   True=pass, False=block, None=data unavailable
        band             str|None    "ok" | "warn" | "block" | None
        top10_percent    float|None  top-N % of supply (real holders only)
        top1_percent     float|None  single largest real holder %
        amm_percent      None        not computed (future)
        confidence       str         "high" if Helius, "low" if public RPC
        reason           str
        source           str
        raw              dict        includes AMM filter stats
    """
    rpc_url    = _get_rpc_url()
    confidence = "high" if "helius" in rpc_url else "low"

    # ── Try Helius path first (supports AMM filtering) ──────
    if "helius" in rpc_url:
        accounts = _helius_get_token_accounts(mint, limit=limit)
        if accounts:
            supply = _helius_get_token_supply(mint)
            if supply and supply["total_raw"] > 0:
                total = supply["total_raw"]
            else:
                # Fallback to sum of fetched accounts
                total = sum(a.get("amount", 0) for a in accounts)

            if total > 0:
                conc = _compute_filtered_concentration(accounts, total, topn=topn)
                topn_pct = conc["topn_pct"]
                top1_pct = conc["top1_pct"]

                if topn_pct <= BAND_OK_PCT:
                    band, ok_val, reason = "ok", True, f"top{topn}={topn_pct:.2f}% (ok, AMM-filtered)"
                elif topn_pct < BAND_WARN_PCT:
                    band, ok_val, reason = "warn", True, f"top{topn}={topn_pct:.2f}% (warn, AMM-filtered)"
                else:
                    band, ok_val, reason = "block", False, f"top{topn}={topn_pct:.2f}% (block, AMM-filtered)"

                return {
                    "ok":             ok_val,
                    "band":           band,
                    "top10_percent":  topn_pct,
                    "top1_percent":   top1_pct,
                    "amm_percent":    None,
                    "confidence":     confidence,
                    "reason":         reason,
                    "source":         "helius_amm_filtered",
                    "raw": {
                        "supply_raw":      total,
                        "real_holders":    conc["real_holder_count"],
                        "amm_holders":     conc["amm_holder_count"],
                        "topn_sum_raw":    conc["topn_sum_raw"],
                        "holders_fetched": len(accounts),
                        "rpc":             "helius",
                    },
                }

    # ── Public RPC fallback (no AMM filtering available) ────
    try:
        mint_info = _get_mint_info(mint, rpc_url)
    except Exception as e:
        return _neutral(reason=f"mint_info_failed:{type(e).__name__}:{e}")

    decimals   = mint_info.get("decimals")
    supply_raw = mint_info.get("supply")
    supply_ui  = None

    try:
        if supply_raw is not None and decimals is not None:
            supply_ui = float(supply_raw) / (10 ** int(decimals))
    except Exception:
        pass

    try:
        holders = _get_largest_accounts(mint, rpc_url, limit=limit)
    except Exception as e:
        return _neutral(
            reason=f"largest_accounts_failed:{type(e).__name__}:{e}",
            raw={"supply_ui": supply_ui, "decimals": decimals},
        )

    if not holders or supply_ui is None or supply_ui <= 0:
        return _neutral(
            reason="top10=unknown (missing supply or holders)",
            raw={"supply_ui": supply_ui, "decimals": decimals, "holders_len": len(holders)},
        )

    amounts = []
    for entry in holders:
        try:
            a = float(entry.get("uiAmount") or entry.get("amount") or 0.0)
            if a > 0:
                amounts.append(a)
        except Exception:
            continue

    if not amounts:
        return _neutral(
            reason="no_positive_amounts",
            raw={"supply_ui": supply_ui, "holders_len": len(holders)},
        )

    amounts_sorted = sorted(amounts, reverse=True)
    n = max(1, min(int(topn), len(amounts_sorted)))

    top1_ui  = amounts_sorted[0]
    topn_sum = sum(amounts_sorted[:n])
    top1_pct = (top1_ui / supply_ui) * 100.0
    topn_pct = (topn_sum / supply_ui) * 100.0

    if topn_pct <= BAND_OK_PCT:
        band, ok_val, reason = "ok", True, f"top{n}={topn_pct:.2f}% (ok, public RPC — no AMM filter)"
    elif topn_pct < BAND_WARN_PCT:
        band, ok_val, reason = "warn", True, f"top{n}={topn_pct:.2f}% (warn, public RPC — no AMM filter)"
    else:
        band, ok_val, reason = "block", False, f"top{n}={topn_pct:.2f}% (block, public RPC — no AMM filter)"

    return {
        "ok":             ok_val,
        "band":           band,
        "top10_percent":  topn_pct,
        "top1_percent":   top1_pct,
        "amm_percent":    None,
        "confidence":     confidence,
        "reason":         reason,
        "source":         "public_rpc_no_filter",
        "raw": {
            "supply_ui":   supply_ui,
            "decimals":    decimals,
            "holders_len": len(holders),
            "topn_sum_ui": topn_sum,
            "top1_ui":     top1_ui,
            "rpc":         "public",
        },
    }


# ─────────────────────────────────────────────────────────────
# SELF-TEST
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    mint = sys.argv[1] if len(sys.argv) > 1 else ""
    if not mint:
        print("Usage: python whale_api.py <mint_address>")
        sys.exit(1)
    result = check_whale_concentration(mint)
    print(f"  ok:            {result['ok']}")
    print(f"  band:          {result['band']}")
    print(f"  top10_percent: {result['top10_percent']}")
    print(f"  top1_percent:  {result['top1_percent']}")
    print(f"  confidence:    {result['confidence']}")
    print(f"  reason:        {result['reason']}")
    print(f"  source:        {result['source']}")
    if result.get("raw"):
        print(f"  raw:           {json.dumps(result['raw'], indent=2)}")
