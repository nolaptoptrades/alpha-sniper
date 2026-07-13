#!/usr/bin/env python3
"""
brain_w.py — Alpha Sniper (Wallet Brain — Helius-powered wallet analyzer)

Pre-entry wallet concentration check + data collector.
Replaces whale_api.py with AMM-filtered holder analysis.

DUAL ENVIRONMENT:
  WSL:  Called synchronously by brain.py at BUY verdict time.
        Also callable from safety.py as upgraded whale_api replacement.
        Uses paths.py for file resolution.
  
  Termux: Standalone background process tailing live_ticks.jsonl.
          Uses hardcoded paths. Falls back gracefully if paths.py unavailable.

ENTRY POINTS:
  capture_wallet_snapshot(mint)  — full wallet data for a mint (brain.py hook)
  check_concentration(mint)      — lightweight AMM-filtered top-10 check (safety.py hook)
  main()                         — standalone loop tailing live_ticks (Termux)

DATA FLOW (WSL):
  brain.py BUY verdict → capture_wallet_snapshot() → wallet_ticks.jsonl
  safety.py whale check → check_concentration() → blocking answer
  post_mortem.py → joins wallet_ticks with trade_summary

REQUIRES:
  - HELIUS_API_KEY in .env or environment
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import requests

# ============================================================
# PATH RESOLUTION — try paths.py (WSL), fall back to hardcoded (Termux)
# ============================================================

try:
    from paths import get_config, get_path, get_setting
    _WSL_MODE = True
except ImportError:
    _WSL_MODE = False

if _WSL_MODE:
    # WSL — resolved via paths.py
    BASE              = get_path("base")
    LOGS_DIR          = get_path("logs")
    STATE_DIR         = get_path("state")
    LIVE_TICKS_PATH   = get_path("live_ticks")
    WALLET_TICKS_PATH = get_path("wallet_ticks")
    OFFSET_PATH       = os.path.join(STATE_DIR, "brain_w_offset.txt")
    LOG_PATH          = os.path.join(LOGS_DIR, "brain_w.log")
    CONFIG_PATH       = get_path("config")
else:
    # Termux — hardcoded paths
    BASE              = os.path.expanduser("~/alpha_sniper/android/abc")
    LOGS_DIR          = os.path.join(BASE, "logs")
    STATE_DIR         = os.path.join(BASE, "state")
    CONFIG_PATH       = os.path.join(BASE, "config.json")
    LIVE_TICKS_PATH   = os.path.join(LOGS_DIR, "live_ticks.jsonl")
    WALLET_TICKS_PATH = os.path.join(LOGS_DIR, "wallet_ticks.jsonl")
    OFFSET_PATH       = os.path.join(STATE_DIR, "brain_w_offset.txt")
    LOG_PATH          = os.path.join(LOGS_DIR, "brain_w.log")

# ============================================================
# CONFIG — load from config.json + .env
# ============================================================

def _load_env():
    """Load .env file. Tries base_dir first, then Termux path."""
    if _WSL_MODE:
        try:
            base_dir = get_config().get("base_dir", "")
            if base_dir:
                env_path = os.path.join(base_dir, ".env")
                if os.path.exists(env_path):
                    with open(env_path, "r") as f:
                        for line in f:
                            line = line.strip()
                            if not line or line.startswith("#") or "=" not in line:
                                continue
                            key, _, val = line.partition("=")
                            key, val = key.strip(), val.strip()
                            if len(val) >= 2 and ((val.startswith('"') and val.endswith('"')) or 
                                                   (val.startswith("'") and val.endswith("'"))):
                                val = val[1:-1]
                            if key:
                                os.environ.setdefault(key, val)
        except Exception:
            pass
    else:
        env_path = os.path.join(BASE, ".env")
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key, val = key.strip(), val.strip()
                    if len(val) >= 2 and ((val.startswith('"') and val.endswith('"')) or 
                                           (val.startswith("'") and val.endswith("'"))):
                        val = val[1:-1]
                    if key:
                        os.environ.setdefault(key, val)

_load_env()

def _load_config() -> Dict[str, Any]:
    """Load brain_wt config block from config.json, with defaults."""
    if _WSL_MODE:
        try:
            cfg = get_config()
            return cfg.get("wallet_brain") or cfg.get("brain_wt") or {}
        except Exception:
            return {}
    else:
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            return cfg.get("brain_wt") or {}
        except Exception:
            return {}

CFG = _load_config()

HELIUS_API_KEY  = os.environ.get("HELIUS_API_KEY", "")
HELIUS_BASE_URL = "https://mainnet.helius-rpc.com/?api-key={api_key}"

HELIUS_TIMEOUT      = int(CFG.get("helius_timeout_sec", 10))
HELIUS_SLEEP        = float(CFG.get("helius_sleep_between_calls", 0.12))
FETCH_DEV_WALLET    = bool(CFG.get("fetch_dev_wallet", True))
FETCH_DEV_AGE       = bool(CFG.get("fetch_dev_wallet_age", True))
TOP_HOLDERS_LIMIT   = int(CFG.get("top_holders_limit", 20))

POLL_INTERVAL_SEC = float(CFG.get("poll_interval_sec", 5))

# Banding thresholds for concentration check
BAND_OK_PCT   = float(CFG.get("concentration_ok_pct", 30.0))
BAND_WARN_PCT = float(CFG.get("concentration_warn_pct", 60.0))

# Known AMM program addresses — filtered from holder concentration
KNOWN_AMM = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",  # LaunchLab
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "boop8hVGQGqehUK2iVEMEnMrL5RbjywRzHKBmBE7ry4",  # Boop.fun
}


# ============================================================
# HELPERS
# ============================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str):
    line = f"[brain_w] {_now_iso()} {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _append_jsonl(path: str, obj: Dict[str, Any]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ============================================================
# HELIUS RPC
# ============================================================

def _helius_post(method: str, params: Any) -> Optional[dict]:
    if not HELIUS_API_KEY:
        return None
    url = HELIUS_BASE_URL.format(api_key=HELIUS_API_KEY)
    payload = {"jsonrpc": "2.0", "id": "brain_w", "method": method, "params": params}
    try:
        r = requests.post(url, json=payload, timeout=HELIUS_TIMEOUT)
        r.raise_for_status()
        result = r.json()
        if result.get("error"):
            _log(f"helius {method} error: {result['error']}")
            return None
        return result.get("result")
    except Exception as e:
        _log(f"helius {method} failed: {e}")
        return None


def _fetch_token_accounts(mint: str) -> List[Dict[str, Any]]:
    """Return top holders sorted by amount descending."""
    raw = _helius_post("getTokenAccounts", {"mint": mint, "limit": TOP_HOLDERS_LIMIT})
    if not raw:
        return []
    accounts = raw.get("token_accounts", [])
    accounts.sort(key=lambda x: x.get("amount", 0), reverse=True)
    return accounts


def _fetch_token_supply(mint: str) -> Optional[Dict[str, Any]]:
    result = _helius_post("getTokenSupply", [mint])
    if not result:
        return None
    val = result.get("value", {})
    return {
        "total_raw": int(val.get("amount", 0)),
        "decimals": val.get("decimals", 0),
        "ui_amount": val.get("uiAmount", 0),
    }


def _fetch_holders_helius(mint: str) -> List[Dict[str, Any]]:
    """
    Fetch top holders via Helius with accurately computed percentages.
    Uses total supply when available, otherwise falls back to sum of fetched accounts.
    """
    accounts = _fetch_token_accounts(mint)
    if not accounts:
        return []

    time.sleep(HELIUS_SLEEP)
    supply = _fetch_token_supply(mint)

    total = 0
    decimals = supply.get("decimals", 0) if supply else 0
    divisor = 10 ** decimals if decimals else 1
    pct_basis = "absolute"

    if supply and supply["total_raw"] > 0:
        total = supply["total_raw"] / divisor
    else:
        total = sum(a.get("amount", 0) / divisor for a in accounts)
        pct_basis = "relative_to_fetched"
        if total == 0:
            return []

    holders = []
    for acc in accounts:
        owner  = acc.get("owner", "")
        amount = acc.get("amount", 0) / divisor
        frozen = acc.get("frozen", False)
        if not owner:
            continue
        pct = round((amount / total) * 100, 4) if total > 0 else 0.0

        holders.append({
            "address": owner,
            "pct": pct,
            "pct_basis": pct_basis,
            "is_amm": owner in KNOWN_AMM,
            "frozen": frozen,
        })

    return holders


def _fetch_dev_wallet(mint: str) -> Optional[Dict[str, Any]]:
    """Fetch token creator (dev wallet) via Helius."""
    if not FETCH_DEV_WALLET or not HELIUS_API_KEY:
        return None

    sigs_result = _helius_post("getSignaturesForAddress", [mint, {"limit": 1, "order": "asc"}])
    time.sleep(HELIUS_SLEEP)
    if not sigs_result:
        return None

    sigs = sigs_result if isinstance(sigs_result, list) else sigs_result.get("result", [])
    if not sigs:
        return None

    oldest_sig = sigs[0].get("signature") if isinstance(sigs[0], dict) else sigs[0]
    block_time = sigs[0].get("blockTime") if isinstance(sigs[0], dict) else None

    tx_result = _helius_post("getTransaction", [oldest_sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
    time.sleep(HELIUS_SLEEP)
    if not tx_result:
        return None

    message = tx_result.get("transaction", {}).get("message", {})
    dev_wallet = (
        message.get("feePayer") or
        tx_result.get("feePayer") or
        (message.get("accountKeys", [{}])[0].get("pubkey") if message.get("accountKeys") else None)
    )
    if not dev_wallet:
        return None

    dev_wallet_age_days = None
    if FETCH_DEV_AGE:
        age_sigs = _helius_post("getSignaturesForAddress", [dev_wallet, {"limit": 1, "order": "asc"}])
        time.sleep(HELIUS_SLEEP)
        if age_sigs:
            age_list = age_sigs if isinstance(age_sigs, list) else age_sigs.get("result", [])
            if age_list:
                first_ts = age_list[0].get("blockTime") if isinstance(age_list[0], dict) else None
                if first_ts and block_time:
                    dev_wallet_age_days = round((block_time - first_ts) / 86400, 1)

    return {
        "dev_wallet": dev_wallet,
        "dev_wallet_age_days": dev_wallet_age_days,
        "dev_wallet_confidence": "high",
    }


# ============================================================
# COMPUTE AMM-FILTERED CONCENTRATION (shared by both entry points)
# ============================================================

def _compute_concentration(holders: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Given a list of holder dicts (with is_amm, pct fields),
    compute AMM-filtered top-10 concentration.
    """
    real_holders = [h for h in holders if not h.get("is_amm")]
    top10_pct = round(sum(h["pct"] for h in real_holders[:10]), 4)
    fresh_wallet_count = sum(1 for h in real_holders if h["pct"] < 2.0)
    frozen_holder_count = sum(1 for h in real_holders if h.get("frozen"))
    amm_holder_count = sum(1 for h in holders if h.get("is_amm"))

    return {
        "top10_pct": top10_pct,
        "fresh_wallet_count": fresh_wallet_count,
        "frozen_holder_count": frozen_holder_count,
        "amm_holder_count": amm_holder_count,
        "real_holder_count": len(real_holders),
    }


