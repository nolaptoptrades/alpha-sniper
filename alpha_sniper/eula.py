"""
eula.py — NLT Alpha Sniper
Single source of truth for all legal text, disclaimers, and terms.

Imported by:
    cli.py              — EULA display on first run
    bridge_bot.py       — signal_footer()

Never edit disclaimer text in other files — always update here.
"""

from paths import get_config

# ============================================================
# VERSIONS
# ============================================================

try:
    _cfg        = get_config()
    APP_VERSION = _cfg["_meta"]["version"]
except Exception:
    APP_VERSION = "unknown"

EULA_VERSION = "1.1"

# ============================================================
# URLS
# ============================================================

TERMS_URL = "https://nolaptoptrades.com/terms"
CONTACT   = "@nolaptoptrades on Telegram"

# ============================================================
# FULL EULA — shown in CLI on first run or version change
# ============================================================

EULA_DISPLAY = f"""
╔══════════════════════════════════════════════════════════════╗
║           NLT ALPHA SNIPER — TERMS & DISCLAIMER              ║
║                      Please read carefully                   ║
╚══════════════════════════════════════════════════════════════╝

RESEARCH & EDUCATIONAL USE ONLY
  This software is provided for research and educational purposes
  only. It does not constitute financial advice, investment advice,
  or a recommendation to buy or sell any asset.

PAPER TRADING SIMULATION
  NLT Alpha Sniper is a paper trading tool. It simulates trades
  using real market data but does not execute real transactions
  or move real funds. Simulated results do not reflect actual
  trading outcomes and carry no guarantee of future performance.

SIGNAL DELIVERY FEATURE
  The optional Telegram signal bot delivers research signals
  based on configured rules (TP/SL/momentum thresholds set by
  you). These signals are informational only. Acting on any
  signal is entirely your decision and at your own risk.
  NoLaptopTrades accepts no liability for losses incurred from
  acting on any signal delivered by this software.

OPEN SOURCE
  NLT Alpha Sniper is open source software licensed under the
  MIT License. You are free to use, modify, and distribute it
  under the terms of that license.

DATA COLLECTION & SHARING
  Sync is opt-in. When enabled, anonymized trade simulation data
  is contributed to the NLT aggregate dataset. This data contains
  trade outcomes and entry conditions only — no personal
  information, no API keys, and no wallet addresses are ever
  transmitted. Users who sync gain access to aggregate network
  analytics via --netstats.

WHAT WE NEVER COLLECT
  - API keys (stored locally on your device only)
  - Wallet addresses
  - Real trading activity or fund balances
  - Any data from your local machine beyond trade simulation

RISK WARNING
  Cryptocurrency trading carries extreme risk. You can lose
  your entire investment. Only trade with funds you can afford
  to lose completely. Past performance of any signal, strategy,
  or simulated result does not predict future outcomes.

LIABILITY
  NoLaptopTrades, its developers, and contributors accept no
  liability for any financial losses, damages, or consequences
  arising from the use of this software, its signals, or any
  action taken based on its output.

  Full terms: {TERMS_URL}
  Contact:    {CONTACT}

  Version {APP_VERSION} — Terms v{EULA_VERSION}
"""

# ============================================================
# SHORT SUMMARY — shown in --help
# ============================================================

TERMS_SUMMARY = (
    f"NLT Alpha Sniper v{APP_VERSION} — research tool, not financial advice.\n"
    f"Paper trading only. No real funds. Trade at your own risk.\n"
    f"Terms: {TERMS_URL}  |  Contact: {CONTACT}"
)

# ============================================================
# TELEGRAM BOT SIGNAL FOOTER
# ============================================================

def signal_footer() -> str:
    """
    Return disclaimer footer for Telegram bot signal messages.
    Reads current trade rules from config.json.
    """
    try:
        cfg      = get_config()
        rules    = cfg.get("trade_rules", {})
        tp       = rules.get("tp_pct",       45)
        sl       = rules.get("sl_pct",       -18)
        hold_sec = rules.get("max_hold_sec", 2700)
        hold_min = int(hold_sec / 60)
    except Exception:
        tp, sl, hold_min = 45, -18, 45

    return (
        f"\n─────────────────────────\n"
        f"⚠ Research signal only. Not financial advice.\n"
        f"Rules: TP {tp}% / SL {sl}% / Max hold {hold_min}min\n"
        f"Trade at your own risk. {TERMS_URL}"
    )
