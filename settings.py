"""Settings window — General / Providers / Whisper tabs, sidebar layout."""
import threading
import tkinter as tk
from typing import Callable

import customtkinter as ctk

from dialogs import alert
from engine  import PROVIDER_KEYS, GROQ_MODELS, CEREBRAS_MODELS, local_provider_available
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
    def __init__(self, root, config: dict, on_save: Callable) -> None:
        self.root    = root
        self.config  = config
        self.on_save = on_save
        self._api_widgets: dict[str, dict]          = {}
        self._nav_btns:    dict[str, ctk.CTkButton] = {}
        self._panels:      dict[str, ctk.CTkFrame]  = {}
        self._build()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.win = ctk.CTkToplevel(self.root)
        self.win.title('Settings — Hotkeys')
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
            ('providers', '🔌  Providers'),
            ('whisper',   '🎙  Whisper'),
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

        self._show_panel('general')
        self._center()

    # ── General panel ─────────────────────────────────────────────────────────

    def _build_general(self, parent) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)

        def section(title):
            ctk.CTkLabel(frame, text=title, font=(FONT_FAMILY, 9, 'bold'),
                         text_color=TEXT_S).pack(anchor='w', padx=PAD, pady=(PAD, 4))

        def divider():
            ctk.CTkFrame(frame, fg_color=BORDER, height=1,
                         corner_radius=0).pack(fill='x', padx=PAD, pady=PAD_SM)

        section('HOTKEYS')
        HK_DEFAULTS = {
            'refine':  'alt+shift+w',
            'library': 'alt+shift+e',
            'whisper': 'ctrl+shift+space',
        }
        hk_cfg = self.config.get('hotkeys', {})
        self._hotkey_vars: dict[str, tk.StringVar] = {}

        hk_actions = [
            ('refine',  'Refine selected text'),
            ('library', 'Open Prompt Library'),
            ('whisper', 'Toggle speech-to-text'),
        ]
        for action, label in hk_actions:
            row = ctk.CTkFrame(frame, fg_color='transparent')
            row.pack(fill='x', padx=PAD, pady=3)
            ctk.CTkLabel(row, text=label, font=(FONT_FAMILY, 10),
                         text_color=TEXT_P, anchor='w', width=200).pack(side='left')
            var = tk.StringVar(value=hk_cfg.get(action, HK_DEFAULTS.get(action, '')))
            self._hotkey_vars[action] = var
            ctk.CTkLabel(row, textvariable=var, font=(FONT_FAMILY, 9, 'bold'),
                         text_color=ACCENT, fg_color=SURF2, corner_radius=RADIUS_SM,
                         padx=10, pady=3, width=130).pack(side='left', padx=(0, 6))
            ctk.CTkButton(
                row, text='Record', width=64, height=26, font=(FONT_FAMILY, 9),
                fg_color=SURF3, hover_color=SURF2, text_color=TEXT_S, corner_radius=RADIUS_SM,
                command=lambda a=action, v=var: self._record_hotkey(a, v),
            ).pack(side='left')

        divider()

        section('STARTUP')
        row = ctk.CTkFrame(frame, fg_color='transparent')
        row.pack(fill='x', padx=PAD, pady=3)
        ctk.CTkLabel(row, text='Launch on Windows startup',
                     font=(FONT_FAMILY, 10), text_color=TEXT_P).pack(side='left')
        self._autostart_var = tk.BooleanVar(value=self.config.get('autostart', True))
        ctk.CTkSwitch(row, text='', variable=self._autostart_var, onvalue=True, offvalue=False,
                      progress_color=ACCENT, button_color=TEXT_P, fg_color=SURF3).pack(side='right')

        divider()

        section('DATA')
        path_frame = ctk.CTkFrame(frame, fg_color=SURF2, corner_radius=RADIUS_SM)
        path_frame.pack(fill='x', padx=PAD, pady=(0, PAD_SM))
        ctk.CTkLabel(path_frame, text=appdata_dir(), font=(FONT_FAMILY, 9),
                     text_color=TEXT_S, anchor='w').pack(padx=PAD_SM, pady=6, anchor='w')
        ctk.CTkLabel(frame, text='Config, prompts, history, and logs are stored here.',
                     font=(FONT_FAMILY, 8), text_color=TEXT_S).pack(anchor='w', padx=PAD)

        return frame

    # ── Hotkey capture ────────────────────────────────────────────────────────

    def _record_hotkey(self, action: str, var: tk.StringVar) -> None:
        import threading, keyboard
        prev = var.get()
        var.set('… press keys …')

        def capture():
            try:
                combo = keyboard.read_hotkey(suppress=False)
                if combo.lower() in ('escape', 'esc'):
                    var.set(prev)
                    return
                parts = {p.lower() for p in combo.split('+')}
                if not parts & {'ctrl', 'alt', 'shift', 'windows', 'win'}:
                    var.set(prev)
                    return
                var.set(combo)
            except Exception:
                var.set(prev)

        threading.Thread(target=capture, daemon=True).start()

    # ── Providers panel ───────────────────────────────────────────────────────

    def _build_providers(self, parent) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)
        pcfg  = self.config.get('providers', {})

        ctk.CTkLabel(frame, text='ACTIVE PROVIDER', font=(FONT_FAMILY, 9, 'bold'),
                     text_color=TEXT_S).pack(anchor='w', padx=PAD, pady=(PAD, 4))

        self._provider_var = tk.StringVar(value=self.config.get('active_provider', 'cerebras'))

        prov_grid = ctk.CTkFrame(frame, fg_color='transparent')
        prov_grid.pack(fill='x', padx=PAD, pady=(0, PAD_SM))
        self._prov_cards: dict[str, ctk.CTkFrame] = {}

        short_desc = {'local': 'Free · Offline', 'groq': 'Free tier', 'cerebras': 'Free tier · Fast'}
        visible_keys = [k for k in PROVIDER_KEYS if k != 'local' or local_provider_available()]
        for i, key in enumerate(visible_keys):
            card = ctk.CTkFrame(prov_grid, fg_color=SURF2, corner_radius=RADIUS,
                                border_width=2, border_color=BG, cursor='hand2')
            card.grid(row=0, column=i, padx=4, sticky='nsew')
            prov_grid.columnconfigure(i, weight=1)
            ctk.CTkLabel(card, text=key.upper(), font=(FONT_FAMILY, 8, 'bold'),
                         text_color=ACCENT).pack(padx=PAD_SM, pady=(8, 0))
            ctk.CTkLabel(card, text=short_desc[key], font=(FONT_FAMILY, 8),
                         text_color=TEXT_S).pack(padx=PAD_SM, pady=(0, 8))
            card.bind('<Button-1>', lambda e, k=key: self._pick_provider(k))
            for child in card.winfo_children():
                child.bind('<Button-1>', lambda e, k=key: self._pick_provider(k))
            self._prov_cards[key] = card

        self._refresh_provider_cards()

        ctk.CTkFrame(frame, fg_color=BORDER, height=1,
                     corner_radius=0).pack(fill='x', padx=PAD, pady=PAD_SM)

        ctk.CTkLabel(frame, text='CREDENTIALS', font=(FONT_FAMILY, 9, 'bold'),
                     text_color=TEXT_S).pack(anchor='w', padx=PAD, pady=(0, 4))

        self._cred_host = ctk.CTkFrame(frame, fg_color='transparent')
        self._cred_host.pack(fill='x', padx=PAD)

        models_map = {'groq': GROQ_MODELS, 'cerebras': CEREBRAS_MODELS}
        for key in ['groq', 'cerebras']:
            p      = pcfg.get(key, {})
            models = models_map[key]
            cframe = ctk.CTkFrame(self._cred_host, fg_color='transparent')
            self._api_widgets[key] = {'frame': cframe}

            ctk.CTkLabel(cframe, text='API Key', font=FONT_SM_BOLD,
                         text_color=TEXT_S).pack(anchor='w', pady=(0, 2))
            key_row = ctk.CTkFrame(cframe, fg_color='transparent')
            key_row.pack(fill='x', pady=(0, 4))

            key_var = tk.StringVar(value=p.get('api_key', ''))
            entry = ctk.CTkEntry(key_row, textvariable=key_var, width=280, show='•',
                                 fg_color=SURF2, border_color=BORDER2, border_width=1,
                                 text_color=TEXT_P, font=(FONT_FAMILY, 10),
                                 corner_radius=RADIUS_SM)
            entry.pack(side='left', fill='x', expand=True)
            ctk.CTkButton(
                key_row, text='Show', width=54, height=32,
                fg_color=SURF3, hover_color=SURF2, text_color=TEXT_S,
                font=(FONT_FAMILY, 9), corner_radius=RADIUS_SM,
                command=lambda e=entry: e.configure(show='' if e.cget('show') == '•' else '•'),
            ).pack(side='left', padx=(4, 0))
            test_btn = ctk.CTkButton(
                key_row, text='Test', width=54, height=32,
                fg_color=SURF3, hover_color=SURF2, text_color=TEXT_S,
                font=(FONT_FAMILY, 9), corner_radius=RADIUS_SM,
            )
            test_btn.pack(side='left', padx=(4, 0))
            test_btn.configure(
                command=lambda k=key, v=key_var, b=test_btn: self._test_api_key(k, v, b)
            )
            self._api_widgets[key]['api_key'] = key_var

            ctk.CTkLabel(cframe, text='Model', font=FONT_SM_BOLD,
                         text_color=TEXT_S).pack(anchor='w', pady=(0, 2))
            model_var = tk.StringVar(value=p.get('model', models[0]))
            ctk.CTkComboBox(cframe, values=models, variable=model_var, width=260,
                            fg_color=SURF2, border_color=BORDER2, border_width=1,
                            text_color=TEXT_P, button_color=SURF3,
                            dropdown_fg_color=SURFACE, font=(FONT_FAMILY, 10),
                            state='readonly', corner_radius=RADIUS_SM).pack(anchor='w', pady=(0, PAD))
            self._api_widgets[key]['model'] = model_var

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

        # ── Model ──────────────────────────────────────────────────────────────
        section('MODEL')
        model_cfg = wcfg.get('model', {})

        self._w_cpu_model = tk.StringVar(value=model_cfg.get('cpu_model', 'small'))
        row_combo(scroll, 'CPU model (no GPU)', self._w_cpu_model, WHISPER_CPU_MODELS)

        self._w_gpu_model = tk.StringVar(value=model_cfg.get('gpu_model', 'large-v3-turbo'))
        row_combo(scroll, 'GPU model (CUDA)', self._w_gpu_model, WHISPER_GPU_MODELS)

        device_vals = ['auto', 'cpu', 'cuda']
        self._w_device = tk.StringVar(value=model_cfg.get('device', 'auto'))
        row_combo(scroll, 'Device', self._w_device, device_vals, width=120)

        compute_vals = ['auto', 'int8', 'float16', 'float32']
        self._w_compute = tk.StringVar(value=model_cfg.get('compute_type', 'auto'))
        row_combo(scroll, 'Compute type', self._w_compute, compute_vals, width=120)

        divider()

        # ── Audio ──────────────────────────────────────────────────────────────
        section('AUDIO')
        audio_cfg = wcfg.get('audio', {})

        self._w_noise_reduction = tk.BooleanVar(value=audio_cfg.get('noise_reduction', True))
        row_switch(scroll, 'Noise reduction', self._w_noise_reduction)

        # Input device selector
        import sounddevice as sd
        try:
            devices    = sd.query_devices()
            in_devices = [f'{i}: {d["name"]}' for i, d in enumerate(devices)
                          if d['max_input_channels'] > 0]
        except Exception:
            in_devices = []

        r = ctk.CTkFrame(scroll, fg_color='transparent')
        r.pack(fill='x', padx=PAD, pady=3)
        ctk.CTkLabel(r, text='Input device', font=(FONT_FAMILY, 10),
                     text_color=TEXT_P, width=200).pack(side='left')
        saved_dev = audio_cfg.get('input_device_index')
        default_dev = f'{saved_dev}: ...' if saved_dev is not None and in_devices else 'Default'
        # Find matching entry
        if saved_dev is not None:
            for d in in_devices:
                if d.startswith(f'{saved_dev}:'):
                    default_dev = d
                    break
        self._w_input_device = tk.StringVar(value=default_dev if in_devices else 'Default')
        dev_values = ['Default'] + in_devices
        ctk.CTkComboBox(r, values=dev_values, variable=self._w_input_device, width=260,
                        fg_color=SURF2, border_color=BORDER2, border_width=1,
                        text_color=TEXT_P, button_color=SURF3,
                        dropdown_fg_color=SURFACE, font=(FONT_FAMILY, 10),
                        state='readonly', corner_radius=RADIUS_SM).pack(side='left')

        divider()

        # ── Transcription ─────────────────────────────────────────────────────
        section('TRANSCRIPTION')
        trans_cfg = wcfg.get('transcription', {})

        saved_lang = trans_cfg.get('language') or 'auto'
        self._w_language = tk.StringVar(value=saved_lang if saved_lang in WHISPER_LANGS else 'auto')
        row_combo(scroll, 'Language', self._w_language, WHISPER_LANGS, width=120)

        divider()

        # ── Output ────────────────────────────────────────────────────────────
        section('OUTPUT')
        out_cfg = wcfg.get('output', {})

        self._w_type_text = tk.BooleanVar(value=out_cfg.get('type_text', True))
        row_switch(scroll, 'Auto-type transcription (Ctrl+V)', self._w_type_text)

        self._w_trailing_space = tk.BooleanVar(value=out_cfg.get('add_trailing_space', True))
        row_switch(scroll, 'Add trailing space', self._w_trailing_space)

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
                      btn: ctk.CTkButton) -> None:
        api_key = key_var.get().strip()
        if not api_key:
            alert(self.win, 'No API Key', 'Enter an API key before testing.')
            return

        btn.configure(state='disabled', text='Testing…')

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
                    _cerebras.Cerebras(api_key=api_key).chat.completions.create(
                        model='llama3.1-8b',
                        messages=[{'role': 'user', 'content': 'hi'}],
                        max_tokens=1,
                    )
                    success = True
            except Exception:
                success = False

            def _update() -> None:
                if success:
                    btn.configure(text='✓ OK', fg_color=OK, hover_color=OK,
                                  text_color='#ffffff', state='normal')
                else:
                    btn.configure(text='✗ Failed', fg_color=ERR, hover_color=ERR,
                                  text_color='#ffffff', state='normal')

                def _reset() -> None:
                    btn.configure(text='Test', fg_color=SURF3, hover_color=SURF2,
                                  text_color=TEXT_S)
                self.win.after(3000, _reset)

            self.win.after(0, _update)

        threading.Thread(target=_run, daemon=True).start()

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save(self) -> None:
        cfg = dict(self.config)

        # Check for duplicate hotkeys
        hk_values = [v.get().strip().lower() for v in self._hotkey_vars.values() if v.get().strip()]
        if len(hk_values) != len(set(hk_values)):
            alert(self.win, 'Duplicate Hotkey',
                  'Two or more actions share the same hotkey.\nPlease assign unique hotkeys.')
            return

        # General
        cfg['active_provider'] = self._provider_var.get()
        cfg['autostart']       = self._autostart_var.get()
        cfg['hotkeys']         = {k: v.get().strip() for k, v in self._hotkey_vars.items()}

        # Providers
        cfg.setdefault('providers', {})
        for key in ['groq', 'cerebras']:
            w = self._api_widgets[key]
            cfg['providers'].setdefault(key, {})
            cfg['providers'][key]['api_key'] = w['api_key'].get().strip()
            cfg['providers'][key]['model']   = w['model'].get()

        # Whisper
        cfg.setdefault('whisper', {})
        cfg['whisper'].setdefault('model', {})
        cfg['whisper']['model']['cpu_model']    = self._w_cpu_model.get()
        cfg['whisper']['model']['gpu_model']    = self._w_gpu_model.get()
        cfg['whisper']['model']['device']       = self._w_device.get()
        cfg['whisper']['model']['compute_type'] = self._w_compute.get()

        cfg['whisper'].setdefault('audio', {})
        cfg['whisper']['audio']['noise_reduction'] = self._w_noise_reduction.get()
        # Parse device index from "0: Microphone" format
        dev_str = self._w_input_device.get()
        if dev_str == 'Default' or not dev_str:
            cfg['whisper']['audio']['input_device_index'] = None
        else:
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
