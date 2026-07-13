#!/usr/bin/env python3
"""
safety.py — NLT Alpha Sniper v0.6.0

Config-driven safety filter. Tails sniper_events.jsonl, runs enabled
checks on each graduation event, passes clean mints to entry_queue.jsonl.

Data flow:
    discovery.py  →  sniper_events.jsonl
    safety.py     reads sniper_events.jsonl
    safety.py     →  entry_queue.jsonl  (passing mints)
    safety.py     →  safety.jsonl       (full audit log)

All thresholds and feature flags live in config.json under "safety".
Each check is independently toggleable. Nothing is forced on the user.

Usage:
    python3 safety.py [--dry-run]
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

from paths import get_config, get_path, get_setting

# ─────────────────────────────────────────────────────────────
# CONSTANTS  — sourced from config.json
# ─────────────────────────────────────────────────────────────

cfg = get_config()

# Whale absolute ceiling — hard block regardless of check mode
# Lives in safety.checks.whale.absolute_ceiling_pct
WHALE_HARD_BLOCK_PCT = float(
    (cfg.get("safety") or {})
    .get("checks", {})
    .get("whale", {})
    .get("absolute_ceiling_pct", 60.0)
)

# Dynamic min_buys per liquidity band — sourced from config
# safety.checks.volume.min_buys_bands
_vol_cfg   = (cfg.get("safety") or {}).get("checks", {}).get("volume", {})
_bands_cfg = _vol_cfg.get("min_buys_bands", {})

MIN_BUYS_BAND_TINY   = int(_bands_cfg.get("liq_under_20k",  12))
MIN_BUYS_BAND_SMALL  = int(_bands_cfg.get("liq_under_50k",  18))
MIN_BUYS_BAND_MED    = int(_bands_cfg.get("liq_under_100k", 25))
MIN_BUYS_BAND_LARGE  = int(_bands_cfg.get("liq_100k_plus",  35))

# ─────────────────────────────────────────────────────────────
# DEFAULT SAFETY CONFIG — fallback if config.json incomplete
# ─────────────────────────────────────────────────────────────

DEFAULT_SAFETY_CONFIG = {
    "checks": {
        "liquidity": {
            "enabled": True,
            "mode":    "hard",
            "min_usd": 200_000.0,
        },
        "volume": {
            "enabled":    True,
            "mode":       "hard",
            "min_bsr":    1.2,
            "min_vol_usd": 300.0,
            "min_trades": 20,
        },
        "momentum": {
            "enabled": True,
            "mode":    "hard",
            "min":     3.0,
            "max":     20.0,
        },
        "age": {
            "enabled": True,
            "mode":    "warn",
            "max_min": 120,
        },
        "honeypot": {
            "enabled": False,
            "mode":    "warn",
        },
        "whale": {
            "enabled": True,
            "mode":    "warn",
            "max_pct": 30.0,
        },
    }
}


# ─────────────────────────────────────────────────────────────
# SAFETY CONFIG LOADER
# ─────────────────────────────────────────────────────────────

def get_safety_cfg() -> Dict[str, Any]:
    """
    Load safety check config from config.json "safety" block.
    Deep-merges user values over DEFAULT_SAFETY_CONFIG.
    """
    cfg         = get_config()
    merged      = json.loads(json.dumps(DEFAULT_SAFETY_CONFIG))
    user_safety = cfg.get("safety") or {}
    user_checks = user_safety.get("checks") or {}

    for check_name, user_vals in user_checks.items():
        if check_name in merged["checks"]:
            merged["checks"][check_name].update(user_vals)
        else:
            merged["checks"][check_name] = user_vals

    return merged


# ─────────────────────────────────────────────────────────────
# FOLLOWER (tail -f pattern)
# ─────────────────────────────────────────────────────────────

def follow(path: str, sleep_sec: float = 2.0) -> Iterator[str]:
    """
    Robust file follower. Tolerates rotation / rewrite.
    Starts at EOF so we only process new events on restart.
    """
    try:
        pos = os.path.getsize(path)
    except FileNotFoundError:
        pos = 0

    while True:
        try:
            size = os.path.getsize(path)
        except FileNotFoundError:
            time.sleep(sleep_sec)
            continue

        if size < pos:
            pos = 0

        try:
            with open(path, "r", encoding="utf-8") as f:
                f.seek(pos)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    pos = f.tell()
                    yield line
        except FileNotFoundError:
            pass

        time.sleep(sleep_sec)


# ─────────────────────────────────────────────────────────────
# TIMESTAMP HELPERS
# ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_local() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _short_mint(mint: str) -> str:
    """Truncate mint address for display: first 5 + last 3 chars."""
    if not mint or len(mint) < 10:
        return mint
    return f"{mint[:5]}...{mint[-3:]}"


def _to_float_ts(x: Any) -> Optional[float]:
    """Normalize timestamps: ISO strings, epoch seconds, epoch ms."""
    if x is None:
        return None
    if isinstance(x, str):
        x = x.strip()
        if x.isdigit():
            x = int(x)
        else:
            try:
                dt = datetime.fromisoformat(x.replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception:
                return None
    try:
        x = float(x)
    except Exception:
        return None
    if x > 1_000_000_000_000:
        x = x / 1000.0
    return x


# ─────────────────────────────────────────────────────────────
# SAFETY CHECKS
# Each returns: { ok, mode, reason, value }
# ok=None means data missing — hard checks treat this as warn
# ─────────────────────────────────────────────────────────────

def check_liquidity(
    dex_data: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    enabled = cfg.get("enabled", True)
    mode    = cfg.get("mode", "hard")
    min_usd = float(cfg.get("min_usd", 200_000.0))

    if not enabled:
        return {"ok": None, "mode": mode, "reason": "check_disabled", "value": None}

    liq_usd  = dex_data.get("liquidity_usd")
    lp_ratio = dex_data.get("lp_ratio")

    if liq_usd is None:
        return {"ok": False, "mode": mode, "reason": "liq=None", "value": None}

    liq_f      = float(liq_usd)
    ok         = liq_f >= min_usd
    ratio_note = f"lp_ratio={lp_ratio:.3f}" if lp_ratio is not None else "lp_ratio=None"
    reason     = (
        f"liq={liq_f:.0f}>={min_usd:.0f} [{ratio_note}]"
        if ok else
        f"liq={liq_f:.0f}<{min_usd:.0f} [{ratio_note}]"
    )
    return {"ok": ok, "mode": mode, "reason": reason, "value": liq_f}


def check_volume(
    dex_data: Dict[str, Any],
    liq_usd: Optional[float],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Gate on 5m volume activity and buy/sell ratio.

    Dynamic min_buys floor by liq band — values from config.json
    safety.checks.volume.min_buys_bands.
    """
    enabled    = cfg.get("enabled", True)
    mode       = cfg.get("mode", "hard")
    min_bsr    = float(cfg.get("min_bsr", 1.2))
    min_vol    = float(cfg.get("min_vol_usd", 300.0))
    min_trades = int(cfg.get("min_trades", 20))

    if not enabled:
        return {"ok": None, "mode": mode, "reason": "check_disabled", "value": None}

    vol_5m = dex_data.get("vol_5m")
    buys5  = dex_data.get("buys_5m")
    sells5 = dex_data.get("sells_5m")

    if any(v is None for v in [vol_5m, buys5, sells5]):
        return {
            "ok":     False,
            "mode":   mode,
            "reason": f"missing_fields vol_5m={vol_5m} buys_5m={buys5} sells_5m={sells5}",
            "value":  None,
        }

    vol_5m_f = float(vol_5m)
    buys5_i  = int(buys5)
    sells5_i = int(sells5)
    total    = buys5_i + sells5_i

    # Dynamic min_buys by liq band — sourced from config constants
    if liq_usd is None or liq_usd < 20_000:
        min_buys = MIN_BUYS_BAND_TINY
    elif liq_usd < 50_000:
        min_buys = MIN_BUYS_BAND_SMALL
    elif liq_usd < 100_000:
        min_buys = MIN_BUYS_BAND_MED
    else:
        min_buys = MIN_BUYS_BAND_LARGE

    if sells5_i == 0:
        bsr = float("inf") if buys5_i > 0 else 1.0
    else:
        bsr = buys5_i / sells5_i

    need_ratio = min_bsr
    if total >= 500:
        need_ratio = max(1.1, min_bsr - 0.1)

    ok = (
        vol_5m_f  >= min_vol
        and buys5_i >= min_buys
        and total   >= min_trades
        and bsr     >= need_ratio
    )

    bsr_str = f"{bsr:.2f}" if bsr != float("inf") else "inf"
    reason  = (
        f"vol={vol_5m_f:.0f} buys={buys5_i} sells={sells5_i} "
        f"total={total} bsr={bsr_str} "
        f"[need_vol>={min_vol:.0f} buys>={min_buys} "
        f"total>={min_trades} bsr>={need_ratio:.2f}]"
    )

    return {
        "ok":   ok,
        "mode": mode,
        "reason": reason,
        "value": {
            "vol_5m":   vol_5m_f,
            "buys_5m":  buys5_i,
            "sells_5m": sells5_i,
            "bsr":      bsr if bsr != float("inf") else None,
        },
    }


