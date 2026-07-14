#!/usr/bin/env python3
"""
manager.py — NLT Alpha Sniper

Process manager + TUI for the Alpha Sniper pipeline.
Run directly: python manager.py  (or via `nlt` alias)

TUI hotkeys:
  [1–6,9]   jump log view to that script
  [r]        restart currently viewed script
  [h]        halt / resume currently viewed script
  [Ctrl+E]   open config editor
  [Ctrl+K]   kill all scripts + quit (confirm required)
  [q]        quit manager and stop all scripts
"""

import curses
import json
import os
import platform
import signal
import subprocess
import sys
import time
import threading
import requests
from datetime import datetime, timezone
from collections import deque

from manager_config import (
    SCRIPTS,
    STARTUP_DELAY_SEC,
    ON_CRASH,
    RESTART_COOLDOWN_SEC,
    MAX_RESTARTS,
    RESTART_RESET_SEC,
    REFRESH_SEC,
    LOG_LINES,
    LOG_VIEW_INIT,
    VERDICTS_PATH,
    PAPER_TRADES_PATH,
    ALERT_BOT_TOKEN,
    ALERT_CHAT_ID,
    HELP_DOCS_URL,
    HELP_VIDEO_URL,
)
from stats_reader import read_all_stats

try:
    from paths import get_config, get_path
    _cfg = get_config()
    CONFIG_PATH = _cfg["paths"]["config"]
except Exception:
    CONFIG_PATH = os.path.expanduser("~/nolaptoptrades/config.json")


# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════

PYTHON = sys.executable

# Script groups
GROUP_PIPELINE   = "pipeline"
GROUP_BACKGROUND = "background"
GROUP_OPTIONAL   = "optional"

# Editor field types
FT_BOOL  = "bool"
FT_INT   = "int"
FT_FLOAT = "float"
FT_STR   = "str"
FT_ENUM  = "enum"

# Danger fields — confirm before save
DANGER_KEYS = {"bypass", "sharing_enabled"}