# ============================================================
# WALLET RISK SCORE
# ============================================================

def _compute_risk_score(
    top10_pct: float,
    concentration_flag: bool,
    fresh_wallet_count: int,
    dev_wallet_age_days: Optional[float],
) -> float:
    """
    Compute a 0-100 wallet risk score. Higher = riskier.

    Components:
      - Concentration risk  (0-40): top 10 holders controlling supply
      - Fresh wallet risk   (0-30): many new wallets = sniper/bot activity
      - Dev wallet age risk (0-30): brand new dev wallet = higher rug risk
    """
    score = 0.0

    # Concentration risk (0-40 points)
    if top10_pct > 50:
        score += 40
    elif top10_pct > 30:
        score += 25
    elif top10_pct > 15:
        score += 10

    # Fresh wallet risk (0-30 points)
    if fresh_wallet_count >= 15:
        score += 30
    elif fresh_wallet_count >= 10:
        score += 20
    elif fresh_wallet_count >= 5:
        score += 10

    # Dev wallet age risk (0-30 points)
    if dev_wallet_age_days is not None:
        if dev_wallet_age_days < 7:
            score += 30
        elif dev_wallet_age_days < 30:
            score += 20
        elif dev_wallet_age_days < 90:
            score += 10

    return round(score, 1)


