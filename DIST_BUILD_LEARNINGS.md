# Hotkeys dist build — hard-won lessons (May 2026 session)

> **Read this BEFORE making any change to `hotkeys.spec`, `library.py`, or rebuilding the dist.**
> Every item below cost real debugging time. Don't repeat the mistakes.

---

## HOLISTIC CHANGE CHECKLIST (run before shipping any new feature)

Every feature touches more of the system than it looks. Before considering a change "done," walk this checklist mentally:

1. **Reset Everything button** (`_do_restore_all_defaults`) — Does your new feature add any **persisted config**? Add it to the reset path. Does it add any **transient in-memory state** (dedupe sets, rate-limiter dicts, last-error caches)? Clear those in the reset path too (see the `Transient cross-call state` block) so the user gets a true clean slate.
2. **Stop everything & reload hotkeys** (`_reload_hotkeys_manual`) — Toplevel windows you create get destroyed by this. Make sure your code self-heals on next access (check `winfo_exists()` before using, rebuild if dead). The Tray History fix is the canonical pattern.
3. **Tab cache invariant in Library** — If your feature changes any tab's data, route through `_invalidate_tab(tab)`, never call `_render_xxx_tab()` directly.
4. **Child-widget event absorption** — If your feature adds a draggable / double-clickable Frame, bind on every non-interactive child too (Labels absorb clicks). Don't bind on Buttons (they have their own action).
5. **Offline / AV-blocked paths** — Any new cloud call: use the existing reachability probe pattern (TCP probe in `_robust_post` for refine, `_cloud_reachable` in transcriber). Don't let an unreachable host hang the UI.
6. **Dist build** — Source-only changes get into the dist on the next `build_dist.py` run automatically. Things to verify after rebuild: hidden imports for any new package, asset bundling in `hotkeys.spec`, no new MKL/torch transitive deps that would bring back the heap-corruption crash.
7. **Audit the log after testing** — Don't trust "looks fine." Tail `app.log` and grep for `WARNING|ERROR|Traceback|unhandled`. Silent failures hide here.
8. **Document it here** — If you debugged something that took >30 min, add it to this file. Future-you (and future agents) will thank present-you.

---

## RECURRING UI BUGS (the ones that keep coming back across sessions)

These are non-crash bugs that have been "fixed" multiple times but keep reappearing because the fix can be silently regressed by anyone adding new code without knowing the invariant. **Read this section before touching `library.py` or any tab-related code.**

### Open: rare transient tab-content crossover (resolves on restart)

Observed once during May 2026 session: after some sequence of macro/recorder/notes operations, the Whiteboard tab visually showed the Notes content (📝 Quick Notes header + "Open Quick Notes" button), and clicking Notes got stuck. After killing + relaunching Hotkeys, both tabs render correctly again.

Could not reproduce after restart. The `_invalidate_tab` fixes for `update_recorder_state` / `update_gif_state` / `refresh_macros` reduce the surface (those were writing to `self._scroll` directly instead of per-tab containers), but the root cause that lets prewarmed tab content end up in a different tab's container wasn't fully traced. Next session: add an `assert self._scroll is self._tab_containers[self._active_tab]` inside each `_render_xxx_tab()` to make the corruption fail loudly the moment it happens, so we can capture the call stack.

### Tab cache MUST go through `_invalidate_tab(tab)`

`library.py` has a per-tab container cache (`self._tab_containers[tab]` + `self._tab_built` set) to keep tab-switching fast. The invariant is:

- Reading from tab (`_show_active_tab`): may use the cached container directly.
- **Mutating tab state**: MUST call `self._invalidate_tab(tab)`. NEVER call `self._render_xxx_tab()` directly.

Why: `_render_xxx_tab()` writes into `self._scroll`. But the per-tab content actually lives inside `self._tab_containers[tab]`. Direct calls leave the new widgets in the wrong parent + leave the cache marker (`_tab_built`) stale, so the next tab visit returns to the empty cached container.

**Symptom of getting it wrong**:
- Saving a screen recording to a non-default folder doesn't show up on the Recorder tab
- Deleting a macro doesn't remove its card
- Switching from Macros → Recorder shows the Macros content stuck
- New recording state changes don't reflect when user isn't already on the right tab

