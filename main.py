"""
Hotkeys — unified text refinement + speech-to-text app.
Merges PromptRefiner (Groq / Cerebras / local Qwen) with KaiWhisper (faster-whisper).
One tray icon, both features, keyboard-library hotkeys.
"""
import os
import sys
import time
import queue
import socket
import ctypes
import logging
import logging.handlers
import threading
import tkinter as tk
import datetime

import customtkinter as ctk
import keyboard
import mouse
import pyperclip
import pystray
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from storage  import (
    load_config, save_config, load_prompts, save_prompts,
    appdata_dir, log_path, models_dir, assets_dir, history_path,
    save_history, load_history, make_whisper_cfg, _HISTORY_MAX_ENTRIES,
)
from engine      import build_provider, LocalProvider, Provider, local_provider_available
from overlay     import OverlayWindow
from library     import LibraryWindow
from sticky_note import PromptStickyNote
from settings    import SettingsWindow
from history_ui  import HistoryWindow
from core.audio       import AudioCapture
from core.vad         import SileroVAD
from core.transcriber import Transcriber
from core.typer       import copy_to_clipboard, paste_from_clipboard, copy_selection, undo_last
from core.sounds      import play_start, play_stop
from screenshot       import take_screenshot, start_prtsc_listener
from macros.recorder      import MacroRecorder
from macros.library       import MacroLibrary
from macros.save_prompt   import MacroSavePrompt
from screen_recorder      import Recorder as ScreenRecorder, RecorderSetupDialog, show_save_dialog
from gif_recorder         import GifRecorder, GifSetupDialog, show_gif_save_dialog

ctk.set_appearance_mode('dark')
ctk.set_default_color_theme('dark-blue')

# ── Logging ───────────────────────────────────────────────────────────────────

os.makedirs(appdata_dir(), exist_ok=True)
_log_handler = logging.handlers.RotatingFileHandler(
    log_path(), maxBytes=1_000_000, backupCount=3, encoding='utf-8',
)
_log_handler.setFormatter(logging.Formatter('%(asctime)s  %(levelname)-8s  %(name)s: %(message)s'))
logging.getLogger().addHandler(_log_handler)
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger('main')

VERSION = '1.0.0'


# ── App ───────────────────────────────────────────────────────────────────────

# ── Splash screen ────────────────────────────────────────────────────────────

class SplashScreen:
    """Startup progress window — shows what the app is doing, closes when ready."""

    _SPINNER = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']

    def __init__(self, root: ctk.CTk, provider_label: str) -> None:
        self._root    = root
        self._closed  = False
        self._done    = {'app': True, 'whisper': False, 'provider': False}
        self._spin_i  = 0
        self._rows: dict[str, tuple[ctk.CTkLabel, ctk.CTkLabel]] = {}

        steps = [
            ('app',      'App started'),
            ('whisper',  'Loading Whisper model'),
            ('provider', provider_label),
        ]

        win = ctk.CTkToplevel(root)
        win.title('')
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.configure(fg_color='#111111')
        win.resizable(False, False)

        card = ctk.CTkFrame(win, fg_color='#1c1c1c', corner_radius=16,
                            border_width=1, border_color='#2e2e2e')
        card.pack(padx=1, pady=1, ipadx=4, ipady=4)

        # Header
        hdr = ctk.CTkFrame(card, fg_color='transparent')
        hdr.pack(fill='x', padx=26, pady=(18, 10))
        ctk.CTkLabel(hdr, text='⚡  Hotkeys',
                     font=('Segoe UI', 17, 'bold'),
                     text_color='#9090e0').pack(side='left')
        self._status_lbl = ctk.CTkLabel(hdr, text='Starting…',
                                         font=('Segoe UI', 11),
                                         text_color='#505050')
        self._status_lbl.pack(side='right')

        # Divider
        ctk.CTkFrame(card, fg_color='#2a2a2a', height=1,
                     corner_radius=0).pack(fill='x', padx=22, pady=(0, 4))

        # Step rows
        body = ctk.CTkFrame(card, fg_color='transparent')
        body.pack(fill='x', padx=26, pady=(8, 20))

        for key, label in steps:
            done = self._done[key]
            row  = ctk.CTkFrame(body, fg_color='transparent')
            row.pack(fill='x', pady=3)

            icon_lbl = ctk.CTkLabel(row,
                                     text='✓' if done else '⠋',
                                     font=('Consolas', 13), width=20,
                                     text_color='#4ec94e' if done else '#505050')
            icon_lbl.pack(side='left')

            text_lbl = ctk.CTkLabel(row, text=label,
                                     font=('Segoe UI', 13),
                                     text_color='#d0d0d0' if done else '#777777',
                                     anchor='w')
            text_lbl.pack(side='left', padx=(8, 0))

            self._rows[key] = (icon_lbl, text_lbl)

        # Position: center screen
        win.update_idletasks()
        rw = win.winfo_reqwidth() + 2
        rh = win.winfo_reqheight() + 2
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry(f'{rw}x{rh}+{(sw - rw) // 2}+{(sh - rh) // 2}')

        self._win = win
        self._animate()   # start spinner

    # ── Public API (call from main thread via _q) ─────────────────────────────

    def mark_done(self, step: str) -> None:
        if self._closed:
            return
        self._done[step] = True
        icon_lbl, text_lbl = self._rows[step]
        icon_lbl.configure(text='✓', text_color='#4ec94e')
        text_lbl.configure(text_color='#e0e0e0')
        if all(self._done.values()):
            self._status_lbl.configure(text='Ready ✓', text_color='#4ec94e')
            self._root.after(1200, self._close)

    def mark_error(self, step: str) -> None:
        if self._closed:
            return
        self._done[step] = True   # treat as done so we don't block forever
        icon_lbl, text_lbl = self._rows[step]
        icon_lbl.configure(text='✗', text_color='#e05050')
        text_lbl.configure(text_color='#cc8888')
        if all(self._done.values()):
            self._status_lbl.configure(text='Error', text_color='#e05050')
            self._root.after(2500, self._close)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _animate(self) -> None:
        """Rotate spinner on all pending steps every 80 ms."""
        if self._closed:
            return
        self._spin_i = (self._spin_i + 1) % len(self._SPINNER)
        ch = self._SPINNER[self._spin_i]
        for key, (icon_lbl, _) in self._rows.items():
            if not self._done.get(key):
                icon_lbl.configure(text=ch)
        self._root.after(80, self._animate)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._win.destroy()
        except Exception:
            pass


# ── App ───────────────────────────────────────────────────────────────────────