# Config editor field definitions
# (section_label, display_label, json_key_path, field_type, extra)
# json_key_path: list of keys to traverse in config dict
# extra: for ENUM = list of choices; for INT/FLOAT = (min, max) or None
CONFIG_FIELDS = [
    # ── DISCOVERY ─────────────────────────────────────────────
    ("DISCOVERY", None, None, None, None),
    ("DISCOVERY", "loop_interval_sec",       ["discovery", "loop_interval_sec"],              FT_INT,   (10, 3600)),
    ("DISCOVERY", "helius › enabled",        ["discovery", "sources", "helius", "enabled"],   FT_BOOL,  None),
    ("DISCOVERY", "helius › max_age_min",    ["discovery", "sources", "helius", "max_age_min"], FT_INT, (1, 1440)),
    ("DISCOVERY", "moralis › enabled",       ["discovery", "sources", "moralis", "enabled"],  FT_BOOL,  None),
    ("DISCOVERY", "moralis › min_liq_usd",   ["discovery", "sources", "moralis", "min_liq_usd"], FT_FLOAT, (0, None)),
    ("DISCOVERY", "dexscreener › enabled",   ["discovery", "sources", "dexscreener", "enabled"], FT_BOOL, None),
    ("DISCOVERY", "dexscreener › min_liq",   ["discovery", "sources", "dexscreener", "min_liq_usd"], FT_FLOAT, (0, None)),
    ("DISCOVERY", "dexscreener › max_age",   ["discovery", "sources", "dexscreener", "max_age_min"], FT_INT, (1, 1440)),

    # ── SAFETY ────────────────────────────────────────────────
    ("SAFETY", None, None, None, None),
    ("SAFETY", "bypass ⚠",                  ["safety", "bypass"],                           FT_BOOL,  None),
    ("SAFETY", "liquidity › enabled",        ["safety", "checks", "liquidity", "enabled"],   FT_BOOL,  None),
    ("SAFETY", "liquidity › mode",           ["safety", "checks", "liquidity", "mode"],      FT_ENUM,  ["warn", "hard"]),
    ("SAFETY", "liquidity › min_usd",        ["safety", "checks", "liquidity", "min_usd"],   FT_INT,   (0, None)),
    ("SAFETY", "volume › enabled",           ["safety", "checks", "volume", "enabled"],      FT_BOOL,  None),
    ("SAFETY", "volume › mode",              ["safety", "checks", "volume", "mode"],         FT_ENUM,  ["warn", "hard"]),
    ("SAFETY", "volume › min_bsr",           ["safety", "checks", "volume", "min_bsr"],      FT_FLOAT, (0, None)),
    ("SAFETY", "volume › min_vol_usd",       ["safety", "checks", "volume", "min_vol_usd"],  FT_FLOAT, (0, None)),
    ("SAFETY", "volume › min_trades",        ["safety", "checks", "volume", "min_trades"],   FT_INT,   (0, None)),
    ("SAFETY", "  bands › liq_under_20k",    ["safety", "checks", "volume", "min_buys_bands", "liq_under_20k"],  FT_INT, (0, None)),
    ("SAFETY", "  bands › liq_under_50k",    ["safety", "checks", "volume", "min_buys_bands", "liq_under_50k"],  FT_INT, (0, None)),
    ("SAFETY", "  bands › liq_under_100k",   ["safety", "checks", "volume", "min_buys_bands", "liq_under_100k"], FT_INT, (0, None)),
    ("SAFETY", "  bands › liq_100k_plus",    ["safety", "checks", "volume", "min_buys_bands", "liq_100k_plus"],  FT_INT, (0, None)),
    ("SAFETY", "momentum › enabled",         ["safety", "checks", "momentum", "enabled"],    FT_BOOL,  None),
    ("SAFETY", "momentum › mode",            ["safety", "checks", "momentum", "mode"],       FT_ENUM,  ["warn", "hard"]),
    ("SAFETY", "momentum › min",             ["safety", "checks", "momentum", "min"],        FT_FLOAT, (0, 100)),
    ("SAFETY", "momentum › max",             ["safety", "checks", "momentum", "max"],        FT_FLOAT, (0, 100)),
    ("SAFETY", "age › enabled",              ["safety", "checks", "age", "enabled"],         FT_BOOL,  None),
    ("SAFETY", "age › mode",                 ["safety", "checks", "age", "mode"],            FT_ENUM,  ["warn", "hard"]),
    ("SAFETY", "age › max_min",              ["safety", "checks", "age", "max_min"],         FT_INT,   (1, 10080)),
    ("SAFETY", "honeypot › enabled",         ["safety", "checks", "honeypot", "enabled"],    FT_BOOL,  None),
    ("SAFETY", "honeypot › mode",            ["safety", "checks", "honeypot", "mode"],       FT_ENUM,  ["warn", "hard"]),
    ("SAFETY", "whale › enabled",            ["safety", "checks", "whale", "enabled"],       FT_BOOL,  None),
    ("SAFETY", "whale › mode",               ["safety", "checks", "whale", "mode"],          FT_ENUM,  ["warn", "hard"]),
    ("SAFETY", "whale › max_pct",            ["safety", "checks", "whale", "max_pct"],       FT_FLOAT, (0, 100)),
    ("SAFETY", "whale › abs_ceiling_pct",    ["safety", "checks", "whale", "absolute_ceiling_pct"], FT_FLOAT, (0, 100)),

    # ── TRADE RULES ───────────────────────────────────────────
    ("TRADE RULES", None, None, None, None),
    ("TRADE RULES", "tp_pct",               ["trade_rules", "tp_pct"],                      FT_FLOAT, (0, 1000)),
    ("TRADE RULES", "sl_pct",               ["trade_rules", "sl_pct"],                      FT_FLOAT, (-100, 0)),
    ("TRADE RULES", "max_hold_sec",         ["trade_rules", "max_hold_sec"],                FT_INT,   (60, 86400)),
    ("TRADE RULES", "tsl_activate_roi",     ["trade_rules", "tsl_activate_roi"],            FT_FLOAT, (0, 1000)),
    ("TRADE RULES", "tsl_lock_roi",         ["trade_rules", "tsl_lock_roi"],               FT_FLOAT, (-100, 1000)),

    # ── BRAIN ─────────────────────────────────────────────────
    ("BRAIN", None, None, None, None),
    ("BRAIN", "buy_score_min",              ["scoring", "buy_score_min"],                   FT_INT,   (0, 100)),
    ("BRAIN", "watch_score_min",            ["scoring", "watch_score_min"],                 FT_INT,   (0, 100)),
    ("BRAIN", "min_mom_m5",                 ["scoring", "min_mom_m5"],                      FT_FLOAT, (0, 100)),
    ("BRAIN", "max_mom_m5",                 ["scoring", "max_mom_m5"],                      FT_FLOAT, (0, 100)),
    ("BRAIN", "hard_blocks › enabled",      ["brain", "hard_blocks", "enabled"],            FT_BOOL,  None),
    ("BRAIN", "hard_blocks › min_liq",      ["brain", "hard_blocks", "min_liq_usd"],        FT_FLOAT, (0, None)),
    ("BRAIN", "hard_blocks › min_mom",      ["brain", "hard_blocks", "min_mom_pct"],        FT_FLOAT, (0, 100)),
    ("BRAIN", "hard_blocks › max_mom",      ["brain", "hard_blocks", "max_mom_pct"],        FT_FLOAT, (0, 100)),
    ("BRAIN", "sample_count",               ["brain", "sample_count"],                      FT_INT,   (5, 200)),
    ("BRAIN", "sample_interval_sec",        ["brain", "sample_interval_sec"],               FT_FLOAT, (0.5, 60)),

    # ── SIMULATOR ─────────────────────────────────────────────
    ("SIMULATOR", None, None, None, None),
    ("SIMULATOR", "max_active_positions",   ["simulator", "max_active_positions"],          FT_INT,   (1, 20)),
    ("SIMULATOR", "paper_poll_sec",         ["simulator", "paper_poll_sec"],                FT_INT,   (1, 60)),
    ("SIMULATOR", "price_miss_max",         ["simulator", "price_miss_max"],               FT_INT,   (1, 30)),

    # ── MANAGER ───────────────────────────────────────────────
    ("MANAGER", None, None, None, None),
    ("MANAGER", "startup_delay_sec",        ["_manager", "startup_delay_sec"],              FT_FLOAT, (0, 10)),
    ("MANAGER", "restart_cooldown_sec",     ["_manager", "restart_cooldown_sec"],           FT_INT,   (0, 300)),
    ("MANAGER", "max_restarts",             ["_manager", "max_restarts"],                   FT_INT,   (1, 50)),
    ("MANAGER", "on_crash",                 ["_manager", "on_crash"],                       FT_ENUM,  ["restart+alert", "restart_only", "alert_only", "nothing"]),
    ("MANAGER", "log_lines",                ["_manager", "log_lines"],                      FT_INT,   (10, 200)),

    # ── Insight + Report ──────────────────────────────────────
    ("INSIGHTS", None, None, None, None),
    ("INSIGHTS", "enabled",    ["insights", "enabled"],  FT_BOOL, None),
    ("INSIGHTS", "auto",       ["insights", "auto"],      FT_BOOL, None),
    ("INSIGHTS", "provider",   ["insights", "provider"],  FT_ENUM, ["gemini", "claude"]),
    ("INSIGHTS", "max_tokens", ["insights", "max_tokens"], FT_INT, (100, 10000)),
    ("REPORTS", None, None, None, None),
    ("REPORTS", "auto_export", ["reports", "auto_export"], FT_BOOL, None),

    # ── OPTIONAL ──────────────────────────────────────────────
    ("OPTIONAL", None, None, None, None),
    ("OPTIONAL", "Bridge-Bot › auto-start",   ["manager", "optional_auto_start", "Bridge-Bot"],   FT_BOOL, None),
    ("OPTIONAL", "Brain Wallet › auto-start", ["manager", "optional_auto_start", "Brain Wallet"], FT_BOOL, None),
]

# Field notes — surfaced in editor footer when a field is selected
# key = tuple(json_key_path)
FIELD_NOTES = {
    ("safety", "bypass"):                               "Skips ALL safety checks. Brain hard blocks still apply. Use only when data sources are down.",
    ("safety", "checks", "liquidity", "min_usd"):       "Primary hard gate. 200k validated floor from paper trade analysis.",
    ("safety", "checks", "momentum", "min"):            "priceChange.m5 lower bound. Keep in sync with brain hard_blocks.min_mom_pct.",
    ("safety", "checks", "momentum", "max"):            "priceChange.m5 upper bound. Rejects runaway pumps.",
    ("safety", "checks", "whale", "enabled"):           "UNRELIABLE until AMM wallets are filtered. absolute_ceiling always hard-blocks regardless of mode.",
    ("safety", "checks", "honeypot", "enabled"):        "Disabled by default. PumpSwap graduation implies sell route exists.",
    ("scoring", "buy_score_min"):                       "Brain score threshold for BUY verdict. Lower = more entries, more noise.",
    ("scoring", "min_mom_m5"):                          "Brain scoring gate. Keep in sync with safety.momentum.min.",
    ("trade_rules", "sl_pct"):                          "Stop-loss. Negative number. -18 = exit at -18% ROI.",
    ("trade_rules", "tsl_lock_roi"):                    "TSL exits are net loss in live trading after Maestro fees at 0.3 SOL position size.",
    ("brain", "hard_blocks", "enabled"):                "Brain's own entry gates. Independent from safety.py. Disable to trust safety filtering only.",
    ("discovery", "loop_interval_sec"):                 "Seconds between full discovery cycles. 90s = ~960 cycles/day free tier.",
    ("_manager", "on_crash"):                           "restart+alert: auto-restart AND Telegram alert. restart_only: silent. alert_only: no restart. nothing: log only.",
    ("_manager", "max_restarts"):                       "Give up restarting after this many attempts. Background scripts (PostMortem) ignore this limit.",
}


