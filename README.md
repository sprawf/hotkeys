# ⚡ Hotkeys

**AI text refiner. Voice-to-text. Audio + screen + GIF + macro recorder. Whiteboard. File transcriber with speaker labels. All from one system-tray icon.**

Select text anywhere → press a hotkey → AI rewrites it and pastes it back in under a second.
Speak into any text field. Record your screen. Sketch on an offline whiteboard. Transcribe podcasts with speaker labels. Edit audio. All from one tiny tray icon, all offline-first, all keyboard-driven.

[![Windows](https://img.shields.io/badge/Windows-Download_latest-0078D6?style=for-the-badge&logo=windows)](https://github.com/sprawf/hotkeys/releases/latest/download/Hotkeys-Windows.zip)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10+-blue?style=for-the-badge&logo=python)](https://python.org)

> [See all releases →](https://github.com/sprawf/hotkeys/releases)

---

## What is this?

Most AI writing tools make you open a browser tab, paste your text, wait, copy the result, switch back, and paste again. That's 7 steps, every single time.

**Hotkeys does it in 1 step**, without leaving whatever you're writing in. And it's not just text refinement: it's a full keyboard-driven productivity toolkit that sits quietly in your system tray.

| | |
|---|---|
| ✍️ **AI Text Refiner** | Select any text, press a hotkey, it's rewritten and pasted back in under a second |
| 📚 **Prompt Library** | 16 built-in prompts + unlimited custom prompts, each with its own hotkey |
| 🎙️ **Voice to Text** | Hold a hotkey, speak, your words appear wherever your cursor is. Fully offline |
| 📝 **Quick Notes** | Resizable floating scratchpad with image-paste OCR and auto-save |
| 🎨 **Whiteboard** | Offline Excalidraw with infinite canvas, hand-drawn aesthetic, all keyboard-driven |
| 🎬 **Screen Recorder** | Capture any window or region as MP4 with optional mic audio |
| 🎞️ **GIF Recorder** | Record any region as an animated GIF with adjustable FPS and duration |
| 🔴 **Macro Recorder** | Record any sequence of clicks and keystrokes; replay with one key |
| 📸 **AI Screenshot** | Capture, ask the AI what's in it, get an answer |
| 🎵 **Audio Editor** | Built-in waveform editor for trimming, mixing, exporting audio |
| 🎤 **File Transcriber** | Drop an MP3 / MP4 / YouTube link; get a transcript with optional speaker labels |

**No API key needed.** Cerebras + Groq keys are baked in. Just download and run.

---

## The Prompt Library

All your prompts in one place. Click any card to activate it, drag to reorder, right-click for options:

![Prompt Library](https://raw.githubusercontent.com/sprawf/hotkeys/main/docs/screenshot_library.png)

### Default prompts (assignable to any hotkey)

| Hotkey | Prompt | What it does |
|---|---|---|
| `F1` | Refine | Fixes grammar, spelling, and clarity, same meaning, natural tone |
| `F2` | Improve & Expand | Makes your text more articulate, detailed, and expressive |
| `F3` | Translate | Translates in place (default: Arabic, change to anything) |
| `F4` | System Prompt | Reformats text into a clean, deployable AI system prompt |
| `F5` | Simplify | Strips jargon and complexity, immediately understandable |
| `F6` | Technical Depth | Adds precision and implementation detail for expert readers |
| `F7` | Expand | Develops underdeveloped ideas without going off-topic |
| `F8` | Professional | Rewrites in polished, formal language at the same length |
| `F9` | Ask Claude | Turns vague thoughts into a sharp, specific AI prompt |
| `F10` | Pirate | Rewrites with nautical flair, same meaning, more swagger |
| — | Brutally Honest | Says exactly what's meant, no softening, no padding |
| — | Story Hook | Turns any idea into a gripping opening line |
| — | Devil's Advocate | Argues the exact opposite with equal conviction |
| — | Haiku | Distils the core idea into a 5-7-5 haiku |
| — | ELI5 | Explains anything as if the reader is five years old |
| — | Tweet | Compresses the sharpest idea into one punchy tweet |

These are the starting point. Hit **+ Add** to write your own.

**The only limit is your imagination.** Want a prompt that rewrites emails in your exact voice? Converts meeting notes into action items? Translates to your language? Summarises legal contracts in plain English? Turns rough ideas into job postings?

**Add it in 10 seconds and it works everywhere, instantly.**

> **Tip:** Press any per-prompt hotkey to see a floating sticky note preview. Edit the prompt on the fly before it fires, then press the key again to apply.

---

## How to install

### ⊞ Windows, one click, no setup

1. **[Download Hotkeys-Windows.zip (latest release)](https://github.com/sprawf/hotkeys/releases/latest/download/Hotkeys-Windows.zip)**
2. Extract the zip anywhere outside `Downloads/` (e.g. `C:\Hotkeys\`)
3. Double-click `Hotkeys.exe`
4. The ⚡ icon appears in your taskbar tray, you're done

No Python. No pip. No API key. No installing anything.

> **If your antivirus pops up** (AVG / Defender / etc.): the build is unsigned for now (signed builds coming later). Right-click → restore + add exception. One-time, then it's clean.

---

## How to use it

### Refine any text (AI rewrite)

1. Select text in any app
2. Press `Alt + Shift + W`
3. Wait ~0.5 seconds, text is rewritten in place

### Use a prompt from the library

1. Select text in any app
2. Press the prompt's hotkey (e.g. `F5` for Simplify)
3. A sticky note appears, read the prompt, edit it if you want
4. Press the same key again to fire it

### Add your own prompt

1. Press `Alt + Shift + E` to open the Prompt Library
2. Click **+ Add**
3. Give it a name and write your instruction
4. *(Optional)* Assign a hotkey via right-click → **Assign hotkey**
5. Click **Save**, available everywhere immediately

### Dictate text (voice to text)

1. Place your cursor where you want the text
2. Press `Ctrl + Enter` to start recording
3. Speak naturally
4. Press `Ctrl + Enter` again to stop, your words appear instantly

> Runs fully offline. Whisper model bundled.

### Quick Notes (floating scratchpad)

1. Press `Shift + F7`, a resizable notes window opens
2. Type your note, paste anything from the clipboard (incl. **images** → OCR to text)
3. Drag any edge to resize, drag the title bar to move
4. Press `Shift + F7` again (or `Esc`) to close, notes auto-saved

### Whiteboard (offline)

1. Press `Shift + F8`, an Excalidraw whiteboard opens
2. Sketch with mouse / pen / touch, drag-and-drop images, draw arrows and shapes
3. Works fully offline, no internet, no account, no telemetry
4. Press `Shift + F8` again to close, scenes auto-saved

### Transcribe a file (with optional speaker labels)

1. Press `Shift + F9`, the Transcribe tab opens in the Library
2. Drop an MP3 / MP4 / WAV / M4A / MKV / WebM, or paste a YouTube URL
3. Pick a Whisper model (base, small, large-v3-turbo, large-v3) and language
4. Toggle **Speaker labels** to add diarization (Speaker 1, Speaker 2, ...)
5. Click **Transcribe**, export as TXT, SRT, VTT, PDF, or DOCX

### Audio editor

1. Press `Shift + F10`, the bundled audio editor opens
2. Drop an audio file onto the waveform to load it
3. Trim, splice, fade, mix, export

### Record a macro

1. Press `Shift + F1` to start recording, a red pill appears
2. Do anything: type, click, switch windows, scroll, every step is captured
3. Press `Shift + F1` again to stop
4. Press `Shift + F1` once more to replay
5. Press `Esc` to cancel or stop playback
6. *(Optional)* Name + save + assign a hotkey to frequently-used macros

### Record your screen

1. Press `Shift + F2`, choose **Full screen**, a **window**, or drag to select a **region**
2. *(Optional)* Enable mic audio
3. Click **Start Recording**, dialog disappears, recording begins
4. Press `Shift + F2` again to stop, save dialog appears
5. Save anywhere, MP4 by default

### Record a GIF

1. Press `Shift + F3`, choose a window or region
2. Set FPS and max duration
3. Press `Shift + F3` again to stop
4. Preview, save, or discard

### AI Screenshot

1. Press `PrtSc`, the screen is captured
2. A dialog appears with the screenshot and an AI chat box
3. Ask anything: *"What does this error mean?"*, *"Summarise this page"*, *"What's in this chart?"*

---

## Settings

Everything is configurable. Open the library (`Alt+Shift+E`) and click the gear icon, or right-click the tray icon.

- **AI Provider**, switch between Cerebras and Groq, or enter your own API key
- **Hotkeys**, change any global shortcut to whatever you prefer
- **Voice model**, choose Whisper base (fast), small (default), large-v3 (best), pick your microphone
- **Transcription**, language, beam size, custom vocabulary, diarization on/off
- **Push-to-talk**, hold to record, release to transcribe
- **Autostart**, launch automatically when your computer starts

---

## All default hotkeys

| Action | Shortcut |
|---|---|
| Refine selected text with AI | `Alt + Shift + W` |
| Open Prompt Library | `Alt + Shift + E` |
| Undo last refine | `Alt + Shift + Z` |
| Per-prompt hotkeys | `F1` to `F10` (assignable) |
| Start / stop voice recording | `Ctrl + Enter` |
| Cancel / stop anything | `Escape` |
| Macro recorder | `Shift + F1` |
| Screen recorder | `Shift + F2` |
| GIF recorder | `Shift + F3` |
| Ask Claude about selection / image | `Shift + F4` |
| Web search prompt | `Shift + F5` |
| Run a prompt chain | `Shift + F6` |
| Quick Notes | `Shift + F7` |
| Whiteboard | `Shift + F8` |
| File transcriber | `Shift + F9` |
| Audio editor | `Shift + F10` |
| AI Screenshot | `PrtSc` |

All hotkeys are customisable in Settings. Per-prompt hotkeys are assigned per prompt via right-click → **Assign hotkey**.

---

## AI Providers

Hotkeys works **out of the box** with no setup required, API access is built in.

If you want to use your own key (for higher limits or your own account), both providers are free:

| Provider | Speed | Free tier | Sign up |
|---|---|---|---|
| **Cerebras** | ~0.3 s | ✅ Yes | [cerebras.ai](https://cerebras.ai) |
| **Groq** | ~0.5 s | ✅ Yes | [console.groq.com](https://console.groq.com) |

Sign up → copy your API key → paste it into Settings → done.

Voice-to-text (Whisper), file transcription, speaker diarization, and the whiteboard all run fully offline. No API key required for any of those features.

---

## Privacy

- 🔒 **Voice + file transcription run locally**, Whisper model is bundled, nothing sent anywhere
- 🔒 **Speaker diarization runs locally**, pyannote model is bundled, runs in its own subprocess
- 🔒 **Whiteboard runs locally**, Excalidraw is bundled, no remote calls
- 🌐 **Text refinement** goes to Cerebras or Groq, same as any AI assistant you use
- 🚫 No analytics, no telemetry, no account required

---

## Feature list

| | Feature | Notes |
|---|---|---|
| ✍️ | **AI text refiner** | Works in any app, any text field |
| 📚 | **Prompt library** | 16 built-in prompts, unlimited custom ones |
| ⌨️ | **Per-prompt hotkeys** | Assign F1-F10 to any prompt; fires from any app |
| 🗒️ | **Sticky note popup** | Preview and edit a prompt before it fires |
| 🔤 | **Live spell check** | Misspellings underlined in red as you type |
| 🔍 | **Prompt search** | Find prompts instantly as you type |
| 🔄 | **Drag to reorder** | Organise prompts by drag and drop |
| 🗂️ | **Folders + colors** | Group prompts into folders, color-code them |
| ↩️ | **Undo last refine** | Instantly revert an AI rewrite with `Alt+Shift+Z` |
| 🎙️ | **Voice to text** | Fully offline, Whisper bundled |
| 🔇 | **Noise reduction** | Works cleanly in noisy environments |
| 🚀 | **Push-to-talk** | Hold to record, release to transcribe |
| 📋 | **Transcription history** | Browse and copy past recordings |
| ⚡ | **Instant paste** | Output types directly where your cursor is |
| 🖥️ | **System tray / menu bar** | Zero UI clutter |
| 🎨 | **Whiteboard** | Offline Excalidraw, infinite canvas, all keyboard-driven (`Shift+F8`) |
| 🎤 | **File transcriber** | MP3/MP4/MKV/WebM/M4A/WAV/FLAC + YouTube URLs (`Shift+F9`) |
| 👥 | **Speaker diarization** | Speaker 1/2/... labels via pyannote, runs out-of-process for stability |
| 🎵 | **Audio editor** | Trim/mix/fade/export audio with bundled waveform editor (`Shift+F10`) |
| 🔴 | **Macro recorder** | Record & replay keystrokes + mouse clicks (`Shift+F1`) |
| 💾 | **Saved macros** | Name, save, and assign hotkeys to your most-used macros |
| 🎬 | **Screen recorder** | MP4, optional mic audio, any window or region (`Shift+F2`) |
| 🎞️ | **GIF recorder** | Animated GIF, any region, adjustable FPS (`Shift+F3`) |
| 📸 | **AI screenshot** | Capture + ask AI anything about it (`PrtSc`) |
| 📄 | **Scan & edit** | Document scanner + Lightroom-lite editor in one window. 4-corner perspective fix, Magic Color / Grayscale / B&W modes, auto-orient via Tesseract OCR, free 8-handle crop, ✨ Auto Enhance, sliders for Brightness/Contrast/Saturation/Sharpness/Warmth, undo (`Ctrl+Z`), Save PDF, Copy, Extract text. Right-click any screenshot region → **Scan document**. |
| 📝 | **Quick Notes** | Floating scratchpad with image-paste OCR (`Shift+F7`) |
| 🔗 | **Web search** | Quick-launch web search with selection (`Shift+F5`) |
| ⛓️ | **Prompt chains** | Run several prompts in sequence on the same text (`Shift+F6`) |
| 🔁 | **Hotkey watchdog** | Auto-recovers if hotkeys stop responding |
| 🍎 | **macOS support** | Full feature parity on Mac |

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

---

## Tech stack

| Component | Library |
|---|---|
| UI | CustomTkinter + tkinter |
| Speech-to-text | faster-whisper (offline) |
| Speaker diarization | pyannote.audio + torch (out-of-process worker) |
| Voice activity detection | Silero VAD |
| AI text refinement | Cerebras / Groq API |
| Spell check | pyspellchecker |
| Global hotkeys | keyboard |
| Macro recorder | pynput |
| Screen capture | win32ui + PyAV |
| Screen / GIF encoding | PyAV (FFmpeg) |
| System tray | pystray |
| Whiteboard | Excalidraw + pywebview (Edge WebView2) |
| Audio editor | Tenacity portable (bundled) |
| OCR | Groq vision API |
| Packaging | PyInstaller (onedir, double-spec for Hotkeys.exe + diarize.exe subprocess) |

---

## License

MIT, free to use, fork, and build on.

---

*If this saved you time, consider leaving a ⭐, it helps others find it.*
