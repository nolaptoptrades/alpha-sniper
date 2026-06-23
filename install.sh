#!/bin/bash
# ─────────────────────────────────────────────
# NLT Alpha Sniper — Quick Installer
# Usage:
#   curl -fsSL https://nolaptoptrades.com/install | bash
# ─────────────────────────────────────────────

set -e

VERSION="1.0.0"
REPO="nolaptoptrades/alpha-sniper"
INSTALL_DIR="$HOME/nolaptoptrades"
SNIPER_DIR="$INSTALL_DIR/alpha_sniper"
VENV_DIR="$INSTALL_DIR/venv"
BASHRC="$HOME/.bashrc"

echo ""
echo "═══════════════════════════════════════════"
echo "  NLT Alpha Sniper v$VERSION — Installer"
echo "═══════════════════════════════════════════"
echo ""

# ── Platform detection ────────────────────────
if [ -d "/data/data/com.termux" ]; then
    PLATFORM="android-arm64"
    echo "  Platform: Android (Termux)"
elif uname -m | grep -q "aarch64"; then
    PLATFORM="android-arm64"
    echo "  Platform: ARM64 Linux"
else
    PLATFORM="linux-x86_64"
    echo "  Platform: Linux x86_64 / WSL"
fi

ZIP_NAME="nlt-alpha-sniper-v${VERSION}-${PLATFORM}.zip"
DOWNLOAD_URL="https://github.com/${REPO}/releases/download/v${VERSION}/${ZIP_NAME}"


# ── Termux bootstrap ──────────────────────────
if [ -d "/data/data/com.termux" ]; then
    echo "  Bootstrapping Termux dependencies..."
    pkg update -y 2>/dev/null || true
    pkg upgrade -y 2>/dev/null || true
    pkg install -y -q python curl unzip 2>/dev/null || true
    pkg install -y -q python-cryptography 2>/dev/null || true
    echo "✓ Termux dependencies ready"
fi

# ── Python check ──────────────────────────────
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    echo "✗ Python not found."
    echo "  WSL/Linux: sudo apt install python3"
    echo "  Termux:    pkg install python"
    exit 1
fi

PY_MAJOR=$($PYTHON -c "import sys; print(sys.version_info.major)")
PY_MINOR=$($PYTHON -c "import sys; print(sys.version_info.minor)")
PY_VERSION="$PY_MAJOR.$PY_MINOR"

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "✗ Python $PY_VERSION found — Python 3.10+ required."
    exit 1
fi
echo "✓ Python $PY_VERSION"

# ── Download ──────────────────────────────────
echo "  Downloading $ZIP_NAME..."
TMP_DIR=$(mktemp -d)
TMP_ZIP="$TMP_DIR/nlt.zip"

if command -v curl &>/dev/null; then
    curl -fsSL "$DOWNLOAD_URL" -o "$TMP_ZIP"
elif command -v wget &>/dev/null; then
    wget -q "$DOWNLOAD_URL" -O "$TMP_ZIP"
else
    echo "✗ curl or wget required."
    echo "  Termux: pkg install curl"
    exit 1
fi
echo "✓ Downloaded"

# ── Extract ───────────────────────────────────
echo "  Extracting..."
if command -v unzip &>/dev/null; then
    unzip -q "$TMP_ZIP" -d "$TMP_DIR/"
else
    echo "✗ unzip required."
    echo "  Termux: pkg install unzip"
    echo "  Linux:  sudo apt install unzip"
    rm -rf "$TMP_DIR"
    exit 1
fi

EXTRACT_DIR="$TMP_DIR/nlt"
if [ ! -d "$EXTRACT_DIR" ]; then
    echo "✗ Unexpected zip structure — expected nlt/ folder"
    rm -rf "$TMP_DIR"
    exit 1
fi
echo "✓ Extracted"

# ── Create directories ────────────────────────
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/logs"
mkdir -p "$INSTALL_DIR/state"
mkdir -p "$INSTALL_DIR/reports"

# ── Copy files ────────────────────────────────
# Always copy support files
cp "$EXTRACT_DIR/requirement.txt" "$INSTALL_DIR/"
cp "$EXTRACT_DIR/config.example.json" "$INSTALL_DIR/" 2>/dev/null || true

if [ ! -d "$SNIPER_DIR" ] || [ -z "$(ls -A $SNIPER_DIR 2>/dev/null)" ]; then
    echo "  Installing files..."
    cp -r "$EXTRACT_DIR/alpha_sniper" "$INSTALL_DIR/"
    echo "✓ Files installed"
