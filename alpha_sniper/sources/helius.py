"""
sources/helius.py — Alpha Sniper V2
Multi-launchpad graduation discovery via Helius RPC.

Supports PumpSwap, LaunchLab (LetsBonk), Boop, Moonshot, Meteora DBC.
Each launchpad is independently cursor-tracked and can be
enabled/disabled in config.
"""

import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from paths import get_config, get_path, log_error

# ------------------------------------------------------------
# CONSTANTS
# ------------------------------------------------------------
ASSOC_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
HELIUS_BASE_URL     = "https://mainnet.helius-rpc.com/?api-key={api_key}"
SIG_FETCH_LIMIT     = 50
TX_FETCH_CAP        = 20
TX_FETCH_SLEEP_SEC  = 0.12
MAX_AGE_MIN         = 30

STABLE_MINTS: Set[str] = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "So11111111111111111111111111111111111111112",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
}

GRADUATION_PROGRAMS_DEFAULT = {
    "pumpswap": {
        "enabled": True,
        "address": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
        "dex":     "pumpswap",
    },
    "launchlab": {
        "enabled": True,
        "address": "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
        "dex":     "raydium",
        "_note":   "Covers LetsBonk + LaunchLab direct — same program ID",
    },
    "boop": {
        "enabled": False,
        "address": "boop8hVGQGqehUK2iVEMEnMrL5RbjywRzHKBmBE7ry4",
        "dex":     "meteora",
    },
    "moonshot": {
        "enabled": False,
        "address": "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",
        "dex":     "dexscreener",
    },
    "meteora_dbc": {
        "enabled": False,
        "address": "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",
        "dex":     "meteora",
    },
}


# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------

def _now_local() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ------------------------------------------------------------
# CONFIG LOADING
# ------------------------------------------------------------

def _load_graduation_programs() -> Dict[str, Any]:
    """
    Load graduation programs from config.json, falling back to defaults.
    """
    cfg          = get_config()
    user_programs = cfg.get("graduation_programs", {})
    merged       = dict(GRADUATION_PROGRAMS_DEFAULT)
    for name, prog in user_programs.items():
        if isinstance(prog, dict):
            merged[name] = {**merged.get(name, {}), **prog}
    return merged


# ------------------------------------------------------------
# VALIDATE
# ------------------------------------------------------------