**The rule**: the ONLY method allowed to call `_render_recorder_tab()` / `_render_gif_tab()` / `_render_macro_cards()` directly is `_render_cards_impl`. Everywhere else, use `self._invalidate_tab('recorder')` (etc).

Grep `_render_recorder_tab(` — if there's more than ONE callsite (inside `_render_cards_impl`), it's a bug.

### Ask Claude (Shift+F4) capture priority MUST be selection > clipboard image

`_capture_and_queue_ask` in `main.py`. The correct order is:

1. Active screenshot overlay selection → OCR
2. **Selected text** (send Ctrl+C, wait for clipboard text to populate) — THIS WINS
3. Image in clipboard → OCR (only if no fresh selection)

Doing #3 before #2 (the old bug) means a stale screenshot lurking in the user's clipboard from hours ago wins over a freshly-selected question like "Why is the sky blue?". Always check selection first; image OCR is a fallback.

Also: when there's no selection AND no image, show `self.refine_overlay.show_no_selection()` — the same compact "No text selected" toast Refine uses — NOT a chat-like AskPill with "Select text first".

### `huggingface_hub` MUST be anchor-imported in `transcribe/engine.py`

Even with `'huggingface_hub'` in spec `hiddenimports` and `collect_submodules('huggingface_hub')`, PyInstaller's static analyzer kept dropping the package when nothing imported it at module level (the only use is a lazy `from huggingface_hub import snapshot_download` inside `_ensure_model_downloaded`). Without the package, on-demand large-v3 / large-v3-turbo downloads fail with ModuleNotFoundError.

**Fix**: at the top of `transcribe/engine.py`, keep this anchor import:

```python
try:
    import huggingface_hub as _hf_anchor  # noqa: F401
except Exception:
    _hf_anchor = None
```

Don't remove it. Don't move it inside a function. PyInstaller's static analyzer must see it at module scope.

### Don't add hollow placeholder hotkeys

F11/F12 used to be "Slot 11/12 reserved, feature TBD" placeholder hotkeys that just toasted "Coming soon". Don't add hotkeys that don't have a real feature behind them — they leak into the user's hotkey list, the Library tab grid, the config defaults, and the conflict-check UI, all of which then have to be reset for every new feature added later. Wait until a feature is real, then add its hotkey.

---

## The crash that ate a full session

### Symptom
Frozen `Hotkeys.exe` crashes at random points after startup. Tray icon either never appears, or appears briefly then ghosts. No Python exception in `app.log`, no Python traceback.

Windows Event Log → Application Error 1000:
- **Exception code:** `0xc0000409` (STATUS_STACK_BUFFER_OVERRUN / `__fastfail`)
- **Fault offset:** `0x1c325` inside `Hotkeys.exe` (PyInstaller bootloader code)
- Deterministic offset, random timing → **heap corruption**, not a logic bug.

### Root cause
**Duplicate native runtimes fighting in the same process.** When `torch` (PyTorch CPU build), `pyannote.audio`, `torchaudio`, and `soundfile` are bundled alongside `ctranslate2`, `onnxruntime`, `numpy`, and `av`, multiple copies of MKL / OpenMP / BLAS load into the same address space. Each C extension was compiled against ITS OWN copy. Windows uses the first DLL it finds; the others' code remains bound to their build-time copy. ABI mismatch → heap corruption → fastfail.

This DOES NOT happen in `pythonw main.py` because dev-mode loads libraries lazily from `venv/site-packages/` with a different process layout. **Source working ≠ dist working.**

### Fix
In `hotkeys.spec`, force-exclude torch, pyannote, torchaudio, soundfile via `excludes` list:

```python
excludes = [
    # ... existing excludes ...
    'torch', 'torchaudio',
    'pyannote', 'pyannote.audio', 'pyannote.core',
    'pyannote.database', 'pyannote.metrics', 'pyannote.pipeline',
    'soundfile', 'lightning', 'pytorch_lightning', 'tensorboard',
]
```