class App:
    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()

        # ── Refine state ──────────────────────────────────────────────────────
        self._refine_t0:         float = 0.0
        self._refine_in_progress: bool = False
        self._refine_gen:          int = 0   # incremented per request; stale callbacks check this

        # ── Whisper state ─────────────────────────────────────────────────────
        self._whisper_recording = False
        self._whisper_t0: float = 0.0
        self._whisper_ready     = False
        self._history: list     = load_history()

        # ── Sticky note (per-prompt hotkey popup) ────────────────────────────
        self._sticky: 'PromptStickyNote | None' = None
        self._sticky_idx: int | None = None   # which prompt index is currently shown

        # ── Undo last refinement ─────────────────────────────────────────────
        self._undo_available: bool  = False
        self._undo_t:         float = 0.0   # timestamp of last completed refinement

        # ── Hotkey re-registration guard ─────────────────────────────────────
        self._hk_reg_lock    = threading.Lock()
        self._hk_reg_pending = False   # set True when a save arrives mid-flight

        # ── Config & prompts ─────────────────────────────────────────────────
        self.config  = load_config()
        self.prompts = load_prompts()

        # ── Restore-defaults state ────────────────────────────────────────────
        # Cache the bundled defaults NOW before any edit can overwrite prompts.json
        # in dev mode (save_prompts writes to the source file in non-frozen builds).
        import json as _json
        try:
            from storage import resource_path as _rp
            with open(_rp('prompts.json'), encoding='utf-8') as _f:
                self._bundled_defaults: list = _json.load(_f)
        except Exception:
            self._bundled_defaults = []
        # Start enabled — only grey out immediately after the user clicks
        # "Restore Default Prompts", and re-enable as soon as any edit is saved.
        # Checking against bundled defaults at startup was too aggressive: it
        # permanently disabled the button whenever prompts happened to match
        # defaults (e.g. fresh install), even if the user never used Restore.
        self._at_default_prompts: bool = False
        self.folders: list[str]       = self.config.get('folders', [])
        self.folder_colors: dict[str, str] = self.config.get('folder_colors', {})
        self.active_prompt: dict = self.prompts[0] if self.prompts else {
            'title': 'Refine', 'prompt': 'Improve the following text and return only the result.'
        }

        # ── Root window (hidden) ─────────────────────────────────────────────
        self.root = ctk.CTk()
        self.root.withdraw()
        self.root.title('Hotkeys')
        self.root.protocol('WM_DELETE_WINDOW', self._quit)

        # ── Text-refine provider ─────────────────────────────────────────────
        self.provider: Provider = build_provider(self.config)

        # ── Splash screen ─────────────────────────────────────────────────────
        if isinstance(self.provider, LocalProvider):
            _prov_label = 'Loading local Qwen model'
        elif self.provider.ready:
            _active = self.config.get('active_provider', 'cerebras').title()
            _prov_label = f'Connecting to {_active}'
        else:
            _prov_label = 'AI provider (add API key in Settings)'
        self._splash = SplashScreen(self.root, _prov_label)
        self.root.update()   # render splash before rest of __init__ runs

        # ── UI windows ───────────────────────────────────────────────────────
        self.refine_overlay    = OverlayWindow(self.root, slot=0)
        self.whisper_overlay   = OverlayWindow(self.root, slot=1)
        self.macro_overlay     = OverlayWindow(self.root, slot=2)
        self.recorder_overlay  = OverlayWindow(self.root, slot=3)
        self.gif_overlay       = OverlayWindow(self.root, slot=4)

        # ── Screen recorder ───────────────────────────────────────────────────
        self._screen_recorder: ScreenRecorder | None = None
        self._recorder_state  = 'idle'   # 'idle' | 'recording' | 'stopping'
        self._recorder_t0     = 0.0
        self._recorder_setup_dlg = None   # open RecorderSetupDialog, if any

        # ── GIF recorder ──────────────────────────────────────────────────────
        self._gif_recorder: GifRecorder | None = None
        self._gif_state       = 'idle'   # 'idle' | 'recording' | 'encoding'
        self._gif_t0          = 0.0
        self._gif_setup_dlg   = None   # open GifSetupDialog, if any

        # ── Macro recorder + library ─────────────────────────────────────────
        self._macro          = MacroRecorder()
        self._macro_state    = 'idle'   # 'idle' | 'recording' | 'ready' | 'playing'
        self._macro_stop_hks: list = []
        self._macro_library  = MacroLibrary(Path(appdata_dir()) / 'macros')
        self._macro_saved_hks: list = []   # registered playback hotkeys for saved macros
        self.library  = LibraryWindow(self.root, self.prompts,
                                      on_select=self._on_prompt_selected,
                                      on_save=self._on_prompts_saved,
                                      hotkey_cfg=self._hotkey_cfg(),
                                      on_hotkey_suspend=self._suspend_hotkeys,
                                      on_hotkey_resume=self._resume_hotkeys,
                                      folders=self.folders,
                                      folder_colors=self.folder_colors,
                                      on_folders_changed=self._on_folders_changed,
                                      vision_extractor=self._vision_extractor,
                                      macro_library=self._macro_library,
                                      on_macro_play=self._on_library_macro_play,
                                      on_macro_hotkeys_changed=self._register_macro_saved_hotkeys)
        # Wire the library's recorder tab toggle button → main.py handler
        self.library._on_recorder_toggle = lambda: self._q.put(('recorder:toggle', None))
        # Wire the library's macros tab right-click record → same queue event as Shift+F1
        self.library._on_macro_toggle    = lambda: self._q.put(('macro:hotkey',    None))
        # Wire the macros tab reset button → abort everything and return to idle
        self.library._on_macro_reset     = lambda: self._q.put(('macro:reset',     None))
        # Wire the library's GIF tab toggle button → main.py handler
        self.library._on_gif_toggle      = lambda: self._q.put(('gif:toggle',      None))
        self.settings = SettingsWindow(self.root, self.config,
                                       on_save=self._on_settings_saved)
        self.history_win = HistoryWindow(self.root,
                                         on_history_cleared=self._on_history_cleared)

        # ── Whisper pipeline ─────────────────────────────────────────────────
        wcfg = make_whisper_cfg(self.config)
        vad_onnx = Path(assets_dir()) / 'silero_vad.onnx'

        self._vad = SileroVAD(
            vad_onnx,
            speech_threshold=wcfg.vad.speech_threshold,
            safety_silence_s=wcfg.vad.safety_silence_s,
        )
        self._vad.set_safety_stop_callback(self._on_vad_safety_stop)

        self._audio = AudioCapture(
            on_chunk=self._on_audio_chunk,
            on_utterance_ready=self._on_utterance_ready,
            cfg=wcfg,
        )

        self._transcriber = Transcriber(
            cfg=wcfg,
            on_result=self._on_transcription_result,
            on_status=self._on_transcriber_status,
            models_dir=models_dir(),
            log_file=log_path(),
        )

        # ── Event dispatch ────────────────────────────────────────────────────
        self._dispatch = {
            'refine':           self._do_refine,
            'undo_refine':      self._do_undo_refine,
            'library':          lambda _: self.library.show(),
            'settings':         lambda _: self.settings.show(),
            'history':          lambda _: self.history_win.show(self._history),
            'refine:done':      self._on_refine_done,
            'refine:error':     self.refine_overlay.show_error,
            'model_ready':      self._on_model_ready,
            'model_error':      self._on_model_error,
            'prewarm:done':     lambda _: self._splash.mark_done('provider'),
            'refine:timeout':   self._on_refine_timeout,
            'refine:unlock':    self._on_refine_unlock,
            'switch_provider':  self._switch_provider,
            'whisper:start':    lambda _: self._whisper_start_recording(),
            'whisper:stop':     lambda _: self._whisper_stop_recording(),
            'whisper:cancel':   lambda _: self._whisper_cancel_recording(),
            'restore_defaults':  lambda _: self._do_restore_defaults(),
            'reload_hotkeys':   lambda _: self._reload_hotkeys_manual(),
            'prompt_hotkey':    self._on_prompt_hotkey,
            'whisper:status':   self._on_transcriber_status_event,
            'whisper:result':   self._on_whisper_result,
            'whisper:error':    self._on_whisper_error,
            'macro:hotkey':       self._on_macro_hotkey,
            'macro:stop':         self._on_macro_emergency_stop,
            'macro:reset':        lambda _: self._macro_reset(),
            'macro:play_saved':   self._on_library_macro_play,
            'screenshot:cancel':  lambda _: self._do_cancel_screenshot(),
            'recorder:toggle':    lambda _: self._on_recorder_toggle(),
            'recorder:cap':       lambda _: self._on_recorder_cap(),
            'recorder:size':      lambda b: None,   # handled by _recorder_tick poll
            'gif:toggle':         lambda _: self._on_gif_toggle(),
            'gif:cap':            lambda _: self._on_gif_cap(),
            'gif:done':           self._on_gif_done,
            'gif:error':          self._on_gif_error,
        }

        self._register_hotkeys()
        self._register_macro_saved_hotkeys()
        self.root.after(2000, self._hotkey_watchdog)
        start_prtsc_listener(self._hk_screenshot)
        self._start_tray()

        if isinstance(self.provider, LocalProvider):
            threading.Thread(target=self._load_model, daemon=True).start()

        threading.Thread(target=self._prewarm, daemon=True).start()
        threading.Thread(target=lambda: self._audio.start(), daemon=True).start()
        threading.Thread(target=self._watch_singleton_socket, daemon=True).start()

        self.root.after(30, self._poll)
        logger.info(f'Hotkeys v{VERSION} started.')

    # ── Hotkeys ───────────────────────────────────────────────────────────────

    def _hotkey_cfg(self) -> dict:
        return self.config.get('hotkeys', {
            'refine':  'alt+shift+w',
            'library': 'alt+shift+e',
            'whisper': 'ctrl+enter',
        })

    def _suspend_hotkeys(self) -> None:
        """Unhook all keyboard and mouse bindings — called while HotkeyCapture dialog
        is open so nothing fires during capture."""
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            mouse.unhook_all()
        except Exception:
            pass

    def _resume_hotkeys(self) -> None:
        """Re-register hotkeys after HotkeyCapture closes."""
        threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()

    def _register_hotkeys(self) -> None:
        """Register all global hotkeys, forcefully resetting the keyboard listener first."""
        # ── Step 1: full teardown ─────────────────────────────────────────────
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            mouse.unhook_all()
        except Exception:
            pass

        # Force-stop the keyboard listener thread so it gets a clean slate on
        # the next add_hotkey call.  After multiple hard-kill / restart cycles
        # the listener thread can become a ghost — new hooks are added but
        # never actually fire.
        try:
            if hasattr(keyboard, '_listener') and keyboard._listener is not None:
                keyboard._listener.stop()
                keyboard._listener = None
        except Exception:
            pass

        # ── Step 2: register with one automatic retry ─────────────────────────
        hk  = self._hotkey_cfg()
        ptt = self.config.get('push_to_talk', False)

        def _do_register():
            # suppress=False: the library observes keypresses but never consumes
            # them.  suppress=True was causing the library's internal modifier-state
            # machine to lock up permanently after each suppressed hotkey — Alt and
            # Shift would appear "stuck" to the library even after the user released
            # them, silently blocking all subsequent hotkeys with no recovery path
            # (even unhook_all + re-registration couldn't fix a broken listener).
            keyboard.add_hotkey(hk.get('refine',       'alt+shift+w'), self._hk_refine,      suppress=False)
            keyboard.add_hotkey(hk.get('library',      'alt+shift+e'), self._hk_library,     suppress=False)
            keyboard.add_hotkey(hk.get('undo_refine',  'alt+shift+z'), self._hk_undo_refine, suppress=False)
            keyboard.add_hotkey(hk.get('macro_record', 'shift+f1'),
                                lambda: self._q.put(('macro:hotkey', None)),     suppress=False)
            keyboard.add_hotkey(hk.get('recorder',     'shift+f2'),
                                lambda: self._q.put(('recorder:toggle', None)), suppress=False)
            keyboard.add_hotkey(hk.get('gif_record',   'shift+f3'),
                                lambda: self._q.put(('gif:toggle',      None)), suppress=False)

            if ptt:
                whisper_hk = hk.get('whisper', 'ctrl+enter')
                ptt_key    = whisper_hk.split('+')[-1]
                keyboard.on_press_key(
                    ptt_key,
                    lambda _: self._q.put(('whisper:start', None)),
                    suppress=False,
                )
                keyboard.on_release_key(
                    ptt_key,
                    lambda _: self._q.put(('whisper:stop', None)),
                    suppress=False,
                )
                logger.info(f'PTT mode: key={ptt_key!r}')
            else:
                keyboard.add_hotkey(hk.get('whisper', 'ctrl+enter'), self._hk_whisper, suppress=False)

            keyboard.add_hotkey('escape', self._hk_escape, suppress=False)

            # Ctrl+scroll-up → refine (same action as Alt+Shift+W).
            # A 500 ms debounce prevents a single scroll gesture from firing
            # multiple times — _refine_in_progress would stop them anyway, but
            # debouncing avoids spawning redundant capture threads.
            _scroll_last = [0.0]
            def _on_ctrl_scroll(event):
                if not (hasattr(event, 'delta') and event.delta > 0):
                    return
                if not keyboard.is_pressed('ctrl'):
                    return
                now = time.time()
                if now - _scroll_last[0] < 0.5:
                    return
                _scroll_last[0] = now
                self._hk_refine()
            mouse.hook(_on_ctrl_scroll)

            # Per-prompt hotkeys (assigned via right-click → Assign hotkey…)
            for _idx, _p in enumerate(self.prompts):
                _hk = _p.get('hotkey', '').strip()
                if not _hk:
                    continue
                def _make_ph_handler(idx=_idx):
                    def _handler():
                        self._q.put(('prompt_hotkey', idx))
                    return _handler
                try:
                    keyboard.add_hotkey(_hk, _make_ph_handler(), suppress=False)
                    logger.info(f'Per-prompt hotkey: {_hk!r} → [{_idx}] {_p["title"]!r}')
                except Exception as _e:
                    logger.warning(f'Per-prompt hotkey {_hk!r} failed: {_e}')

        try:
            _do_register()
            logger.info(f'Hotkeys registered: {hk}  PTT={ptt}')
        except Exception as e:
            logger.warning(f'Hotkey registration failed ({e}) — retrying in 0.5 s')
            time.sleep(0.5)
            try:
                keyboard.unhook_all()
                mouse.unhook_all()
                _do_register()
                logger.info(f'Hotkeys registered (retry ok): {hk}')
            except Exception as e2:
                logger.error(f'Hotkey registration failed after retry: {e2}')

    @property
    def _vision_extractor(self):
        """Return a callable (img) → str that extracts text from a PIL Image.

        Uses the Groq vision API with the personal bundled key.  The callable is
        safe to pass to LibraryWindow / PromptStickyNote and call from threads.
        """
        from vision import extract_text, DEFAULT_VISION_MODEL
        from engine import _resolve_keys
        import logging as _log
        config = self.config

        def _extract(img):
            model = config.get('providers', {}).get('groq', {}).get(
                'vision_model', DEFAULT_VISION_MODEL)
            keys = _resolve_keys(config, 'groq')
            last_err = None
            for key in keys:
                try:
                    return extract_text(img, key, model)
                except RuntimeError as e:
                    msg = str(e)
                    if 'rate limit' in msg.lower() or '429' in msg or 'quota' in msg.lower():
                        _log.getLogger(__name__).warning(
                            f'Vision: Groq key …{key[-6:]} rate-limited — trying next key')
                        last_err = e
                        continue
                    raise
            raise last_err or RuntimeError('All Groq vision keys exhausted')

        return _extract

    def _reregister_after_action(self) -> None:
        """Re-register hotkeys after a paste action.

        keyboard.send() and injected Ctrl+V/C events flow through the
        keyboard library's own WH_KEYBOARD_LL hook.  With suppress=True
        active on all hotkeys, the library's internal modifier-key state
        can get stuck (it thinks Alt/Shift are still held), silently
        preventing subsequent hotkeys from firing.  Re-registering clears
        the hook, flushes all state, and reinstalls fresh hooks.
        """
        threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()

    def _hotkey_watchdog(self) -> None:
        """Periodic safety net: re-register if the keyboard listener thread died."""
        try:
            listener = getattr(keyboard, '_listener', None)
            if listener is not None:
                t = getattr(listener, 'thread', None)
                if t is not None and not t.is_alive():
                    logger.warning('Hotkey listener thread dead — auto re-registering.')
                    threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
        except Exception:
            pass
        self.root.after(2000, self._hotkey_watchdog)

    def _reload_hotkeys_manual(self) -> None:
        """Full reset from tray menu — cancels anything stuck, re-registers hotkeys."""
        logger.info('Manual reload requested from tray.')
        # Close any open recorder/gif setup dialogs that may be stuck
        # (e.g. triggered with library closed → parent withdrawn → dialog invisible)
        for attr in ('_recorder_setup_dlg', '_gif_setup_dlg'):
            dlg = getattr(self, attr, None)
            if dlg is not None:
                try:
                    dlg.win.grab_release()
                    dlg.win.destroy()
                except Exception:
                    pass
                setattr(self, attr, None)
        # Cancel any stuck recording
        try:
            if self._whisper_recording:
                self._whisper_cancel_recording()
        except Exception:
            pass
        # Cancel any stuck refine
        try:
            self.refine_overlay.hide()
        except Exception:
            pass
        # Hide all overlays
        try:
            self.whisper_overlay.hide()
        except Exception:
            pass
        # Re-register hotkeys cleanly
        self._register_hotkeys()
        self._notify('Hotkeys reset ⚡', 'All hotkeys reloaded and ready.')

    def _schedule_rereg(self, delay_ms: int = 80) -> None:
        """Schedule a hotkey re-registration *delay_ms* after a hotkey fires.

        Called from every hotkey handler (keyboard hook thread) so the
        keyboard library always gets a clean state after each press.
        The 80 ms default gives the OS time to see all key-up events before
        we unhook; _register_hotkeys_bg is a no-op if already in-flight.
        """
        self.root.after(
            delay_ms,
            lambda: threading.Thread(
                target=self._register_hotkeys_bg, daemon=True,
            ).start(),
        )

    def _hk_refine(self) -> None:
        logger.info('Refine hotkey fired.')
        threading.Thread(target=self._capture_and_queue, daemon=True).start()

    def _capture_and_queue(self) -> None:
        # Wait until Alt and Shift are physically released before injecting
        # Ctrl+C.  If they're still held, the target app sees Ctrl+Shift+Alt+C
        # instead of plain Ctrl+C and silently ignores it (→ "select text first").
        # GetAsyncKeyState is used directly so this works even while the
        # keyboard hook is briefly suspended during re-registration.
        _u32 = ctypes.windll.user32
        _deadline = time.time() + 0.5          # give up after 500 ms
        while time.time() < _deadline:
            if not (_u32.GetAsyncKeyState(0x10) & 0x8000 or   # VK_SHIFT
                    _u32.GetAsyncKeyState(0x12) & 0x8000):    # VK_MENU (Alt)
                break
            time.sleep(0.015)
        time.sleep(0.04)                       # brief settle after release
        try:
            prev = pyperclip.paste()
        except Exception:
            prev = ''
        try:
            pyperclip.copy('')
        except Exception:
            pass
        copy_selection()
        captured = ''
        for _ in range(25):                   # up to 0.75 s total
            time.sleep(0.03)                  # poll every 30 ms (was 50 ms)
            try:
                current = pyperclip.paste()
            except Exception:
                continue
            if current and current.strip():
                captured = current
                break
        if not captured:
            try:
                pyperclip.copy(prev)
            except Exception:
                pass
        logger.info(f'Captured text ({len(captured)} chars): {captured[:80]!r}')
        self._q.put(('refine', captured))

    def _hk_undo_refine(self) -> None:
        self._q.put(('undo_refine', None))

    def _hk_library(self) -> None:
        self._q.put(('library', None))

    def _hk_whisper(self) -> None:
        if not self._whisper_recording:
            self._q.put(('whisper:start', None))
        else:
            self._q.put(('whisper:stop', None))

    def _hk_screenshot(self) -> None:
        self.root.after(0, lambda: take_screenshot(self.root))

    def _do_cancel_screenshot(self) -> None:
        """Cancel the active screenshot overlay. Called on main thread via _poll."""
        from screenshot import cancel_screenshot
        cancel_screenshot()

    def _hk_escape(self) -> None:
        # Screenshot overlay has top priority — Esc must always dismiss it,
        # even if the grab is still in flight (main thread not yet blocked).
        from screenshot import _overlay_active
        if _overlay_active[0]:
            self._q.put(('screenshot:cancel', None))
            return
        # Macro takes priority — stop recording/playback first.
        if self._macro_state in ('recording', 'playing'):
            self._q.put(('macro:stop', None))
            return
        # GIF recording — Esc aborts capture.
        if self._gif_state == 'recording':
            self._q.put(('gif:toggle', None))   # stop → encode → save dialog
            return
        if self._whisper_recording:
            self._q.put(('whisper:cancel', None))

    # ── Per-prompt hotkey handler ─────────────────────────────────────────────

    def _on_prompt_hotkey(self, idx: int) -> None:
        """Called on main thread when a per-prompt hotkey fires.

        Activates the prompt and opens (or replaces) the floating sticky note.
        """
        if idx >= len(self.prompts):
            return
        prompt = self.prompts[idx]

        # 1. Activate via library._select — updates active_idx, highlight, header
        #    label, and fires on_select (which sets self.active_prompt) all at once.
        try:
            self.library._select(idx)
        except Exception:
            self._on_prompt_selected(prompt)   # fallback if library isn't built yet

        # Guard: if the tracked sticky window no longer exists (destroyed externally,
        # or mid-close flash), clear the stale reference so we don't get stuck.
        if self._sticky is not None:
            try:
                alive = self._sticky.win.winfo_exists()
            except Exception:
                alive = False
            if not alive:
                self._sticky     = None
                self._sticky_idx = None

        # 2. If the SAME prompt's note is already open — apply & close it (toggle).
        #    Pressing F1 → F1 is the quick "confirm and continue" flow.
        if self._sticky is not None and self._sticky_idx == idx:
            try:
                self._sticky.close()
            except Exception:
                self._sticky.destroy()
            return

        # 2b. Different prompt's note is open — replace it silently.
        if self._sticky is not None:
            try:
                self._sticky.destroy()
            except Exception:
                pass
            self._sticky = None
            self._sticky_idx = None

        # 3. Save callback: write changes back to prompts list + disk
        def _on_note_save(updated: dict) -> None:
            # Guard: prompt may have been deleted while the note was open
            if idx >= len(self.prompts):
                logger.warning(f'Sticky note save: prompt[{idx}] no longer exists — discarding')
                return
            updated['hotkey'] = self.prompts[idx].get('hotkey', '')
            self.prompts[idx] = updated
            self.active_prompt = updated
            # File I/O off the main thread
            threading.Thread(
                target=save_prompts, args=(list(self.prompts),), daemon=True,
            ).start()
            # Always sync the library's prompt list so it's current next open
            try:
                self.library.prompts = self.prompts
                if self.library.win.winfo_ismapped():
                    self.library._render_cards()
            except Exception:
                pass
            logger.info(f'Sticky note saved changes to prompt[{idx}] {updated["title"]!r}')

        # 4. on_close: clear self._sticky / _sticky_idx so future hotkey presses
        #    don't try to destroy an already-gone window.
        def _on_note_close() -> None:
            self._sticky     = None
            self._sticky_idx = None

        # 5. Open sticky note
        self._sticky_idx = idx
        self._sticky = PromptStickyNote(
            self.root, prompt, on_save=_on_note_save, on_close=_on_note_close,
            vision_extractor=self._vision_extractor,
        )
        logger.info(f'Prompt hotkey fired → [{idx}] {prompt["title"]!r}')

    # ── History callbacks ─────────────────────────────────────────────────────

    def _on_history_cleared(self) -> None:
        self._history = []

    # ── Refine callbacks ──────────────────────────────────────────────────────

    def _on_prompt_selected(self, prompt: dict) -> None:
        self.active_prompt = prompt

    def _on_prompts_saved(self, prompts: list) -> None:
        self.prompts = prompts
        self._at_default_prompts = False   # any edit re-enables Restore Default Prompts
        self._update_tray()
        # Save to disk in background — no need to block the UI thread for file I/O
        threading.Thread(target=save_prompts, args=(prompts,), daemon=True).start()
        if prompts and self.active_prompt not in prompts:
            self.active_prompt = prompts[0]
        # Re-register hotkeys in background: _register_hotkeys() has a 150 ms
        # sleep inside it (OS hook flush) — running it here would freeze the UI.
        threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()

    def _on_folders_changed(self, folders: list, folder_colors: dict | None = None) -> None:
        self.folders = folders
        self.config['folders'] = folders
        if folder_colors is not None:
            self.folder_colors = folder_colors
            self.config['folder_colors'] = folder_colors
        threading.Thread(target=save_config, args=(self.config,), daemon=True).start()

    def _register_hotkeys_bg(self) -> None:
        """Thread-safe wrapper — guarantees the latest prompt list is always applied.

        Uses a pending flag so rapid saves (e.g. drag-reorder + edit in quick
        succession) never silently lose a registration: if the lock is busy the
        flag is set, and the in-flight run loops once more after finishing.
        """
        if not self._hk_reg_lock.acquire(blocking=False):
            self._hk_reg_pending = True   # in-flight run will re-register after
            return
        try:
            while True:
                self._hk_reg_pending = False
                self._register_hotkeys()
                if not self._hk_reg_pending:
                    break   # nothing changed while we were registering
            # Always re-register saved-macro hotkeys after _register_hotkeys()
            # because unhook_all() inside it wipes them out.
            self._register_macro_saved_hotkeys()
        finally:
            self._hk_reg_lock.release()

    def _on_settings_saved(self, new_config: dict) -> None:
        if self._whisper_recording:
            self._whisper_cancel_recording()
        self.config   = new_config
        save_config(new_config)
        self.provider = build_provider(new_config)
        if isinstance(self.provider, LocalProvider):
            threading.Thread(target=self._load_model, daemon=True).start()
        # Rebuild whisper pipeline with new config
        self._rebuild_whisper_pipeline(new_config)
        # Re-register hotkeys off the main thread — _register_hotkeys() has a
        # 150 ms sleep inside it (OS hook flush) that would freeze the UI here.
        threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
        self._update_tray()

    def _rebuild_whisper_pipeline(self, config: dict) -> None:
        """Recreate audio + transcriber with updated config (called after settings save)."""
        try:
            self._audio.stop()
        except Exception:
            pass
        wcfg = make_whisper_cfg(config)
        # Update VAD thresholds
        self._vad._threshold = wcfg.vad.speech_threshold
        self._vad._silence_chunks_limit = int(
            wcfg.vad.safety_silence_s * 1000 / 32
        )
        # Rebuild audio capture with new device setting
        self._audio = AudioCapture(
            on_chunk=self._on_audio_chunk,
            on_utterance_ready=self._on_utterance_ready,
            cfg=wcfg,
        )
        threading.Thread(target=lambda: self._audio.start(), daemon=True).start()
        # Transcriber model is already loaded; create new one only if model changed
        # (For simplicity, recreate — user is in settings anyway so latency is ok)
        self._transcriber.shutdown()
        self._transcriber = Transcriber(
            cfg=wcfg,
            on_result=self._on_transcription_result,
            on_status=self._on_transcriber_status,
            models_dir=models_dir(),
            log_file=log_path(),
        )

    # ── Prewarm ───────────────────────────────────────────────────────────────

    def _prewarm(self) -> None:
        if isinstance(self.provider, LocalProvider):
            return   # splash provider step handled by model_ready
        if not self.provider.ready:
            self._q.put(('prewarm:done', None))   # no API key — mark done immediately
            return
        try:
            self.provider.refine('Hello', 'Reply with one word: OK')
            logger.info('Connection pre-warmed.')
        except Exception as e:
            logger.info(f'Pre-warm skipped: {e!s:.60}')
        self._q.put(('prewarm:done', None))

    # ── Model loading (local Qwen) ────────────────────────────────────────────

    def _load_model(self) -> None:
        try:
            self.provider.load()
            self._q.put(('model_ready', None))
        except Exception as e:
            logger.error(f'Model load failed: {e}')
            self._q.put(('model_error', str(e)))

    # ── Event poll loop ───────────────────────────────────────────────────────

    def _poll(self) -> None:
        # Reschedule FIRST so a handler that calls wait_window() (which creates
        # a nested Tk event loop) doesn't prevent the next poll from running.
        # Without this, any modal dialog opened from a handler would stop all
        # queue processing — including tray "Reload hotkeys" — until it closed.
        self.root.after(30, self._poll)
        try:
            while True:
                event, data = self._q.get_nowait()
                handler = self._dispatch.get(event)
                if handler:
                    try:
                        handler(data)
                    except Exception:
                        logger.exception(f'_poll: unhandled exception in handler for {event!r}')
        except queue.Empty:
            pass

    # ── Refine actions ────────────────────────────────────────────────────────

    def _do_refine(self, text: str) -> None:
        if self._whisper_recording:
            return   # don't clobber the clipboard mid-recording
        if self._refine_in_progress:
            return   # already running — ignore rapid double-press
        if not text or not text.strip():
            self.refine_overlay.show_no_selection()
            # Re-register so the keyboard library resets after the suppressed
            # hotkey — without this, the library's stuck modifier state blocks
            # all subsequent hotkeys until the next successful paste re-reg.
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return
        if isinstance(self.provider, LocalProvider) and not self.provider.ready:
            self.refine_overlay.show_loading_model()
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return
        if not self.provider.ready:
            self.refine_overlay.show_error('API key required — open Settings')
            threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
            return

        self._undo_available = False   # new refinement invalidates any prior undo
        self._refine_in_progress = True
        self._refine_gen += 1
        gen      = self._refine_gen
        self._refine_t0 = time.time()
        self.refine_overlay.show()
        prompt   = self.active_prompt
        provider = self.provider

        def infer() -> None:
            # 30-second hard timeout — fires refine:timeout on the main thread
            timer = threading.Timer(
                30.0, lambda: self._q.put(('refine:timeout', gen))
            )
            timer.start()
            try:
                result = provider.refine(text, prompt['prompt'])
                timer.cancel()
                if gen != self._refine_gen:
                    return   # timeout already fired and reset gen
                if not result or not result.strip():
                    self._q.put(('refine:error', 'Empty response from AI'))
                else:
                    self._q.put(('refine:done', result))
            except Exception as e:
                timer.cancel()
                logger.error(f'Inference error: {e}')
                if gen == self._refine_gen:
                    msg = str(e)
                    if '429' in msg or 'rate' in msg.lower() or 'quota' in msg.lower():
                        msg = 'Daily limit reached — try again later or add your own API key in Settings'
                    elif 'api key' in msg.lower() or 'api_key' in msg.lower() or 'unauthorized' in msg.lower() or '401' in msg:
                        msg = 'Invalid API key — check Settings'
                    self._q.put(('refine:error', msg[:80]))
            finally:
                self._q.put(('refine:unlock', gen))

        threading.Thread(target=infer, daemon=True).start()

    def _on_refine_done(self, result: str) -> None:
        elapsed = time.time() - self._refine_t0
        self.refine_overlay.show_done(elapsed)
        pyperclip.copy(result)
        # Use direct Win32 SendInput (same path as whisper) — avoids routing
        # through the keyboard library, which can leave its key-state machine
        # stale (stuck modifier keys) and break subsequent hotkeys.
        self.root.after(40, paste_from_clipboard)
        # Re-register hotkeys after the paste lands — resets any library state
        # corruption that injected Ctrl+V events may have caused.
        self.root.after(150, self._reregister_after_action)
        self._undo_available = True
        self._undo_t         = time.time()
        logger.info(f'Refinement complete in {elapsed:.2f}s')

    def _do_undo_refine(self, _) -> None:
        """Undo the last AI refinement by sending Ctrl+Z to the active window."""
        if not self._undo_available:
            return
        if time.time() - self._undo_t > 30.0:   # 30-second undo window
            self._undo_available = False
            return
        self._undo_available = False
        # Ctrl+Z in the focused app undoes our Ctrl+V paste, restoring the
        # original selected text.  We delay 40 ms so the hotkey release clears
        # before the synthetic key arrives.  Uses Win32 SendInput directly —
        # not keyboard.send() — to avoid corrupting the library's modifier state.
        self.root.after(40, undo_last)
        logger.info('Undo last refinement')

    def _prompts_are_default(self, prompts: list | None = None) -> bool:
        """Return True if the given prompts match the cached bundled defaults.

        Uses self._bundled_defaults (loaded once at startup) so that dev-mode
        saves — which overwrite prompts.json — don't corrupt the comparison.
        """
        defaults = getattr(self, '_bundled_defaults', [])
        if not defaults:
            return False
        current = prompts if prompts is not None else (
            self.library.prompts if getattr(self, 'library', None) else []
        )
        if not current or len(current) != len(defaults):
            return False
        return all(
            c.get('title') == d.get('title') and c.get('prompt') == d.get('prompt')
            for c, d in zip(current, defaults)
        )

    def _do_restore_defaults(self) -> None:
        """Restore the 16 bundled default prompts (called from tray menu)."""
        from dialogs import confirm
        if not confirm(self.root,
                       'Restore Default Prompts',
                       'This will permanently delete all your existing prompts\n'
                       'and restore the 16 default prompts.\n\n'
                       'This cannot be undone.'):
            return
        defaults = getattr(self, '_bundled_defaults', [])
        if not defaults:
            logger.error('Restore defaults: bundled defaults not cached')
            return
        # Update app state
        self.prompts = list(defaults)
        self.active_prompt = self.prompts[0]
        self._at_default_prompts = True
        # Update library UI
        self.library.prompts = list(defaults)
        self.library._render_cards()
        self.library._select(0)
        # Save to disk and re-register hotkeys
        threading.Thread(target=save_prompts, args=(self.prompts,), daemon=True).start()
        threading.Thread(target=self._register_hotkeys_bg, daemon=True).start()
        # Rebuild tray menu so Restore item becomes greyed again
        self._update_tray()
        self._notify('Hotkeys', 'Default prompts restored.')
        logger.info('Default prompts restored.')

    def _on_refine_timeout(self, gen: int) -> None:
        if gen != self._refine_gen:
            return   # already handled by normal completion
        self._refine_in_progress = False
        self._refine_gen += 1   # invalidate so any late result is discarded
        self.refine_overlay.show_error('Request timed out — try again')
        logger.warning('Refine request timed out after 30s')

    def _on_refine_unlock(self, gen: int) -> None:
        """Called after every infer() thread regardless of outcome."""
        if gen == self._refine_gen:
            self._refine_in_progress = False

    def _on_model_ready(self, _) -> None:
        logger.info('Local model ready.')
        self._splash.mark_done('provider')
        self._update_tray()
        hk = self._hotkey_cfg().get('refine', 'alt+shift+w').upper()
        self._notify('Hotkeys is ready ⚡', f'Select any text and press {hk} to refine it.')

    def _on_model_error(self, msg: str) -> None:
        self._splash.mark_error('provider')
        self._notify('Model failed to load', msg[:120])

    # ── Whisper actions ───────────────────────────────────────────────────────

    def _whisper_start_recording(self) -> None:
        if self._whisper_recording:
            return   # already recording — ignore key-repeat in PTT mode
        if not self._whisper_ready:
            self.whisper_overlay.show_whisper_loading()
            return
        self._whisper_recording = True
        self._whisper_t0 = time.time()
        self._vad.reset()
        self._audio.start_recording()
        play_start()
        self.whisper_overlay.show_recording()
        self._update_tray()
        logger.info('Whisper recording started.')

    def _whisper_stop_recording(self) -> None:
        if not self._whisper_recording:
            return
        self._whisper_recording = False
        play_stop()
        self._audio.stop_recording()
        self.whisper_overlay.show_transcribing()
        self._update_tray()
        logger.info('Whisper recording stopped — transcribing.')

    def _whisper_cancel_recording(self) -> None:
        if not self._whisper_recording:
            return
        self._whisper_recording = False
        self._audio.cancel_recording()
        self.whisper_overlay.show_whisper_cancelled()
        self._update_tray()
        logger.info('Whisper recording cancelled.')

    def _on_vad_safety_stop(self) -> None:
        """Called from audio thread when silence limit exceeded."""
        self._q.put(('whisper:stop', None))

    def _on_audio_chunk(self, chunk) -> None:
        if self._whisper_recording:
            self._vad.process_chunk(chunk)

    def _on_utterance_ready(self, audio) -> None:
        self._transcriber.submit(audio)

    def _on_transcriber_status(self, status: str) -> None:
        """Called from transcriber thread — post to main queue."""
        self._q.put(('whisper:status', status))

    def _on_transcriber_status_event(self, status: str) -> None:
        """Handle transcriber status on main thread."""
        if status == 'loading':
            self._whisper_ready = False
        elif status == 'ready':
            self._whisper_ready = True
            self._splash.mark_done('whisper')
            hk = self._hotkey_cfg().get('whisper', 'ctrl+enter').upper()
            self._notify('Hotkeys — Whisper ready 🎙', f'Press {hk} to start recording.')
        elif status == 'error':
            self._whisper_ready = True  # allow retry
            self._splash.mark_error('whisper')
            self._q.put(('whisper:error', 'Transcription failed'))

    def _on_transcription_result(self, text: str, language: str, duration_s: float) -> None:
        """Called from transcriber thread — post to main queue."""
        self._q.put(('whisper:result', (text, language, duration_s)))

    def _on_whisper_result(self, payload) -> None:
        text, language, duration_s = payload
        elapsed = time.time() - self._whisper_t0

        if not text:
            self.whisper_overlay.show_whisper_cancelled()
            logger.info('Whisper: no speech detected.')
            return

        out_cfg = self.config.get('whisper', {}).get('output', {})
        out  = text + (' ' if out_cfg.get('add_trailing_space', True) else '')

        copy_to_clipboard(out)
        if out_cfg.get('type_text', True):
            self.root.after(60, paste_from_clipboard)
        # Re-register after paste for the same reason as refine — injected
        # Ctrl+V can leave the keyboard library's state stale.
        self.root.after(150, self._reregister_after_action)

        self.whisper_overlay.show_whisper_done(elapsed)

        # Save to history off the main thread so it never delays the paste
        self._history.append({
            'text':     text,
            'language': language,
            'duration': round(duration_s, 2),
            'ts':       datetime.datetime.now().isoformat(timespec='seconds'),
        })
        if len(self._history) > _HISTORY_MAX_ENTRIES:
            self._history = self._history[-_HISTORY_MAX_ENTRIES:]
        _snap = list(self._history)
        threading.Thread(target=save_history, args=(_snap,), daemon=True).start()
        logger.info(f'Whisper complete: {len(text)} chars in {elapsed:.2f}s')

    def _on_whisper_error(self, msg: str) -> None:
        self.whisper_overlay.show_whisper_error(msg)
        logger.error(f'Whisper error: {msg}')

    # ── Macro record & replay ─────────────────────────────────────────────────

    def _on_macro_hotkey(self, _=None) -> None:
        """Shift+F1 — cycles: idle→recording, recording→ready, ready→playing."""
        state = self._macro_state
        if state == 'idle':
            self._macro_start_recording()
        elif state == 'recording':
            self._macro_stop_recording()
        elif state == 'ready':
            self._macro_start_playback()
        elif state == 'playing':
            self._on_macro_emergency_stop()

    def _set_macro_state(self, state: str) -> None:
        """Set macro state on both main.py and the library window (for right-click menu labels)."""
        self._macro_state = state
        self.library._macro_state = state

    def _macro_reset(self) -> None:
        """Abort any active recording/playback and return to idle — called from Library reset button."""
        self._macro.force_stop()
        self._macro.clear()
        self._macro_unregister_stop_keys()
        self._set_macro_state('idle')
        self.macro_overlay._close()
        self.library.refresh_macros()
        logger.info('Macro session discarded — reset to idle')

    def _macro_start_recording(self) -> None:
        self._set_macro_state('recording')
        self._macro.start_recording()
        self._macro_register_stop_keys()
        self.macro_overlay.show_macro_recording()
        logger.info('Macro recording started')

    def _macro_stop_recording(self) -> None:
        self._macro.stop_recording()
        n = self._macro.event_count
        self._set_macro_state('ready' if n > 0 else 'idle')
        self._macro_unregister_stop_keys()
        if n > 0:
            self.macro_overlay.show_macro_ready(n)
            logger.info(f'Macro recording stopped — {n} events, {self._macro.duration:.2f}s')
        else:
            self.macro_overlay._close()
            logger.info('Macro recording stopped — no events captured')

    def _macro_start_playback(self) -> None:
        if not self._macro.event_count:
            return
        self._set_macro_state('playing')
        self._macro_register_stop_keys()
        self.macro_overlay.show_macro_playing()
        self._macro.start_playback(
            on_done=lambda: self.root.after(0, self._macro_play_done),
            on_stop=lambda: self.root.after(0, self._macro_play_stopped),
        )
        logger.info('Macro playback started')

    def _macro_play_done(self) -> None:
        self._set_macro_state('ready')
        self._macro_unregister_stop_keys()
        self.macro_overlay.show_macro_done()
        logger.info('Macro playback complete')
        # Show save prompt after a short delay (let pill appear first)
        self.root.after(900, self._macro_show_save_prompt)

    def _macro_show_save_prompt(self) -> None:
        """Show 'Save this macro?' dialog near cursor."""
        if self._macro_state != 'ready' or not self._macro.event_count:
            return
        default_name = self._macro_library.next_default_name()
        default_hk   = self._macro_library.next_available_hotkey()
        dlg = MacroSavePrompt(
            self.root,
            default_name=default_name,
            default_hotkey=default_hk,
            on_hotkey_suspend=self._suspend_hotkeys,
            on_hotkey_resume=self._resume_hotkeys,
        )
        self.root.wait_window(dlg)
        # Reset to idle regardless of save/discard so Shift+F1 starts fresh.
        self._set_macro_state('idle')
        if dlg.result:
            name = dlg.result['name'].strip() or default_name
            hk   = dlg.result['hotkey']
            meta = self._macro_library.save(self._macro, name, hk)
            logger.info(f'Macro saved: "{name}" ({meta["event_count"]} events) hotkey={hk!r}')
            self._register_macro_saved_hotkeys()
            self.library.refresh_macros()
            # Confirmation pill — replaces the "done" pill
            self.macro_overlay.show_macro_saved(name, hk)
        # Clear after save (or discard) — not before, otherwise save gets empty events
        self._macro.clear()

    def _on_library_macro_play(self, meta: dict) -> None:
        """Play a saved macro triggered from the Library UI."""
        if self._macro_state in ('recording', 'playing'):
            return
        rec = self._macro_library.load_recorder(meta['id'])
        # Replace the live recorder temporarily for playback
        self._macro = rec
        self._set_macro_state('playing')
        self._macro_register_stop_keys()
        self.macro_overlay.show_macro_playing()
        self._macro.start_playback(
            on_done=lambda: self.root.after(0, self._macro_saved_play_done),
            on_stop=lambda: self.root.after(0, self._macro_play_stopped),
        )
        logger.info(f'Macro playback (saved): "{meta["name"]}"')

    def _macro_saved_play_done(self) -> None:
        """Playback of a saved macro finished — don't offer save again."""
        self._set_macro_state('idle')
        self._macro_unregister_stop_keys()
        self.macro_overlay.show_macro_done()
        logger.info('Saved macro playback complete')

    def _register_macro_saved_hotkeys(self) -> None:
        """Re-register all saved-macro playback hotkeys."""
        for hk in self._macro_saved_hks:
            try:
                keyboard.remove_hotkey(hk)
            except Exception:
                pass
        self._macro_saved_hks = []
        for meta in self._macro_library.macros:
            hk = meta.get('hotkey', '').strip()
            if not hk:
                continue
            mid  = meta['id']
            name = meta['name']
            try:
                handle = keyboard.add_hotkey(
                    hk,
                    lambda m=meta: self._q.put(('macro:play_saved', m)),
                    suppress=False,
                )
                self._macro_saved_hks.append(handle)
                logger.info(f'Macro hotkey registered: {hk!r} -> "{name}"')
            except Exception as e:
                logger.warning(f'Could not register macro hotkey {hk!r}: {e}')

    def _macro_play_stopped(self) -> None:
        # Guard: _on_macro_emergency_stop already ran if state is no longer 'playing'
        if self._macro_state != 'playing':
            return
        self._set_macro_state('ready')
        self._macro_unregister_stop_keys()
        self.macro_overlay.show_macro_stopped()
        logger.info('Macro playback force-stopped')

    def _on_macro_emergency_stop(self, _=None) -> None:
        """Esc or Del — abort recording or playback immediately."""
        state = self._macro_state
        if state not in ('recording', 'playing'):
            return
        self._macro.force_stop()
        if state == 'recording':
            n = self._macro.event_count
            self._set_macro_state('ready' if n > 0 else 'idle')
            self._macro_unregister_stop_keys()
            if n > 0:
                self.root.after(0, lambda: self.macro_overlay.show_macro_ready(n))
            else:
                self.root.after(0, self.macro_overlay._close)
            logger.info(f'Macro recording aborted by stop key — {n} events kept')
        else:   # playing
            self._set_macro_state('ready')
            self._macro_unregister_stop_keys()
            self.root.after(0, self.macro_overlay.show_macro_stopped)
            logger.info('Macro playback aborted by stop key')

    def _macro_register_stop_keys(self) -> None:
        # Esc is handled by the permanent _hk_escape (which checks macro state),
        # so we only add Delete here to avoid a double-Esc handler.
        self._macro_stop_hks = [
            keyboard.add_hotkey('delete', lambda: self._q.put(('macro:stop', None)), suppress=False),
        ]

    def _macro_unregister_stop_keys(self) -> None:
        for hk in self._macro_stop_hks:
            try:
                keyboard.remove_hotkey(hk)
            except Exception:
                pass
        self._macro_stop_hks = []

    # ── Screen recorder ───────────────────────────────────────────────────────

    def _on_recorder_toggle(self) -> None:
        """Shift+F2 or Library tab button — starts or stops screen recording."""
        if self._recorder_state == 'idle':
            self._recorder_start_setup()
        elif self._recorder_state == 'recording':
            self._recorder_stop()

    def _recorder_start_setup(self) -> None:
        """Show pre-recording options dialog then start recording."""
        # Parent to library window if it's mapped, else root.
        # RecorderSetupDialog handles the withdrawn-parent case internally
        # (no transient, deiconify, screen-centre) so we can always pass root.
        try:
            parent = self.library.win if self.library.win.winfo_ismapped() else self.root
        except Exception:
            parent = self.root
        try:
            dlg = RecorderSetupDialog(parent)
        except Exception as exc:
            logger.exception(f'RecorderSetupDialog creation failed: {exc}')
            return
        self._recorder_setup_dlg = dlg
        parent.wait_window(dlg.win)
        self._recorder_setup_dlg = None
        if dlg.result is None:
            return   # user cancelled

        cfg = dlg.result
        self._screen_recorder = ScreenRecorder(
            hwnd=cfg['hwnd'],
            mon=cfg.get('mon'),
            mic=cfg['mic'],
            mic_device=cfg.get('mic_device'),
            fps=cfg['fps'],
            on_size_update=lambda b: self._q.put(('recorder:size', b)),
            on_cap_reached=lambda: self._q.put(('recorder:cap', None)),
        )
        try:
            self._screen_recorder.start()
        except Exception as exc:
            logger.error(f'Screen recorder failed to start: {exc}')
            from dialogs import alert
            alert(self.root, 'Recorder error', str(exc))
            self._screen_recorder = None
            return

        self._recorder_state = 'recording'
        self._recorder_t0    = time.time()
        self.recorder_overlay.show_recorder_recording()
        self._update_library_recorder_state()
        self._recorder_tick()
        logger.info('Screen recording started')

    def _recorder_stop(self) -> None:
        """Stop recording and show save dialog."""
        if self._screen_recorder is None:
            return
        self._recorder_state = 'stopping'
        self.recorder_overlay.show_recorder_stopping()
        self._update_library_recorder_state()

        rec = self._screen_recorder

        def _finish():
            rec.stop()
            self.root.after(0, lambda: self._recorder_finish(rec))

        threading.Thread(target=_finish, daemon=True, name='rec-stop').start()

    def _recorder_finish(self, rec: ScreenRecorder) -> None:
        """Called on main thread after encoding is complete."""
        self._screen_recorder = None
        self._recorder_state  = 'idle'
        self.recorder_overlay._close()
        self._update_library_recorder_state()
        logger.info(f'Screen recording stopped — {rec.bytes_written/1024**2:.1f} MB')

        if rec.error:
            from dialogs import alert
            alert(self.root, 'Recorder error', rec.error)
            return
        if not rec.output_path or not os.path.exists(rec.output_path):
            return
        if os.path.getsize(rec.output_path) == 0:
            from dialogs import alert
            try:
                os.unlink(rec.output_path)
            except Exception:
                pass
            alert(self.root, 'Recording failed',
                  'The output file is empty — the encoder produced no data.\n\n'
                  'This can happen if the recording was stopped too quickly\n'
                  'or if the screen capture failed to initialise.')
            return

        dur     = int(rec.elapsed())
        size_mb = os.path.getsize(rec.output_path) / (1024 ** 2)
        try:
            parent = self.library.win if self.library.win.winfo_ismapped() else self.root
        except Exception:
            parent = self.root
        dest = show_save_dialog(parent, rec.output_path, dur, size_mb)
        if dest:
            logger.info(f'Recording saved: {dest}')
            # Track path in index so it shows in the list regardless of save location
            try:
                from screen_recorder import add_to_recordings_index
                add_to_recordings_index(dest)
            except Exception:
                pass
            # Refresh the library recorder tab list
            if hasattr(self, 'library'):
                self.library.update_recorder_state('idle')

    def _on_recorder_cap(self) -> None:
        """1 GB cap hit — auto-stop."""
        logger.info('Screen recording: 1 GB cap reached — stopping')
        from dialogs import alert
        self._recorder_stop()
        self.root.after(500, lambda: alert(
            self.root, '1 GB limit reached',
            'The recording reached the 1 GB size cap\nand has been stopped automatically.'))

    def _recorder_tick(self) -> None:
        """Called every 500ms while recording to push live state to the library tab."""
        if self._recorder_state != 'recording' or self._screen_recorder is None:
            return
        elapsed = time.time() - self._recorder_t0
        size_mb = self._screen_recorder.bytes_written / (1024 ** 2)
        self._update_library_recorder_state(elapsed=elapsed, size_mb=size_mb)
        self.root.after(500, self._recorder_tick)

    def _update_library_recorder_state(self, elapsed: float = 0.0, size_mb: float = 0.0) -> None:
        try:
            self.library.update_recorder_state(self._recorder_state, elapsed, size_mb)
        except Exception:
            pass

    # ── GIF recorder ─────────────────────────────────────────────────────────

    def _on_gif_toggle(self) -> None:
        """Shift+F3 / button press — start or stop GIF recording."""
        if self._gif_state == 'idle':
            self._gif_start()
        elif self._gif_state == 'recording':
            self._gif_stop()
        # 'encoding' — ignore, let it finish

    def _gif_start(self) -> None:
        """Show setup dialog, then begin capturing."""
        try:
            mapped = self.library.win.winfo_ismapped()
            parent = self.library.win if mapped else self.root
        except Exception:
            mapped = False
            parent = self.root
        try:
            dlg = GifSetupDialog(parent)
        except Exception as exc:
            logger.exception(f'GIF setup dialog creation failed: {exc}')
            return
        self._gif_setup_dlg = dlg
        parent.wait_window(dlg.win)
        self._gif_setup_dlg = None
        if dlg.result is None:
            return   # user cancelled

        cfg = dlg.result
        logger.info(f'GIF setup: {cfg}')
        try:
            self._gif_recorder = GifRecorder(
                hwnd=cfg['hwnd'],
                mon=cfg.get('mon'),
                fps=cfg['fps'],
                max_width=cfg['max_width'],
                max_duration_s=cfg['max_duration_s'],
            )
            self._gif_recorder.start(
                on_done=lambda path, dur: self._q.put(('gif:done', (path, dur))),
                on_error=lambda msg: self._q.put(('gif:error', msg)),
                on_cap_reached=lambda: self._q.put(('gif:cap', None)),
            )
        except Exception as exc:
            logger.error(f'GIF recorder failed to start: {exc}')
            from dialogs import alert
            alert(self.root, 'GIF error', str(exc))
            self._gif_recorder = None
            return

        self._gif_state = 'recording'
        self._gif_t0    = time.time()
        self.gif_overlay.show_gif_recording()
        self._update_library_gif_state()
        self._gif_tick()
        logger.info('GIF recording started')

    def _gif_stop(self) -> None:
        """Signal capture to stop; encoding happens in background."""
        if self._gif_recorder is None:
            return
        self._gif_state = 'encoding'
        self.gif_overlay.show_gif_encoding()
        self._update_library_gif_state()
        self._gif_recorder.stop()

    def _on_gif_done(self, data) -> None:
        """Called on main thread when encoding finishes successfully."""
        tmp_path, dur = data
        self._gif_recorder = None
        self._gif_state    = 'idle'
        self.gif_overlay._close()
        self._update_library_gif_state()
        elapsed = int(dur)
        logger.info(f'GIF recording complete — {elapsed}s, {tmp_path}')

        if not tmp_path or not os.path.exists(tmp_path):
            return

        try:
            parent = self.library.win if self.library.win.winfo_ismapped() else self.root
        except Exception:
            parent = self.root

        dest = show_gif_save_dialog(parent, tmp_path, dur)
        if dest:
            logger.info(f'GIF saved: {dest}')
            # Refresh library GIF tab
            try:
                self.library.update_gif_state('idle')
            except Exception:
                pass

    def _on_gif_error(self, msg: str) -> None:
        self._gif_recorder = None
        self._gif_state    = 'idle'
        self.gif_overlay._close()
        self._update_library_gif_state()
        logger.error(f'GIF recorder error: {msg}')
        from dialogs import alert
        alert(self.root, 'GIF error', msg)

    def _on_gif_cap(self) -> None:
        """Max duration cap reached — auto-stop."""
        if self._gif_recorder is None:
            return
        logger.info('GIF recording: max duration reached — stopping')
        dur_s = int(self._gif_recorder.max_duration_s)
        self.gif_overlay.show_gif_capped(dur_s)
        self._gif_state = 'encoding'
        self._update_library_gif_state()
        self._gif_recorder.stop()

    def _gif_tick(self) -> None:
        """Push live elapsed/frame count to the library tab every 500ms."""
        if self._gif_state != 'recording' or self._gif_recorder is None:
            return
        elapsed = time.time() - self._gif_t0
        frames  = self._gif_recorder.frame_count
        self._update_library_gif_state(elapsed=elapsed, frames=frames)
        self.root.after(500, self._gif_tick)

    def _update_library_gif_state(self, elapsed: float = 0.0, frames: int = 0) -> None:
        try:
            self.library.update_gif_state(self._gif_state, elapsed, frames)
        except Exception:
            pass

    # ── System tray ───────────────────────────────────────────────────────────

    def _make_icon(self) -> Image.Image:
        # Render at 8× then downsample to 64×64 for clean anti-aliased edges.
        S = 8
        B = 64 * S   # 512 px working canvas

        def _hex(h):
            h = h.lstrip('#')
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

        def _grad_mask(mask, c1, c2):
            """Apply a top→bottom gradient through a white-on-black mask."""
            r1,g1,b1 = _hex(c1); r2,g2,b2 = _hex(c2)
            grad = Image.new('RGBA', (B, B))
            dg   = ImageDraw.Draw(grad)
            for y in range(B):
                t = y / (B - 1)
                dg.line([(0,y),(B,y)], fill=(
                    int(r1+(r2-r1)*t), int(g1+(g2-g1)*t), int(b1+(b2-b1)*t), 255))
            out = Image.new('RGBA', (B, B), (0,0,0,0))
            out.paste(grad, mask=mask.split()[0])
            return out

        # ── Background: purple border + dark fill ─────────────────────────────
        base = Image.new('RGBA', (B, B), (0,0,0,0))
        d    = ImageDraw.Draw(base)
        d.rounded_rectangle([0, 0, B-1, B-1], radius=13*S, fill='#7c3aed')   # ACCENT border
        d.rounded_rectangle([3*S, 3*S, B-1-3*S, B-1-3*S], radius=11*S, fill='#080f1a')

        # ── Lightning bolt polygon ────────────────────────────────────────────
        BOLT = [(x*S, y*S) for x,y in [(42,4),(10,34),(28,34),(22,60),(52,26),(36,26)]]

        bolt_mask = Image.new('RGBA', (B, B), (0,0,0,0))
        ImageDraw.Draw(bolt_mask).polygon(BOLT, fill='white')

        # Glow layer
        from PIL import ImageFilter as _IF
        glow = _grad_mask(bolt_mask.filter(_IF.GaussianBlur(12)), '#7dd3fc', '#1e40af')
        base = Image.alpha_composite(base, glow)

        # Sharp bolt — sky blue top → deep navy bottom
        base = Image.alpha_composite(base, _grad_mask(bolt_mask, '#bae6fd', '#0f2a6e'))

        # Downsample to final 64×64
        return base.resize((64, 64), Image.LANCZOS)

    def _start_tray(self) -> None:
        self._tray = pystray.Icon(
            'Hotkeys', self._make_icon(), self._tooltip(), self._make_menu(),
        )
        t = threading.Thread(target=self._run_tray, daemon=True)
        t.start()
        logger.info('Tray started.')

    def _run_tray(self) -> None:
        """Run the pystray event loop. On macOS, AppKit must be invoked carefully
        from a background thread — log any failure clearly instead of crashing silently."""
        try:
            self._tray.run()
        except Exception as e:
            logger.error(f'Tray crashed: {e}')
            # On macOS pystray may fail if AppKit isn't available on this thread.
            # The app continues working (hotkeys, transcription) — only the tray icon is lost.
            if sys.platform == 'darwin':
                logger.error(
                    'macOS tray error — this usually means pystray could not access AppKit. '
                    'The app will keep running but the menu bar icon will be missing. '
                    'Check that pyobjc-framework-Cocoa is installed: pip install pyobjc-framework-Cocoa'
                )

    def _make_menu(self) -> pystray.Menu:
        def prov_item(key: str, label: str) -> pystray.MenuItem:
            return pystray.MenuItem(
                label,
                lambda: self._q.put(('switch_provider', key)),
                checked=lambda item, k=key: self.config.get('active_provider') == k,
                radio=True,
            )

        hk = self._hotkey_cfg()
        w_state = '🔴 Recording...' if self._whisper_recording else '🎙 Whisper'

        return pystray.Menu(
            pystray.MenuItem(f'Hotkeys  v{VERSION}', None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Take a screenshot', lambda: self._hk_screenshot()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Provider', pystray.Menu(
                *([prov_item('local', 'Qwen 2.5 1.5B (Local · Free)')] if local_provider_available() else []),
                prov_item('groq',     'Groq'),
                prov_item('cerebras', 'Cerebras'),
            )),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                f'Prompt Library  ({hk.get("library", "alt+shift+e").upper()})',
                lambda: self._q.put(('library', None)),
            ),
            pystray.MenuItem('History', lambda: self._q.put(('history', None))),
            pystray.MenuItem('Settings', lambda: self._q.put(('settings', None))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(w_state, None, enabled=False),
            pystray.MenuItem(
                'Push-to-talk mode',
                self._toggle_ptt,
                checked=lambda item: self.config.get('push_to_talk', False),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Restore Default Prompts',
                             lambda: self._q.put(('restore_defaults', None)),
                             enabled=lambda _: not self._at_default_prompts),
            pystray.MenuItem('↺  Reload hotkeys', lambda: self._q.put(('reload_hotkeys', None))),
            pystray.MenuItem('Quit', self._quit),
        )

    def _switch_provider(self, key: str) -> None:
        if self.config.get('active_provider') == key:
            return   # already on this provider — nothing to do
        self.config['active_provider'] = key
        save_config(self.config)
        self.provider = build_provider(self.config)
        if isinstance(self.provider, LocalProvider) and not self.provider.ready:
            threading.Thread(target=self._load_model, daemon=True).start()
        self._update_tray()
        logger.info(f'Switched to provider: {key}')

    def _toggle_ptt(self) -> None:
        self.config['push_to_talk'] = not self.config.get('push_to_talk', False)
        save_config(self.config)
        self._register_hotkeys()
        self._update_tray()
        state = 'ON' if self.config['push_to_talk'] else 'OFF'
        logger.info(f'Push-to-talk toggled: {state}')

    def _tooltip(self) -> str:
        active  = self.config.get('active_provider', 'cerebras')
        r_state = 'Ready' if self.provider.ready else 'Loading…'
        w_state = '🔴 Recording' if self._whisper_recording else 'Idle'
        return f'Hotkeys  ·  {active.title()}  ·  {r_state}  ·  Whisper: {w_state}'

    def _update_tray(self) -> None:
        try:
            self._tray.title = self._tooltip()
            self._tray.menu  = self._make_menu()
        except Exception:
            pass

    def _notify(self, title: str, msg: str) -> None:
        try:
            self._tray.notify(msg, title)
        except Exception:
            pass

    def _watch_singleton_socket(self) -> None:
        """Background thread: waits for a new instance to signal QUIT.

        The TCP connection itself is proof a new instance is running — we
        do not do a secondary PID check, because in dist builds the process
        name / cmdline heuristic is unreliable during the brief startup window.
        """
        if not _singleton_sock:
            return
        while True:
            try:
                conn, _ = _singleton_sock.accept()
                try:
                    conn.recv(16)
                finally:
                    conn.close()
                logger.info('New instance launched — shutting down gracefully.')
                self.root.after(0, self._quit)
                return
            except Exception:
                return   # socket closed during normal _quit()

    def _quit(self) -> None:
        logger.info('Shutting down.')

        # Schedule a hard kill in case any cleanup step hangs
        def _force_exit():
            logger.warning('Forced exit after timeout.')
            os._exit(0)
        _killer = threading.Timer(5.0, _force_exit)
        _killer.daemon = True
        _killer.start()

        try:
            if _singleton_sock:
                _singleton_sock.close()
        except Exception:
            pass
        keyboard.unhook_all()
        try:
            self._audio.stop()
        except Exception:
            pass
        try:
            self._transcriber.shutdown()
        except Exception:
            pass
        try:
            self._tray.visible = False
            self._tray.stop()
            time.sleep(0.6)   # let pystray finish Shell_NotifyIcon(NIM_DELETE)
        except Exception:
            pass
        _killer.cancel()
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
        sys.exit(0)

    def run(self) -> None:
        self.root.mainloop()