def validate_config(cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """Check HELIUS_API_KEY is present."""
    api_key = os.environ.get("HELIUS_API_KEY", "").strip()
    if not api_key:
        return False, "HELIUS_API_KEY not found in .env — add it to use Helius"
    return True, "ok"


# ------------------------------------------------------------
# CURSOR (per-program)
# ------------------------------------------------------------

def _cursor_path(launchpad_name: str, cfg: Dict[str, Any]) -> str:
    base = cfg.get("_cursor_path") or get_path("helius_cursor")
    return base.replace(".txt", f"_{launchpad_name}.txt")


def _load_cursor(launchpad_name: str, cfg: Dict[str, Any]) -> Optional[str]:
    path = _cursor_path(launchpad_name, cfg)
    try:
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip() or None
    except Exception:
        pass
    return None


def _save_cursor(launchpad_name: str, sig: str, cfg: Dict[str, Any]) -> None:
    path = _cursor_path(launchpad_name, cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(sig)


# ------------------------------------------------------------
# FETCH FROM ONE PROGRAM
# ------------------------------------------------------------

def _fetch_from_program(
    url:            str,
    program_address: str,
    launchpad_name: str,
    dex_name:       str,
    cfg:            Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Fetch graduation events for a single launchpad program.
    Returns normalized rows with launchpad and dex fields populated.
    Silent on success — errors only.
    """
    import requests

    max_age_min = float(cfg.get("max_age_min",         MAX_AGE_MIN))
    tx_cap      = int(cfg.get("tx_fetch_cap",          TX_FETCH_CAP))
    tx_sleep    = float(cfg.get("tx_fetch_sleep_sec",  TX_FETCH_SLEEP_SEC))
    max_age_sec = max_age_min * 60
    now_ts      = time.time()
    last_sig    = _load_cursor(launchpad_name, cfg)

    # ── Step 1: fetch signatures ───────────────────────────────
    sig_params: Dict[str, Any] = {"limit": SIG_FETCH_LIMIT}
    if last_sig:
        sig_params["until"] = last_sig

    try:
        r = requests.post(url, json={
            "jsonrpc": "2.0",
            "id":      "sigs",
            "method":  "getSignaturesForAddress",
            "params":  [program_address, sig_params],
        }, timeout=10)
        r.raise_for_status()
        sigs_result = r.json().get("result") or []
    except Exception as e:
        print(f"[helius:{launchpad_name}] {_now_local()}  sig fetch error — {e}")
        return []

    all_sigs   = [s["signature"] for s in sigs_result]
    fresh_sigs = [
        s["signature"] for s in sigs_result
        if not s.get("err")
        and s.get("blockTime")
        and (now_ts - s["blockTime"]) <= max_age_sec
    ]

    if all_sigs:
        _save_cursor(launchpad_name, all_sigs[0], cfg)

    if not fresh_sigs:
        return []

    # ── Step 2: fetch transactions, filter pool creation ───────
    rows:       List[Dict[str, Any]] = []
    seen_mints: Set[str]             = set()
    tx_errors                        = 0
    cap                              = min(len(fresh_sigs), tx_cap)

    for sig in fresh_sigs[:cap]:
        try:
            r = requests.post(url, json={
                "jsonrpc": "2.0",
                "id":      "tx",
                "method":  "getTransaction",
                "params":  [sig, {
                    "encoding":                       "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment":                     "confirmed",
                }],
            }, timeout=10)
            r.raise_for_status()
            tx = r.json().get("result")

            if not tx:
                continue

            meta = tx.get("meta") or {}
            if meta.get("err"):
                continue

            block_time = tx.get("blockTime")
            if not block_time:
                continue

            age_sec = now_ts - block_time
            if age_sec > max_age_sec:
                continue

            # Pool creation filter (AToken invoke)
            log_messages   = meta.get("logMessages") or []
            is_pool_create = any(
                f"{ASSOC_TOKEN_PROGRAM} invoke" in log
                for log in log_messages
            )
            if not is_pool_create:
                continue

            # Extract dominant non-stable mint
            mint = None
            for bal in meta.get("postTokenBalances") or []:
                m = bal.get("mint")
                if not m or m in STABLE_MINTS or m in seen_mints:
                    continue
                ui = (bal.get("uiTokenAmount") or {}).get("uiAmount") or 0
                if ui > 0:
                    mint = m
                    break

            if not mint:
                continue

            seen_mints.add(mint)
            rows.append({
                "source":     "helius",
                "launchpad":  launchpad_name,
                "mint":       mint,
                "symbol":     None,
                "dex":        dex_name,
                "usd":        None,
                "block_time": block_time,
                "age_sec":    round(age_sec),
                "sig":        sig,
            })

        except Exception:
            tx_errors += 1

        time.sleep(tx_sleep)

    # Report TX errors as a single summary line — not one per TX
    if tx_errors:
        print(f"[helius:{launchpad_name}] {_now_local()}  {tx_errors} tx error(s) this cycle")

    return rows


# ------------------------------------------------------------
# MAIN FETCH (multi-program)
# ------------------------------------------------------------

def fetch(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Iterate all enabled graduation programs, fetch from each,
    combine and de-duplicate by mint.
    Silent on success — discovery.py handles user-facing output.
    """
    api_key = os.getenv("HELIUS_API_KEY", "").strip()
    if not api_key:
        print(f"[helius] {_now_local()}  HELIUS_API_KEY not set — skipping")
        return []

    url      = HELIUS_BASE_URL.format(api_key=api_key)
    programs = _load_graduation_programs()

    all_rows:   List[Dict] = []
    seen_mints: Set[str]   = set()

    for launchpad_name, prog_cfg in programs.items():
        if not prog_cfg.get("enabled", False):
            continue

        rows = _fetch_from_program(
            url             = url,
            program_address = prog_cfg["address"],
            launchpad_name  = launchpad_name,
            dex_name        = prog_cfg.get("dex", "unknown"),
            cfg             = cfg,
        )

        for row in rows:
            mint = row.get("mint")
            if mint and mint not in seen_mints:
                seen_mints.add(mint)
                all_rows.append(row)

        time.sleep(0.5)   # rate limit between programs

    return all_rows
