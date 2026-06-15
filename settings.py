"""Settings window, General / Providers / Whisper tabs, sidebar layout."""
import subprocess
import sys
import threading
import tkinter as tk
from typing import Callable

import customtkinter as ctk

from dialogs import alert
from engine  import (PROVIDER_KEYS, GROQ_MODELS, CEREBRAS_MODELS, provider_available,
                     OPENAI_MODELS, ANTHROPIC_MODELS, GEMINI_MODELS,
                     local_provider_available)
import os
from pathlib import Path
from storage import set_autostart, appdata_dir
from theme   import (
    BG, SURFACE, SURF2, SURF3, BORDER, BORDER2,
    ACCENT, ACCENTL, TEXT_P, TEXT_S,
    OK, ERR,
    FONT_FAMILY, FONT_SM_BOLD,
    PAD, PAD_SM, RADIUS, RADIUS_SM,
)

SIDEBAR_W = 180

WHISPER_CPU_MODELS = ['base', 'small']
WHISPER_GPU_MODELS = ['large-v3-turbo', 'small']
WHISPER_LANGS = [
    'auto',
    'en', 'ur', 'ar', 'zh', 'es', 'fr', 'de', 'hi', 'pt', 'ru',
    'ja', 'ko', 'it', 'tr', 'pl', 'nl', 'sv', 'fa', 'id', 'vi',
]