ALSO comment out their `collect_all()` calls and binary/data/hidden-import additions — `excludes` alone wasn't enough, PyInstaller kept pulling them via transitive deps. The combination of `excludes` + dropped explicit collection is what finally worked.

### Side effect
Shift+F9 transcribe loses **speaker labels** (pyannote diarization). The transcribe pipeline already has a `try/except` (`transcribe/engine.py:578`) that gracefully degrades to no-speaker-labels mode. Transcript text still works perfectly.

### Speaker labels restored via out-of-process diarization (DONE)

`diarize.exe` is a separate PyInstaller-built exe at `dist/Hotkeys/diarize/diarize.exe` (~62 MB exe + ~750 MB `_internal/` torch+pyannote). When the main app needs speaker labels, `transcribe/engine.py:_run_diarization_subprocess`:

1. Decodes the audio in the main process using faster-whisper's PyAV path → mono 16 kHz float32 numpy array
2. Creates a temp work dir, writes `input.npy` (waveform) + `input.json` (config)
3. Spawns `diarize.exe <work_dir>` with full subprocess isolation (`DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB`, DEVNULL stdio, close_fds)
4. Polls for `<work_dir>/output.json` (worker writes JSON `{ok, turns: [[start, end, label]...]}`)
5. Cleans up the temp dir

Worker has its own clean heap. Torch/MKL/OpenMP runtimes never collide with main exe's ctranslate2/onnxruntime/numpy. Tested with pyannote's sample 30 s clip → returned 12 turns across 2 speakers in 64 s on this machine. Source files:
- `diarize_worker.py` — the worker script (~150 lines)
- `hotkeys_diarize.spec` — separate PyInstaller spec with everything-not-torch-pyannote excluded
- `build_dist.py` — Step 3b builds the worker into `dist/Hotkeys/diarize/`
- `transcribe/engine.py` — `_run_diarization_subprocess` (frozen mode), original `_run_diarization` (dev mode, unchanged)

Dev mode (`pythonw main.py`) still imports pyannote directly since the venv loads libraries lazily without the bundling conflict.

Note: pyannote 4.x's `DiarizeOutput` exposes `.exclusive_speaker_diarization` (one-speaker-per-moment) and `.speaker_diarization` (may overlap). Both are `None` on all-silence audio; the worker handles that gracefully (returns empty turns list) instead of erroring.

---

## Don't EVER exclude these from the spec

PyInstaller's `excludes` list is treacherous. Some entries seem harmless but break runtime:

| Module | Why you can't exclude it |
|---|---|
| `unittest` | `scipy.signal.resample` lazily imports it for input validation. Excluding it causes "No module named 'unittest'" on every audio chunk, eventually crashing the audio callback thread with `0xc0000005` ACCESS_VIOLATION |
| `test` | Generic stdlib `test` package; multiple deps probe it |
| `tkinter.test` | Tk widget code occasionally references it for type-introspection |
| Anything `*.test` / `*.tests` at stdlib level | Same story |

**Safe to exclude**: `sklearn.datasets.tests`, `sklearn.tests`, third-party test dirs that the library proves it doesn't import at runtime. Verify by `grep -r 'unittest' venv/Lib/site-packages/<package>` before excluding anything stdlib-ish.

---

## Defensive env vars at top of `main.py`

Before any heavy import, set:

```python
os.environ.setdefault('KMP_DUPLICATE_LIB_OK',  'TRUE')
os.environ.setdefault('OMP_NUM_THREADS',       '1')
os.environ.setdefault('MKL_NUM_THREADS',       '1')
os.environ.setdefault('OPENBLAS_NUM_THREADS',  '1')
os.environ.setdefault('NUMEXPR_NUM_THREADS',   '1')
```

These tell Intel/Open OpenMP "yes there might be duplicates, tolerate them" and force single-thread to prevent runtime races. They DID NOT fix the torch/pyannote heap corruption on their own (we still had to exclude those), but they make the bundle safer against future duplicate-runtime issues.

---

## Diagnostic toolkit for the next native crash

If `Hotkeys.exe` crashes again with no Python traceback, **don't add `logger.info()` lines**. Use these instead:

### 1. `faulthandler` (Python stdlib, free)

