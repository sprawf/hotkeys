# Hotkeys — Project Notes

Developer reference for AI-assisted sessions. Complements FIXES.md (bug history).

---

## Project location

**Always edit `E:\Hotkeys` — never the copy on C drive.**

---

## Module map

| File | Purpose |
|---|---|
| `main.py` | App entry point, event queue (`_poll`), hotkey registration, all feature orchestration |
| `library.py` | The main UI window — 5-tab library |
| `overlay.py` | Floating pill notifications (OverlayWindow) |
| `explain_pill.py` | Floating answer pill for the Explain / Shift+F4 feature |
| `screen_recorder.py` | ScreenRecorder class + recordings index (`recordings_index.json`) |
| `gif_recorder.py` | GifRecorder class + GIF index (`gifs_index.json`) |
| `screenshot.py` | PrtSc overlay for region capture |
| `macros/recorder.py` | MacroRecorder (pynput-based) |
| `macros/library.py` | Persisted saved macros |
| `macros/save_prompt.py` | "Save this macro?" dialog |
| `settings.py` | Settings window |
| `storage.py` | All path helpers + config/prompts/history I/O |
| `engine.py` | AI provider abstraction (Groq / Cerebras / Local) |
| `theme.py` | Colour constants shared across all UI files |
| `vision.py` | Groq vision API — OCR images for Explain feature |
| `sticky_note.py` | Per-prompt floating note (per-prompt hotkeys) |
| `dialogs.py` | Shared `confirm()` / `alert()` helpers |
| `spellcheck.py` | Right-click spell-check for text widgets |
| `single_instance.py` | Not a separate import — implemented inline in `main.py` as `_ensure_single_instance()` |
| `build_dist.py` | One-shot PyInstaller build script |
| `hotkeys.spec` | PyInstaller spec |

---

## Library window — 6 tabs

| UI label | Internal key (`_active_tab`) | Hotkey |
|---|---|---|
| ✦ Prompts | `'prompts'` | — |
| ⏺ Macros | `'macros'` | Shift+F1 |
| 🎥 Screen | `'recorder'` | Shift+F2 |
| 🎞 GIF | `'gif'` | Shift+F3 |
| ✦ Explain | `'ask'` | Shift+F4 |
| 🌐 Web | `'web'` | Shift+F5 |

Shift+F5 opens the **active bookmark** directly in the browser (not the library).
Bookmarks stored in `appdata_dir()/bookmarks.json` — works in both dev and dist.

Internal variable names use `ask` (e.g. `_hint_ask_text`, `_render_ask_tab`, `on_ask`) — same thing.

---

## Default hotkeys

Stored in `config['hotkeys']`. All rebindable in Settings.

| Action | Default |
|---|---|
| Refine (active prompt) | Alt+Shift+W |
| Open Library | Alt+Shift+E |
| Undo last refinement | Alt+Shift+Z |
| Whisper dictation | Ctrl+Enter |
| Macro record/stop/play | Shift+F1 |
| Screen record start/stop | Shift+F2 |
| GIF record start/stop | Shift+F3 |
| Explain / ask | Shift+F4 |
| Open active bookmark | Shift+F5 |

Per-prompt hotkeys (F1–F12) are stored per-prompt in `prompts.json` under `"hotkey"`.

---

## State machines

### Screen recorder (`_recorder_state`)
`'idle'` → **Shift+F2** → `'recording'` → **Shift+F2** → `'stopping'` → `'idle'`

- **No setup dialog** — Shift+F2 starts immediately (full screen, no mic, 30 fps).
- `RecorderSetupDialog` still exists in `screen_recorder.py` but is no longer called from `main.py`.
- Files saved outside the default folder are tracked in `appdata_dir()/recordings_index.json`.

### GIF recorder (`_gif_state`)
`'idle'` → **Shift+F3** → shows `GifSetupDialog` → `'recording'` → **Shift+F3 / Esc** → `'encoding'` → `'idle'`

- Files saved outside the default folder are tracked in `appdata_dir()/gifs_index.json`.
- Library tab uses `list_gifs()` from `gif_recorder.py` (not a raw folder scan).

### Macro recorder (`_macro_state`)
`'idle'` → **Shift+F1** → `'recording'` → **Shift+F1** → `'ready'` → **Shift+F1** → `'playing'` → `'ready'`
- Esc / Del aborts recording or playback.
- After playback, `MacroSavePrompt` dialog appears. Save → persisted in `appdata_dir()/macros/`.

---

## Event queue pattern