# ============================================================
# ENTRY POINT 1: Full wallet snapshot (called by brain.py at BUY verdict)
# ============================================================

def capture_wallet_snapshot(mint: str) -> Optional[Dict[str, Any]]:
    """
    Synchronous wallet data capture for a single mint.
    Called by brain.py when a BUY verdict is emitted.
    Returns a wallet_ticks-compatible record, or None if no data.
    """
    holders = _fetch_holders_helius(mint)
    if not holders:
        return None

    conc = _compute_concentration(holders)
    top10_pct = conc["top10_pct"]

    concentration_flag = top10_pct > 50.0
    concentration_reason = (
        f"top-10 holders control {top10_pct}% (AMM-filtered)"
        if concentration_flag
        else "concentration within acceptable range"
    )

    dev_info = _fetch_dev_wallet(mint) if FETCH_DEV_WALLET else None
    dev_wallet_age_days = dev_info.get("dev_wallet_age_days") if dev_info else None

    # Compute composite risk score
    wallet_risk_score = _compute_risk_score(
        top10_pct=top10_pct,
        concentration_flag=concentration_flag,
        fresh_wallet_count=conc["fresh_wallet_count"],
        dev_wallet_age_days=dev_wallet_age_days,
    )

    event_ts = _now_iso()
    real_holders = [h for h in holders if not h.get("is_amm")]

    return {
        "ts": event_ts,
        "event_ts": event_ts,
        "match_key": mint,
        "mint": mint,
        "trade_id": None,
        "phase": "entry_snapshot",
        "trade_outcome": None,

        "top_holders": [
            {
                "address": h["address"],
                "pct": h["pct"],
                "pct_basis": h.get("pct_basis", "absolute"),
                "is_amm": h["is_amm"],
                "funding_source": None,
                "buy_ts": None,
            }
            for h in real_holders[:10]
        ],

        "top10_pct_filtered": top10_pct,
        "fresh_wallet_count": conc["fresh_wallet_count"],
        "frozen_holder_count": conc["frozen_holder_count"],

        "concentration_flag": concentration_flag,
        "concentration_reason": concentration_reason,

        "coordination_flag": False,
        "coordination_reason": "pending — requires Brain T transaction analysis",

        "dev_wallet": dev_info.get("dev_wallet") if dev_info else None,
        "dev_wallet_age_days": dev_wallet_age_days,
        "dev_wallet_confidence": dev_info.get("dev_wallet_confidence") if dev_info else "unavailable",

        "wallet_risk_score": wallet_risk_score,
        "data_sources": {
            "holders": "helius",
            "transfers": None,
            "dev_wallet": "helius" if dev_info else None,
        },
    }


