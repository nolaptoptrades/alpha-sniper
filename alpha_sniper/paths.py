"""
paths.py — NLT Alpha Sniper

Runtime path resolver. Imported by every pipeline script.
Nuitka-safe: never uses __file__ to locate itself.

How it works:
    1. Looks for config.json next to sys.executable (compiled binary)
    2. Falls back to walking up from cwd (dev/source mode)
    3. Resolves all {base_dir} placeholders in config paths at runtime
    4. Loads .env file from base_dir (zero-dependency, Nuitka-safe)

Usage:
    from paths import get_config, get_path, BASE_DIR

    cfg = get_config()
    log_path = get_path("safety_log")   # fully resolved string
"""

import json
import logging
import logging.handlers
import os
import sys
import traceback as _traceback
from functools import lru_cache
from typing import Any, Dict, Optional

# ============================================================
# CONSTANTS
# ============================================================

CONFIG_FILENAME = "config.json"
ENV_FILENAME    = ".env"


# ============================================================
# DOTENV LOADER
# ============================================================

def _load_dotenv(base_dir: str) -> None:
    env_path = os.path.join(base_dir, ENV_FILENAME)
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2:
                if (value.startswith('"') and value.endswith('"')) or                    (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
            if key:
                os.environ.setdefault(key, value)


# ============================================================
# CONFIG LOCATION DISCOVERY
# ============================================================

def _find_config_path() -> str:
    exe_dir   = os.path.dirname(os.path.abspath(sys.executable))
    candidate = os.path.join(exe_dir, CONFIG_FILENAME)
    if os.path.isfile(candidate):
        return candidate
    current = os.path.abspath(os.getcwd())
    while True:
        candidate = os.path.join(current, CONFIG_FILENAME)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    raise FileNotFoundError(
        f"[paths] Could not locate {CONFIG_FILENAME}. "
        "Make sure config.json is in your project root or next to the binary."
    )


# ============================================================
# CONFIG LOADER
# ============================================================

@lru_cache(maxsize=1)
def get_config() -> Dict[str, Any]:
    config_path = _find_config_path()
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    base_dir = _resolve_base_dir(cfg, config_path)
    _load_dotenv(base_dir)
    cfg = _resolve_placeholders(cfg, base_dir)
    return cfg


# ============================================================
# DEFAULT CONFIG (factory reset)
# ============================================================

def get_default_config() -> Dict[str, Any]:
    return {
        "_meta": {"version": "1.0.0"},
        "discovery": {
            "sources": {
                "helius": {"enabled": True},
                "dexscreener": {"enabled": True},
            },
            "loop_interval_sec": 90,
        },
        "safety": {
            "bypass": False,
            "checks": {
                "liquidity": {"enabled": True, "mode": "warn", "min_usd": 200000},
                "volume": {"enabled": True, "mode": "warn", "min_bsr": 1.2},
                "momentum": {"enabled": True, "mode": "hard", "min": 3.0, "max": 20.0},
                "age": {"enabled": True, "mode": "warn", "max_min": 120},
                "whale": {"enabled": False, "mode": "warn", "max_pct": 30},
                "honeypot": {"enabled": False, "mode": "warn"},
            },
        },
        "scoring": {
            "buy_score_min": 60,
            "min_mom_m5": 6.5,
            "max_mom_m5": 10.0,
        },
        "trade_rules": {
            "tp_pct": 30.0,
            "sl_pct": -22.0,
            "max_hold_sec": 2700,
            "tsl_activate_roi": 10.0,
            "tsl_lock_roi": 1.0,
        },
        "data": {"sharing_enabled": True},
        "paths": {
            "logs": "{base_dir}/logs",
            "master_summary": "{base_dir}/logs/master_summary.jsonl",
            "paper_trades": "{base_dir}/logs/paper_trades.jsonl",
            "paper_ticks": "{base_dir}/logs/paper_ticks.jsonl",
            "wallet_ticks": "{base_dir}/logs/wallet_ticks.jsonl",
            "verdicts": "{base_dir}/logs/verdicts.jsonl",
            "config": "{base_dir}/config.json",
        },
    }


def _resolve_base_dir(cfg: Dict[str, Any], config_path: str) -> str:
    env_override = os.environ.get("ALPHA_SNIPER_BASE", "").strip()
    if env_override:
        return os.path.expanduser(env_override)
    cfg_base = cfg.get("base_dir", "").strip()
    if cfg_base:
        return os.path.expanduser(cfg_base)
    return os.path.dirname(os.path.abspath(config_path))


def _resolve_placeholders(cfg: Dict[str, Any], base_dir: str) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(cfg))
    cfg["_resolved_base_dir"] = base_dir

    def _walk(obj: Any) -> Any:
        if isinstance(obj, str):
            return obj.replace("{base_dir}", base_dir)
        if isinstance(obj, dict):
            return {k: _walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(i) for i in obj]
        return obj

    cfg["paths"] = _walk(cfg.get("paths") or {})
    return cfg