class SettingsWindow:
    def __init__(self, root, config: dict, on_save: Callable,
                 on_restore: Callable | None = None) -> None:
        self.root       = root
        self.config     = config
        self.on_save    = on_save
        self.on_restore = on_restore
        self._api_widgets: dict[str, dict]          = {}
        self._nav_btns:    dict[str, ctk.CTkButton] = {}
        self._panels:      dict[str, ctk.CTkFrame]  = {}
        self._build()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.win = ctk.CTkToplevel(self.root)
        self.win.title('Settings, Hotkeys')
        self.win.configure(fg_color=BG)
        self.win.resizable(False, False)
        self.win.withdraw()
        self.win.protocol('WM_DELETE_WINDOW', self.hide)

        # Header
        hdr = ctk.CTkFrame(self.win, fg_color=SURFACE, corner_radius=0, height=60)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text='Settings', font=(FONT_FAMILY, 15, 'bold'),
                     text_color=TEXT_P).pack(side='left', anchor='w', padx=PAD, pady=PAD_SM)
        ctk.CTkLabel(hdr, text='Hotkeys', font=(FONT_FAMILY, 9),
                     text_color=TEXT_S).pack(side='right', anchor='e', padx=PAD)

        # Body: sidebar + content
        body = ctk.CTkFrame(self.win, fg_color=BG, corner_radius=0)
        body.pack(fill='both', expand=True)

        sidebar = ctk.CTkFrame(body, fg_color=SURFACE, corner_radius=0, width=SIDEBAR_W)
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        ctk.CTkLabel(sidebar, text='MENU', font=(FONT_FAMILY, 8, 'bold'),
                     text_color=TEXT_S).pack(anchor='w', padx=PAD, pady=(PAD, 4))

        content_host = ctk.CTkFrame(body, fg_color=BG, corner_radius=0)
        content_host.pack(side='left', fill='both', expand=True)

        self._panels['general']   = self._build_general(content_host)
        self._panels['providers'] = self._build_providers(content_host)
        self._panels['whisper']   = self._build_whisper(content_host)

        nav_items = [
            ('general',   '⚙  General'),
            ('providers', '🔌  AI providers'),
            ('whisper',   '🎙  Audio & dictation'),
        ]
        for key, label in nav_items:
            btn = ctk.CTkButton(
                sidebar, text=label, anchor='w', font=(FONT_FAMILY, 10), height=36,
                fg_color='transparent', hover_color=SURF2, text_color=TEXT_P,
                corner_radius=RADIUS_SM, command=lambda k=key: self._show_panel(k),
            )
            btn.pack(fill='x', padx=6, pady=2)
            self._nav_btns[key] = btn

        # Footer
        foot = ctk.CTkFrame(self.win, fg_color=SURFACE, corner_radius=0, height=56)
        foot.pack(fill='x', side='bottom')
        foot.pack_propagate(False)
        ctk.CTkButton(foot, text='Save',   width=100, height=34, fg_color=ACCENT,  hover_color=ACCENTL,
                      text_color=TEXT_P, font=(FONT_FAMILY, 10), corner_radius=RADIUS_SM,
                      command=self._save).pack(side='right', padx=PAD, pady=PAD_SM)
        ctk.CTkButton(foot, text='Cancel', width=80,  height=34, fg_color=SURF2,   hover_color=SURF3,
                      text_color=TEXT_P, font=(FONT_FAMILY, 10), corner_radius=RADIUS_SM,
                      command=self.hide).pack(side='right', pady=PAD_SM)
        if self.on_restore:
            ctk.CTkButton(foot, text='↺  Reset everything…', width=160, height=34,
                          fg_color=SURF2, hover_color=ERR,
                          text_color=TEXT_S, font=(FONT_FAMILY, 10), corner_radius=RADIUS_SM,
                          command=self.on_restore).pack(side='left', padx=PAD, pady=PAD_SM)

        self._show_panel('general')
        self._center()

    # ── General panel ─────────────────────────────────────────────────────────

    def _build_general(self, parent) -> ctk.CTkFrame:
        outer = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)
        # Scrollable so content never gets clipped if window is short
        scroll = ctk.CTkScrollableFrame(
            outer, fg_color=BG,
            scrollbar_button_color=SURF2,
            scrollbar_button_hover_color=SURF3,
        )
        scroll.pack(fill='both', expand=True)
        frame = scroll   # all children go into the scrollable area

        def section(title):
            ctk.CTkLabel(frame, text=title, font=(FONT_FAMILY, 9, 'bold'),
                         text_color=TEXT_S).pack(anchor='w', padx=PAD, pady=(PAD, 4))

        def divider():
            ctk.CTkFrame(frame, fg_color=BORDER, height=1,
                         corner_radius=0).pack(fill='x', padx=PAD, pady=PAD_SM)

        # ── HOTKEYS (configurable) ────────────────────────────────────────────
        section('HOTKEYS')
        from storage import DEFAULT_CONFIG as _DC
        HK_DEFAULTS = _DC['hotkeys']
        hk_cfg = self.config.get('hotkeys', {})
        self._hotkey_vars: dict[str, tk.StringVar] = {}

        hk_actions = [
            ('refine',       'Refine selected text'),
            ('library',      'Open Library'),
            ('whisper',      'Start / stop dictation'),
            ('undo_refine',  'Undo last refinement'),
            ('macro_record', 'Record / stop / play macro'),
            ('recorder',     'Toggle screen recording'),
            ('gif_record',   'Start / stop GIF recording'),
            ('ask',          'Explain / ask a question'),
            ('web',          'Open active web bookmark'),
        ]
        for action, label in hk_actions:
            row = ctk.CTkFrame(frame, fg_color='transparent')
            row.pack(fill='x', padx=PAD, pady=3)
            ctk.CTkLabel(row, text=label, font=(FONT_FAMILY, 10),
                         text_color=TEXT_P, anchor='w', width=200).pack(side='left')
            var = tk.StringVar(value=hk_cfg.get(action, HK_DEFAULTS.get(action, '')))
            self._hotkey_vars[action] = var
            badge = ctk.CTkLabel(row, textvariable=var, font=(FONT_FAMILY, 9, 'bold'),
                         text_color=ACCENT, fg_color=SURF2, corner_radius=RADIUS_SM,
                         padx=10, pady=3, width=130, cursor='hand2')
            badge.pack(side='left', padx=(0, 6))
            badge.bind('<Button-1>', lambda e, a=action, v=var: self._record_hotkey(a, v))
            ctk.CTkButton(
                row, text='Record', width=64, height=26, font=(FONT_FAMILY, 9),
                fg_color=SURF3, hover_color=SURF2, text_color=TEXT_P, corner_radius=RADIUS_SM,
                command=lambda a=action, v=var: self._record_hotkey(a, v),
            ).pack(side='left')

        divider()

        # ── MACRO SHORTCUTS (fixed reference) ────────────────────────────────
        section('FIXED SHORTCUTS')
        ctk.CTkLabel(frame, text='These cannot be changed.',
                     font=(FONT_FAMILY, 8), text_color=TEXT_S).pack(anchor='w', padx=PAD, pady=(0, 6))

        macro_shortcuts = [
            ('Esc  /  Del',     'Abort macro recording or playback immediately'),
            ('Ctrl+F1 … F12',   'Play a saved macro (assign hotkey in Library → Macros)'),
        ]
        for keys, desc in macro_shortcuts:
            row = ctk.CTkFrame(frame, fg_color='transparent')
            row.pack(fill='x', padx=PAD, pady=2)
            ctk.CTkLabel(row, text=keys,
                         font=(FONT_FAMILY, 9, 'bold'), text_color=ACCENT,
                         fg_color=SURF2, corner_radius=RADIUS_SM,
                         padx=8, pady=3, width=138, anchor='w').pack(side='left', padx=(0, 10))
            ctk.CTkLabel(row, text=desc,
                         font=(FONT_FAMILY, 10), text_color=TEXT_P,
                         anchor='w').pack(side='left', fill='x', expand=True)

        divider()

        # ── STARTUP ───────────────────────────────────────────────────────────
        section('STARTUP')
        row = ctk.CTkFrame(frame, fg_color='transparent')
        row.pack(fill='x', padx=PAD, pady=3)
        ctk.CTkLabel(row, text='Launch on Windows startup',
                     font=(FONT_FAMILY, 10), text_color=TEXT_P).pack(side='left')
        self._autostart_var = tk.BooleanVar(value=self.config.get('autostart', True))
        ctk.CTkSwitch(row, text='', variable=self._autostart_var, onvalue=True, offvalue=False,
                      progress_color=ACCENT, button_color=TEXT_P, fg_color=SURF3).pack(side='right')

        divider()

        # ── DATA (file locations) ─────────────────────────────────────────────
        section('DATA')

        def _open_folder(path: str) -> None:
            p = Path(path)
            p.mkdir(parents=True, exist_ok=True)
            # Subprocess spawn rule (PROJECT.md #12): CREATE_NO_WINDOW +
            # close_fds even for explorer (otherwise the spawning console,
            # in source mode, briefly flashes).
            flags = (subprocess.CREATE_NO_WINDOW
                     if sys.platform == 'win32' else 0)
            subprocess.Popen(
                ['explorer', os.path.normpath(str(p))],
                creationflags=flags,
                close_fds=True,
            )

        def _path_row(label_text: str, path: str, note: str) -> None:
            pf = ctk.CTkFrame(frame, fg_color=SURF2, corner_radius=RADIUS_SM)
            pf.pack(fill='x', padx=PAD, pady=(0, 2))
            ctk.CTkLabel(pf, text=label_text, font=(FONT_FAMILY, 8, 'bold'),
                         text_color=TEXT_S, anchor='w').pack(side='left', padx=(PAD_SM, 4), pady=6)
            ctk.CTkLabel(pf, text=path, font=(FONT_FAMILY, 9),
                         text_color=TEXT_S, anchor='w').pack(side='left', padx=(0, 4), pady=6)
            ctk.CTkButton(
                pf, text='📁  Open', width=82, height=22,
                font=(FONT_FAMILY, 9), corner_radius=RADIUS_SM,
                fg_color=SURF3, hover_color=BORDER2, text_color=TEXT_P,
                command=lambda p=path: _open_folder(p),
            ).pack(side='right', padx=PAD_SM, pady=4)
            ctk.CTkLabel(frame, text=note,
                         font=(FONT_FAMILY, 8), text_color=TEXT_S).pack(anchor='w', padx=PAD, pady=(0, 6))

        _path_row('App data  ', appdata_dir(),
                  'Config, prompts, history, and logs.')
        _path_row('Macros    ', str(Path(appdata_dir()) / 'macros'),
                  'Saved macro recordings (.json files).')

        return outer

    # ── Hotkey capture ────────────────────────────────────────────────────────

    def _record_hotkey(self, action: str, var: tk.StringVar) -> None:
        import threading, keyboard
        prev = var.get()
        var.set('… press keys …')

        def capture():
            try:
                combo = keyboard.read_hotkey(suppress=False)
                if combo.lower() in ('escape', 'esc'):
                    self.win.after(0, lambda: var.set(prev))
                    return
                parts = {p.lower() for p in combo.split('+')}
                if not parts & {'ctrl', 'alt', 'shift', 'windows', 'win'}:
                    self.win.after(0, lambda: var.set(prev))
                    return
                self.win.after(0, lambda v=combo: var.set(v))
            except Exception:
                self.win.after(0, lambda: var.set(prev))

        threading.Thread(target=capture, daemon=True).start()

    # ── Providers panel ───────────────────────────────────────────────────────

    def _build_providers(self, parent) -> ctk.CTkFrame:
        frame  = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)
        scroll = ctk.CTkScrollableFrame(frame, fg_color=BG,
                                        scrollbar_button_color=SURF2,
                                        scrollbar_button_hover_color=SURF3)
        scroll.pack(fill='both', expand=True)
        pcfg  = self.config.get('providers', {})

        ctk.CTkLabel(scroll, text='ACTIVE PROVIDER', font=(FONT_FAMILY, 9, 'bold'),
                     text_color=TEXT_S).pack(anchor='w', padx=PAD, pady=(PAD, 4))

        self._provider_var = tk.StringVar(value=self.config.get('active_provider', 'cerebras'))

        prov_grid = ctk.CTkFrame(scroll, fg_color='transparent')
        prov_grid.pack(fill='x', padx=PAD, pady=(0, PAD_SM))
        self._prov_cards: dict[str, ctk.CTkFrame] = {}

        short_desc = {
            'local':     'Free · Offline',
            'groq':      'Free tier',
            'cerebras':  'Free tier · Fast',
            'openai':    'Paid · GPT-4o',
            'anthropic': 'Paid · Claude',
            'gemini':    'Free tier',
            'custom':    'Any endpoint',
        }
        # Hide providers whose SDK is not bundled in this build.
        # local needs llama_cpp; openai/anthropic/gemini each need
        # their own SDK. In dist (frozen exe) users can't pip install
        # missing packages, so a disabled option that raises "pip
        # install X" on use is hostile — filter it instead.
        visible_keys = [k for k in PROVIDER_KEYS if provider_available(k)]

        N_COLS = 3
        for col_i in range(N_COLS):
            prov_grid.columnconfigure(col_i, weight=1)

        for i, key in enumerate(visible_keys):
            row_i = i // N_COLS
            col_i = i % N_COLS
            card  = ctk.CTkFrame(prov_grid, fg_color=SURF2, corner_radius=RADIUS,
                                 border_width=2, border_color=BG, cursor='hand2')
            card.grid(row=row_i, column=col_i, padx=4, pady=4, sticky='nsew')
            ctk.CTkLabel(card, text=key.upper(), font=(FONT_FAMILY, 8, 'bold'),
                         text_color=ACCENT).pack(padx=PAD_SM, pady=(8, 0))
            ctk.CTkLabel(card, text=short_desc.get(key, ''), font=(FONT_FAMILY, 8),
                         text_color=TEXT_S).pack(padx=PAD_SM, pady=(0, 8))
            card.bind('<Button-1>', lambda e, k=key: self._pick_provider(k))
            for child in card.winfo_children():
                child.bind('<Button-1>', lambda e, k=key: self._pick_provider(k))
            self._prov_cards[key] = card

        self._refresh_provider_cards()

        ctk.CTkFrame(scroll, fg_color=BORDER, height=1,
                     corner_radius=0).pack(fill='x', padx=PAD, pady=PAD_SM)

        ctk.CTkLabel(scroll, text='CREDENTIALS', font=(FONT_FAMILY, 9, 'bold'),
                     text_color=TEXT_S).pack(anchor='w', padx=PAD, pady=(0, 4))

        self._cred_host = ctk.CTkFrame(scroll, fg_color='transparent')
        self._cred_host.pack(fill='x', padx=PAD)

        # ── Standard providers: API key + model ───────────────────────────────────
        _std_models: dict[str, list[str]] = {
            'groq':      GROQ_MODELS,
            'cerebras':  CEREBRAS_MODELS,
            'openai':    OPENAI_MODELS,
            'anthropic': ANTHROPIC_MODELS,
            'gemini':    GEMINI_MODELS,
        }
        for key, models in _std_models.items():
            p      = pcfg.get(key, {})
            cframe = ctk.CTkFrame(self._cred_host, fg_color='transparent')
            self._api_widgets[key] = {'frame': cframe}

            ctk.CTkLabel(cframe, text='API Key', font=FONT_SM_BOLD,
                         text_color=TEXT_S).pack(anchor='w', pady=(0, 2))
            key_row = ctk.CTkFrame(cframe, fg_color='transparent')
            key_row.pack(fill='x', pady=(0, 4))

            key_var = tk.StringVar(value=p.get('api_key', ''))
            entry   = ctk.CTkEntry(key_row, textvariable=key_var, show='•',
                                   fg_color=SURF2, border_color=BORDER2, border_width=1,
                                   text_color=TEXT_P, font=(FONT_FAMILY, 10),
                                   corner_radius=RADIUS_SM)
            entry.pack(side='left', fill='x', expand=True)
            ctk.CTkButton(
                key_row, text='Show', width=54, height=32,
                fg_color=SURF3, hover_color=SURF2, text_color=TEXT_P,
                font=(FONT_FAMILY, 9), corner_radius=RADIUS_SM,
                command=lambda e=entry: e.configure(show='' if e.cget('show') == '•' else '•'),
            ).pack(side='left', padx=(4, 0))
            test_btn = ctk.CTkButton(
                key_row, text='Test', width=54, height=32,
                fg_color=SURF3, hover_color=SURF2, text_color=TEXT_P,
                font=(FONT_FAMILY, 9), corner_radius=RADIUS_SM,
            )
            test_btn.pack(side='left', padx=(4, 0))
            test_btn.configure(
                command=lambda k=key, v=key_var, b=test_btn: self._test_api_key(k, v, b)
            )
            entry.bind('<Return>', lambda e, k=key, v=key_var, b=test_btn: self._test_api_key(k, v, b))
            self._api_widgets[key]['api_key'] = key_var

            ctk.CTkLabel(cframe, text='Model', font=FONT_SM_BOLD,
                         text_color=TEXT_S).pack(anchor='w', pady=(0, 2))
            model_var = tk.StringVar(value=p.get('model', models[0]))
            # groq/cerebras: readonly (curated list); others: editable (user may type newer IDs)
            cb_state  = 'readonly' if key in ('groq', 'cerebras') else 'normal'
            ctk.CTkComboBox(cframe, values=models, variable=model_var, width=320,
                            fg_color=SURF2, border_color=BORDER2, border_width=1,
                            text_color=TEXT_P, button_color=SURF3,
                            dropdown_fg_color=SURFACE, font=(FONT_FAMILY, 10),
                            state=cb_state, corner_radius=RADIUS_SM).pack(anchor='w', pady=(0, PAD))
            self._api_widgets[key]['model'] = model_var

        # ── Custom provider: base URL + optional key + model name ─────────────────
        cpfg   = pcfg.get('custom', {})
        cframe = ctk.CTkFrame(self._cred_host, fg_color='transparent')
        self._api_widgets['custom'] = {'frame': cframe}

        ctk.CTkLabel(cframe, text='Base URL',
                     font=FONT_SM_BOLD, text_color=TEXT_S).pack(anchor='w', pady=(0, 2))
        url_var = tk.StringVar(value=cpfg.get('base_url', ''))
        ctk.CTkEntry(cframe, textvariable=url_var,
                     placeholder_text='http://localhost:11434/v1',
                     fg_color=SURF2, border_color=BORDER2, border_width=1,
                     text_color=TEXT_P, font=(FONT_FAMILY, 10),
                     corner_radius=RADIUS_SM).pack(fill='x', pady=(0, PAD_SM))
        self._api_widgets['custom']['base_url'] = url_var

        ctk.CTkLabel(cframe, text='API Key  (optional, leave blank for local servers)',
                     font=FONT_SM_BOLD, text_color=TEXT_S).pack(anchor='w', pady=(0, 2))
        ckey_row = ctk.CTkFrame(cframe, fg_color='transparent')
        ckey_row.pack(fill='x', pady=(0, PAD_SM))
        ckey_var = tk.StringVar(value=cpfg.get('api_key', ''))
        centry   = ctk.CTkEntry(ckey_row, textvariable=ckey_var, show='•',
                                fg_color=SURF2, border_color=BORDER2, border_width=1,
                                text_color=TEXT_P, font=(FONT_FAMILY, 10),
                                corner_radius=RADIUS_SM)
        centry.pack(side='left', fill='x', expand=True)
        ctk.CTkButton(
            ckey_row, text='Show', width=54, height=32,
            fg_color=SURF3, hover_color=SURF2, text_color=TEXT_P,
            font=(FONT_FAMILY, 9), corner_radius=RADIUS_SM,
            command=lambda e=centry: e.configure(show='' if e.cget('show') == '•' else '•'),
        ).pack(side='left', padx=(4, 0))
        ctest_btn = ctk.CTkButton(
            ckey_row, text='Test', width=54, height=32,
            fg_color=SURF3, hover_color=SURF2, text_color=TEXT_P,
            font=(FONT_FAMILY, 9), corner_radius=RADIUS_SM,
        )
        ctest_btn.pack(side='left', padx=(4, 0))
        ctest_btn.configure(
            command=lambda v=ckey_var, u=url_var, b=ctest_btn: self._test_api_key(
                'custom', v, b, extra={'base_url': u.get()})
        )
        centry.bind('<Return>', lambda e, v=ckey_var, u=url_var, b=ctest_btn: self._test_api_key(
            'custom', v, b, extra={'base_url': u.get()}))
        self._api_widgets['custom']['api_key'] = ckey_var

        ctk.CTkLabel(cframe, text='Model Name',
                     font=FONT_SM_BOLD, text_color=TEXT_S).pack(anchor='w', pady=(0, 2))
        cmodel_var = tk.StringVar(value=cpfg.get('model', ''))
        ctk.CTkEntry(cframe, textvariable=cmodel_var,
                     placeholder_text='e.g. llama3.2, mistral, qwen2.5, gpt-4o…',
                     fg_color=SURF2, border_color=BORDER2, border_width=1,
                     text_color=TEXT_P, font=(FONT_FAMILY, 10),
                     corner_radius=RADIUS_SM).pack(fill='x', pady=(0, PAD))
        self._api_widgets['custom']['model'] = cmodel_var

        self._refresh_cred_panel()
        return frame

    # ── Whisper panel ─────────────────────────────────────────────────────────

    def _build_whisper(self, parent) -> ctk.CTkFrame:
        frame  = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)
        wcfg   = self.config.get('whisper', {})
        scroll = ctk.CTkScrollableFrame(frame, fg_color=BG,
                                        scrollbar_button_color=SURF2,
                                        scrollbar_button_hover_color=SURF3)
        scroll.pack(fill='both', expand=True)

        def section(title, parent_w=scroll):
            ctk.CTkLabel(parent_w, text=title, font=(FONT_FAMILY, 9, 'bold'),
                         text_color=TEXT_S).pack(anchor='w', padx=PAD, pady=(PAD, 4))

        def divider(parent_w=scroll):
            ctk.CTkFrame(parent_w, fg_color=BORDER, height=1,
                         corner_radius=0).pack(fill='x', padx=PAD, pady=PAD_SM)

        def row_switch(parent_w, label, var):
            r = ctk.CTkFrame(parent_w, fg_color='transparent')
            r.pack(fill='x', padx=PAD, pady=3)
            ctk.CTkLabel(r, text=label, font=(FONT_FAMILY, 10),
                         text_color=TEXT_P).pack(side='left')
            ctk.CTkSwitch(r, text='', variable=var, onvalue=True, offvalue=False,
                          progress_color=ACCENT, button_color=TEXT_P,
                          fg_color=SURF3).pack(side='right')
            return r

        def row_combo(parent_w, label, var, values, width=200):
            r = ctk.CTkFrame(parent_w, fg_color='transparent')
            r.pack(fill='x', padx=PAD, pady=3)
            ctk.CTkLabel(r, text=label, font=(FONT_FAMILY, 10),
                         text_color=TEXT_P, width=200).pack(side='left')
            ctk.CTkComboBox(r, values=values, variable=var, width=width,
                            fg_color=SURF2, border_color=BORDER2, border_width=1,
                            text_color=TEXT_P, button_color=SURF3,
                            dropdown_fg_color=SURFACE, font=(FONT_FAMILY, 10),
                            state='readonly', corner_radius=RADIUS_SM).pack(side='left')
            return r

        # ── Speech recognition model ──────────────────────────────────────────
        section('SPEECH RECOGNITION')
        model_cfg = wcfg.get('model', {})

        # Default changed to 'base' for snappy local fallback. Users
        # who want higher accuracy can pick 'small' (~3× slower).
        self._w_cpu_model = tk.StringVar(value=model_cfg.get('cpu_model', 'base'))
        row_combo(scroll,
                  'Local model (base = ~2s fast / small = ~6s accurate)',
                  self._w_cpu_model, WHISPER_CPU_MODELS)

        self._w_gpu_model = tk.StringVar(value=model_cfg.get('gpu_model', 'large-v3-turbo'))
        row_combo(scroll, 'NVIDIA GPU model (if available)',
                  self._w_gpu_model, WHISPER_GPU_MODELS)

        device_vals = ['auto', 'cpu', 'cuda']
        self._w_device = tk.StringVar(value=model_cfg.get('device', 'auto'))
        row_combo(scroll,
                  'Run on (auto picks GPU when available, else CPU)',
                  self._w_device, device_vals, width=120)

        compute_vals = ['auto', 'int8', 'float16', 'float32']
        self._w_compute = tk.StringVar(value=model_cfg.get('compute_type', 'auto'))
        row_combo(scroll,
                  'Precision (auto balances speed and quality)',
                  self._w_compute, compute_vals, width=120)

        divider()

        # ── Microphone & recording ────────────────────────────────────────────
        section('MICROPHONE')
        audio_cfg = wcfg.get('audio', {})

        self._w_noise_reduction = tk.BooleanVar(value=audio_cfg.get('noise_reduction', True))
        row_switch(scroll,
                   'Clean up background noise (auto, only when needed)',
                   self._w_noise_reduction)

        # Cloud transcription toggle, ~13× faster when online (Groq's hosted
        # Whisper large-v3-turbo). Falls back to local automatically if
        # offline, rate-limited, or slow.
        self._w_cloud_enabled = tk.BooleanVar(value=audio_cfg.get('cloud_enabled', True))
        row_switch(scroll,
                   'Use cloud for speed (~500ms with internet, falls back to local)',
                   self._w_cloud_enabled)

        # Input device selector, show ONLY real microphones by default.
        # Most users see this dropdown and pick wrong (e.g. Stereo Mix, MIDI,
        # virtual DroidCam). We use the same heuristic the runtime uses to
        # auto-detect a physical mic, hiding everything else behind a
        # "Show all" expander (added below).
        import sounddevice as sd
        from core.audio import is_virtual_mic
        try:
            devices = sd.query_devices()
            in_devices_all = [
                (i, d['name']) for i, d in enumerate(devices)
                if d.get('max_input_channels', 0) > 0
            ]
        except Exception:
            in_devices_all = []
        # Default view: physical mics only. If the saved device is virtual,
        # include it too so the user can see what they currently have set.
        saved_dev_for_filter = audio_cfg.get('input_device_index')
        in_devices = [
            f'{i}: {n}' for i, n in in_devices_all
            if (not is_virtual_mic(n)) or i == saved_dev_for_filter
        ]

        r = ctk.CTkFrame(scroll, fg_color='transparent')
        r.pack(fill='x', padx=PAD, pady=3)
        ctk.CTkLabel(r, text='Microphone', font=(FONT_FAMILY, 10),
                     text_color=TEXT_P, width=200).pack(side='left')
        saved_dev = audio_cfg.get('input_device_index')
        # "Auto detect" is what the user picks to mean "let the app use the
        # Windows default mic, and self-heal if it disappears later".
        # Internally it maps to input_device_index=None.
        AUTO_LABEL = 'Auto detect (recommended)'
        default_dev = f'{saved_dev}: ...' if saved_dev is not None and in_devices else AUTO_LABEL
        # Find matching entry
        if saved_dev is not None:
            for d in in_devices:
                if d.startswith(f'{saved_dev}:'):
                    default_dev = d
                    break
        self._w_input_device = tk.StringVar(value=default_dev if in_devices else AUTO_LABEL)
        dev_values = [AUTO_LABEL] + in_devices
        ctk.CTkComboBox(r, values=dev_values, variable=self._w_input_device, width=260,
                        fg_color=SURF2, border_color=BORDER2, border_width=1,
                        text_color=TEXT_P, button_color=SURF3,
                        dropdown_fg_color=SURFACE, font=(FONT_FAMILY, 10),
                        state='readonly', corner_radius=RADIUS_SM).pack(side='left')

        divider()

        # ── Language ──────────────────────────────────────────────────────────
        section('LANGUAGE')
        trans_cfg = wcfg.get('transcription', {})

        saved_lang = trans_cfg.get('language') or 'auto'
        self._w_language = tk.StringVar(value=saved_lang if saved_lang in WHISPER_LANGS else 'auto')
        row_combo(scroll,
                  'Spoken language (auto detects from speech)',
                  self._w_language, WHISPER_LANGS, width=120)

        divider()

        # ── What happens after recognition ────────────────────────────────────
        section('AFTER DICTATION')
        out_cfg = wcfg.get('output', {})

        self._w_type_text = tk.BooleanVar(value=out_cfg.get('type_text', True))
        row_switch(scroll, 'Type the result into the focused app', self._w_type_text)

        self._w_trailing_space = tk.BooleanVar(value=out_cfg.get('add_trailing_space', True))
        row_switch(scroll, 'Add a space at the end', self._w_trailing_space)

        return frame

    # ── Nav ───────────────────────────────────────────────────────────────────

    def _show_panel(self, key: str) -> None:
        for k, panel in self._panels.items():
            panel.pack_forget()
        self._panels[key].pack(fill='both', expand=True)
        for k, btn in self._nav_btns.items():
            btn.configure(fg_color=SURF2 if k == key else 'transparent')

    def _pick_provider(self, key: str) -> None:
        self._provider_var.set(key)
        self._refresh_provider_cards()
        self._refresh_cred_panel()

    def _refresh_provider_cards(self) -> None:
        active = self._provider_var.get()
        for k, card in self._prov_cards.items():
            card.configure(border_color=ACCENT if k == active else BG)

    def _refresh_cred_panel(self) -> None:
        selected = self._provider_var.get()
        for key, w in self._api_widgets.items():
            if key == selected:
                w['frame'].pack(fill='x')
            else:
                w['frame'].pack_forget()

    # ── API key test ──────────────────────────────────────────────────────────

    def _test_api_key(self, provider: str, key_var: tk.StringVar,
                      btn: ctk.CTkButton, extra: dict | None = None) -> None:
        api_key  = key_var.get().strip()
        extra    = extra or {}
        base_url = extra.get('base_url', '')

        if provider != 'custom' and not api_key:
            alert(self.win, 'No API Key', 'Enter an API key before testing.')
            return
        if provider == 'custom' and not base_url:
            alert(self.win, 'No Base URL', 'Enter a base URL before testing.')
            return

        btn.configure(state='disabled', text='Testing…')

        # Snapshot the custom model name on the main thread before spawning
        custom_model = ''
        if provider == 'custom':
            try:
                custom_model = self._api_widgets['custom']['model'].get().strip()
            except Exception:
                pass

        def _run() -> None:
            success = False
            try:
                if provider == 'groq':
                    import groq as _groq
                    _groq.Groq(api_key=api_key).chat.completions.create(
                        model='llama-3.3-70b-versatile',
                        messages=[{'role': 'user', 'content': 'hi'}],
                        max_tokens=1,
                    )
                    success = True
                elif provider == 'cerebras':
                    import cerebras.cloud.sdk as _cerebras
                    # Pull from engine.CEREBRAS_MODELS so the Test
                    # button doesn't break when Cerebras rotates models.
                    from engine import CEREBRAS_MODELS
                    _cerebras.Cerebras(api_key=api_key).chat.completions.create(
                        model=CEREBRAS_MODELS[0],
                        messages=[{'role': 'user', 'content': 'hi'}],
                        max_tokens=1,
                    )
                    success = True
                elif provider == 'openai':
                    from openai import OpenAI
                    OpenAI(api_key=api_key).chat.completions.create(
                        model='gpt-4o-mini',
                        messages=[{'role': 'user', 'content': 'hi'}],
                        max_tokens=1,
                    )
                    success = True
                elif provider == 'anthropic':
                    import anthropic
                    anthropic.Anthropic(api_key=api_key).messages.create(
                        model='claude-3-5-haiku-20241022',
                        max_tokens=1,
                        messages=[{'role': 'user', 'content': 'hi'}],
                    )
                    success = True
                elif provider == 'gemini':
                    from openai import OpenAI
                    OpenAI(
                        api_key=api_key,
                        base_url='https://generativelanguage.googleapis.com/v1beta/openai/',
                    ).chat.completions.create(
                        model='gemini-2.0-flash',
                        messages=[{'role': 'user', 'content': 'hi'}],
                        max_tokens=1,
                    )
                    success = True
                elif provider == 'custom':
                    from openai import OpenAI
                    OpenAI(
                        api_key=api_key or 'none',
                        base_url=base_url,
                    ).chat.completions.create(
                        model=custom_model or 'test',
                        messages=[{'role': 'user', 'content': 'hi'}],
                        max_tokens=1,
                    )
                    success = True
            except Exception:
                success = False

            def _update() -> None:
                if success:
                    btn.configure(text='✓ OK', fg_color=OK, hover_color=OK,
                                  text_color=TEXT_P, state='normal')
                else:
                    btn.configure(text='✗ Failed', fg_color=ERR, hover_color=ERR,
                                  text_color=TEXT_P, state='normal')
                self.win.after(3000, lambda: btn.configure(
                    text='Test', fg_color=SURF3, hover_color=SURF2, text_color=TEXT_P))

            self.win.after(0, _update)

        threading.Thread(target=_run, daemon=True).start()

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save(self) -> None:
        cfg = dict(self.config)

        # Reject fields still showing the capture-in-progress placeholder
        _PLACEHOLDER = '… press keys …'
        for action, var in self._hotkey_vars.items():
            if var.get().strip() == _PLACEHOLDER:
                alert(self.win, 'Hotkey not finished',
                      'One of the hotkey fields is still listening for a '
                      'key.\nPress the keys you want, or click the field '
                      'again to cancel.')
                return

        # Full conflict check, duplicates, OS-reserved, syntax errors,
        # collisions with per-prompt / per-chain / per-macro hotkeys.
        # Warnings (whiteboard clashes, risky pickings like Ctrl+W) are
        # surfaced as a confirmation prompt; the user can proceed if they
        # know what they're doing.
        try:
            from hotkey_validator import (
                validate_batch, collect_app_hotkeys, ERROR, WARN)
            proposed = {k: v.get().strip()
                        for k, v in self._hotkey_vars.items()}
            others = collect_app_hotkeys(
                self.config,
                prompts=getattr(self, 'prompts', None),
                chains=getattr(self, 'chains', None),
                macros=getattr(self, 'macros', None),
            )
            # Treat the Settings-controlled keys as being replaced,
            # don't compare each new binding against its own current value.
            for k in proposed: others.pop(k, None)
            diags = validate_batch(proposed, other_assignments=others)
            blockers = [d for d in diags if d.severity == ERROR]
            warnings = [d for d in diags if d.severity == WARN]
            if blockers:
                msg = '\n\n'.join(
                    f'• {d.action}: {d.message}' for d in blockers)
                # Some blockers are real two-action conflicts; others are
                # "Windows owns this combo, no app can ever capture it".
                # Split the title so the user knows which it is.
                only_reserved = all(
                    ('reserved by' in d.message) or ('Pick something else' in d.message)
                    for d in blockers
                )
                title = 'Hotkey not allowed' if only_reserved else 'Hotkey conflict'
                alert(self.win, title,
                      'Cannot save, fix these first:\n\n' + msg)
                return
            if warnings:
                from dialogs import confirm
                msg = '\n\n'.join(
                    f'• {d.action}: {d.message}' for d in warnings)
                if not confirm(self.win, 'Conflicts with common shortcuts',
                               'Heads up, these combos are also used by '
                               'other apps:\n\n' + msg + '\n\nThe app will '
                               'still receive them, but other windows may '
                               'also react. Save anyway?'):
                    return
        except ImportError:
            # validator module missing, fall back to the old duplicate-only check
            hk_values = [v.get().strip().lower()
                         for v in self._hotkey_vars.values() if v.get().strip()]
            if len(hk_values) != len(set(hk_values)):
                alert(self.win, 'Duplicate Hotkey',
                      'Two or more actions share the same hotkey.\n'
                      'Please assign unique hotkeys.')
                return

        # General
        cfg['active_provider'] = self._provider_var.get()
        cfg['autostart']       = self._autostart_var.get()
        cfg['hotkeys']         = {k: v.get().strip() for k, v in self._hotkey_vars.items()}

        # Providers, standard (API key + model)
        cfg.setdefault('providers', {})
        for key in ['groq', 'cerebras', 'openai', 'anthropic', 'gemini']:
            if key not in self._api_widgets:
                continue
            w = self._api_widgets[key]
            cfg['providers'].setdefault(key, {})
            cfg['providers'][key]['api_key'] = w['api_key'].get().strip()
            cfg['providers'][key]['model']   = w['model'].get().strip()

        # Custom provider
        if 'custom' in self._api_widgets:
            w = self._api_widgets['custom']
            cfg['providers'].setdefault('custom', {})
            cfg['providers']['custom']['api_key']  = w['api_key'].get().strip()
            cfg['providers']['custom']['base_url'] = w['base_url'].get().strip()
            cfg['providers']['custom']['model']    = w['model'].get().strip()

        # Whisper
        cfg.setdefault('whisper', {})
        cfg['whisper'].setdefault('model', {})
        cfg['whisper']['model']['cpu_model']    = self._w_cpu_model.get()
        cfg['whisper']['model']['gpu_model']    = self._w_gpu_model.get()
        cfg['whisper']['model']['device']       = self._w_device.get()
        cfg['whisper']['model']['compute_type'] = self._w_compute.get()

        cfg['whisper'].setdefault('audio', {})
        cfg['whisper']['audio']['noise_reduction'] = self._w_noise_reduction.get()
        cfg['whisper']['audio']['cloud_enabled']   = self._w_cloud_enabled.get()
        # Parse device index from "0: Microphone" format. Any non-numeric
        # label (Auto detect (recommended), legacy 'Default', empty, etc.)
        # maps to None, which means "use Windows default" and triggers the
        # self-healing fallback in core/audio.py if that default disappears.
        dev_str = self._w_input_device.get()
        try:
            cfg['whisper']['audio']['input_device_index'] = int(dev_str.split(':')[0])
        except Exception:
            cfg['whisper']['audio']['input_device_index'] = None

        cfg['whisper'].setdefault('transcription', {})
        lang = self._w_language.get()
        cfg['whisper']['transcription']['language'] = None if lang == 'auto' else lang

        cfg['whisper'].setdefault('output', {})
        cfg['whisper']['output']['type_text']          = self._w_type_text.get()
        cfg['whisper']['output']['add_trailing_space'] = self._w_trailing_space.get()
        cfg['whisper']['output']['copy_to_clipboard']  = True  # always keep in clipboard

        try:
            set_autostart(cfg['autostart'])
        except Exception:
            pass

        self.on_save(cfg)
        self.hide()

    # ── Show / hide ───────────────────────────────────────────────────────────

    def show(self) -> None:
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def hide(self) -> None:
        self.win.withdraw()

    def _center(self) -> None:
        self.win.update_idletasks()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        w = max(self.win.winfo_reqwidth()  or 620, 620)
        h = max(self.win.winfo_reqheight() or 520, 520)
        self.win.geometry(f'{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}')