def check_momentum(
    dex_data: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Gate on priceChange.m5. Falls back to h1 if m5 unavailable.
    """
    enabled = cfg.get("enabled", True)
    mode    = cfg.get("mode", "hard")
    mom_min = float(cfg.get("min", 3.0))
    mom_max = float(cfg.get("max", 20.0))

    if not enabled:
        return {"ok": None, "mode": mode, "reason": "check_disabled", "value": None}

    mom_pct = dex_data.get("priceChange_m5")
    mom_src = "priceChange_m5"

    if mom_pct is None:
        mom_pct = dex_data.get("priceChange_h1")
        mom_src = "priceChange_h1"

    if mom_pct is None:
        return {"ok": None, "mode": mode, "reason": "no_momentum_data", "value": None}

    try:
        mom_pct = float(mom_pct)
    except Exception:
        return {"ok": None, "mode": mode, "reason": "momentum_invalid", "value": None}

    ok     = (mom_min <= mom_pct <= mom_max)
    reason = f"mom={mom_pct:.2f}% src={mom_src} [{mom_min:.1f}%<=x<={mom_max:.1f}%]"
    return {"ok": ok, "mode": mode, "reason": reason, "value": mom_pct}


def check_age(
    dex_data: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Gate on pair age derived from pair_created_at (epoch seconds).
    Warn-only by default — DexScreener pairCreatedAt is unreliable.
    """
    enabled = cfg.get("enabled", True)
    mode    = cfg.get("mode", "warn")
    max_min = float(cfg.get("max_min", 120))

    if not enabled:
        return {"ok": None, "mode": mode, "reason": "check_disabled", "value": None}

    created_ts = dex_data.get("pair_created_at")

    # multi-key fallback for legacy bundle shapes
    if created_ts is None:
        raw = dex_data.get("raw") or {}
        created_ts = (
            raw.get("pairCreatedAt")
            or raw.get("pair_created_at")
        )
        created_ts = _to_float_ts(created_ts)

    if created_ts is None:
        return {"ok": None, "mode": mode, "reason": "no_created_ts", "value": None}

    max_age_sec = max_min * 60
    now         = datetime.now(timezone.utc).timestamp()
    age_sec     = max(0.0, now - float(created_ts))
    age_min     = round(age_sec / 60.0, 1)
    ok          = age_sec <= max_age_sec
    reason      = f"age={age_min}min [max={max_min}min]"

    return {"ok": ok, "mode": mode, "reason": reason, "value": age_min}


def check_honeypot(
    whale_data: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Disabled by default. PumpSwap graduation implies sell route exists.
    Kept for future sniping regimes where route availability matters.
    """
    enabled = cfg.get("enabled", False)
    mode    = cfg.get("mode", "warn")

    if not enabled:
        return {"ok": None, "mode": mode, "reason": "check_disabled", "value": None}

    # When enabled, expects honeypot_data from honeypot_api.check_honeypot()
    sell_ok = whale_data.get("is_safe")
    if sell_ok is None:
        return {"ok": None, "mode": mode, "reason": "no_honeypot_data", "value": None}

    ok     = bool(sell_ok)
    reason = "sell_route_ok" if ok else "sell_route_missing"
    return {"ok": ok, "mode": mode, "reason": reason, "value": ok}


def check_whale(
    whale_data: Dict[str, Any],
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Gate on top-10 holder concentration.
    Warn-only by default — unreliable until AMM wallets filtered.
    Extreme concentration (>= WHALE_HARD_BLOCK_PCT) always hard-blocks
    regardless of mode setting.
    """
    enabled = cfg.get("enabled", True)
    mode    = cfg.get("mode", "warn")
    max_pct = float(cfg.get("max_pct", 30.0))

    if not enabled:
        return {"ok": None, "mode": mode, "reason": "check_disabled", "value": None}

    top10 = whale_data.get("top10_percent")

    if top10 is None:
        return {"ok": None, "mode": mode, "reason": "top10=None(unknown)", "value": None}

    try:
        top10_f = float(top10)
    except Exception:
        return {"ok": None, "mode": mode, "reason": f"top10={top10}(invalid)", "value": None}

    ok     = top10_f <= max_pct
    reason = (
        f"top10={top10_f:.2f}% "
        f"[warn>{max_pct:.0f}% hard>{WHALE_HARD_BLOCK_PCT:.0f}%]"
    )
    return {
        "ok":          ok,
        "mode":        mode,
        "reason":      reason,
        "value":       top10_f,
        "_hard_block": top10_f >= WHALE_HARD_BLOCK_PCT,
    }


# ─────────────────────────────────────────────────────────────
# AGGREGATOR
# ─────────────────────────────────────────────────────────────

def run_safety_checks(
    mint:       str,
    bundle:     Dict[str, Any],
    safety_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    checks_cfg    = safety_cfg.get("checks") or {}
    dex_data      = bundle.get("dex") or {}
    whale_data    = bundle.get("whale") or {}
    honeypot_data = bundle.get("honeypot") or {}

    liq_usd_raw = dex_data.get("liquidity_usd")
    liq_usd     = float(liq_usd_raw) if liq_usd_raw is not None else None

    results = {
        "liquidity": check_liquidity(dex_data,      checks_cfg.get("liquidity") or {}),
        "volume":    check_volume(dex_data, liq_usd, checks_cfg.get("volume")    or {}),
        "momentum":  check_momentum(dex_data,        checks_cfg.get("momentum")  or {}),
        "age":       check_age(dex_data,             checks_cfg.get("age")       or {}),
        "honeypot":  check_honeypot(honeypot_data,   checks_cfg.get("honeypot")  or {}),
        "whale":     check_whale(whale_data,          checks_cfg.get("whale")     or {}),
    }

    hard_fails: list = []
    warnings:   list = []

    for check_name, result in results.items():
        ok   = result.get("ok")
        mode = result.get("mode", "hard")

        if check_name == "whale" and result.get("_hard_block"):
            hard_fails.append(f"whale_extreme({result.get('value', '?')}%)")
            continue

        if ok is False:
            if mode == "hard":
                hard_fails.append(check_name)
            else:
                warnings.append(check_name)
        elif ok is None:
            if mode == "hard" and (checks_cfg.get(check_name) or {}).get("enabled", True):
                warnings.append(f"{check_name}=unknown")

    all_ok = len(hard_fails) == 0
    reason = (
        "ok" + (f" warnings=[{','.join(warnings)}]" if warnings else "")
        if all_ok else
        f"fail:[{','.join(hard_fails)}]" + (f" warnings=[{','.join(warnings)}]" if warnings else "")
    )

    return {
        "mint":       mint,
        "all_ok":     all_ok,
        "warnings":   warnings,
        "hard_fails": hard_fails,
        "checks":     results,
        "reason":     reason,
        "ts":         _now_iso(),
    }


# ─────────────────────────────────────────────────────────────
# DATA BUNDLE BUILDER
# Stage 1: dex data  (cheap — one HTTP call, early exit if dead)
# Stage 2: whale     (medium — RPC call, only if Stage 1 passes)
# Stage 3: honeypot  (expensive — only if enabled in config)
# ─────────────────────────────────────────────────────────────

def fetch_bundle(
    mint:       str,
    safety_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    # Stage 1 — single DexScreener call
    try:
        from dex_api import fetch_dex_data
        dex = fetch_dex_data(mint)
    except Exception as e:
        dex = {}
        print(f"[safety] {_now_local()}  dex fetch error — {_short_mint(mint)}: {e}")

    checks_cfg     = safety_cfg.get("checks") or {}
    liq_usd        = (dex or {}).get("liquidity_usd")
    liq_min        = float((checks_cfg.get("liquidity") or {}).get("min_usd", 200_000.0))
    liq_ok_preview = isinstance(liq_usd, (int, float)) and float(liq_usd) >= liq_min
    vol_has_data   = (dex or {}).get("vol_5m") is not None

    if not liq_ok_preview or not vol_has_data:
        return {"dex": dex, "whale": {}, "honeypot": {}}

    # Stage 2 — whale (RPC, warn-only)
    whale     = {}
    whale_cfg = checks_cfg.get("whale") or {}
    if whale_cfg.get("enabled", True):
        try:
            from whale_api import check_whale_concentration
            whale = check_whale_concentration(mint)
        except Exception as e:
            whale = {}
            print(f"[safety] {_now_local()}  whale fetch error — {_short_mint(mint)}: {e}")

    # Stage 3 — honeypot (disabled by default)
    honeypot     = {}
    honeypot_cfg = checks_cfg.get("honeypot") or {}
    if honeypot_cfg.get("enabled", False):
        try:
            from honeypot_api import check_honeypot as _hp
            honeypot = _hp(mint)
        except Exception as e:
            honeypot = {}
            print(f"[safety] {_now_local()}  honeypot fetch error — {_short_mint(mint)}: {e}")

    return {"dex": dex, "whale": whale, "honeypot": honeypot}


# ─────────────────────────────────────────────────────────────
# QUEUE / LOG WRITERS
# ─────────────────────────────────────────────────────────────

_queued_this_session: set = set()


def append_to_entry_queue(mint: str, ts: str, verdict: Dict[str, Any], launchpad: str = "unknown") -> None:
    """Write a passing mint to entry_queue.jsonl. Session-level dedup."""
    if mint in _queued_this_session:
        return
    _queued_this_session.add(mint)

    path = get_path("entry_queue")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "ts":       ts,
        "mint":     mint,
        "launchpad": launchpad,
        "warnings": verdict.get("warnings") or [],
        "reason":   verdict.get("reason"),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, separators=(",", ":")) + "\n")


def append_to_safety_log(record: Dict[str, Any]) -> None:
    path = get_path("safety_log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


# ─────────────────────────────────────────────────────────────
# EVENT PROCESSOR
# ─────────────────────────────────────────────────────────────

def process_event_line(
    line:       str,
    safety_cfg: Dict[str, Any],
    dry_run:    bool = False,
) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line:
        return None

    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        print(f"[safety] {_now_local()}  bad json in event line — skipping")
        return None

    mint = event.get("mint")
    launchpad = event.get("launchpad", "unknown")
    ts   = event.get("ts") or event.get("timestamp") or _now_iso()

    if not mint:
        print(f"[safety] {_now_local()}  event missing mint — skipping")
        return None

    if (get_config().get("safety") or {}).get("bypass", False):
        if not dry_run:
            append_to_entry_queue(mint, ts, {"warnings": [], "reason": "bypass"}, launchpad)
        print(f"[safety] {_now_local()}  BYPASS {_short_mint(mint)}")
        return None

    bundle  = fetch_bundle(mint, safety_cfg)
    verdict = run_safety_checks(mint, bundle, safety_cfg)

    record = {
        "ts":         ts,
        "mint":       mint,
        "all_ok":     verdict["all_ok"],
        "reason":     verdict["reason"],
        "warnings":   verdict["warnings"],
        "hard_fails": verdict["hard_fails"],
        "checks": {
            name: {
                "ok":     r.get("ok"),
                "mode":   r.get("mode"),
                "reason": r.get("reason"),
                "value":  r.get("value"),
            }
            for name, r in verdict["checks"].items()
        },
    }

    if not dry_run:
        append_to_safety_log(record)
        if verdict["all_ok"]:
            append_to_entry_queue(mint, ts, verdict, launchpad)

    _print_verdict(record)
    return record


def _print_verdict(record: Dict[str, Any]) -> None:
    ts       = _now_local()
    mint     = _short_mint(record.get("mint", ""))
    all_ok   = record["all_ok"]
    warnings = record.get("warnings") or []
    fails    = record.get("hard_fails") or []
    checks   = record.get("checks") or {}

    # Pull key values for GO line
    liq_val = (checks.get("liquidity") or {}).get("value")
    mom_val = (checks.get("momentum")  or {}).get("value")
    liq_str = f"  liq ${liq_val:,.0f}" if liq_val is not None else ""
    mom_str = f"  mom {mom_val:.1f}%"  if mom_val is not None else ""

    if all_ok:
        warn_str = f"  warn: {' '.join(warnings)}" if warnings else ""
        label    = "GO  "
        print(f"[safety] {ts}  {label} {mint}{liq_str}{mom_str}{warn_str}")
    else:
        # Friendly fail reason — first hard fail drives the message
        fail_msgs = []
        for name in fails:
            r = checks.get(name) or {}
            val = r.get("value")
            if name == "liquidity" and val is not None:
                fail_msgs.append(f"low liquidity (${val:,.0f})")
            elif name == "momentum" and val is not None:
                fail_msgs.append(f"momentum out of range ({val:.1f}%)")
            elif name == "volume":
                fail_msgs.append("low volume/activity")
            elif name.startswith("whale"):
                fail_msgs.append("whale concentration")
            else:
                fail_msgs.append(name)
        reason_str = "  " + ", ".join(fail_msgs) if fail_msgs else ""
        print(f"[safety] {ts}  SKIP {mint}{reason_str}")


# ─────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Alpha Sniper — Safety Filter")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Process events but do not write to entry_queue or safety log",
    )
    args = parser.parse_args()

    safety_cfg = get_safety_cfg()
    inbox_path = get_path("sniper_events")
    poll_sec   = float(get_setting("settings", "poll_interval_sec", 2))

    dry_tag = "  [dry-run]" if args.dry_run else ""
    print(f"[safety] ready — checks: {_enabled_checks_summary(safety_cfg)}{dry_tag}")

    for line in follow(inbox_path, sleep_sec=poll_sec):
        process_event_line(line, safety_cfg, dry_run=args.dry_run)


def _enabled_checks_summary(safety_cfg: Dict[str, Any]) -> str:
    checks = safety_cfg.get("checks") or {}
    parts  = []
    for name, cfg in checks.items():
        if cfg.get("enabled", True):
            mode = cfg.get("mode", "hard")
            parts.append(f"{name}({mode})")
    return ", ".join(parts) if parts else "none"


# ─────────────────────────────────────────────────────────────
# DIRECT CALL HELPER (testing / REPL)
# ─────────────────────────────────────────────────────────────

def check_mint(mint: str) -> Dict[str, Any]:
    """
    Run full safety check on a single mint. No file I/O.

    Example:
        from safety import check_mint
        result = check_mint("AbCd...XyZ")
        print(result["all_ok"], result["reason"])
    """
    safety_cfg = get_safety_cfg()
    bundle     = fetch_bundle(mint, safety_cfg)
    return run_safety_checks(mint, bundle, safety_cfg)


if __name__ == "__main__":
    main()