# ═══════════════════════════════════════════════════════════════
# GLOBALS
# ═══════════════════════════════════════════════════════════════

state      = {}
state_lock = threading.Lock()

log_view_index = 0

stats_cache = {
    "evaluated": 0, "buy": 0, "skip": 0, "watch": 0,
    "open": 0, "tp": 0, "sl": 0, "tsl": 0,
    "rug": 0, "timeout": 0, "pricemiss": 0, "close": 0,
}
stats_lock = threading.Lock()

manager_log      = deque(maxlen=50)
manager_log_lock = threading.Lock()

running = True

# Screen state — dirty flag to reduce redraws
_last_render_hash = None


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def now_ts() -> float:
    return time.time()

def format_uptime(start_ts: float) -> str:
    if start_ts is None:
        return "—"
    sec = int(now_ts() - start_ts)
    if sec < 60:   return f"{sec}s"
    if sec < 3600: return f"{sec // 60}m {sec % 60}s"
    h = sec // 3600
    m = (sec % 3600) // 60
    return f"{h}h {m}m"

def mlog(msg: str):
    with manager_log_lock:
        manager_log.append(f"{now_iso()} {msg}")

def send_alert(msg: str):
    if not ALERT_BOT_TOKEN or not ALERT_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": ALERT_CHAT_ID, "text": f"🚨 MANAGER: {msg}"}, timeout=5)
    except Exception:
        pass

def open_url(url: str):
    """Open URL in browser — handles Termux, WSL, Linux, macOS."""
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", url])
        elif "microsoft" in platform.uname().release.lower():
            subprocess.Popen(["cmd.exe", "/c", "start", url])
        elif os.path.exists("/data/data/com.termux"):
            subprocess.Popen(["termux-open-url", url])
        else:
            subprocess.Popen(["xdg-open", url])
    except Exception as e:
        mlog(f"open_url failed: {e} — URL: {url}")


# ═══════════════════════════════════════════════════════════════
# CONFIG EDITOR — read/write helpers
# ═══════════════════════════════════════════════════════════════

def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)

def save_config(cfg: dict):
    """Atomic write — write to .tmp then rename."""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)

def cfg_get(cfg: dict, key_path: list):
    """Get value by key path list. Returns None if path missing."""
    if key_path[0] == "_manager":
        return _manager_get(key_path[1])
    if key_path[0] == "_optional":
        return _optional_get(key_path[1])
    node = cfg
    for k in key_path:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node

def cfg_set(cfg: dict, key_path: list, value):
    """Set value by key path list. Creates intermediate dicts if needed."""
    if key_path[0] == "_manager":
        _manager_set(key_path[1], value)
        return
    if key_path[0] == "_optional":
        _optional_set(key_path[1], value)
        return
    node = cfg
    for k in key_path[:-1]:
        if k not in node or not isinstance(node[k], dict):
            node[k] = {}
        node = node[k]
    node[key_path[-1]] = value

def _manager_get(key: str):
    """Read manager setting from manager_config module."""
    import manager_config as mc
    mapping = {
        "startup_delay_sec":    "STARTUP_DELAY_SEC",
        "restart_cooldown_sec": "RESTART_COOLDOWN_SEC",
        "max_restarts":         "MAX_RESTARTS",
        "on_crash":             "ON_CRASH",
        "log_lines":            "LOG_LINES",
    }
    attr = mapping.get(key)
    return getattr(mc, attr, None) if attr else None

def _manager_set(key: str, value):
    """Patch manager_config.py on disk for the given key."""
    mapping = {
        "startup_delay_sec":    "STARTUP_DELAY_SEC",
        "restart_cooldown_sec": "RESTART_COOLDOWN_SEC",
        "max_restarts":         "MAX_RESTARTS",
        "on_crash":             "ON_CRASH",
        "log_lines":            "LOG_LINES",
    }
    attr = mapping.get(key)
    if not attr:
        return
    mc_path = os.path.join(os.path.dirname(__file__), "manager_config.py")
    try:
        with open(mc_path, "r") as f:
            lines = f.readlines()
        new_val = f'"{value}"' if isinstance(value, str) else str(value)
        for i, line in enumerate(lines):
            if line.startswith(attr + " "):
                # preserve inline comment if present
                comment = ""
                if "#" in line:
                    comment = "  " + line[line.index("#"):]
                lines[i] = f"{attr:<24} = {new_val}{comment}" if comment else f"{attr:<24} = {new_val}\n"
                break
        with open(mc_path, "w") as f:
            f.writelines(lines)
    except Exception as e:
        mlog(f"ERROR patching manager_config.py: {e}")


# ═══════════════════════════════════════════════════════════════
# LOG TAIL THREAD
# ═══════════════════════════════════════════════════════════════

def _optional_get(script_name: str):
    """Get optional script auto-start from config.json."""
    try:
        cfg = load_config()
        return cfg.get("manager", {}).get("optional_auto_start", {}).get(script_name, False)
    except Exception:
        return False

def _optional_set(script_name: str, value: bool):
    """Save optional script auto-start to config.json."""
    try:
        cfg = load_config()
        if "manager" not in cfg:
            cfg["manager"] = {}
        if "optional_auto_start" not in cfg["manager"]:
            cfg["manager"]["optional_auto_start"] = {}
        cfg["manager"]["optional_auto_start"][script_name] = value
        save_config(cfg)
    except Exception as e:
        mlog(f"ERROR saving optional_auto_start: {e}")

def tail_log(name: str, log_path: str):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    open(log_path, "a").close()
    buf = deque(maxlen=LOG_LINES * 3)
    pos = 0
    while running:
        try:
            size = os.path.getsize(log_path)
            if size < pos:
                pos = 0
            with open(log_path, "r", errors="replace") as f:
                f.seek(pos)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    stripped = line.strip()
                    if stripped:
                        buf.append(stripped)
                pos = f.tell()
            with state_lock:
                if name in state:
                    state[name]["log_buf"] = list(buf)[-LOG_LINES:]
        except Exception:
            pass
        time.sleep(0.5)


# ═══════════════════════════════════════════════════════════════
# PROCESS MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def init_state():
    for s in SCRIPTS:
        state[s["name"]] = {
            "proc":          None,
            "pid":           None,
            "status":        "STOPPED",
            "start_ts":      None,
            "restarts":      0,
            "last_crash_ts": None,
            "log_buf":       [],
            "path":          s["path"],
            "log":           s["log"],
            "key":           s["key"],
            "group":         s.get("group", GROUP_PIPELINE),
        }