In `main.py` BEFORE any other import:

```python
import faulthandler
crash_fh = open('data/crash.log', 'a', buffering=1)
faulthandler.enable(file=crash_fh, all_threads=True)
faulthandler.dump_traceback_later(timeout=5, repeat=True, file=crash_fh, exit=False)
```

- `enable(all_threads=True)` catches segfaults / heap-fastfails / stack overflows the instant they fire. Dumps Python call stack of every thread to the file BEFORE the process dies.
- `dump_traceback_later(5, repeat=True)` appends a snapshot every 5 seconds, so even if the crash bypasses faulthandler we see WHERE the app was sitting before death.

### 2. Win32 SEH filter

Catches the lower-level Windows exceptions faulthandler misses:

```python
import ctypes
LPTOP_FILTER = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
def _seh(ep):
    code = ctypes.cast(ep, ctypes.POINTER(ctypes.c_uint))[0] if ep else 0
    crash_fh.write(f'*** WIN32 SEH: code=0x{code:08x} ts={time.strftime("%H:%M:%S")} ***\n')
    crash_fh.flush()
    return 0  # EXCEPTION_CONTINUE_SEARCH
SEH_REF = LPTOP_FILTER(_seh)
ctypes.windll.kernel32.SetUnhandledExceptionFilter(SEH_REF)
```

Logs the exception code (e.g., `0xc0000005` = access violation, `0xc0000409` = stack buffer overrun) and timestamp the instant the OS notices the fault.

### 3. Reading the output

`E:\Hotkeys\dist\Hotkeys\data\crash.log` accumulates across launches. The first stack snapshot after a crash will show the offending thread. **Match thread IDs across the file** — the same thread ID in the 5-sec snapshot right before a SEH line tells you which Python code was running when the C-level fault fired.

### 4. Don't use Application Event Log alone

The fault offset there is in the PyInstaller bootloader's address space (Hotkeys.exe), NOT in your Python code. Tells you the OS noticed a fault but not WHERE in Python. Useful only as confirmation that a crash happened — not for finding the cause.

---

## AVG / Defender quirks on this dev machine

### IDP.HELU.PSD11 — behavioral heuristic
AVG's behavioral scanner flags PyInstaller-bundled apps that combine:
- Low-level keyboard hooks (`keyboard`, `pynput`)
- Screen capture (`win32ui`, `mss`)
- Network egress (`httpx` to AI APIs)
- Bundled foreign exe (`tenacity.exe`)
- Self-spawn (`hotkeys.exe --whiteboard`)
- High file entropy (PyInstaller bundles compress badly)

After ~5-6 launches AVG's "score" tips over the threshold and quarantines the exe on sight. Restoring + adding-to-exception is per-hash, so a fresh rebuild gets flagged again.

