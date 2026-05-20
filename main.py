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
import pyperclip
import pystray
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from storage  import (
    load_config, save_config, load_prompts, save_prompts,
    appdata_dir, log_path, models_dir, assets_dir, history_path,
    save_history, load_history, make_whisper_cfg, _HISTORY_MAX_ENTRIES,
)
from engine      import build_provider, LocalProvider, Provider
from overlay     import OverlayWindow
from library     import LibraryWindow
from settings    import SettingsWindow
from history_ui  import HistoryWindow
from core.audio       import AudioCapture
from core.vad         import SileroVAD
from core.transcriber import Transcriber
from core.typer       import copy_to_clipboard, paste_from_clipboard
from core.sounds      import play_start, play_stop

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

        # ── Config & prompts ─────────────────────────────────────────────────
        self.config  = load_config()
        self.prompts = load_prompts()
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
        self.refine_overlay  = OverlayWindow(self.root, slot=0)
        self.whisper_overlay = OverlayWindow(self.root, slot=1)
        self.library  = LibraryWindow(self.root, self.prompts,
                                      on_select=self._on_prompt_selected,
                                      on_save=self._on_prompts_saved,
                                      hotkey_cfg=self._hotkey_cfg())
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
            'whisper:status':   self._on_transcriber_status_event,
            'whisper:result':   self._on_whisper_result,
            'whisper:error':    self._on_whisper_error,
        }

        self._register_hotkeys()
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

    def _register_hotkeys(self) -> None:
        keyboard.unhook_all()
        hk  = self._hotkey_cfg()
        ptt = self.config.get('push_to_talk', False)
        try:
            keyboard.add_hotkey(hk.get('refine',  'alt+shift+w'), self._hk_refine,  suppress=True)
            keyboard.add_hotkey(hk.get('library', 'alt+shift+e'), self._hk_library, suppress=True)

            if ptt:
                # Push-to-talk: hold to record, release to stop.
                # Use the last component of the whisper hotkey string as the PTT key.
                whisper_hk  = hk.get('whisper', 'ctrl+enter')
                ptt_key     = whisper_hk.split('+')[-1]
                keyboard.on_press_key(
                    ptt_key,
                    lambda _: self._q.put(('whisper:start', None)),
                    suppress=True,
                )
                keyboard.on_release_key(
                    ptt_key,
                    lambda _: self._q.put(('whisper:stop', None)),
                    suppress=True,
                )
                logger.info(f'PTT mode: key={ptt_key!r}')
            else:
                keyboard.add_hotkey(hk.get('whisper', 'ctrl+enter'), self._hk_whisper, suppress=True)

            keyboard.add_hotkey('escape', self._hk_escape, suppress=False)
            logger.info(f'Hotkeys registered: {hk}  PTT={ptt}')
        except Exception as e:
            logger.error(f'Hotkey registration failed: {e}')

    def _hk_refine(self) -> None:
        logger.info('Refine hotkey fired.')
        threading.Thread(target=self._capture_and_queue, daemon=True).start()

    def _capture_and_queue(self) -> None:
        time.sleep(0.05)                      # wait for key release (was 0.08)
        try:
            prev = pyperclip.paste()
        except Exception:
            prev = ''
        try:
            pyperclip.copy('')
        except Exception:
            pass
        keyboard.send('ctrl+c')
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

    def _hk_library(self) -> None:
        self._q.put(('library', None))

    def _hk_whisper(self) -> None:
        if not self._whisper_recording:
            self._q.put(('whisper:start', None))
        else:
            self._q.put(('whisper:stop', None))

    def _hk_escape(self) -> None:
        if self._whisper_recording:
            self._q.put(('whisper:cancel', None))

    # ── History callbacks ─────────────────────────────────────────────────────

    def _on_history_cleared(self) -> None:
        self._history = []

    # ── Refine callbacks ──────────────────────────────────────────────────────

    def _on_prompt_selected(self, prompt: dict) -> None:
        self.active_prompt = prompt

    def _on_prompts_saved(self, prompts: list) -> None:
        self.prompts = prompts
        save_prompts(prompts)
        if prompts and self.active_prompt not in prompts:
            self.active_prompt = prompts[0]

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
        self._register_hotkeys()
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
        try:
            while True:
                event, data = self._q.get_nowait()
                handler = self._dispatch.get(event)
                if handler:
                    handler(data)
        except queue.Empty:
            pass
        self.root.after(30, self._poll)

    # ── Refine actions ────────────────────────────────────────────────────────

    def _do_refine(self, text: str) -> None:
        if self._whisper_recording:
            return   # don't clobber the clipboard mid-recording
        if self._refine_in_progress:
            return   # already running — ignore rapid double-press
        if not text or not text.strip():
            self.refine_overlay.show_no_selection()
            return
        if isinstance(self.provider, LocalProvider) and not self.provider.ready:
            self.refine_overlay.show_loading_model()
            return
        if not self.provider.ready:
            self.refine_overlay.show_error('API key required — open Settings')
            return

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
                    self._q.put(('refine:error', str(e)[:60]))
            finally:
                self._q.put(('refine:unlock', gen))

        threading.Thread(target=infer, daemon=True).start()

    def _on_refine_done(self, result: str) -> None:
        elapsed = time.time() - self._refine_t0
        self.refine_overlay.show_done(elapsed)
        pyperclip.copy(result)
        self.root.after(40, lambda: keyboard.send('ctrl+v'))   # was 60 ms
        logger.info(f'Refinement complete in {elapsed:.2f}s')

    def _on_refine_timeout(self, gen: int) -> None:
        if gen != self._refine_gen:
            return   # already handled by normal completion
        self._refine_in_progress = False
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

    # ── System tray ───────────────────────────────────────────────────────────

    def _make_icon(self) -> Image.Image:
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)
        d.rounded_rectangle([2, 2, 62, 62], radius=14, fill='#1a1a2e')
        try:
            fnt = ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf', 22)
            d.text((8, 14), '⚡HK', fill='#a0a0ff', font=fnt)
        except Exception:
            d.text((10, 20), 'HK', fill='#a0a0ff')
        return img

    def _start_tray(self) -> None:
        self._tray = pystray.Icon(
            'Hotkeys', self._make_icon(), self._tooltip(), self._make_menu(),
        )
        threading.Thread(target=self._tray.run, daemon=True).start()
        logger.info('Tray started.')

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
            pystray.MenuItem('Provider', pystray.Menu(
                prov_item('local',    'Qwen 2.5 1.5B (Local · Free)'),
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

        Before acting on the signal we confirm a live Hotkeys process exists —
        this filters out stale QUIT messages left over from rapid-launch races
        where the sender has already exited.
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
                # Only quit if a real new instance is actually still running
                if _find_other_hotkeys_pids():
                    logger.info('New instance launched — shutting down gracefully.')
                    self.root.after(0, self._quit)
                    return
                # else: stale QUIT from an already-dead launcher — ignore
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

    The venv pythonw.exe is a launcher that spawns the real Python interpreter
    as a child, then waits inside a Windows Job Object.  If we kill the launcher
    (our own parent/ancestor), the Job Object closes and kills us too.

    We therefore exclude our entire lineage — both descendants AND ancestors —
    from the kill list.  Only genuine *other* root instances are returned.
    """
    import psutil
    my_pid = os.getpid()

    # Build the set of PIDs we must never touch: our children AND our parents.
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

    # First pass — collect all candidate PIDs
    candidates: dict[int, int] = {}   # pid → parent_pid
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'ppid']):
        try:
            if proc.pid in safe:
                continue
            name = (proc.info['name'] or '').lower()
            if name not in ('pythonw.exe', 'python.exe', 'hotkeys.exe'):
                continue
            cmdline = ' '.join(proc.info['cmdline'] or []).lower()
            if 'hotkeys' in cmdline and 'main.py' in cmdline:
                candidates[proc.pid] = proc.info.get('ppid') or 0
        except Exception:
            pass

    # Second pass — keep only roots (parent is not another candidate)
    return [pid for pid, ppid in candidates.items() if ppid not in candidates]


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

    # 3. Brief pause for the OS to clear ghost tray icons
    time.sleep(1.5)

    # 4. Bind socket as graceful-quit channel for the NEXT launch
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('127.0.0.1', _SINGLETON_PORT))
        s.listen(5)
        _singleton_sock = s
    except Exception:
        pass

    if sys.platform == 'win32':
        kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)


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
        _ensure_single_instance()
        app = App()
        import signal
        signal.signal(signal.SIGTERM, lambda *_: app._quit())
        signal.signal(signal.SIGINT,  lambda *_: app._quit())
        app.run()
    # else: we are pystray's multiprocessing worker — do nothing here;
    # multiprocessing's spawn handler will call the real target function.