def start_script(name: str):
    with state_lock:
        s        = state[name]
        log_path = s["log"]
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    script_path = state[name]["path"]
    if not os.path.exists(script_path):
        mlog(f"ERROR: {name} not found at {script_path}")
        with state_lock:
            state[name]["status"] = "NOT FOUND"
        return
    try:
        log_file = open(log_path, "a")
        cmd = ["bash", script_path] if script_path.endswith(".sh") else [PYTHON, "-u", script_path]
        proc = subprocess.Popen(cmd, stdout=log_file, stderr=log_file, preexec_fn=os.setsid)
        with state_lock:
            state[name]["proc"]     = proc
            state[name]["pid"]      = proc.pid
            state[name]["status"]   = "RUNNING"
            state[name]["start_ts"] = now_ts()
        mlog(f"started {name} (pid={proc.pid})")
    except Exception as e:
        mlog(f"ERROR starting {name}: {e}")
        with state_lock:
            state[name]["status"] = "ERROR"

def stop_script(name: str):
    with state_lock:
        proc = state[name].get("proc")
        state[name]["status"] = "STOPPING"
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
    with state_lock:
        state[name]["proc"]     = None
        state[name]["pid"]      = None
        state[name]["status"]   = "STOPPED"
        state[name]["start_ts"] = None
    mlog(f"stopped {name}")

def restart_script(name: str):
    mlog(f"restarting {name}")
    stop_script(name)
    time.sleep(1)
    start_script(name)

def halt_script(name: str):
    mlog(f"halting {name}")
    stop_script(name)
    with state_lock:
        state[name]["status"] = "HALTED"

def start_pipeline():
    """Start all pipeline scripts in order. Skips background + optional."""
    for s in SCRIPTS:
        if s.get("group") == GROUP_PIPELINE:
            mlog(f"starting {s['name']}...")
            start_script(s["name"])
            time.sleep(STARTUP_DELAY_SEC)

def stop_all():
    for s in reversed(SCRIPTS):
        name = s["name"]
        with state_lock:
            status = state[name].get("status")
        if status not in ("STOPPED", "HALTED", "NOT FOUND", "FAILED"):
            stop_script(name)


# ═══════════════════════════════════════════════════════════════
# MONITOR THREAD
# ═══════════════════════════════════════════════════════════════

def monitor_loop():
    while running:
        for s in SCRIPTS:
            name  = s["name"]
            group = s.get("group", GROUP_PIPELINE)
            with state_lock:
                proc     = state[name].get("proc")
                status   = state[name].get("status")
                restarts = state[name].get("restarts", 0)
                start_ts = state[name].get("start_ts")

            if status != "RUNNING" or proc is None:
                continue
            if proc.poll() is None:
                continue

            with state_lock:
                state[name]["status"]        = "CRASHED"
                state[name]["proc"]          = None
                state[name]["pid"]           = None
                state[name]["last_crash_ts"] = now_ts()

            mlog(f"CRASH detected: {name}")

            if start_ts and (now_ts() - start_ts) > RESTART_RESET_SEC:
                with state_lock:
                    state[name]["restarts"] = 0
                restarts = 0

            # Background scripts — always restart, no limit
            if group == GROUP_BACKGROUND:
                mlog(f"background {name} crashed — restarting immediately")
                if ON_CRASH in ("restart+alert", "alert_only"):
                    send_alert(f"{name} crashed — restarting (always-on)")
                with state_lock:
                    state[name]["restarts"] = restarts + 1
                start_script(name)
                continue

            # Pipeline + optional — respect MAX_RESTARTS
            if restarts >= MAX_RESTARTS:
                mlog(f"gave up restarting {name} after {MAX_RESTARTS} attempts")
                if ON_CRASH in ("restart+alert", "alert_only"):
                    send_alert(f"{name} crashed {MAX_RESTARTS}x — gave up")
                with state_lock:
                    state[name]["status"] = "FAILED"
                continue

            if ON_CRASH in ("restart+alert", "alert_only"):
                send_alert(f"{name} crashed — {'restarting' if 'restart' in ON_CRASH else 'manual restart needed'}")

            if ON_CRASH in ("restart+alert", "restart_only"):
                mlog(f"waiting {RESTART_COOLDOWN_SEC}s before restarting {name}")
                time.sleep(RESTART_COOLDOWN_SEC)
                with state_lock:
                    state[name]["restarts"] = restarts + 1
                start_script(name)

        time.sleep(2)


# ═══════════════════════════════════════════════════════════════
# STATS THREAD
# ═══════════════════════════════════════════════════════════════

def stats_loop():
    while running:
        try:
            s = read_all_stats(VERDICTS_PATH, PAPER_TRADES_PATH)
            with stats_lock:
                stats_cache.update(s)
        except Exception:
            pass
        time.sleep(10)


# ═══════════════════════════════════════════════════════════════
# CONFIG EDITOR OVERLAY
# ═══════════════════════════════════════════════════════════════