### Behavior Shield blocks files during extract
**`Expand-Archive` and `[System.IO.Compression.ZipFile]::ExtractToDirectory` silently drop files** when AV is real-time scanning a large multi-GB extract. We saw ~2065 of 16699 files vanish from `C:\Users\User\Downloads\Hotkeys\` extract — yt_dlp .py files, whiteboard bundle, etc.

**Use file-by-file extract** with explicit per-file try/catch:

```powershell
Add-Type -AssemblyName System.IO.Compression.FileSystem
$arc = [System.IO.Compression.ZipFile]::OpenRead($zip)
foreach ($e in $arc.Entries) {
  if ($e.FullName.EndsWith('/')) { continue }
  $target = Join-Path $dst $e.FullName.Replace('/','\')
  $dir = Split-Path $target
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
  try { [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true) } catch {}
}
$arc.Dispose()
```

### AVG exclusion gotchas
- "Add folder exception" REJECTS `C:\Users\User\Downloads\Hotkeys` as "too broad" because Downloads is itself a high-risk path. Use `E:\Hotkeys` (or any non-Downloads location) instead.
- File-level exception is by hash. Every rebuild needs re-whitelisting.
- Once SignPath signing lands, this all goes away — signed binaries get AVG's higher trust threshold.

---

## Process-launch gotchas during testing

### Job-object inheritance
PowerShell child processes inherit the session's Job Object on some Claude Code harness setups. When the harness's bash command ends, the Job is closed, taking the child process with it. Symptom: log shows `Tray started.` + `Hotkeys v1.0.0 started.` then process vanishes a few seconds later with no fault.

**Reliable launch from automation**:
- `cmd /c start /B '' '<exe>'` — properly detaches in MOST cases
- **More reliable**: have the user manually double-click. Don't trust automated launch results for "did the app survive 30 seconds?" tests.

### Conflating real crashes with job-object terminations
If you see `0xc0000409` in event log, that's a real crash. If you see process vanish with NO event log entry, that's usually job-object termination — re-test by user double-click.

---

## What previous dist versions did (working baseline)

- **v3.0** (May 27): Worked on this machine. **Did NOT bundle torch / pyannote / yt_dlp / imageio_ffmpeg / pyannote diarization assets.** Smaller dist (~700 MB). No Shift+F9 transcribe-with-diarization, but everything else.
- **v3.1+ (this session's broken builds)**: Added Shift+F9 transcribe with diarization → bundled torch + pyannote → heap corruption.
- **Current fix**: v3.1 features + torch/pyannote force-excluded. Speaker labels lost but transcribe text works. ~35 MB exe, ~1.5 GB dist.

---

## Spec changes recap (what's IN the current working spec)

In `hotkeys.spec`:

✅ **Keep**:
- `collect_data_files('faster_whisper')`, `customtkinter`, `spellchecker`, `tkinterdnd2`, `ctranslate2`, `_sounddevice_data`, `onnxruntime`
- `collect_all('av')`, `collect_all('pynput')`, `collect_all('yt_dlp')`, `collect_all('imageio_ffmpeg')`
- Hidden imports for app modules (`storage`, `engine`, `library`, ...)
- Bundled whisper models + diarization assets + audio_editor_assets + whiteboard_assets
- Brand icon generation up front

❌ **Drop** (in `excludes`):
- `torch`, `torchaudio`, `pyannote*`, `soundfile`, `lightning`, `pytorch_lightning`, `tensorboard`
- `llama_cpp`, `matplotlib`, sklearn test data
- DO NOT add: `unittest`, `test`, `tkinter.test` (stdlib runtime deps)

❌ **Don't `collect_all`**:
- `pythonnet` — caused heap corruption when added. `clr_loader`'s `collect_all` is enough for pywebview.

---

## Whiteboard — CONFIRMED WORKING in dist (after the three-pronged fix below)

End-to-end verified: `Shift+F8` opens the Excalidraw whiteboard. Main process stable through the whole IPC test sweep (14/14 features). The three fixes that finally made it work, in order of importance:

## Whiteboard subprocess launch — the THREE-pronged fix

`subprocess.Popen()` directly spawning `Hotkeys.exe --whiteboard` killed the parent process EVERY time, even with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB | DEVNULL stdio | close_fds=True`. No fault event was generated; the parent process simply vanished. We never fully diagnosed WHY direct spawn killed the parent (suspected Edge WebView2 COM activation under same AUMID), but three changes combined to fix it:

### 1. PowerShell intermediary
Instead of `subprocess.Popen([exe, '--whiteboard'])`, do:

```python
ps_script = f"Start-Process -FilePath '{exe}' -ArgumentList \"--whiteboard\" -WindowStyle Hidden -WorkingDirectory '{cwd}'"
subprocess.Popen(
    ['powershell', '-NoProfile', '-WindowStyle', 'Hidden', '-Command', ps_script],
    creationflags=subprocess.CREATE_NO_WINDOW,
    close_fds=True,
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
```

PowerShell launches the actual whiteboard process and exits within ~1s. The whiteboard process becomes orphaned (re-parented to System) — zero linkage back to Hotkeys main process.

### 2. --whiteboard short-circuit at the TOP of main.py
The whiteboard subprocess re-runs `main.py` from the top. Without an early exit, it imports every heavy module (engine, library, transcribe, faster_whisper, ctranslate2, etc), wasting ~10s and ~500MB RAM in the subprocess. Worse, that import wave somehow tangled with the parent's already-loaded state. Solution: check `--whiteboard` in `sys.argv` BEFORE any other top-level import in main.py and short-circuit to `from whiteboard import main as _wb_main; _wb_main(); sys.exit(0)`.

### 3. Hidden imports for stdlib lazy-import chains
PyInstaller's static analyzer can't follow:
- `webview/http.py` → `from wsgiref.simple_server import make_server` → `import http.server`
- bottle / wsgiref helpers use various `xml.etree` / `socketserver` modules

Add to `hiddenimports` in spec:

```python
'http', 'http.server', 'http.client',
'wsgiref', 'wsgiref.simple_server', 'wsgiref.util', 'wsgiref.headers',
'wsgiref.handlers', 'wsgiref.validate',
'socketserver',
'xml', 'xml.etree', 'xml.etree.ElementTree',
```

Without these, the subprocess crashes with `ModuleNotFoundError: No module named 'http.server'` (or similar) and shows the MessageBox in the user's face.

### Note on offline behavior
The `http.server` requirement is purely **local loopback** (127.0.0.1). pywebview serves the bundled Excalidraw HTML/JS from disk to the embedded Edge WebView2 via a local mini server. No internet involved, no DNS, no remote endpoints. Whiteboard works 100% offline once WebView2 runtime is installed (pre-installed on Win11 and Win10 21H2+, free Microsoft download for older Win10).

---

## DETACHED_PROCESS is required for subprocesses

When the main process spawns whiteboard.py or tenacity.exe via `subprocess.Popen`, the child MUST be spawned with `DETACHED_PROCESS | CREATE_NO_WINDOW`. **NOT just `CREATE_NO_WINDOW` alone.**

### Symptom of getting it wrong
- Child process spawns fine
- ~5-10 seconds later, MAIN process crashes with `ACCESS_VIOLATION (0xc0000005)` inside `python312.dll`
- Crash signature is unrelated to the child process — it's the parent's Popen object finalizer touching the inherited child handle

### Why DETACHED_PROCESS matters
Without it, the child stays in the parent's process group. The parent's `Popen` object retains a handle that the parent's GC finalizes later. When GC runs (`__del__` on Popen), the subprocess module tries to clean up the child handle. If the child is still alive AND the parent didn't fully detach, the cleanup touches state in an unsafe way → access violation.

### Side bonus
Also keep a **hard reference** to the Popen object (e.g. `self._wb_proc = proc`) so it lives as long as the child does. Don't let it be GC'd while the subprocess is running.

### AVG concern is real but resolved by signing
The combo `CREATE_NO_WINDOW | DETACHED_PROCESS` from a parent re-exec is flagged by AVG/Avast/Norton as keylogger-class. For signed builds (SignPath / EV / MS Store) this is fine — the signature outweighs the heuristic. For unsigned local installs, the per-folder AVG exception handles it. Either way, **DETACHED_PROCESS must be set** because the alternative is crashing the parent process.

---

## SignPath signing (in progress)

- Applied at https://signpath.org/apply for sprawf/hotkeys
- Free OS plan, 2-7 day human review
- GitHub Action workflow already written at `.github/workflows/build_win_signed.yml` — needs SignPath's project slug + org ID + API token filled in after approval
- Once signed, AVG/Defender heuristics no longer trigger; family members get zero-friction install

---

## Quick build/test loop

1. Edit source
2. `E:\Hotkeys\venv\Scripts\python.exe E:\Hotkeys\build_dist.py` → ~7-10 min
3. Look for "BUILD COMPLETE" or "BUILD FAILED" at the end
4. If FAILED → `build_dist.py` lists the missing file. Either add to spec or it's a real missing asset.
5. Manual launch: double-click `E:\Hotkeys\dist\Hotkeys\Hotkeys.exe`
6. Watch `data\app.log` for normal startup → `Tray started.` + `Hotkeys v1.0.0 started.`
7. If crash: read `data\crash.log` for the thread stack snapshot at time of fault. Match thread ID with any preceding 5-sec dump to find what was running.

Build pipeline is solid now. Don't change `excludes` or `collect_all` lists casually — every change is one rebuild + one launch test minimum.
