<div align="center">
  <h1>NLT Alpha Sniper</h1>
  <p><strong>Assess before you commit.</strong></p>
  <p>Solana memecoin market analysis and paper trading pipeline.</p>

  ![Version](https://img.shields.io/badge/version-1.0.0-blue)
  ![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20WSL%20%7C%20Termux-green)
  ![License](https://img.shields.io/badge/license-proprietary-red)
  ![Python](https://img.shields.io/badge/python-3.10+-yellow)
</div>

---

## What is NLT Alpha Sniper?

NLT Alpha Sniper is a **Solana graduation sniper pipeline** — a terminal-first
risk assessment and market analysis tool that monitors PumpSwap graduation events,
filters for quality setups across multiple dimensions, and paper trades in real time.

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
- Linux, WSL (Windows), Android (Termux), or any VPS
- Helius API key — free tier at [helius.dev](https://helius.dev)

---

## Installation

```bash
curl -fsSL https://nolaptoptrades.com/install | bash
```

Then:

```bash
source ~/.bashrc
nlt --license    # activate your license key
nano ~/nolaptoptrades/.env  # add your API keys
nlt              # launch
```

> **Manual install:** Download the zip for your platform from
> [Releases](https://github.com/nolaptoptrades/alpha-sniper/releases),
> extract, and run `bash install.sh`

---

## Platform Downloads

| Platform | File |
|---|---|
| Linux / WSL | `nlt-alpha-sniper-v1.0.0-linux-x86_64.zip` |
| Android (Termux) | `nlt-alpha-sniper-v1.0.0-android-arm64.zip` |
| macOS | Coming soon |
| VPS (Ubuntu) | Use Linux build |

---

## API Keys

Add to `~/nolaptoptrades/.env` after install:

```env
# Required
HELIUS_API_KEY=        # helius.dev — free tier works

# Optional — Telegram alerts + signal bot
TELEGRAM_BOT_TOKEN=    # @BotFather on Telegram
TELEGRAM_CHAT_ID=      # @userinfobot on Telegram

# Optional — AI insights (pick one)
GEMINI_API_KEY=        # aistudio.google.com — free
ANTHROPIC_API_KEY=     # console.anthropic.com
```

> All keys are stored locally on your machine only.
> NLT never collects or transmits your API keys.

---

## Dependencies

Installed automatically during setup:

| Package | Purpose |
|---|---|
| `requests` | HTTP client for API calls |
| `python-dotenv` | `.env` file loader |
| `rich` | Terminal formatting |
| `cython` | Runtime compilation |
| `setuptools` | Build tooling |
| `toml` | Config parsing |
| `codeenigma-runtime` | Code protection layer (bundled in release) |

> On Termux, `python`, `curl`, and `unzip` are installed via `pkg` automatically.

---

## License Tiers

| Feature | Free | Hobbyist | Pro |
|---|---|---|---|
| Full pipeline | ✓ | ✓ | ✓ |
| Local stats (`--mystats`) | Basic | + liq band | + all breakdowns |
| Network stats (`--netstats`) | ✗ | ✓ | ✓ |
| AI insights (`--insights`) | ✗ | ✓ BYOK | ✓ BYOK |
| Data sharing | Required | Optional | Optional |

**Get a free trial:**
- DM [@nolaptoptrades](https://t.me/nolaptoptrades) on Telegram
- Or message the trial bot: `/trial` → coming soon

---

## CLI Reference

```
nlt                        Launch pipeline TUI
nlt --license              Activate license key
nlt --mystats              Local trade summary
nlt --mystats --insights   AI-powered insights (BYOK)
nlt --netstats             Network aggregate stats
nlt --storage              Disk usage breakdown
nlt --clear-cache          Delete log and handshake files
nlt --clear-trades         Remove compiled trade records
nlt --export               Save report to txt file
nlt --logs COMPONENT       Tail a component log
nlt --reset                Clear state files
nlt --version              Show version
nlt --dry-run              Launch in dry-run mode
```

---

## Links

- 🌐 Website: [nolaptoptrades.com](https://nolaptoptrades.com)
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