All hotkey handlers are thin — they only `self._q.put(('event_name', data))`.
`_poll()` runs every 30ms on the main thread and dispatches to `self._dispatch[event]`.
This keeps all UI operations on the main thread (Tkinter requirement).

---

## Overlay pills (OverlayWindow)

Slots 0–4, stacked vertically near top-right of screen:
- 0 — Refine
- 1 — Whisper
- 2 — Macro
- 3 — Screen recorder
- 4 — GIF recorder

Explain (`explain_pill.py`) creates its own floating `AskPill` window independently (not an OverlayWindow slot) — it appears near the cursor.

---

## Data paths

`storage.appdata_dir()` is the single source of truth for all user data:
- **Dev (source):** `%APPDATA%\Hotkeys\`
- **Dist (frozen):** `<exe_dir>\data\` — fully portable, no registry, no APPDATA dependency

All new persistent files (indices, caches, etc.) must use `appdata_dir()`.

---

## ⚠ Hotkey integrity rule

**Every new keyboard binding must:**

1. Have a default in `DEFAULT_CONFIG['hotkeys']` (storage.py)
2. Pass `hotkey_validator.validate_batch()` against EVERY other binding in
   the app (config hotkeys + per-prompt hotkeys + per-chain hotkeys +
   per-macro hotkeys)
3. Be registered inside `_register_hotkeys()` (main.py) so "Reload hotkeys"
   picks it up
4. Surface its current value in any UI label via `library.refresh_hotkeys()`
   so the labels never go stale after a rebind

### What "Reload hotkeys" must always do

`_reload_hotkeys_manual()` in main.py is the user's escape hatch when
the keyboard listener gets into a confused state. It must:

| Step | What | Why |
|---|---|---|
| 1 | Reload config from disk | Pick up any in-flight save the user just made |
| 2 | Call `keyboard.unhook_all()` + force-stop `keyboard._listener` | Stale listener thread → silent dead hotkeys |
| 3 | Re-register every binding from config + prompts + chains + macros | Coverage of every source |
| 4 | Push the new cfg to `library.refresh_hotkeys()` | Live label refresh — header pill, hint bar, tab tooltips |
| 5 | Show "Hotkeys reset ⚡" notification | User confirmation |

If you add a new SOURCE of hotkeys (e.g. per-bookmark hotkeys, per-note
hotkeys), step 3 needs an additional call to register them, AND
`collect_app_hotkeys()` in `hotkey_validator.py` needs to read them so
they're considered in conflict checks.

### Tab-feature shortcuts inside the Library window

Some tabs install local key bindings (Prompts F1-F12 for active-prompt
selection, Whiteboard tool letters, Macros Play/Stop). These are **window-
local** (only fire when that window has focus), so they cannot clash with
global hotkeys at the OS level.

But: the validator's `_BUNDLED_SHORTCUTS` list IS what tells the
whiteboard's reserved-keys interceptor "the host owns these — don't
intercept." When you add or remove a tab-local key, update that list so
the runtime override stays accurate.

### Self-check before declaring "done" on any new hotkey

1. ⬜ Default added to `DEFAULT_CONFIG['hotkeys']`?
2. ⬜ Registered in `_register_hotkeys()`?
3. ⬜ `validate_batch()` passes against all existing bindings?
4. ⬜ "Reload hotkeys" tested — new binding still active afterward?
5. ⬜ Any UI label showing the binding refreshes when user rebinds?
6. ⬜ If it's a tab-local key inside Whiteboard's scope, added to
   `_BUNDLED_SHORTCUTS`?

### When you rename a config hotkey key (e.g. `slot9` → `transcribe`)

- Update `DEFAULT_CONFIG` (storage.py)
- Update `_register_hotkeys()` (main.py)
- Update `_TAB_HOTKEY_MAP` (library.py) if the action is shown as a tab
- Update `library.refresh_hotkeys()` to read the new key
- Migrate existing user configs (or leave the old key dormant in their
  config — it does no harm but is dead weight)

---

## ⚠ Reset-everything completeness rule

**Every change that touches user state must also be reflected in the Reset
Everything flow.** No exceptions.

The "Reset everything…" dialog and code path is the user's escape hatch
when the app gets into a confusing state. It must be **comprehensive**
(actually puts everything back to factory) and **honest** (the dialog
copy lists exactly what will and won't be touched). Drift between what
the app stores and what Reset addresses is one of the most damaging bug
classes — users hit "Reset" expecting a clean slate and get a
half-stale one.

### When you add ANY of these, you must update Reset:

| You added… | What Reset must do | Where |
|---|---|---|
| New config field (e.g. `whisper.audio.cloud_enabled`) | Add a sensible default to `DEFAULT_CONFIG` in `storage.py`. The reset code does `deepcopy(DEFAULT_CONFIG['whisper'])` — if the field is there, it auto-resets. |
| New top-level config key (e.g. `transcripts_history`) | Update `_do_restore_all_defaults` in `main.py` to explicitly handle it. Add a bullet to the dialog body. |
| New persistence file (e.g. `appdata_dir/transcripts/*.json`) | Add cleanup in `_do_restore_all_defaults`. Add a bullet ("X history → cleared"). Use per-file try/except so files in use by a live worker thread don't block the whole reset. |
| New cache dir (`.transcripts_cache/`, `.hf_cache/`, etc.) | Add to the `candidates` list in the transcripts-cleanup block. |
| New hotkey (e.g. `transcribe`, `slot10`) | Default in `DEFAULT_CONFIG['hotkeys']` — already picked up by the existing reset path. |
| New preserved-on-reset thing (e.g. user-picked mic) | Preserve explicitly in `_do_restore_all_defaults` AND list it in the "Won't be touched" section of the dialog. |
| New feature with its own state (macros, recordings, GIFs) | Decide if it gets reset. If yes, wipe it + list in "Will be reset"; if no, list in "Won't be touched". Either way, the user MUST KNOW. |

### Dialog body must stay in plain English

The reset dialog is read by laypeople in a panic. No internal codenames
("whisper", "providers", "VAD"). Examples of the right phrasing:

- ✅ "AI templates → back to the defaults" (NOT "Prompts → bundled defaults")
- ✅ "Voice typing → cloud on, fast local model" (NOT "Whisper → default model, VAD threshold")
- ✅ "AI helper choice → fastest free option" (NOT "Active provider → Cerebras")
- ✅ "Multi-step workflows" (NOT "Chains")
- ✅ "Won't be touched: Your saved API keys" (assurance, not omission)

### When you delete or rename a setting

Update the dialog body AND remove the obsolete bullet. A dialog that
promises to reset `whiteboard_geometry` when that key no longer exists
is exactly the kind of stale UI the user has complained about.

### Self-check before declaring "done" on any feature

1. ⬜ Added new config field → default in `DEFAULT_CONFIG`?
2. ⬜ Added new file/dir → cleaned up in `_do_restore_all_defaults`?
3. ⬜ Dialog body bullet added (or "Won't be touched" added)?
4. ⬜ Dialog language plain-English (no internal codenames)?
5. ⬜ Manually tested: trigger Reset, verify the new feature returns to factory state?

If any box is unchecked, the feature is not done — it has drifted from
the Reset path and a future user will hit it.

---

## ⚠ Feature isolation rule

**Every tab / feature must be self-contained.** Opening, using, or
even importing one feature must NEVER affect the responsiveness,
speed, accuracy, or behaviour of any other.

Concrete examples of what this rules out:

| Cross-feature leak | Why it's banned |
|---|---|
| Module A's import sets process-wide state (env vars, OMP threads, signal handlers, monkey-patches, locale, sd.default) | Affects every other feature in the same Python process — and the side effect is invisible to whoever uses A. |
| Module A's heavy import warmed-up a model that A doesn't release | Live dictation (B) inherits A's memory pressure. If B is on a low-end PC, this matters. |
| Module A's worker thread does not yield CPU when A's UI isn't active | Mic dictation (B) gets starved while A churns in the background. |
| Module A throws an exception that bubbles up to the event loop and breaks dispatch for B | One bad path in A kills hotkeys for the whole app. |
| Module A modifies shared config (active provider, mic index, theme) when it merely needs to read it | B sees its config silently mutated by an unrelated action. |
| Module A's hotkey/binding clashes with B's without the conflict checker catching it | Pressing the bound key fires the wrong feature. |

### The rules

1. **Lazy-import** heavy stuff inside the function that uses it, not at
   the top of the module. Cost is paid only when the user invokes
   the feature.
2. **Never** set process-global config (`torch.set_num_threads`,
   `os.environ['OMP_*']`, `sd.default.device`, `signal.signal`) at
   import time. If you must, do it inside the specific operation that
   needs it, and *restore* the previous value when done.
3. **Each worker thread** is owned and cancellable by exactly one
   feature. No global thread pools shared across features.
4. **Every except-clause** in a feature must keep the failure local —
   bubble a clean error to the user, leave the app's main event loop
   untouched.
5. **Shared config is read-only** unless the feature owns it. A tab
   that only displays a value never writes it back, even on Save
   "no-op" code paths.
6. **Hotkey conflicts** must go through `hotkey_validator.py`, which
   knows about every feature's bindings.
7. **First-class assumption**: a user who never opens feature A should
   not be able to tell, by any measurement, that A exists in the dist.

This is non-negotiable. Any change that violates it must be rewritten,
not patched around.

---

## ⚠ Think-dist-first rule

**Every change, every addition, every fix — think from the dist user's
angle before declaring done.** End users don't have a developer to fix
problems. If they hit an error message, they're stuck unless the app
solved it for them. The bar isn't "works on my machine" — it's "works
on a freshly imaged Windows laptop with default settings, no extras
installed, no manual setup."

For every code change, walk through this checklist:

| Question | If "yes" / "maybe" |
|---|---|
| Does it depend on a Python package? | Add to `hotkeys.spec` (hiddenimports + collect_all). |
| Does it depend on a binary (ffmpeg, DLL, model file)? | Bundle in `datas` / `binaries`. |
| Does it depend on a HuggingFace model? | Pre-download on dev box, bundle the cache dir under `assets/`. Set `HF_HOME` to a project-local dir at module import so on-demand fetches never go to `C:\Users\<x>\.cache\`. |
| Does it call out to the network? | Use `truststore.inject_into_ssl()` early; assume corporate AVs MITM TLS. Handle offline gracefully with `engine.is_offline_error()` / `friendly_error_message()`. |
| Does it touch the filesystem? | Use `storage.appdata_dir()` / `models_dir()` / `assets_dir()` — these resolve to `<exe>/data` or `<_MEIPASS>` when frozen. **Never** hardcode a path on C: or in `__file__.parent` (read-only in dist). |
| Does it require a device (mic, GPU, camera)? | The selected device may not exist on the user's box. Fall back to the system default; if that fails, fall back to CPU / silence / a clear actionable error. **Never** show "permission denied" wording when the real cause is something else (e.g. invalid sample rate). |
| Does it assume a sample rate / format / channel count? | Probe the device first; resample / re-format on the fly in the callback. |
| Does it write to the user's config? | The user may have customized things — preserve their intent. Don't silently change `input_device_index` / hotkey assignments / active prompt / etc. without telling them. |
| Could it fail mid-operation? | Clean up partial state in the error path (open file handles, temp downloads, threads). |
| Does it require Visual C++ runtime, .NET, WebView2? | Verify the dist user has it (Win10 1809+ ships WebView2; older Win10 needs the bootstrapper). Document the floor in README if any.|
| Would a corporate antivirus quarantine the binary? | yt-dlp, screen recorder, keyboard listener are common false-positives. Document the risk; consider code-signing eventually. |

### "How will users resolve this?" failure modes

Whenever you add an error path, mentally simulate a non-developer hitting
it. **The error should either:**

1. **Self-heal silently** — the app retried with a fallback and works
   (preferred — e.g. `core/audio.py` device fallback to system default).
2. **Show a clear actionable dialog** — not the Python exception class
   name; not "ffmpeg returned 1"; **the actual fix.** See `_show_mic_error`
   for the pattern: categorise the underlying error string and tailor
   the suggested fix to it.
3. **Fail loudly with a way out** — if recovery is impossible, the
   dialog must include either: a link to fix it (Windows Settings path),
   a copy-pasteable command, or a clear "right-click tray → Settings
   → X" path. **Never** a stack trace.

### Anti-patterns that have bitten us before

| Smell | Why it broke for dist users |
|---|---|
| `pip install` at first run | Dist has no pip; venv site-packages are baked into `<_MEIPASS>`. Anything that's not in `hotkeys.spec` doesn't exist. |
| HuggingFace `from_pretrained('repo/id')` without local fallback | Gated model → silent crash on a fresh install with no token. Pre-download + bundle. |
| `subprocess.run(['ffmpeg', ...])` relying on PATH | Most Windows users don't have ffmpeg installed. Resolve via `imageio_ffmpeg.get_ffmpeg_exe()` and pass `ffmpeg_location` to yt-dlp. |
| Saved device index in config | Devices come and go (USB unplugged, virtual mics uninstalled). Always have a self-healing fallback to `device=None`. |
| `requests.get(url, verify=True)` without truststore | Corporate AVs (AVG, Norton, etc.) MITM TLS and the venv `certifi` bundle doesn't trust the local CA. Always inject truststore. |
| `Path(__file__).parent / 'asset.bin'` | In frozen dist `__file__` lives under `<_MEIPASS>` which is read-only and deleted on exit. Use `storage.assets_dir()` for read-only bundled assets, `storage.appdata_dir()` for writable state. |
| Tk widget touched from a worker thread | Crashes with "main thread is not in main loop". Marshal via `root.after(0, ...)`. |
| Hot-loading a Python module change without restart | Doesn't work. If a code change matters, an app restart is needed. **Always tell the user before restarting.** |
| Hardcoding test machine specifics | A device index, mic name, file path, locale-specific string — anything that's "obvious on the dev machine" is a dist landmine. |
| **Calling `torch.set_num_threads()` at module import time** | torch's API sets OpenMP globally for the whole process. CTranslate2 (which Whisper uses) reads the same OpenMP config — so a "let's reserve 2 cores for the UI" call in feature A silently throttles feature B's hot path. If you need to constrain a specific feature's threads, do it via that feature's OWN config (e.g. CTranslate2's `cpu_threads` param, pyannote's `inference_kwargs`), never via a process-global setter. |
| Feature A's import has side effects on feature B's performance | Lazy-import expensive stuff inside the function that uses it. If an `__init__` or top-of-module call mutates global state (env vars, OMP threads, fork patches, signal handlers, sd.default), it'll bite an unrelated code path eventually. |

### When responding to the user

Tag every change explicitly:

- `[ships in dist]` — Python source / spec / bundled asset. Affects every installer.
- `[your-machine-only]` — config edit / data file / dev-only smoke. Doesn't go anywhere.

If a change is `[ships in dist]`, finish by stating: **how a fresh dist
user would experience this in each failure mode**, and confirm there's
no new dependency that isn't bundled.

---

## ⚠ Tooltip & hover-help rule

**A tooltip's only job is to add what the user can't read from the
visible label.** Never repeat the label name in its own tooltip,
never paste the same instruction across multiple surfaces, never
write paragraphs when one line covers the gap.

The visible surface (button text, dropdown label, menu entry) is
ground truth. The tooltip exists to fill the gap between what that
surface says and what the user needs to act on it.

Checklist before writing any tooltip / hover hint / placeholder:

| Question | If "yes" |
|---|---|
| Does my tooltip restate words already in the label? | Cut them. |
| Does the same instruction appear on N tooltips? | Move it ONCE to a hint bar or header. |
| Is the tooltip > ~15 words? | The first half is almost always re-explaining the label. Trim. |
| Could a layperson with no tech background act on this in 3 seconds? | If no, rewrite — clearer, not longer. |
| Does it contain an acronym (LRC, SRT, OCR, VTT)? | The tooltip exists for exactly that — explain the acronym + name the canonical app it opens in. |

Examples of good tooltips:

- Button `📷  Paste Image` → tooltip: `Copy an image to clipboard, then click to extract its text.`
   (Adds the workflow, doesn't repeat "paste image".)
- Dropdown label `Fast — good for clear speech` → tooltip: `~74 MB on disk · ~2-3× realtime on CPU`
   (Adds spec, doesn't repeat the speed claim.)
- Export button `LRC` → tooltip: `Synced lyrics for karaoke / lyric videos. Reads in VLC, Spotify, Aegisub.`
   (Explains the acronym + canonical apps. Doesn't restate "lyrics".)

This rule applies app-wide. When the user gives layperson-UX
feedback about one tooltip, the principle propagates to every
surface — Library tabs, Settings, Quick Notes, Whiteboard, tray
menu, every dropdown and entry placeholder.

---

## Dist build

```
E:\Hotkeys\venv\Scripts\python.exe E:\Hotkeys\build_dist.py
```

Output: `E:\Hotkeys\dist\Hotkeys\` — zip the whole folder to ship.

`build_dist.py` steps:
1. PyInstaller via `hotkeys.spec`
2. Copy pywin32 DLLs to dist root (required for win32ui/win32gui)
3. Copy `macros/` package to dist root as plain `.py` (PyInstaller misses subpackages)
4. Verify critical files present
5. Print total size

**`hotkeys.spec` hidden imports must include every app module** — PyInstaller misses modules only referenced through `_dispatch` dicts or late imports. `explain_pill` is there; check if adding new modules.

---

## Key architecture rules (see FIXES.md for full history)

1. **One `tk.Tk()` per process** — all windows are `Toplevel` children of `self.root`
2. **All Tkinter calls on the main thread** — use `root.after(0, fn)` from threads
3. **`suppress=False` always** for `keyboard.add_hotkey()` — suppress=True corrupts modifier state permanently
4. **Win32 `SendInput` for Ctrl+C/V/Z** — never `keyboard.send()` for injected keystrokes
5. **All HTTPS calls via `_robust_post()`** — handles AVG/antivirus SSL inspection transparently
6. **Set error state before raising** — "running" flags must be reset in the exception path