# ── Single-instance guard ─────────────────────────────────────────────────────

# ── Single-instance guard ─────────────────────────────────────────────────────
_singleton_sock: socket.socket | None = None
_SINGLETON_PORT = 47_294   # localhost IPC port


def _find_other_hotkeys_pids() -> list[int]:
    """Return PIDs of other TOP-LEVEL Hotkeys instances only.

    Works for both frozen dist builds (Hotkeys.exe / Hotkeys) and source
    runs (python / pythonw / python3 … main.py).

    Excludes our entire lineage (descendants AND ancestors) so we never
    accidentally kill the venv launcher (our parent) which would collapse
    its Windows Job Object and kill us too.
    """
    try:
        import psutil
    except ImportError:
        return []

    my_pid    = os.getpid()
    is_frozen = getattr(sys, 'frozen', False)   # True when bundled by PyInstaller

    # Build the set of PIDs we must never touch: us, our children, our parents.
    safe: set[int] = {my_pid}
    try:
        for c in psutil.Process(my_pid).children(recursive=True):
            safe.add(c.pid)
    except Exception:
        pass
    try:
        p = psutil.Process(my_pid)
        while True:
            p = p.parent()
            if p is None:
                break
            safe.add(p.pid)
    except Exception:
        pass

    # Exe names to match (both platforms, case-insensitive)
    FROZEN_NAMES  = {'hotkeys.exe', 'hotkeys'}
    SOURCE_NAMES  = {'pythonw.exe', 'python.exe', 'python3', 'python',
                     'python3.11', 'python3.12', 'hotkeys.exe', 'hotkeys'}

    candidates: dict[int, int] = {}   # pid → parent_pid
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'ppid']):
        try:
            if proc.pid in safe:
                continue
            name = (proc.info['name'] or '').lower()

            if is_frozen:
                # Dist build: just match by executable name — no cmdline needed
                if name in FROZEN_NAMES:
                    candidates[proc.pid] = proc.info.get('ppid') or 0
            else:
                # Source run: python interpreter running main.py inside Hotkeys dir
                if name not in SOURCE_NAMES:
                    continue
                cmdline = ' '.join(proc.info['cmdline'] or []).lower()
                if 'main.py' in cmdline and 'hotkeys' in cmdline:
                    candidates[proc.pid] = proc.info.get('ppid') or 0
        except Exception:
            pass

    # Keep only roots (parent is not itself a candidate) to avoid double-counting
    return [pid for pid, ppid in candidates.items() if ppid not in candidates]


