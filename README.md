# ⚡ Hotkeys

**Rewrite any text, in any app, instantly. No copy-paste. No switching windows. Just a hotkey.**

Select text anywhere → press `Alt+Shift+W` → AI rewrites it and pastes it back. Done.  
Works in Gmail, Notion, Slack, Word, VS Code, Discord — every app on your computer.

[![Windows](https://img.shields.io/badge/Windows-Download_v2.0-0078D6?style=for-the-badge&logo=windows)](https://github.com/sprawf/hotkeys/releases/download/v2.0.0/Hotkeys-v2.0-win64.zip)
[![Windows v1](https://img.shields.io/badge/Windows-v1.0_(legacy)-555555?style=for-the-badge&logo=windows)](https://github.com/sprawf/hotkeys/releases/download/v1.0.0/Hotkeys-v1.0-win64.zip)
[![Mac](https://img.shields.io/badge/Mac-Installer-999999?style=for-the-badge&logo=apple)](https://github.com/sprawf/hotkeys/raw/main/install_mac.command)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10+-blue?style=for-the-badge&logo=python)](https://python.org)

---

## What it does

Most AI writing tools require you to open a browser tab, paste your text, wait, copy the result, switch back, and paste again. **That's 7 steps.**

Hotkeys does it in **1 step** — without leaving whatever you're typing in.

It lives in your system tray and gives you three superpowers:

🖊️ **AI Text Refiner** — select any text, press a hotkey, it's rewritten and pasted back in under a second  
⌨️ **Per-Prompt Hotkeys** — assign F1–F12 to any prompt and fire it from any app, no library needed  
🎙️ **Voice to Text** — press a hotkey, speak, your words appear wherever your cursor is — fully offline

---

## Demo

> *Demo GIF coming soon — select text → press hotkey → text rewritten in place*

---

## The Prompt Library

Build a personal library of reusable AI instructions. 16 prompts ship ready to use, each pre-assigned to a function key:

| Hotkey | Prompt | What it does |
|---|---|---|
| `F1` | Refine | Fixes grammar, spelling, and clarity — same meaning, natural tone |
| `F2` | Improve & Expand | Makes your text more articulate, detailed, and expressive |
| `F3` | Translate | Translates in place (default: Arabic — change it to anything) |
| `F4` | System Prompt | Reformats text into a clean, deployable AI system prompt |
| `F5` | Simplify | Strips jargon and complexity — immediately understandable |
| `F6` | Technical Depth | Adds precision and implementation detail for expert readers |
| `F7` | Expand | Develops underdeveloped ideas without going off-topic |
| `F8` | Professional | Rewrites in polished, formal language at the same length |
| `F9` | Ask Claude | Turns vague thoughts into a sharp, specific AI prompt |
| `F10` | Pirate | Rewrites with nautical flair — same meaning, more swagger |
| `F11` | ELI5 | Explains anything as if the reader is five years old |
| `F12` | Tweet | Compresses the sharpest idea into one punchy tweet |
| — | Brutally Honest | Says exactly what's meant, no softening, no padding |
| — | Story Hook | Turns any idea into a gripping opening line |
| — | Devil's Advocate | Argues the exact opposite with equal conviction |
| — | Haiku | Distils the core idea into a 5-7-5 haiku |

Press the hotkey → a floating sticky note opens showing the prompt → press again to apply. Edit the prompt on the fly before it fires. Build as many custom prompts as you want.

---

## Features

| | Feature | Notes |
|---|---|---|
| ✍️ | **AI text refiner** | Works in any app, any text field |
| 📚 | **Prompt library** | 16 built-in prompts, unlimited custom ones |
| ⌨️ | **Per-prompt hotkeys** | Assign F1–F12 to any prompt; fires from any app |
| 🗒️ | **Sticky note popup** | Preview and edit a prompt before it fires; press again to apply |
| 🔤 | **Live spell check** | Misspellings underlined in red as you type in the editor |
| 🔍 | **Prompt search** | Find prompts instantly as you type |
| 🔄 | **Drag to reorder** | Organise prompts by drag and drop |
| 🎙️ | **Voice to text** | Fully offline, no data sent anywhere |
| 🔇 | **Noise reduction** | Works in noisy environments |
| 🚀 | **Push-to-talk** | Hold to record, release to transcribe |
| 📋 | **Transcription history** | Browse and copy past recordings |
| ⚡ | **Instant paste** | Output types directly where your cursor is |
| 🖥️ | **System tray** | Zero UI clutter, always available |

---

## Hotkeys

| Action | Default shortcut |
|---|---|
| Refine selected text with AI | `Alt + Shift + W` |
| Open prompt library | `Alt + Shift + E` |
| Fire prompt 1–12 | `F1` – `F12` |
| Start / stop voice recording | `Ctrl + Enter` |
| Cancel recording | `Escape` |

All hotkeys are customisable in Settings. Per-prompt hotkeys are assigned per-prompt via right-click → **Assign hotkey**.

---

## Installation

### ⊞ Windows — one click, no setup

1. **[Download Hotkeys-v2.0-win64.zip](https://github.com/sprawf/hotkeys/releases/download/v2.0.0/Hotkeys-v2.0-win64.zip)**
2. Extract the zip anywhere
3. Double-click `Hotkeys.exe`
4. The ⚡ icon appears in your taskbar tray — you're done

No Python. No pip. No dependencies. It just works.

> **[Download v1.0 (legacy)](https://github.com/sprawf/hotkeys/releases/download/v1.0.0/Hotkeys-v1.0-win64.zip)**

---

### 🍎 Mac — automated installer

1. **[Download install_mac.command](https://github.com/sprawf/hotkeys/raw/main/install_mac.command)**
2. Right-click it → **Open** → click **Open** again (Mac security prompt)
3. A terminal window installs everything automatically (~10 min, 600 MB models)
4. Grant keyboard permission when prompted (one-time, 30 seconds)
5. Double-click **Hotkeys.command** on your Desktop — done

---

## AI Providers — both free

Hotkeys uses Cerebras or Groq for text refinement. Both are free and take 2 minutes to set up.

| Provider | Speed | Free tier | Sign up |
|---|---|---|---|
| **Cerebras** | ~0.3s | ✅ Yes | [cerebras.ai](https://cerebras.ai) |
| **Groq** | ~0.5s | ✅ Yes | [console.groq.com](https://console.groq.com) |

Sign up → copy your API key → paste it into Hotkeys Settings. Done.

Voice-to-text works without any API key — it runs fully offline on your device.

---

## Privacy

- 🔒 **Voice is transcribed locally** — the Whisper model runs on your computer, nothing is sent anywhere
- 🌐 **Text refinement** goes to Cerebras or Groq — same as any AI assistant you already use
- 🚫 No analytics, no telemetry, no account required

---

## Running from source

```bash
git clone https://github.com/sprawf/hotkeys.git
cd hotkeys

# Windows
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python main.py

# Mac / Linux
python3 -m venv venv
venv/bin/pip install -r requirements_mac.txt
venv/bin/python3 main.py
```

---

## Tech stack

| Component | Library |
|---|---|
| UI | CustomTkinter + tkinter |
| Speech-to-text | faster-whisper (runs offline) |
| Voice activity detection | Silero VAD |
| AI text refinement | Cerebras / Groq API |
| Spell check | pyspellchecker |
| Global hotkeys | keyboard |
| System tray | pystray |
| Packaging | PyInstaller |

---

## License

MIT — free to use, fork, and build on.

---

*If this saved you time, consider leaving a ⭐ — it helps others find it.*