# ============================================================
# PATH ACCESSOR
# ============================================================

def get_path(key: str, default: "Optional[str]" = None) -> str:
    cfg   = get_config()
    paths = cfg.get("paths") or {}
    if key not in paths:
        if default is not None:
            return os.path.expanduser(default)
        raise KeyError(
            f"[paths] Key '{key}' not found in config paths. "
            f"Available: {list(paths.keys())}"
        )
    return os.path.expanduser(paths[key])


def get_setting(section: str, key: str, default: Any = None) -> Any:
    cfg  = get_config()
    node = cfg.get(section)
    if node is None:
        return default
    for part in key.split("."):
        if not isinstance(node, dict):
            return default
        node = node.get(part)
        if node is None:
            return default
    return node





# ============================================================
# CONVENIENCE EXPORTS
# ============================================================

def _lazy_base_dir() -> str:
    return get_config().get("_resolved_base_dir", "")


# ============================================================
# ERROR LOGGER  — rotating file logger for dev diagnostics
# Never shown in TUI. User sends error.log when reporting bugs.
# ============================================================

_error_logger = None


def _get_error_logger() -> logging.Logger:
    """Return the rotating file logger. Initialised once per process."""
    global _error_logger
    if _error_logger is not None:
        return _error_logger

    try:
        log_path = get_path("error_log")
    except Exception:
        log_path = os.path.expanduser(
            "~/nolaptoptrades/alpha_sniper/logs/error.log"
        )

    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    logger = logging.getLogger("nlt.error")
    if not logger.handlers:
        handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=1_000_000,  # 1 MB per file
            backupCount=3,       # 3 rotated backups -> 4 MB max total
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            fmt="[%(asctime)s] [%(component)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.ERROR)
        logger.propagate = False  # never bubble to root logger / stdout

    _error_logger = logger
    return logger


def log_error(component: str, message: str) -> None:
    """
    Write an error entry to the rotating dev error log.

    Full traceback is captured automatically when called from an
    except block via traceback.format_exc().

    Usage:
        try:
            ...
        except Exception as e:
            log_error("brain", f"loop error: {e}")

    Args:
        component:  script name, e.g. "brain", "safety", "simulator"
        message:    short description of what failed
    """
    try:
        logger = _get_error_logger()
        tb = _traceback.format_exc()
        full_msg = (
            message if tb.strip() == "NoneType: None"
            else f"{message}\n{tb.rstrip()}"
        )
        logger.error(full_msg, extra={"component": component})
    except Exception:
        pass  # logging must never crash the pipeline


# ============================================================
# SELF-TEST (run directly: python paths.py)
# ============================================================

if __name__ == "__main__":
    print("[paths] self-test")
    print()

    try:
        cfg = get_config()
        print(f"  config found:  {_find_config_path()}")
        print(f"  base_dir:      {cfg['_resolved_base_dir']}")
        print()
        print("  Resolved paths:")
        for k, v in (cfg.get("paths") or {}).items():
            print(f"    {k:20s}  {v}")
        print()
        print("  Env vars loaded from .env:")
        for key in ["HELIUS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
            val = os.environ.get(key, "")
            masked = val[:8] + "..." if len(val) > 8 else val
            print(f"    {key}: {masked if val else '(not set)'}")
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)

    print()
    print("[paths] OK")