def _sweep_ghost_tray_icons() -> None:
    """Simulate mouse movement across the Windows notification-area toolbars.

    When Windows receives WM_MOUSEMOVE over a tray slot whose owner process is
    dead, it removes that icon automatically — no user hover needed.
    Covers both the visible tray and the overflow (hidden icons) area.
    """
    if sys.platform != 'win32':
        return
    try:
        import struct
        u32 = ctypes.windll.user32
        WM_MOUSEMOVE = 0x0200

        def _child(parent: int, cls: str) -> int:
            return u32.FindWindowExW(parent, None, cls, None)

        def _sweep(toolbar: int) -> None:
            if not toolbar:
                return
            buf = ctypes.create_string_buffer(16)
            u32.GetClientRect(toolbar, buf)
            _, _, w, h = struct.unpack('iiii', buf.raw)
            mid_y = (h // 2) & 0xFFFF
            for x in range(0, max(w, 1), 4):
                u32.SendMessageW(toolbar, WM_MOUSEMOVE, 0, (x & 0xFFFF) | (mid_y << 16))

        # Primary notification area
        tray    = u32.FindWindowW('Shell_TrayWnd', None)
        notify  = _child(tray,   'TrayNotifyWnd')
        pager   = _child(notify, 'SysPager')
        _sweep(_child(pager, 'ToolbarWindow32'))

        # Overflow (hidden icons) area
        overflow = u32.FindWindowW('NotifyIconOverflowWindow', None)
        _sweep(_child(overflow, 'ToolbarWindow32'))
    except Exception:
        pass


def _ensure_single_instance(_depth: int = 0) -> None:
    """Guarantee exactly one running copy.

    On Windows a named mutex serialises concurrent launches.
    On macOS/Linux the socket-based approach is used directly.
    """
    global _singleton_sock
    import psutil

    # ── Windows: use a named mutex to serialise concurrent launches ───────────
    if sys.platform == 'win32':
        kernel32   = ctypes.windll.kernel32
        MUTEX_NAME = 'Hotkeys_StartupLock_v3'

        mutex = kernel32.CreateMutexW(None, True, MUTEX_NAME)
        err   = kernel32.GetLastError()

        if err == 183:      # ERROR_ALREADY_EXISTS — another launch is starting
            kernel32.CloseHandle(mutex)
            if _depth >= 3:
                sys.exit(1)
            time.sleep(4.0)
            if _find_other_hotkeys_pids():
                sys.exit(0)
            _ensure_single_instance(_depth + 1)
            return

    # ── All platforms: graceful quit + hard-kill + socket bind ───────────────

    try:
        # 1. Graceful quit via socket
        c = socket.create_connection(('127.0.0.1', _SINGLETON_PORT), timeout=1)
        c.sendall(b'QUIT')
        c.close()
        time.sleep(2.5)
    except Exception:
        pass

    # 2. Hard-kill anything still alive
    for pid in _find_other_hotkeys_pids():
        try:
            proc = psutil.Process(pid)
            for child in proc.children(recursive=True):
                try:
                    child.kill()
                except Exception:
                    pass
            proc.kill()
        except Exception:
            pass

    # 3. Actively sweep the notification area to evict ghost icons from dead
    #    processes (no hovering required), then give the OS a moment to settle.
    _sweep_ghost_tray_icons()
    time.sleep(0.8)

    # 4. Bind socket as graceful-quit channel for the NEXT launch
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(('127.0.0.1', _SINGLETON_PORT))
        s.listen(5)
        _singleton_sock = s
    except Exception:
        pass

    if sys.platform == 'win32':
        kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)


