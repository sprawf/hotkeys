# ⚡ Hotkeys

> **Rewrite any text, in any app, in under a second. No copy-paste. No switching windows. Just a hotkey.**

Hotkeys sits silently in your system tray and gives you two superpowers:

- **AI Text Refiner** — select any text anywhere, press a hotkey, it gets rewritten by AI and pasted back instantly
- **Voice-to-Text** — press a hotkey, speak, release — your words appear wherever your cursor is

Works in every app. Notion, Gmail, Slack, Word, VS Code, browsers — anywhere.

---

## What makes it different

Most AI writing tools live inside a browser tab or a separate window. You have to copy your text, switch apps, paste, wait, copy again, switch back, paste again.

Hotkeys does it **in place**. Select text → press `Alt+Shift+W` → it's already rewritten and pasted back. Never leave the app you're in.

The **prompt library** is what makes it powerful. Build a collection of reusable instructions:

- *"Fix grammar and spelling"*
- *"Make this sound more professional"*
- *"Translate to Spanish"*
- *"Make this shorter"*
- *"Rewrite this as bullet points"*

One click on any prompt → applied to your selected text → pasted back. Instantly.

---

## Features

| Feature | Description |
|---|---|
| 🎙️ Voice to text | Press hotkey → speak → text appears in any app |
| ✍️ AI text refiner | Select text → hotkey → AI rewrites it in place |
| 📚 Prompt library | Save and reuse your favourite AI instructions |
| 🔍 Live search | Search your prompt library as you type |
| 🔄 Drag to reorder | Drag prompts to organise them your way |
| 📋 History | Browse all your past transcriptions |
| 🔇 Noise reduction | Works even in noisy environments |
| 🚀 Push-to-talk | Hold hotkey to record, release to transcribe |
| ⚡ Instant paste | Output appears directly where your cursor is |
| 🖥️ System tray | Runs in the background, zero UI clutter |

---

## Hotkeys

| Action | Shortcut |
|---|---|
| Start / stop voice recording | `Ctrl + Enter` |
| Refine selected text with AI | `Alt + Shift + W` |
| Open prompt library | `Alt + Shift + E` |
| Cancel recording | `Escape` |

All hotkeys are customisable in Settings.

---

## AI Providers

Hotkeys works with **Cerebras** and **Groq** — both offer free API tiers that are fast enough for real-time use.

| Provider | Speed | Free tier |
|---|---|---|
| Cerebras | ~0.3s | ✅ Yes — [cerebras.ai](https://cerebras.ai) |
| Groq | ~0.5s | ✅ Yes — [console.groq.com](https://console.groq.com) |

Sign up, grab a free API key, paste it in Settings. Done.

---

## Installation

### Windows

1. Download the latest release: **[Hotkeys-v1.0-win64.zip](https://github.com/sprawf/hotkeys/releases)**
2. Extract anywhere
3. Double-click `Hotkeys.exe`
4. The ⚡HK icon appears in your taskbar tray

No Python. No setup. No dependencies. Just run it.

---

### Mac

1. Download the installer: **[install_mac.command](https://github.com/sprawf/hotkeys/raw/main/install_mac.command)**
2. Right-click it → **Open** (Mac security step, one time only)
3. A terminal window opens and installs everything automatically (~10 min)
4. Follow the on-screen prompt to grant keyboard permission
5. Double-click **Hotkeys.command** on your Desktop to launch

The installer handles Python, all packages, and the AI models (~600 MB download). You don't need to install anything manually.

---

## Screenshots

*Coming soon*

---

## Running from source

```bash
git clone https://github.com/sprawf/hotkeys.git
cd hotkeys

# Windows
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python main.py

# Mac
python3 -m venv venv
venv/bin/pip install -r requirements_mac.txt
venv/bin/python3 main.py
```

Models are downloaded automatically on first run if not present locally.

---

## Tech stack

- **UI** — CustomTkinter (dark themed, no web view)
- **Speech-to-text** — faster-whisper (Whisper small/base models, runs fully offline)
- **VAD** — Silero VAD (auto-stops recording when you stop speaking)
- **AI providers** — Cerebras, Groq (cloud, requires API key)
- **Hotkeys** — keyboard library (global, works across all apps)
- **Tray** — pystray

---

## Privacy

- Voice is transcribed **locally on your device** — never sent to any server
- Text refinement is sent to Cerebras or Groq (whichever you configure) — same as any AI assistant
- No analytics, no telemetry, no accounts required

---

## License

MIT — free to use, modify, and distribute.