else
    echo "✓ Existing install found — skipping file copy"
    echo "  To reinstall: rm -rf $SNIPER_DIR and run again"
fi

# ── Virtual environment ───────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "  Creating virtual environment..."
    if [ -d "/data/data/com.termux" ]; then
        $PYTHON -m venv "$VENV_DIR" --system-site-packages
    else
        $PYTHON -m venv "$VENV_DIR"
    fi
fi
echo "✓ Virtual environment ready"

# ── Install dependencies ──────────────────────
echo "  Installing dependencies..."
"$VENV_DIR/bin/pip" install -q --upgrade pip
if [ -d "/data/data/com.termux" ]; then
    "$VENV_DIR/bin/pip" install -q --no-deps -r "$INSTALL_DIR/requirement.txt"
    "$VENV_DIR/bin/pip" install -q requests python-dotenv rich cython setuptools toml
else
    "$VENV_DIR/bin/pip" install -q -r "$INSTALL_DIR/requirement.txt"
fi

# Install CodeEnigma runtime
WHEEL=$(ls "$EXTRACT_DIR/"codeenigma_runtime-*.whl 2>/dev/null | head -1)
if [ -d "/data/data/com.termux" ]; then
    SYSTEM_CRYPTO=$(python3 -c "import cryptography; import os; print(os.path.dirname(cryptography.__file__))" 2>/dev/null)
    if [ -n "$SYSTEM_CRYPTO" ]; then
        VENV_SITE="$VENV_DIR/lib/python3.13/site-packages"
        cp -r "$SYSTEM_CRYPTO" "$VENV_SITE/" 2>/dev/null || true
        echo "✓ Cryptography linked from system"
    fi
    "$VENV_DIR/bin/pip" install -q --force-reinstall --no-deps "$WHEEL"
else
    "$VENV_DIR/bin/pip" install -q --force-reinstall "$WHEEL"
fi
echo "✓ Dependencies installed"

# ── Config ────────────────────────────────────
if [ ! -f "$INSTALL_DIR/config.json" ]; then
    cp "$INSTALL_DIR/config.example.json" "$INSTALL_DIR/config.json"
    sed -i "s|{base_dir}|$INSTALL_DIR|g" "$INSTALL_DIR/config.json"
    echo "✓ config.json created"
else
    echo "✓ config.json exists — not overwritten"
fi

# ── .env ─────────────────────────────────────
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cat > "$INSTALL_DIR/.env" << 'ENVFILE'
# NLT Alpha Sniper — API Keys

HELIUS_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# AI Insights (optional — pick one)
# GEMINI_API_KEY=
# ANTHROPIC_API_KEY=
ENVFILE
    echo "✓ .env template created"
else
    echo "✓ .env exists — not overwritten"
fi

# ── nlt alias ────────────────────────────────
NLT_CMD="alias nlt=\"$VENV_DIR/bin/python $SNIPER_DIR/cli.py\""

if grep -q "alias nlt=" "$BASHRC" 2>/dev/null; then
    sed -i "s|alias nlt=.*|$NLT_CMD|" "$BASHRC"
    echo "✓ nlt alias updated"
else
    echo "" >> "$BASHRC"
    echo "# NLT Alpha Sniper" >> "$BASHRC"
    echo "$NLT_CMD" >> "$BASHRC"
    echo "✓ nlt alias added"
fi

# ── Termux profile ────────────────────────────
if [ -d "/data/data/com.termux" ]; then
    PROFILE="$HOME/.profile"
    if ! grep -q "alias nlt=" "$PROFILE" 2>/dev/null; then
        echo "" >> "$PROFILE"
        echo "# NLT Alpha Sniper" >> "$PROFILE"
        echo "$NLT_CMD" >> "$PROFILE"
        echo "✓ nlt alias added to ~/.profile"
    fi
fi

# ── Cleanup ───────────────────────────────────
rm -rf "$TMP_DIR"

echo ""
echo "═══════════════════════════════════════════"
echo "  Installation complete!"
echo "═══════════════════════════════════════════"
echo ""
echo "  Next steps:"
echo "  1. source ~/.bashrc"
echo "  2. nano $INSTALL_DIR/.env    ← add API keys"
echo "  3. nlt --license             ← activate key"
echo "  4. nlt                       ← launch"
echo ""
echo "  Docs:  https://nolaptoptrades.com/docs"
echo "  Help:  @nolaptoptrades on Telegram"
echo ""