# ── macOS accessibility permission ────────────────────────────────────────────

def _mac_ensure_accessibility() -> None:
    """macOS only: block startup until Accessibility permission is granted.

    Global hotkeys (keyboard library) require the Accessibility entitlement.
    If not yet granted, open System Settings to the right pane and show a
    clear CTk dialog that waits until the user has toggled the switch.
    Silently returns on Windows/Linux or if already trusted.
    """
    if sys.platform != 'darwin':
        return

    try:
        from ctypes import cdll
        _libax = cdll.LoadLibrary(
            '/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices'
        )
        _is_trusted = lambda: bool(_libax.AXIsProcessTrusted())
    except Exception:
        return   # Can't check — proceed; keyboard will fail naturally if needed

    if _is_trusted():
        return

    import subprocess as _sp

    # Open the Accessibility pane in System Settings automatically
    _sp.Popen(['open',
               'x-apple.systempreferences:'
               'com.apple.preference.security?Privacy_Accessibility'])

    # Build a blocking CTk dialog — auto-closes the moment permission is granted
    _setup_root = ctk.CTk()
    _setup_root.withdraw()

    _win = ctk.CTkToplevel(_setup_root)
    _win.title('Hotkeys — One-time Setup')
    _win.resizable(False, False)
    _win.attributes('-topmost', True)
    _win.geometry('460x340')
    _win.protocol('WM_DELETE_WINDOW', lambda: None)   # prevent accidental close

    ctk.CTkLabel(_win, text='⚡  Almost ready!',
                 font=ctk.CTkFont(size=22, weight='bold')).pack(pady=(30, 8))

    ctk.CTkLabel(_win,
                 text=(
                     'Hotkeys needs one permission to work.\n'
                     'System Settings has opened for you — just:\n'
                 ),
                 font=ctk.CTkFont(size=14)).pack()

    ctk.CTkLabel(_win,
                 text='1.  Find Hotkeys in the list\n2.  Flip the switch  ON',
                 font=ctk.CTkFont(size=16, weight='bold'),
                 justify='left').pack(pady=8)

    ctk.CTkLabel(_win,
                 text="That's it. Hotkeys will start automatically.",
                 font=ctk.CTkFont(size=13),
                 text_color='#94a3b8').pack()

    # Animated waiting indicator
    _dots = ['', '.', '..', '...']
    _dot_idx = [0]
    _wait_lbl = ctk.CTkLabel(_win, text='Waiting for permission...',
                             font=ctk.CTkFont(size=12), text_color='#7c3aed')
    _wait_lbl.pack(pady=(16, 0))

    def _poll():
        if _is_trusted():
            _setup_root.quit()
            return
        _dot_idx[0] = (_dot_idx[0] + 1) % len(_dots)
        _wait_lbl.configure(text=f'Waiting for permission{_dots[_dot_idx[0]]}')
        _setup_root.after(500, _poll)

    _setup_root.after(500, _poll)
    _setup_root.mainloop()
    try:
        _setup_root.destroy()
    except Exception:
        pass


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # pystray uses multiprocessing on Windows and spawns a child process with
    # the exact same command line (pythonw.exe main.py).  That child re-imports
    # __main__, so __name__ == '__main__' is True inside it too.  We MUST skip
    # _ensure_single_instance() and App() in that child or it will kill us.
    # multiprocessing.current_process().name is 'MainProcess' only in the real
    # user-launched process; spawned workers get names like 'Process-1'.
    import multiprocessing as _mp
    if _mp.current_process().name == 'MainProcess':
        _mac_ensure_accessibility()   # no-op on Windows; blocks until permission granted on Mac
        _ensure_single_instance()
        app = App()
        import signal
        signal.signal(signal.SIGTERM, lambda *_: app._quit())
        signal.signal(signal.SIGINT,  lambda *_: app._quit())
        app.run()
    # else: we are pystray's multiprocessing worker — do nothing here;
    # multiprocessing's spawn handler will call the real target function.
