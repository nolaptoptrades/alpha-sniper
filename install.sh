#!/bin/bash

set -e

INSTALL_DIR="$HOME/nolaptoptrades"
SNIPER_DIR="$INSTALL_DIR/alpha_sniper"
VENV_DIR="$INSTALL_DIR/venv"
BASHRC="$HOME/.bashrc"
REPO_URL="https://github.com/nolaptoptrades/alpha-sniper"

echo ""
echo "═══════════════════════════════════════════"
echo "  NLT Alpha Sniper — Installation"
echo "═══════════════════════════════════════════"
echo ""

# ── Detect platform ───────────────────────────
IS_TERMUX=false
if [ -d "/data/data/com.termux" ]; then
    IS_TERMUX=true
    echo "  Platform: Android (Termux)"
else
    echo "  Platform: Linux / VPS"
fi

# ── Python version check ──────────────────────
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    echo ""
    echo "✗ Python not found."
    if [ "$IS_TERMUX" = true ]; then
        echo "  Run: pkg install python"
    else
        echo "  Run: sudo apt install python3"
    fi
    exit 1
fi

PY_VERSION=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$($PYTHON -c "import sys; print(sys.version_info.major)")
PY_MINOR=$($PYTHON -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "✗ Python $PY_VERSION found — Python 3.10+ required."
    exit 1
fi
echo "✓ Python $PY_VERSION"

# ── pip + venv (non-Termux only) ──────────────
if [ "$IS_TERMUX" = false ]; then
    $PYTHON -m pip --version &>/dev/null || {
        echo "  Installing pip..."
        curl -fsSL https://bootstrap.pypa.io/get-pip.py | $PYTHON
    }
    $PYTHON -m venv --help &>/dev/null || {
        echo "  Installing python3-venv..."
        sudo apt install -y python3-venv python3-pip 2>/dev/null || true
    }
    # Ensure ncurses is available (needed by manager TUI)
    sudo apt install -y libncurses-dev 2>/dev/null || true
fi

# ── Git check ─────────────────────────────────
if ! command -v git &>/dev/null; then
    echo ""
    echo "✗ git not found."
    if [ "$IS_TERMUX" = true ]; then
        echo "  Run: pkg install git"
    else
        echo "  Run: sudo apt install git"
    fi
    exit 1
fi
echo "✓ git found"

# ── Clone or update repo ──────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "  Updating existing install..."
    git -C "$INSTALL_DIR" pull --ff-only
    echo "✓ Updated to latest"
elif [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/config.json" ]; then
    # Existing non-git install — copy files in without touching config/.env
    echo "  Existing install found — pulling latest source..."
    TMP_DIR=$(mktemp -d)
    git clone --depth 1 "$REPO_URL" "$TMP_DIR"
    cp -r "$TMP_DIR/alpha_sniper" "$INSTALL_DIR/"
    cp "$TMP_DIR/requirement.txt" "$INSTALL_DIR/"
    rm -rf "$TMP_DIR"
    echo "✓ Source updated (config.json and .env preserved)"
else
    echo "  Cloning NLT Alpha Sniper..."
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
    echo "✓ Cloned to $INSTALL_DIR"
fi

# ── Create runtime dirs ───────────────────────
mkdir -p "$SNIPER_DIR/logs"
mkdir -p "$SNIPER_DIR/state"
mkdir -p "$SNIPER_DIR/reports"
mkdir -p "$SNIPER_DIR/discovery"
echo "✓ Directories ready"

# ── Virtual environment ───────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "  Creating virtual environment..."
    $PYTHON -m venv "$VENV_DIR"
fi
echo "✓ Virtual environment ready"

# ── Install dependencies ──────────────────────
echo "  Installing dependencies..."
"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q -r "$INSTALL_DIR/requirement.txt"
echo "✓ Dependencies installed"

# ── Config setup ─────────────────────────────
if [ ! -f "$INSTALL_DIR/config.json" ]; then
    if [ -f "$INSTALL_DIR/config.example.json" ]; then
        cp "$INSTALL_DIR/config.example.json" "$INSTALL_DIR/config.json"
        # Inject actual base_dir
        sed -i "s|{base_dir}|$INSTALL_DIR|g" "$INSTALL_DIR/config.json"
        echo "✓ config.json created"
    else
        echo "⚠ config.example.json not found — create config.json manually"
    fi
else
    echo "✓ config.json exists — not overwritten"
fi

# ── .env setup ───────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cat > "$INSTALL_DIR/.env" << 'ENVFILE'
# NLT Alpha Sniper — Environment Variables
# Edit this file to add your API keys.
# Lines starting with # are ignored.

# ── Required ──────────────────────────────────
# Helius RPC + event stream. Get a free key at helius.dev
HELIUS_API_KEY=

# ── Sync (optional) ───────────────────────────
# Contribute anonymous trade data to the NLT network.
# Enables data sharing. Get your key at nolaptoptrades.com
NLT_SYNC_KEY=

# ── Telegram Bridge Bot (optional) ────────────
# For live trade signal delivery to your Telegram channel.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# ── AI Insights (optional — pick one) ─────────
# GEMINI_API_KEY=
# ANTHROPIC_API_KEY=
ENVFILE
    echo "✓ .env template created"
else
    echo "✓ .env exists — not overwritten"
fi

# ── alphas alias ─────────────────────────────
NLT_CMD="alias alphas=\"cd $INSTALL_DIR && $VENV_DIR/bin/python $SNIPER_DIR/cli.py\""

if grep -q "alias alphas=" "$BASHRC" 2>/dev/null; then
    sed -i "s|alias alphas=.*|$NLT_CMD|" "$BASHRC"
    echo "✓ alphas alias updated"
else
    echo "" >> "$BASHRC"
    echo "# NLT Alpha Sniper" >> "$BASHRC"
    echo "$NLT_CMD" >> "$BASHRC"
    echo "✓ alphas alias added to ~/.bashrc"
fi

# ── Termux: also write to ~/.profile ─────────
if [ "$IS_TERMUX" = true ]; then
    PROFILE="$HOME/.profile"
    if ! grep -q "alias alphas=" "$PROFILE" 2>/dev/null; then
        echo "" >> "$PROFILE"
        echo "# NLT Alpha Sniper" >> "$PROFILE"
        echo "$NLT_CMD" >> "$PROFILE"
        echo "✓ alphas alias added to ~/.profile (Termux)"
    fi
fi

echo ""
echo "═══════════════════════════════════════════"
echo "  Installation complete!"
echo "═══════════════════════════════════════════"
echo ""
echo "  Next steps:"
echo "  1. source ~/.bashrc"
echo "  2. nano $INSTALL_DIR/.env    ← add your HELIUS_API_KEY"
echo "  3. alphas                    ← launch"
echo ""
echo "  Docs:     https://nolaptoptrades.com/docs"
echo "  Network:  https://nolaptoptrades.com/netstats"
echo "  Telegram: @nolaptoptrades"
echo ""