# ============================================================
# ENTRY POINT 2: Lightweight concentration check (replaces whale_api)
# ============================================================

def check_concentration(mint: str) -> Dict[str, Any]:
    """
    AMM-filtered top-10 holder concentration check.
    Drop-in replacement for whale_api.check_whale_concentration().
    Returns the same shape so safety.py needs no changes.
    """
    holders = _fetch_holders_helius(mint)

    if not holders:
        return {
            "ok": None,
            "band": None,
            "top10_percent": None,
            "top1_percent": None,
            "amm_percent": None,
            "confidence": "low",
            "reason": "no_holder_data_from_helius",
            "source": "brain_w_amm_filtered",
            "raw": {},
        }

    conc = _compute_concentration(holders)
    top10_pct = conc["top10_pct"]

    # Top 1 holder percentage (AMM-filtered)
    real_holders = [h for h in holders if not h.get("is_amm")]
    top1_pct = real_holders[0]["pct"] if real_holders else 0.0

    if top10_pct <= BAND_OK_PCT:
        band, ok_val, reason = "ok", True, f"top10={top10_pct:.2f}% (ok, AMM-filtered)"
    elif top10_pct < BAND_WARN_PCT:
        band, ok_val, reason = "warn", True, f"top10={top10_pct:.2f}% (warn, AMM-filtered)"
    else:
        band, ok_val, reason = "block", False, f"top10={top10_pct:.2f}% (block, AMM-filtered)"

    return {
        "ok": ok_val,
        "band": band,
        "top10_percent": top10_pct,
        "top1_percent": top1_pct,
        "amm_percent": None,
        "confidence": "high",
        "reason": reason,
        "source": "brain_w_amm_filtered",
        "raw": {
            "real_holders": conc["real_holder_count"],
            "amm_holders": conc["amm_holder_count"],
            "fresh_wallets": conc["fresh_wallet_count"],
            "frozen_wallets": conc["frozen_holder_count"],
            "holders_fetched": len(holders),
            "rpc": "helius",
        },
    }


