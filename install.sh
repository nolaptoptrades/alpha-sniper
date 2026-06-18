#!/bin/bash
# ─────────────────────────────────────────────
# NLT Alpha Sniper — Installer
# Run once after extracting the zip:
#   bash install.sh
# ─────────────────────────────────────────────

set -e

INSTALL_DIR="$HOME/nolaptoptrades"
SNIPER_DIR="$INSTALL_DIR/alpha_sniper"
VENV_DIR="$INSTALL_DIR/venv"
BASHRC="$HOME/.bashrc"

echo ""
echo "═══════════════════════════════════════════"
echo "  NLT Alpha Sniper — Installation"
echo "═══════════════════════════════════════════"
echo ""

# ── Python version check ──────────────────────
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    echo "✗ Python not found. Install Python 3.10+ and try again."
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

# ── Create install directory ──────────────────
mkdir -p "$INSTALL_DIR"
mkdir -p "$SNIPER_DIR/logs"
mkdir -p "$SNIPER_DIR/state"
mkdir -p "$SNIPER_DIR/reports"

# ── Copy files if running from zip extract dir ─
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    echo "  Copying files to $INSTALL_DIR..."
    cp -r "$SCRIPT_DIR/"* "$INSTALL_DIR/" 2>/dev/null || true
fi
echo "✓ Files installed to $INSTALL_DIR"

# ── Virtual environment ───────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "  Creating virtual environment..."
    $PYTHON -m venv "$VENV_DIR"
fi
echo "✓ Virtual environment ready"

# ── Install dependencies ──────────────────────
echo "  Installing dependencies..."
"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q -r "$SNIPER_DIR/requirements.txt"
echo "✓ Dependencies installed"

# ── Config setup ─────────────────────────────
if [ ! -f "$INSTALL_DIR/config.json" ]; then
    if [ -f "$INSTALL_DIR/config.example.json" ]; then
        cp "$INSTALL_DIR/config.example.json" "$INSTALL_DIR/config.json"
        echo "✓ config.json created from example"
    else
        echo "⚠ config.json not found — create it before running nlt"
    fi
else
    echo "✓ config.json exists"
fi

# ── .env setup ───────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cat > "$INSTALL_DIR/.env" << 'ENVFILE'
# NLT Alpha Sniper — API Keys
# Fill in your keys below

HELIUS_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# AI Insights (optional — pick one)
# GEMINI_API_KEY=
# ANTHROPIC_API_KEY=
ENVFILE
    echo "✓ .env template created — add your API keys before running"
else
    echo "✓ .env exists"
fi

# ── nlt alias ────────────────────────────────
NLT_CMD="alias nlt=\"$VENV_DIR/bin/python $SNIPER_DIR/cli.py\""

if grep -q "alias nlt=" "$BASHRC" 2>/dev/null; then
    # Update existing alias
    sed -i "s|alias nlt=.*|$NLT_CMD|" "$BASHRC"
    echo "✓ nlt alias updated"
else
    echo "" >> "$BASHRC"
    echo "# NLT Alpha Sniper" >> "$BASHRC"
    echo "$NLT_CMD" >> "$BASHRC"
    echo "✓ nlt alias added to ~/.bashrc"
fi

# ── Termux: also add to ~/.profile ───────────
if [ -d "/data/data/com.termux" ]; then
    PROFILE="$HOME/.profile"
    if ! grep -q "alias nlt=" "$PROFILE" 2>/dev/null; then
        echo "" >> "$PROFILE"
        echo "# NLT Alpha Sniper" >> "$PROFILE"
        echo "$NLT_CMD" >> "$PROFILE"
        echo "✓ nlt alias added to ~/.profile (Termux)"
    fi
fi

echo ""
echo "═══════════════════════════════════════════"
echo "  Installation complete!"
echo "═══════════════════════════════════════════"
echo ""
echo "  Next steps:"
echo "  1. Add your API keys:  nano $INSTALL_DIR/.env"
echo "  2. Activate alias:     source ~/.bashrc"
echo "  3. Activate license:   nlt --license"
echo "  4. Launch pipeline:    nlt"
echo ""
echo "  Docs: https://nolaptoptrades.com/docs"
echo "  Help: @nolaptoptrades on Telegram"
echo ""