def run_config_editor(stdscr, colors: dict):
    """
    Full-screen config editor overlay.
    Returns when user presses Q (back) or saves.
    """
    curses.curs_set(1)
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)

    GREEN  = colors["GREEN"]
    RED    = colors["RED"]
    YELLOW = colors["YELLOW"]
    CYAN   = colors["CYAN"]
    NORMAL = colors["NORMAL"]
    DIM    = colors["DIM"]

    # Load live config
    try:
        cfg = load_config()
    except Exception as e:
        mlog(f"config editor: failed to load config — {e}")
        curses.curs_set(0)
        return

    # Build flat list of (section, label, key_path, ftype, extra, is_header)
    # Sections are collapsible — track open/closed state
    sections_open = {}
    for row in CONFIG_FIELDS:
        sec = row[0]
        if sec not in sections_open:
            sections_open[sec] = False  # all collapsed by default

    def build_visible():
        """Return list of visible field rows."""
        rows = []
        last_sec = None
        for entry in CONFIG_FIELDS:
            sec, label, key_path, ftype, extra = entry
            is_header = label is None
            if is_header:
                rows.append({"type": "header", "section": sec})
                last_sec = sec
            else:
                if sections_open.get(last_sec, True):
                    rows.append({
                        "type":     "field",
                        "section":  sec,
                        "label":    label,
                        "key_path": key_path,
                        "ftype":    ftype,
                        "extra":    extra,
                    })
        return rows

    dirty        = {}  # key_path tuple → new value (pending)
    cursor       = 0
    scroll_off   = 0
    editing      = False
    edit_buf     = ""
    status_msg   = ""
    confirm_kill = False
    pending_save = False

    def get_value(key_path):
        tp = tuple(key_path)
        if tp in dirty:
            return dirty[tp]
        return cfg_get(cfg, key_path)

    def safe_add(row, col, text, attr=None):
        h, w = stdscr.getmaxyx()
        if row < 0 or row >= h or col < 0:
            return
        text = text[:max(0, w - col - 1)].replace("\x00", "")
        try:
            if attr:
                stdscr.addstr(row, col, text, attr)
            else:
                stdscr.addstr(row, col, text)
        except curses.error:
            pass

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        visible = build_visible()
        # Clamp cursor
        cursor = max(0, min(cursor, len(visible) - 1))

        # Scroll to keep cursor visible
        max_rows = h - 10  # header(6) + footer(4)
        if cursor < scroll_off:
            scroll_off = cursor
        if cursor >= scroll_off + max_rows:
            scroll_off = cursor - max_rows + 1

        # ── Header ─────────────────────────────────────────────
        title = " CONFIG EDITOR "
        safe_add(0, 0, "═" * w, CYAN)
        safe_add(1, 0, title.center(w)[:w], CYAN)
        safe_add(2, 0, "═" * w, CYAN)
        note_w = min(64, w - 2)
        safe_add(3, 1, ("╔" + "═" * (note_w - 2) + "╗")[:w - 1], YELLOW)
        safe_add(4, 1, ("║  NOTE: Changes take effect after restarting the component" + " " * note_w)[:note_w - 1] + "║", YELLOW)
        safe_add(5, 1, ("║        Use [r] in the main TUI to restart" + " " * note_w)[:note_w - 1] + "║", YELLOW)
        safe_add(6, 1, ("╚" + "═" * (note_w - 2) + "╝")[:w - 1], YELLOW)

        # ── Rows ───────────────────────────────────────────────
        display_row = 7
        for i, entry in enumerate(visible):
            if i < scroll_off:
                continue
            if display_row >= h - 4:
                break

            is_cursor = (i == cursor)

            if entry["type"] == "header":
                sec   = entry["section"]
                open_ = sections_open.get(sec, False)
                arrow = "▾" if open_ else "▸"
                prefix = ">" if is_cursor else " "
                text  = f"{prefix}{arrow} {sec}"
                attr  = (YELLOW | curses.A_BOLD | curses.A_REVERSE) if is_cursor else YELLOW
                safe_add(display_row, 0, text[:w], attr)

            else:
                label    = entry["label"]
                key_path = entry["key_path"]
                ftype    = entry["ftype"]
                val      = get_value(key_path)
                tp       = tuple(key_path)
                is_dirty = tp in dirty
                is_danger = any(k in DANGER_KEYS for k in key_path)

                # Format value
                if ftype == FT_BOOL:
                    val_str = ("true" if val else "false") if val is not None else "?"
                else:
                    val_str = str(val) if val is not None else "?"

                # Build line
                label_col = 4
                val_col   = 36
                line_attr = CYAN if is_cursor else NORMAL

                prefix = ">" if is_cursor else " "
                dirty_mark = "*" if is_dirty else " "
                safe_add(display_row, 0, f" {prefix}{dirty_mark}", line_attr)
                safe_add(display_row, label_col, label[:val_col - label_col - 2], line_attr)

                if is_danger:
                    val_attr = RED | curses.A_BOLD if is_cursor else RED
                elif ftype == FT_BOOL:
                    val_attr = GREEN if val else (RED if val is not None else DIM)
                elif ftype == FT_ENUM or ftype == FT_STR:
                    val_attr = CYAN
                else:
                    val_attr = YELLOW

                # Editing inline
                if is_cursor and editing:
                    edit_display = edit_buf + "█"
                    safe_add(display_row, val_col, edit_display[:w - val_col - 1], GREEN)
                else:
                    safe_add(display_row, val_col, val_str[:w - val_col - 1], val_attr)

            display_row += 1

        # ── Note bar ───────────────────────────────────────────
        note_row = h - 4
        safe_add(note_row, 0, "─" * w, DIM)
        if cursor < len(visible):
            entry = visible[cursor]
            if entry["type"] == "field":
                note = FIELD_NOTES.get(tuple(entry["key_path"]), "")
                if not note:
                    note = "No notes for this field."
                note_line = f" {note}"
                safe_add(note_row + 1, 0, note_line[:w], DIM)
            else:
                safe_add(note_row + 1, 0, f" Section: {entry['section']} — press Enter to expand/collapse", DIM)
        else:
            safe_add(note_row + 1, 0, "", DIM)

        # ── Status / footer ────────────────────────────────────
        unsaved = len(dirty)
        if status_msg:
            safe_add(h - 2, 0, f" {status_msg}"[:w], YELLOW)
        elif unsaved:
            safe_add(h - 2, 0, f" ● {unsaved} unsaved change{'s' if unsaved > 1 else ''} — [S] save  [Esc] discard"[:w], YELLOW)
        else:
            safe_add(h - 2, 0, " No unsaved changes"[:w], DIM)

        if editing:
            hint = " [Enter] confirm  [Esc] cancel  [Backspace] clear"
        else:
            hint = " [↑↓] move  [Enter/Space] edit/toggle  [PgUp/Dn] sections  [S] save  [?] docs url  [Q] back"
        safe_add(h - 1, 0, hint[:w], CYAN)

        stdscr.refresh()

        # ── Input ──────────────────────────────────────────────
        stdscr.nodelay(False)
        try:
            key = stdscr.getch()
        except Exception:
            key = -1
        stdscr.nodelay(True)

        status_msg = ""

        if editing:
            entry = visible[cursor] if cursor < len(visible) else None
            if entry and entry["type"] == "field":
                ftype    = entry["ftype"]
                key_path = entry["key_path"]

                if key in (curses.KEY_ENTER, 10, 13):
                    # Confirm edit
                    raw = edit_buf.strip()
                    try:
                        if ftype == FT_INT:
                            parsed = int(raw)
                        elif ftype == FT_FLOAT:
                            parsed = float(raw)
                        else:
                            parsed = raw
                        lo_hi = entry["extra"]
                        if ftype in (FT_INT, FT_FLOAT) and lo_hi:
                            lo, hi = lo_hi
                            if lo is not None and parsed < lo:
                                raise ValueError(f"min {lo}")
                            if hi is not None and parsed > hi:
                                raise ValueError(f"max {hi}")
                        dirty[tuple(key_path)] = parsed
                        editing  = False
                        edit_buf = ""
                        cursor   = min(cursor + 1, len(visible) - 1)
                    except ValueError as e:
                        status_msg = f"Invalid value: {e}"
                        edit_buf   = ""

                elif key == 27:  # Esc
                    editing  = False
                    edit_buf = ""

                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    edit_buf = edit_buf[:-1]

                elif 32 <= key < 127:
                    edit_buf += chr(key)

        else:
            # Navigation mode
            if key in (ord("q"), ord("Q"), 27):
                if dirty:
                    status_msg = "Unsaved changes — press S to save first, or Q again to discard"
                    # Second Q press discards
                    stdscr.nodelay(False)
                    k2 = stdscr.getch()
                    stdscr.nodelay(True)
                    if k2 in (ord("q"), ord("Q")):
                        break
                else:
                    break

            elif key == curses.KEY_UP:
                cursor = max(0, cursor - 1)
                pending_save = False

            elif key == curses.KEY_DOWN:
                cursor = min(len(visible) - 1, cursor + 1)
                pending_save = False

            elif key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bstate = curses.getmouse()
                except curses.error:
                    bstate, my = 0, -1

                btn4 = getattr(curses, "BUTTON4_PRESSED", 0)
                btn5 = getattr(curses, "BUTTON5_PRESSED", 0)

                if btn4 and (bstate & btn4):
                    cursor = max(0, cursor - 1)
                elif btn5 and (bstate & btn5):
                    cursor = min(len(visible) - 1, cursor + 1)
                elif my >= 0:
                    idx_in_view = my - 7
                    if idx_in_view >= 0:
                        target_i = scroll_off + idx_in_view
                        if 0 <= target_i < len(visible):
                            cursor = target_i
                            click_mask = getattr(curses, "BUTTON1_CLICKED", 0) or getattr(curses, "BUTTON1_PRESSED", 0)
                            if bstate & click_mask:
                                entry = visible[cursor]
                                if entry["type"] == "header":
                                    sec = entry["section"]
                                    currently_open = sections_open.get(sec, False)
                                    if not currently_open:
                                        for s in sections_open:
                                            sections_open[s] = False
                                        sections_open[sec] = True
                                    else:
                                        sections_open[sec] = False
                                    new_visible = build_visible()
                                    for i, e in enumerate(new_visible):
                                        if e["type"] == "header" and e["section"] == sec:
                                            cursor = i
                                            break
                                elif entry["ftype"] == FT_BOOL:
                                    tp  = tuple(entry["key_path"])
                                    cur = get_value(entry["key_path"])
                                    dirty[tp] = not cur
                                elif entry["ftype"] == FT_ENUM:
                                    tp      = tuple(entry["key_path"])
                                    cur     = get_value(entry["key_path"])
                                    choices = entry["extra"] or []
                                    idx = (choices.index(cur) + 1) % len(choices) if cur in choices else 0
                                    dirty[tp] = choices[idx]
                                else:
                                    editing  = True
                                    edit_buf = str(get_value(entry["key_path"]) or "")
                                    
            elif key == curses.KEY_PPAGE:
                # Jump to previous section header
                for i in range(cursor - 1, -1, -1):
                    if visible[i]["type"] == "header":
                        cursor = i
                        break

            elif key == curses.KEY_NPAGE:
                # Jump to next section header
                for i in range(cursor + 1, len(visible)):
                    if visible[i]["type"] == "header":
                        cursor = i
                        break

            elif key in (curses.KEY_ENTER, 10, 13):
                entry = visible[cursor] if cursor < len(visible) else None
                if entry:
                    if entry["type"] == "header":
                        sec = entry["section"]
                        currently_open = sections_open.get(sec, False)
                        if not currently_open:
                            for s in sections_open:
                                sections_open[s] = False
                            sections_open[sec] = True
                        else:
                            sections_open[sec] = False
                        new_visible = build_visible()
                        for i, e in enumerate(new_visible):
                            if e["type"] == "header" and e["section"] == sec:
                                cursor = i
                                break
                    elif entry["ftype"] == FT_BOOL:
                        # Toggle directly
                        tp  = tuple(entry["key_path"])
                        cur = get_value(entry["key_path"])
                        dirty[tp] = not cur
                    elif entry["ftype"] == FT_ENUM:
                        # Cycle through options
                        tp      = tuple(entry["key_path"])
                        cur     = get_value(entry["key_path"])
                        choices = entry["extra"] or []
                        if cur in choices:
                            idx = (choices.index(cur) + 1) % len(choices)
                        else:
                            idx = 0
                        dirty[tp] = choices[idx]
                    else:
                        # Start text edit
                        editing  = True
                        edit_buf = str(get_value(entry["key_path"]) or "")

            elif key == ord(" "):
                # Space: same as Enter for bool/enum
                entry = visible[cursor] if cursor < len(visible) else None
                if entry and entry["type"] == "field":
                    if entry["ftype"] == FT_BOOL:
                        tp  = tuple(entry["key_path"])
                        cur = get_value(entry["key_path"])
                        dirty[tp] = not cur
                    elif entry["ftype"] == FT_ENUM:
                        tp      = tuple(entry["key_path"])
                        cur     = get_value(entry["key_path"])
                        choices = entry["extra"] or []
                        if cur in choices:
                            idx = (choices.index(cur) + 1) % len(choices)
                        else:
                            idx = 0
                        dirty[tp] = choices[idx]

            elif key in (ord("s"), ord("S")):
                # Save — two-stage for danger fields (no blocking getch)
                danger_fields = [k for k in dirty if any(part in DANGER_KEYS for part in k)]
                if danger_fields and not pending_save:
                    pending_save = True
                    status_msg = "⚠ Danger field modified — press [S] again to confirm save"
                else:
                    pending_save = False
                    for key_path_tuple, value in dirty.items():
                        cfg_set(cfg, list(key_path_tuple), value)
                    try:
                        save_config(cfg)
                        dirty      = {}
                        status_msg = "Saved. Restart affected components ([r] in main TUI) for changes to take effect."
                        mlog("config saved via editor")
                    except Exception as e:
                        status_msg = f"Save failed: {e}"
                        mlog(f"config save error: {e}")

            elif key == ord("?"):
                status_msg = "Docs: https://nolaptoptrades.com/docs/config"

            elif key in (ord("v"), ord("V")):
                status_msg = "Video: https://nolaptoptrades.com/docs/config"

    curses.curs_set(0)


