#!/bin/bash
# ── Hotkeys launcher (run this once to open Hotkeys) ──────────────────────────
#
# Double-click this file. If macOS blocks it, right-click → Open → Open.
# After the first run you can launch Hotkeys directly from your Applications folder.
# ─────────────────────────────────────────────────────────────────────────────

APP="$HOME/Applications/Hotkeys.app"
if [ ! -d "$APP" ]; then
    APP="/Applications/Hotkeys.app"
fi

if [ ! -d "$APP" ]; then
    osascript -e 'display alert "Hotkeys.app not found" message "Please drag Hotkeys.app to your Applications folder first, then run this script." as warning'
    exit 1
fi

# Remove the macOS quarantine flag so the app opens without a security warning
xattr -dr com.apple.quarantine "$APP" 2>/dev/null

# Launch
open "$APP"
