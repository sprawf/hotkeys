#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  Hotkeys — Mac installer
#  Double-click this file to install. Run time: ~5-10 min (model download).
# ─────────────────────────────────────────────────────────────────────────────

set -e
APP_DIR="$HOME/Hotkeys"
REPO_URL="https://github.com/sprawf/hotkeys/archive/refs/heads/main.zip"
VENV="$APP_DIR/venv"
PYTHON="$VENV/bin/python3"
PIP="$VENV/bin/pip"

# Pretty output
info()    { echo ""; echo "  ▶  $1"; }
success() { echo "  ✓  $1"; }
fail()    { echo ""; echo "  ✗  ERROR: $1"; echo ""; exit 1; }

clear
echo ""
echo "  ╔══════════════════════════════════════╗"
echo "  ║        Hotkeys — Mac Installer       ║"
echo "  ╚══════════════════════════════════════╝"
echo ""

# ── 1. Homebrew ───────────────────────────────────────────────────────────────
info "Checking Homebrew..."
if ! command -v brew &>/dev/null; then
    info "Installing Homebrew (you may be asked for your Mac password)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for Apple Silicon Macs
    eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
    eval "$(/usr/local/bin/brew shellenv)" 2>/dev/null || true
fi
success "Homebrew ready"

# ── 2. Python 3.11 ───────────────────────────────────────────────────────────
info "Checking Python 3.11+..."
BREW_PYTHON=""
for v in python@3.12 python@3.11; do
    if brew list "$v" &>/dev/null 2>&1 || brew install "$v" &>/dev/null 2>&1; then
        BREW_PYTHON="$(brew --prefix "$v")/bin/python3"
        break
    fi
done

if [ -z "$BREW_PYTHON" ] || [ ! -f "$BREW_PYTHON" ]; then
    # Fall back to any python3 in PATH that is 3.10+
    if python3 -c "import sys; assert sys.version_info >= (3,10)" &>/dev/null 2>&1; then
        BREW_PYTHON="$(which python3)"
    else
        fail "Could not install Python. Please install Python 3.11 from https://python.org and re-run this installer."
    fi
fi
success "Python ready: $($BREW_PYTHON --version)"

# ── 3. Download app ───────────────────────────────────────────────────────────
info "Downloading Hotkeys..."
rm -rf "$APP_DIR" 2>/dev/null || true
mkdir -p "$APP_DIR"
TMP_ZIP="$(mktemp /tmp/hotkeys_XXXX.zip)"
curl -L --progress-bar "$REPO_URL" -o "$TMP_ZIP" || fail "Download failed. Check your internet connection."
unzip -q "$TMP_ZIP" -d /tmp/hotkeys_extract
# The zip contains a single top-level folder — move its contents into APP_DIR
EXTRACTED="$(ls -d /tmp/hotkeys_extract/*/)"
cp -r "$EXTRACTED"* "$APP_DIR/"
rm -rf "$TMP_ZIP" /tmp/hotkeys_extract
success "App files ready"

# ── 4. Virtual environment ────────────────────────────────────────────────────
info "Creating Python environment..."
"$BREW_PYTHON" -m venv "$VENV"
"$PIP" install --quiet --upgrade pip
success "Environment created"

# ── 5. Install packages ───────────────────────────────────────────────────────
info "Installing packages (this takes a few minutes)..."
"$PIP" install --quiet -r "$APP_DIR/requirements_mac.txt" || \
    fail "Package install failed. Check your internet connection."
success "Packages installed"

# ── 6. Download Whisper models ────────────────────────────────────────────────
info "Downloading speech models (≈600 MB, please wait)..."
"$PYTHON" - <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.expanduser('~/Hotkeys'))

from huggingface_hub import snapshot_download
import os

models_dir = os.path.expanduser('~/Hotkeys/models')
os.makedirs(models_dir, exist_ok=True)

for name, repo in [('base', 'Systran/faster-whisper-base'),
                   ('small', 'Systran/faster-whisper-small')]:
    dest = os.path.join(models_dir, name)
    if os.path.exists(os.path.join(dest, 'model.bin')):
        print(f'  {name} model already present, skipping.')
        continue
    print(f'  Downloading {name} model...')
    snapshot_download(repo, local_dir=dest, local_dir_use_symlinks=False)
    print(f'  {name} model done.')
PYEOF
success "Models ready"

# ── 7. Desktop launcher ───────────────────────────────────────────────────────
info "Creating desktop launcher..."
LAUNCHER="$HOME/Desktop/Hotkeys.command"
cat > "$LAUNCHER" <<LAUNCHER
#!/bin/bash
cd "$APP_DIR"
"$PYTHON" main.py
LAUNCHER
chmod +x "$LAUNCHER"
success "Launcher created on Desktop"

# ── 8. Accessibility permission reminder ──────────────────────────────────────
echo ""
echo "  ┌─────────────────────────────────────────────────────┐"
echo "  │  ONE-TIME SETUP  (30 seconds, do this now)          │"
echo "  │                                                     │"
echo "  │  Hotkeys needs permission to listen for your        │"
echo "  │  keyboard shortcuts (Ctrl+Enter etc.)               │"
echo "  │                                                     │"
echo "  │  1. Open:  System Settings → Privacy & Security     │"
echo "  │            → Accessibility                          │"
echo "  │  2. Click the  +  button                            │"
echo "  │  3. Add  Terminal  (or your terminal app)           │"
echo "  │  4. Toggle it ON                                    │"
echo "  │                                                     │"
echo "  │  You only need to do this once.                     │"
echo "  └─────────────────────────────────────────────────────┘"
echo ""

# Open System Settings to the right pane automatically
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" 2>/dev/null || true

echo ""
echo "  ✅  Installation complete!"
echo ""
echo "  To launch Hotkeys:"
echo "  → Double-click  'Hotkeys.command'  on your Desktop"
echo ""
echo "  (You can close this window.)"
echo ""
