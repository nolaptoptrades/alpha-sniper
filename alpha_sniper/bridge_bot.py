#!/usr/bin/env python3
"""
bridge_bot.py — Alpha Sniper V2 (OPTIONAL)

Tails entry_verdicts.jsonl for BUY verdicts and sends formatted
signal messages to your Telegram chat via a bot.

SETUP (one time):
  1. Message @BotFather on Telegram, create a bot
  2. Copy the bot token
  3. Message @userinfobot to get your chat ID
  4. Add to your .env file:
       TELEGRAM_BOT_TOKEN=your_token
       TELEGRAM_CHAT_ID=your_chat_id

Enable in config.json:
  "bridge": {
    "enabled": true,
    "mode": "bot"
  }

Author: NoLaptopTrades
"""

import html
import json
import os
import time
from datetime import datetime, timezone

import requests

from paths import get_config, get_path

# ============================================================
# CONFIG
# ============================================================

# Load .env before reading tokens — same pattern as rest of pipeline
try:
    from dotenv import load_dotenv
    _cfg      = get_config()
    _env_path = os.path.join(_cfg.get("base_dir", ""), ".env")
    load_dotenv(_env_path)
except Exception:
    pass

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# Paths
VERDICTS_PATH     = get_path("verdicts")
OFFSET_PATH       = os.path.join(get_path("state"), "bridge_bot_offset.txt")

POLL_INTERVAL_SEC = 3
SEND_DELAY_SEC    = 1


# ============================================================
# HELPERS
# ============================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def _now_local() -> str:
    return datetime.now().strftime("%H:%M:%S")

def _log(msg: str):
    print(f"[bridge_bot] {_now_local()}  {msg}")


def _get_offset() -> int:
    try:
        with open(OFFSET_PATH) as f:
            return int(f.read().strip())
    except Exception:
        # First run — start from end of file so we don't replay old signals
        try:
            with open(VERDICTS_PATH) as f:
                return len(f.readlines())
        except Exception:
            return 0


def _save_offset(n: int):
    os.makedirs(os.path.dirname(OFFSET_PATH), exist_ok=True)
    tmp = OFFSET_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(n))
    os.replace(tmp, OFFSET_PATH)


def _read_new_buys(offset: int):
    """Read entry_verdicts.jsonl from offset. Returns (buys, new_offset)."""
    buys       = []
    new_offset = offset
    try:
        with open(VERDICTS_PATH, "r") as f:
            lines = f.readlines()
        for line in lines[offset:]:
            line = line.strip()
            if line:
                try:
                    rec = json.loads(line)
                    if rec.get("verdict") == "BUY":
                        buys.append(rec)
                except Exception:
                    pass
            new_offset += 1
    except FileNotFoundError:
        pass
    return buys, new_offset


def _send_message(text: str) -> bool:
    """Send a single Telegram message. Returns True on success."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code == 200:
            return True
        _log(f"send failed — HTTP {r.status_code}: {r.text[:300]}")
        return False
    except Exception as e:
        _log(f"send error — {e}")
        return False


def _send_signal(rec: dict):
    """Format and send a BUY signal to Telegram."""
    mint        = rec.get("mint", "unknown")
    score       = rec.get("score", "?")
    trigger     = rec.get("trigger", "?")
    reasons     = rec.get("reasons", [])[:3]
    reasons_str = "\n".join(f"• {html.escape(r)}" for r in reasons)

    diagnostics = rec.get("diagnostics") or {}
    liq_raw     = diagnostics.get("liq_end")
    mom_raw     = diagnostics.get("mom_end")
    bsr_raw     = diagnostics.get("bsr_end")

    liq_str = f"${int(liq_raw/1000)}K" if liq_raw else "?"
    mom_str = f"{mom_raw:.1f}%" if mom_raw is not None else "?"
    bsr_str = f"{bsr_raw:.2f}" if bsr_raw is not None else "?"

    dex_url     = f"https://dexscreener.com/solana/{mint}"
    solscan_url = f"https://solscan.io/token/{mint}"

    msg = (
        f"🟢 <b>BUY SIGNAL</b>\n"
        f"Score: {score}/100 | Trigger: {trigger}\n"
        f"{reasons_str}\n\n"
        f"Liq: {liq_str}  Mom: {mom_str}  BSR: {bsr_str}\n\n"
        f"<b>Mint</b> <i>(tap to copy)</i>\n"
        f"<code>{mint}</code>\n"
        f'🔍 <a href="{dex_url}">DexScreener</a>  '
        f'🔎 <a href="{solscan_url}">Solscan</a>'
    )

    if _send_message(msg):
        _log(f"signal sent — score={score} trigger={trigger}")


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN or not CHAT_ID:
        _log("ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env")
        _log("  → Message @BotFather to create a bot, @userinfobot for your chat ID")
        return

    _log(f"ready — polling verdicts every {POLL_INTERVAL_SEC}s")
    offset = _get_offset()
    _log(f"starting from offset {offset}")

    last_heartbeat = time.time()
    HEARTBEAT_SEC  = 600   # log "still watching" every 10 min during silence

    while True:
        try:
            buys, new_offset = _read_new_buys(offset)

            for rec in buys:
                _send_signal(rec)
                last_heartbeat = time.time()   # reset on activity
                time.sleep(SEND_DELAY_SEC)

            if new_offset != offset:
                offset = new_offset
                _save_offset(offset)

        except Exception as e:
            _log(f"loop error — {e}")

        # Heartbeat — reassurance during long quiet periods
        if time.time() - last_heartbeat >= HEARTBEAT_SEC:
            _log("watching — no signals in last 10min")
            last_heartbeat = time.time()

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
