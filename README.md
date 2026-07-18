<div align="center">
  <h1>NLT Alphas</h1>
  <p><strong>Assess before you commit.</strong></p>
  <p>Solana memecoin market analysis and paper trading pipeline.</p>

  ![Version](https://img.shields.io/badge/version-1.0.0-blue)
  ![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Termux%20%7C%20Windows-green)
  ![License](https://img.shields.io/badge/license-MIT-brightgreen)
  ![Python](https://img.shields.io/badge/python-3.10+-yellow)
</div>

---

## What is NLT Alphas?

NLT Alphas is a **Solana graduation sniper pipeline** — a terminal-first
market analysis and paper trading tool that monitors PumpSwap graduation events,
filters for quality setups across multiple dimensions, and simulates trades in real time.

It answers one question: **would this trade have worked?**

Built entirely on Android. No laptop required — but runs anywhere.

> ⚠️ Paper trading only. No real funds. Not financial advice.
> By using this software you agree to the [Terms & Conditions](https://nolaptoptrades.com/terms).

---

## Pipeline

```
Discovery → Safety → Brain → Simulator → PostMortem
```

| Component | Role |
|---|---|
| **Discovery** | Monitors Helius, DexScreener, Moralis for new PumpSwap graduations |
| **Safety** | Multi-check filter — liquidity, volume, momentum, age, whale concentration |
| **Brain** | 12-signal rule scorer with hard blocks and fast-track entry gate |
| **Simulator** | Paper trades with TP, SL, TSL, timeout logic — no real funds |
| **PostMortem** | Compiles trade records, shadow tracking, syncs to network |

---

## Requirements

- Python 3.10+
- Linux, WSL (Windows), or Android (Termux)
- Helius API key — free tier at [helius.dev](https://helius.dev)

---

## Installation

```bash
curl -fsSL https://nolaptoptrades.com/install | bash
```

Or clone manually:

```bash
git clone https://github.com/nolaptoptrades/alphas.git ~/nolaptoptrades
cd ~/nolaptoptrades
bash install.sh
```

Then:

```bash
source ~/.bashrc
nano ~/nolaptoptrades/.env  # add your API keys
alphas                      # launch
```

---

## API Keys

Add to `~/nolaptoptrades/.env` after install:

```env
# Required
HELIUS_API_KEY=        # helius.dev — free tier works

# Optional — Telegram signal bot
TELEGRAM_BOT_TOKEN=    # @BotFather on Telegram
TELEGRAM_CHAT_ID=      # @userinfobot on Telegram

# Optional — AI insights (pick one)
GEMINI_API_KEY=        # aistudio.google.com — free tier
ANTHROPIC_API_KEY=     # console.anthropic.com
```

> All keys are stored locally on your machine only.
> NLT never collects or transmits your API keys.

---

## Data & Sync

Sync is opt-in. When enabled, anonymous trade simulation data is contributed
to the NLT aggregate dataset — no personal information, no API keys, no wallet
addresses are ever transmitted.

Users who sync gain access to network-wide aggregate analytics at
[nolaptoptrades.com/netstats](https://nolaptoptrades.com/netstats).

To enable sync, set `data.sharing_enabled: true` in `config.json` and add your
`NLT_SYNC_KEY` to `.env`.

---

## Dependencies

Installed automatically during setup:

| Package | Purpose |
|---|---|
| `requests` | HTTP client for API calls and sync |
| `python-dotenv` | `.env` file loader |
| `rich` | Terminal formatting |

---

## CLI Reference

```
alphas                          Launch pipeline TUI
alphas --mystats                Local trade summary and stats
alphas --mystats --insights     AI-powered trade insights (BYOK)
alphas --sync-on                Enable anonymous trade data sync
alphas --sync-off               Disable anonymous trade data sync
alphas --export                 Save report to txt file
alphas --storage                Disk usage breakdown
alphas --clear-cache            Delete log and handshake files
alphas --clear-trades           Remove compiled trade records
alphas --logs COMPONENT         Tail a component log
alphas --reset                  Clear state files
alphas --version                Show version
alphas --sync-on                Toggle sync
```

---

## Links

- 🌐 Website: [nolaptoptrades.com](https://nolaptoptrades.com)
- 📊 Network Stats: [nolaptoptrades.com/netstats](https://nolaptoptrades.com/netstats)
- 📄 Docs: [nolaptoptrades.com/docs](https://nolaptoptrades.com/docs)
- 💬 Telegram: [@nolaptoptrades](https://t.me/nolaptoptrades)
- 🎬 YouTube: [@nolaptoptrades](https://youtube.com/@nolaptoptrades)
- 🐦 X: [@nolaptoptrades](https://x.com/nolaptoptrades)

---

<div align="center">
  <sub>Built on a phone. No laptop required.</sub><br>
  <sub>Terms: <a href="https://nolaptoptrades.com/terms">nolaptoptrades.com/terms</a> | 
  Contact: <a href="https://t.me/nolaptoptrades">@nolaptoptrades</a></sub>
</div>