# ═══════════════════════════════════════════════════════════════
# MAIN TUI DRAW LOOP
# ═══════════════════════════════════════════════════════════════

def draw(stdscr):
    global log_view_index, running

    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()

    curses.init_pair(1, curses.COLOR_GREEN,  -1)
    curses.init_pair(2, curses.COLOR_RED,    -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_CYAN,   -1)
    curses.init_pair(5, curses.COLOR_WHITE,  -1)
    curses.init_pair(6, -1,                  -1)  # DIM — terminal default

    GREEN  = curses.color_pair(1) | curses.A_BOLD
    RED    = curses.color_pair(2) | curses.A_BOLD
    YELLOW = curses.color_pair(3) | curses.A_BOLD
    CYAN   = curses.color_pair(4) | curses.A_BOLD
    NORMAL = curses.color_pair(5)
    DIM    = curses.color_pair(6)

    colors = {"GREEN": GREEN, "RED": RED, "YELLOW": YELLOW, "CYAN": CYAN, "NORMAL": NORMAL, "DIM": DIM}

    script_names = [s["name"] for s in SCRIPTS]
    if LOG_VIEW_INIT in script_names:
        log_view_index = script_names.index(LOG_VIEW_INIT)

    key_to_index = {s["key"]: i for i, s in enumerate(SCRIPTS)}

    # Kill-all confirm state
    kill_confirm = False
    kill_confirm_ts = 0.0

    def status_color(status):
        if status == "RUNNING":               return GREEN,  "●"
        if status == "HALTED":                return YELLOW, "⏸"
        if status in ("CRASHED", "FAILED",
                      "ERROR", "NOT FOUND"):  return RED,    "✖"
        if status in ("STOPPING", "STOPPED"): return YELLOW, "○"
        return NORMAL, "?"

    def safe_add(row, col, text, attr=None):
        h, w = stdscr.getmaxyx()
        if row < 0 or row >= h or col < 0:
            return
        text = text[:max(0, w - col - 1)].replace("\x00", "")
        try:
            if attr is not None:
                stdscr.addstr(row, col, text, attr)
            else:
                stdscr.addstr(row, col, text)
        except curses.error:
            pass

    while running:
        # ── Build state snapshot ───────────────────────────────
        with state_lock:
            snap = {n: dict(v) for n, v in state.items()}
        with stats_lock:
            sc = dict(stats_cache)

        h, w = stdscr.getmaxyx()
        ts   = datetime.now().strftime("%m-%d %H:%M:%S")
        view_name = script_names[log_view_index % len(script_names)]

        # ── Render ─────────────────────────────────────────────
        try:
            stdscr.erase()

            if h < 14 or w < 30:
                safe_add(0, 0, "Terminal too small", YELLOW)
                stdscr.refresh()
                time.sleep(REFRESH_SEC)
                continue


            portrait = w < 72   # below 72 cols use compact layout
            row = 0

            # Header
            safe_add(row, 0, "═" * w, CYAN); row += 1
            safe_add(row, 0, f" ALPHA SNIPER  {ts} ".center(w)[:w], CYAN); row += 1
            safe_add(row, 0, "═" * w, CYAN); row += 1

            # ── Pipeline scripts ───────────────────────────────
            pipeline = [s for s in SCRIPTS if s.get("group") == GROUP_PIPELINE]
            for s in pipeline:
                row = _render_script_row(stdscr, safe_add, s, snap, view_name, portrait, row, h, w,
                                         GREEN, RED, YELLOW, CYAN, NORMAL, DIM, status_color)

            # ── Separator + background ─────────────────────────
            if row < h - 3:
                safe_add(row, 0, "┄" * w, DIM); row += 1

            background = [s for s in SCRIPTS if s.get("group") == GROUP_BACKGROUND]
            for s in background:
                row = _render_script_row(stdscr, safe_add, s, snap, view_name, portrait, row, h, w,
                                         GREEN, RED, YELLOW, CYAN, NORMAL, DIM, status_color,
                                         tag="bg")

            # ── Separator + optional ───────────────────────────
            optional = [s for s in SCRIPTS if s.get("group") == GROUP_OPTIONAL]
            if optional:
                if row < h - 3:
                    safe_add(row, 0, "┄" * w, DIM); row += 1
                for s in optional:
                    row = _render_script_row(stdscr, safe_add, s, snap, view_name, portrait, row, h, w,
                                             GREEN, RED, YELLOW, CYAN, NORMAL, DIM, status_color,
                                             tag="opt")

            # ── Stats ──────────────────────────────────────────
            if row < h - 3:
                safe_add(row, 0, "─" * w, DIM); row += 1
            if row < h - 3:
                line1 = f" Eval:{sc['evaluated']} BUY:{sc['buy']} SKIP:{sc['skip']} Open:{sc['open']}"
                safe_add(row, 0, line1[:w], CYAN); row += 1
            if row < h - 3:
                line2 = f" TP:{sc['tp']} SL:{sc['sl']} TSL:{sc['tsl']} RUG:{sc['rug']} TO:{sc['timeout']} $MISS:{sc['pricemiss']} CLOSE:{sc['close']}"
                safe_add(row, 0, line2[:w], CYAN); row += 1

            # ── Log view ───────────────────────────────────────
            if row < h - 3:
                safe_add(row, 0, "─" * w, DIM); row += 1
            if row < h - 3:
                log_label = f" LOGS — {view_name} "
                safe_add(row, 0, log_label[:w], CYAN); row += 1

            with state_lock:
                log_buf = list(snap.get(view_name, {}).get("log_buf", []))
            if not log_buf:
                with manager_log_lock:
                    log_buf = list(manager_log)[-LOG_LINES:]

            max_log_rows = h - row - 2
            for line in log_buf[-max(1, max_log_rows):]:
                if row < h - 2:
                    safe_add(row, 0, (" " + line)[:w - 1], NORMAL); row += 1

            # ── Kill confirm overlay ───────────────────────────
            if kill_confirm:
                elapsed = now_ts() - kill_confirm_ts
                if elapsed > 5:
                    kill_confirm = False
                else:
                    msg = f" KILL ALL? Press Ctrl+K again to confirm ({int(5 - elapsed)}s) "
                    safe_add(h - 2, max(0, (w - len(msg)) // 2), msg[:w], RED)

            # ── Footer ─────────────────────────────────────────
            footer = "[#]view [r]restart [h]halt [C-A]start all [C-P]pause all [C-E]config [C-K]kill [q]quit"
            safe_add(h - 1, 0, footer.center(w)[:w - 1], CYAN)

            stdscr.refresh()

        except curses.error:
            try:
                stdscr.clear()
                curses.flushinp()
            except Exception:
                pass

        # ── Key input ──────────────────────────────────────────
        try:
            key = stdscr.getch()
            curses.flushinp()
        except Exception:
            key = -1

        if key == -1:
            time.sleep(REFRESH_SEC)
            continue


        # ── q — graceful quit ──────────────────────────────────
        if key == ord("q"):
            running = False
            break

        # ── Ctrl+A (1) — start all pipeline ───────────────────
        elif key == 1:
            def _start_all_pipeline():
                try:
                    _cfg = load_config()
                    OPTIONAL_AUTO_START = _cfg.get("manager", {}).get("optional_auto_start", {})
                except Exception:
                    OPTIONAL_AUTO_START = {}
                for s in SCRIPTS:
                    if s.get("group") == GROUP_PIPELINE:
                        with state_lock:
                            st = state[s["name"]].get("status")
                        if st not in ("RUNNING",):
                            start_script(s["name"])
                            time.sleep(STARTUP_DELAY_SEC)
                    elif s.get("group") == GROUP_OPTIONAL:
                        if OPTIONAL_AUTO_START.get(s["name"], False):
                            with state_lock:
                                st = state[s["name"]].get("status")
                            if st not in ("RUNNING",):
                                start_script(s["name"])
                                time.sleep(STARTUP_DELAY_SEC)
            threading.Thread(target=_start_all_pipeline, daemon=True).start()
            mlog("Ctrl+A — starting all pipeline scripts")

        # ── Ctrl+P (16) — pause all pipeline ──────────────────
        elif key == 16:
            def _pause_all_pipeline():
                for s in SCRIPTS:
                    if s.get("group") == GROUP_PIPELINE:
                        with state_lock:
                            st = state[s["name"]].get("status")
                        if st == "RUNNING":
                            halt_script(s["name"])
            threading.Thread(target=_pause_all_pipeline, daemon=True).start()
            mlog("Ctrl+P — halting all pipeline scripts")

        # ── Ctrl+E (5) — config editor ─────────────────────────
        elif key == 5:
            run_config_editor(stdscr, colors)
            curses.curs_set(0)
            stdscr.nodelay(True)
            curses.flushinp()
            stdscr.clear()

        # ── Ctrl+K (11) — kill all + quit (confirm) ───────────
        elif key == 11:
            if kill_confirm and (now_ts() - kill_confirm_ts) < 5:
                running = False
                break
            else:
                kill_confirm    = True
                kill_confirm_ts = now_ts()

        # ── r — restart viewed script ──────────────────────────
        elif key == ord("r"):
            name  = script_names[log_view_index % len(script_names)]
            group = next((s.get("group") for s in SCRIPTS if s["name"] == name), GROUP_PIPELINE)
            if group == GROUP_BACKGROUND:
                mlog(f"[TUI] {name} is always-on — cannot manually restart")
            else:
                threading.Thread(target=restart_script, args=(name,), daemon=True).start()

        # ── h — halt / resume viewed script ───────────────────
        elif key == ord("h"):
            name  = script_names[log_view_index % len(script_names)]
            group = next((s.get("group") for s in SCRIPTS if s["name"] == name), GROUP_PIPELINE)
            if group == GROUP_BACKGROUND:
                mlog(f"[TUI] {name} is always-on — cannot halt")
            else:
                with state_lock:
                    cur_status = state[name].get("status")
                if cur_status == "HALTED":
                    threading.Thread(target=start_script, args=(name,), daemon=True).start()
                    mlog(f"resuming {name}")
                elif cur_status == "RUNNING":
                    threading.Thread(target=halt_script, args=(name,), daemon=True).start()

        # ── number keys — jump log view ────────────────────────
        else:
            char = chr(key) if 0 <= key < 256 else ""
            if char in key_to_index:
                log_view_index = key_to_index[char]

        time.sleep(REFRESH_SEC)


def _handle_key(key, script_names, key_to_index, kill_confirm, kill_confirm_ts, stdscr, colors):
    """Thin wrapper — key handling called from no-redraw path."""
    pass  # key processing is inline in draw() above; this avoids duplication for the dirty-check fast path


def _render_script_row(stdscr, safe_add, s, snap, view_name, portrait, row, h, w,
                        GREEN, RED, YELLOW, CYAN, NORMAL, DIM, status_color, tag=None):
    """Render one script row. Returns updated row index."""
    name      = s["name"]
    key_ch    = s["key"]
    info      = snap.get(name, {})
    status    = info.get("status", "UNKNOWN")
    start_ts  = info.get("start_ts")
    restarts  = info.get("restarts", 0)
    uptime    = format_uptime(start_ts) if start_ts else "—"
    scol, sym = status_color(status)
    is_active = (name == view_name)
    row_attr  = CYAN if is_active else NORMAL

    if row >= h - 3:
        return row

    if portrait:
        name_trunc = name[:12]
        safe_add(row, 0,  f"[{key_ch}] {name_trunc:<12} ", row_attr)
        safe_add(row, 17, f"{sym} {status[:7]:<7} ",        scol)
        safe_add(row, 26, uptime[:8],                        NORMAL)
        if restarts > 0 and w > 36:
            safe_add(row, w - 5, f"R:{restarts}", YELLOW)
        if tag == "bg" and w > 38:
            safe_add(row, w - 9, "⚙always", DIM)
        if tag == "opt" and w > 38:
            safe_add(row, w - 8, "optional", DIM)
    else:
        col_key    = 1
        col_name   = 5
        col_status = 22
        col_uptime = 36
        col_extra  = min(48, w - 14)   # clamp so tag always fits

        safe_add(row, col_key,    f"[{key_ch}]",           row_attr)
        safe_add(row, col_name,   name[:16],                row_attr)
        safe_add(row, col_status, f"{sym} {status}"[:13],  scol)
        safe_add(row, col_uptime, uptime[:10],              NORMAL)

        if restarts > 0 and col_extra + 6 < w:
            safe_add(row, col_extra, f"R:{restarts}", YELLOW)

        tag_col = col_extra + 6 if restarts > 0 else col_extra
        if tag == "bg" and tag_col + 10 < w:
            safe_add(row, tag_col, "⚙ always-on", DIM)
        elif tag == "opt" and tag_col + 8 < w:
            safe_add(row, tag_col, "optional", DIM)

    return row + 1


# ═══════════════════════════════════════════════════════════════
# HEADLESS MODE (for server.py / PWA integration)
# ═══════════════════════════════════════════════════════════════

def start_headless(auto_start: bool = False):
    """Start manager in headless mode. Called by server.py."""
    global running
    running = True

    for s in SCRIPTS:
        try:
            result = subprocess.run(["pgrep", "-f", s["path"]], capture_output=True, text=True)
            if result.stdout.strip():
                pids = result.stdout.strip().split("\n")
                print(f"[manager] WARNING: {s['name']} already running (pid={','.join(pids)})")
        except Exception:
            pass

    init_state()

    for s in SCRIPTS:
        threading.Thread(target=tail_log, args=(s["name"], s["log"]), daemon=True).start()
    threading.Thread(target=stats_loop,   daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()

    # Always-on background scripts start immediately
    for s in SCRIPTS:
        if s.get("always_on") or s.get("group") == GROUP_BACKGROUND:
            threading.Thread(target=start_script, args=(s["name"],), daemon=True).start()
            print(f"[manager] Auto-started background: {s['name']}")

    if auto_start:
        for s in SCRIPTS:
            if s.get("group") == GROUP_PIPELINE:
                threading.Thread(target=start_script, args=(s["name"],), daemon=True).start()
                time.sleep(STARTUP_DELAY_SEC)
        print("[manager] Headless — auto-started pipeline")
    else:
        print("[manager] Headless — background scripts running, pipeline halted (awaiting PWA commands)")


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main(dry_run: bool = False):
    global running

    # Check for already-running scripts
    any_running = False
    for s in SCRIPTS:
        try:
            result = subprocess.run(["pgrep", "-f", s["path"]], capture_output=True, text=True)
            if result.stdout.strip():
                pids = result.stdout.strip().split("\n")
                print(f"WARNING: {s['name']} already running (pid={','.join(pids)})")
                any_running = True
        except Exception:
            pass

    if any_running:
        print("\nExisting scripts detected. They will appear in the TUI.")
        print("Use [h] in the TUI to halt them if needed.\n")
        time.sleep(1)

    init_state()

    # Start threads
    for s in SCRIPTS:
        threading.Thread(target=tail_log, args=(s["name"], s["log"]), daemon=True).start()
    threading.Thread(target=stats_loop,   daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()

    # Auto-start background (always-on) scripts
    for s in SCRIPTS:
        if s.get("always_on") or s.get("group") == GROUP_BACKGROUND:
            threading.Thread(target=start_script, args=(s["name"],), daemon=True).start()
            mlog(f"auto-started background: {s['name']}")
            time.sleep(0.2)

    # Sync preference fallback — fires if user skipped cli.py
    try:
        from sync_prefs import prompt_sync_preference
        prompt_sync_preference()
    except Exception:
        pass

    # Launch TUI — no ENTER gate
    try:
        curses.wrapper(draw)
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        print("\nShutting down...")
        stop_all()
        print("Done.")


if __name__ == "__main__":
    main()
