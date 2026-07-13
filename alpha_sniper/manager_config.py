"""
manager_config.py — NLT Alpha Sniper

All settings for the Alpha Sniper process manager.
Edit this file to change behaviour — never need to touch manager.py.

Paths are sourced from config.json via paths.py.
Never hardcode BASE or log paths here.
"""

import os
import sys

# ── Resolve paths from config.json ────────────────────────────
try:
    from paths import get_config, get_path
    cfg      = get_config()
    BASE     = cfg["paths"]["base"]
    LOGS_DIR = cfg["paths"]["logs"]
except Exception as e:
    print(f"[manager_config] ERROR: could not load config.json — {e}")
    sys.exit(1)

# ── Scripts — three groups ─────────────────────────────────────
#
# GROUP 1: PIPELINE  — started by "start all", user-controlled halt/resume
# GROUP 2: BACKGROUND — always-on, auto-restart, no kill button
# GROUP 3: OPTIONAL  — user-controlled, excluded from "start all"
#
# Fields:
#   key        str   — number key to jump log view
#   name       str   — display name (must match component names exactly)
#   path       str   — absolute path to script
#   log        str   — absolute path to .log file
#   group      str   — "pipeline" | "background" | "optional"
#   always_on  bool  — auto-start + unlimited restarts (background group)

SCRIPTS = [
    {
        "key":   "1",
        "name":  "Discovery",
        "path":  os.path.join(BASE, "discovery.py"),
        "log":   os.path.join(LOGS_DIR, "discovery.log"),
        "group": "pipeline",
    },
    {
        "key":   "2",
        "name":  "Safety",
        "path":  os.path.join(BASE, "safety.py"),
        "log":   os.path.join(LOGS_DIR, "safety.log"),
        "group": "pipeline",
    },
    {
        "key":   "3",
        "name":  "Brain",
        "path":  os.path.join(BASE, "brain.py"),
        "log":   os.path.join(LOGS_DIR, "brain.log"),
        "group": "pipeline",
    },
    {
        "key":   "4",
        "name":  "Simulator",
        "path":  os.path.join(BASE, "simulator.py"),
        "log":   os.path.join(LOGS_DIR, "simulator.log"),
        "group": "pipeline",
    },
    {
        "key":       "9",
        "name":      "PostMortem",
        "path":      os.path.join(BASE, "post_mortem.py"),
        "log":       os.path.join(LOGS_DIR, "post_mortem.log"),
        "group":     "background",
        "always_on": True,
    },
    {
        "key":   "5",
        "name":  "Brain Wallet",
        "path":  os.path.join(BASE, "brain_w.py"),
        "log":   os.path.join(LOGS_DIR, "brain_w.log"),
        "group": "optional",
    },
    {
        "key":   "6",
        "name":  "Bridge-Bot",
        "path":  os.path.join(BASE, "bridge_bot.py"),
        "log":   os.path.join(LOGS_DIR, "bridge_bot.log"),
        "group": "optional",
    },
]
# ── Optional script auto-start ───────────────────────────────────
# Which optional scripts start automatically with Ctrl+A (start all)
OPTIONAL_AUTO_START          = {'Bridge-Bot': False}



# ── Startup ────────────────────────────────────────────────────
STARTUP_DELAY_SEC = 0.5       # seconds between starting each pipeline script

# ── Crash behaviour ────────────────────────────────────────────
# "restart+alert"  — auto-restart AND send Telegram alert
# "restart_only"   — auto-restart silently
# "alert_only"     — alert only, no auto-restart
# "nothing"        — log and do nothing
ON_CRASH             = "restart+alert"
RESTART_COOLDOWN_SEC = 10     # wait before restarting a crashed script
MAX_RESTARTS         = 5      # give up after this many attempts in a row
RESTART_RESET_SEC    = 300    # reset counter if script was stable this long

# ── TUI display ────────────────────────────────────────────────
REFRESH_SEC   = 1             # TUI refresh rate in seconds
LOG_LINES                = 40  # log lines shown per script
LOG_VIEW_INIT = "Discovery"   # which script log to show on startup

# ── Stats paths ────────────────────────────────────────────────
VERDICTS_PATH     = get_path("verdicts")
PAPER_TRADES_PATH = get_path("paper_trades")

# ── Help URLs (update when content is live) ────────────────────
HELP_DOCS_URL  = "https://nolaptoptrades.com/docs/config"
HELP_VIDEO_URL = "https://youtube.com/watch?v=TODO"

# ── Telegram alerts ────────────────────────────────────────────
try:
    import dotenv
    _env_path = os.path.join(cfg["base_dir"], ".env")
    dotenv.load_dotenv(_env_path)
except ImportError:
    pass

ALERT_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALERT_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