# ============================================================
# ENTRY POINT 3: Standalone loop (Termux — tails live_ticks)
# ============================================================

def _get_offset() -> int:
    try:
        with open(OFFSET_PATH) as f:
            return int(f.read().strip())
    except Exception:
        try:
            with open(LIVE_TICKS_PATH) as f:
                return sum(1 for _ in f)
        except Exception:
            return 0


def _save_offset(n: int):
    os.makedirs(os.path.dirname(OFFSET_PATH), exist_ok=True)
    tmp = OFFSET_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(n))
    os.replace(tmp, OFFSET_PATH)


def main():
    if not HELIUS_API_KEY:
        _log("ERROR: HELIUS_API_KEY not set")
        return

    _log(f"starting — {'WSL' if _WSL_MODE else 'Termux'} mode")
    _log(f"wallet_ticks: {WALLET_TICKS_PATH}")
    _log(f"dev_wallet:   {'enabled' if FETCH_DEV_WALLET else 'disabled'}")
    _log(f"dev_age:      {'enabled' if FETCH_DEV_AGE else 'disabled'}")

    os.makedirs(os.path.dirname(WALLET_TICKS_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(OFFSET_PATH), exist_ok=True)

    offset = _get_offset()
    seen_mints: Set[str] = set()
    _log(f"starting from offset={offset}")

    while True:
        try:
            if not os.path.exists(LIVE_TICKS_PATH):
                time.sleep(POLL_INTERVAL_SEC)
                continue

            with open(LIVE_TICKS_PATH, "r") as f:
                f.seek(offset)
                new_lines = f.readlines()

            if new_lines:
                with open(LIVE_TICKS_PATH, "r") as f:
                    f.seek(0, os.SEEK_END)
                    offset = f.tell()

                for raw_line in new_lines:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        tick = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    mint = tick.get("mint")
                    if not mint or mint in seen_mints:
                        continue

                    seen_mints.add(mint)
                    _log(f"new mint: {mint[:16]}…")

                    wallet_data = capture_wallet_snapshot(mint)
                    if wallet_data:
                        _append_jsonl(WALLET_TICKS_PATH, wallet_data)
                        _log(f"  wrote — holders={len(wallet_data['top_holders'])} "
                             f"fresh={wallet_data['fresh_wallet_count']} "
                             f"conc={wallet_data['concentration_flag']} "
                             f"risk={wallet_data['wallet_risk_score']} "
                             f"dev={'✓' if wallet_data.get('dev_wallet') else '✗'} "
                             f"dev_age={wallet_data.get('dev_wallet_age_days')}")
                    else:
                        _log(f"  no data from Helius")

                _save_offset(offset)

        except Exception as e:
            _log(f"loop error: {e}")

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
