#!/usr/bin/env python3
"""
bridge_telethon.py — Alpha Sniper V2 (OPTIONAL)

Tails entry_verdicts.jsonl for BUY verdicts and forwards
the mint address as a plain text message to @maestro bot via DM.
Maestro receives it as if you typed it manually → auto-buys.

SETUP (one time):
  1. Go to https://my.telegram.org/apps
  2. Create an app — get API_ID and API_HASH
  3. Add to your .env file:
       TELEGRAM_API_ID=your_id
       TELEGRAM_API_HASH=your_hash
  4. First run: enter phone number + verification code
  5. Session saved to state/bridge_session.session — never asks again

REQUIRES: pip install telethon

Enable in config.json:
  "bridge": {
    "enabled": true,
    "maestro_username": "maestro"
  }

Author: NoLaptopTrades
"""

import json
import os
import time
from datetime import datetime, timezone

from telethon.sync import TelegramClient

from paths import get_config, get_path

# ============================================================
# CONFIG
# ============================================================

# paths.py already loaded .env — keys are in os.environ
API_ID   = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")

# Bridge config from config.json
BRIDGE_CFG = get_config().get("bridge") or {}
MAESTRO_USERNAME = BRIDGE_CFG.get("maestro_username", "maestro")

# Paths
VERDICTS_PATH = get_path("verdicts")
OFFSET_PATH   = os.path.join(get_path("state"), "bridge_maestro_offset.txt")
SESSION_PATH  = os.path.join(get_path("state"), "bridge_maestro_session")
LOG_PATH      = os.path.join(get_path("logs"), "bridge_maestro.log")

# Settings
POLL_INTERVAL_SEC = 3
SEND_DELAY_SEC    = 1


# ============================================================
# HELPERS
# ============================================================

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _log(msg: str):
    line = f"[bridge_maestro] {_now_iso()} {msg}"
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _get_offset() -> int:
    try:
        with open(OFFSET_PATH) as f:
            return int(f.read().strip())
    except Exception:
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
    """Read entry_verdicts.jsonl from offset, return list of BUY mints and new offset."""
    buys = []
    new_offset = offset
    try:
        with open(VERDICTS_PATH, "r") as f:
            lines = f.readlines()
        new_lines = lines[offset:]
        for line in new_lines:
            line = line.strip()
            if not line:
                new_offset += 1
                continue
            try:
                rec = json.loads(line)
                if rec.get("verdict") == "BUY":
                    mint = rec.get("mint")
                    if mint:
                        buys.append({
                            "mint": mint,
                            "score": rec.get("score"),
                            "trigger": rec.get("trigger"),
                        })
                        _log(f"queued mint={mint[:16]}… score={rec.get('score')} trigger={rec.get('trigger')}")
            except Exception:
                pass
            new_offset += 1
    except FileNotFoundError:
        pass
    return buys, new_offset


# ============================================================
# MAIN
# ============================================================

def main():
    if not API_ID or not API_HASH:
        _log("ERROR: TELEGRAM_API_ID or TELEGRAM_API_HASH not set in .env")
        _log("  Go to https://my.telegram.org/apps to get your credentials.")
        return

    _log("starting — connecting to Telegram")

    os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)

    with TelegramClient(SESSION_PATH, API_ID, API_HASH) as client:
        _log("connected to Telegram")

        # Resolve Maestro entity once at startup
        try:
            maestro = client.get_entity(MAESTRO_USERNAME)
            _log(f"resolved @{MAESTRO_USERNAME} OK")
        except Exception as e:
            _log(f"ERROR resolving @{MAESTRO_USERNAME}: {e}")
            return

        offset = _get_offset()
        _log(f"starting from verdict offset={offset}")

        while True:
            try:
                buys, new_offset = _read_new_buys(offset)

                for buy in buys:
                    try:
                        client.send_message(maestro, buy["mint"])
                        _log(f"sent to @{MAESTRO_USERNAME}: {buy['mint'][:16]}…")
                        time.sleep(SEND_DELAY_SEC)
                    except Exception as e:
                        _log(f"ERROR sending {buy['mint'][:16]}: {e}")

                if new_offset != offset:
                    offset = new_offset
                    _save_offset(offset)

            except Exception as e:
                _log(f"ERROR in main loop: {e}")

            time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
