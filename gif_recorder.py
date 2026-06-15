"""
GIF Recorder, captures screen frames with Win32 BitBlt and saves as animated GIF.

Reuses ScreenCapture from screen_recorder.py.
Encoding uses Pillow (already a dependency).
"""

import logging
import os
import threading
import time
import tkinter as tk
import tkinter.filedialog as fd
from pathlib import Path

logger = logging.getLogger(__name__)

import customtkinter as ctk
from PIL import Image

from screen_recorder import ScreenCapture, list_windows, RegionSelectorOverlay
from storage import appdata_dir
from theme import (
    BG, SURFACE, SURF2, SURF3, BORDER, BORDER2,
    ACCENT, ACCENTL, TEXT_P, TEXT_S,
    OK, ERR,
    FONT_FAMILY, FONT_SM_BOLD,
    PAD, PAD_SM, RADIUS, RADIUS_SM,
)


# ── GifRecorder ───────────────────────────────────────────────────────────────

class GifRecorder:
    """
    Captures screen frames and encodes them as an animated GIF.

    Usage
    -----
    rec = GifRecorder(hwnd=0, fps=10, max_width=640, max_duration_s=30)
    rec.start(on_done=lambda path, n, dur: ..., on_error=lambda msg: ...)
    # ... time passes ...
    rec.stop()    # can also call force_stop() to discard

    After on_done fires, rec.output_path holds the temp GIF path.
    Pass it to show_gif_save_dialog() so the user can save or discard.
    """

    def __init__(
        self,
        hwnd: int = 0,
        mon=None,
        fps: int = 10,
        max_width: int = 640,
        max_duration_s: float = 30.0,
        # ~1.5 GB worth of raw RGB frames (PIL.Image in RAM). The 1280px
        # 5-minute combo is uncapped ≈ 12 GB and OOMs, plus the encode
        # path makes a second quantized copy. This cap stops capture
        # cleanly before either runs out of memory; the user sees the
        # already-recorded portion encode and save.
        max_total_bytes: int = 1_500 * 1024 * 1024,
    ) -> None:
        self.hwnd            = hwnd
        self.mon             = mon
        self.fps             = max(1, min(fps, 30))
        self.max_width       = max(240, max_width)
        self.max_duration_s  = max(5.0, max_duration_s)
        self.max_total_bytes = int(max_total_bytes)

        self._recording     = False
        self._encoding      = False
        self._stop_event    = threading.Event()
        self._lock          = threading.Lock()
        self._frames_rgb: list[Image.Image] = []
        self._t0: float     = 0.0
        self._output_path: str | None = None
        self._error: str | None       = None

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def is_encoding(self) -> bool:
        return self._encoding

    @property
    def frame_count(self) -> int:
        with self._lock:
            return len(self._frames_rgb)

    @property
    def elapsed(self) -> float:
        if self._t0 == 0.0:
            return 0.0
        return time.perf_counter() - self._t0

    @property
    def output_path(self) -> str | None:
        return self._output_path

    @property
    def error(self) -> str | None:
        return self._error

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, on_done=None, on_error=None, on_cap_reached=None) -> None:
        """Begin capturing frames in a background thread."""
        if self._recording or self._encoding:
            return
        self._recording  = True
        self._stop_event.clear()
        self._frames_rgb.clear()
        self._output_path = None
        self._error       = None
        self._t0          = time.perf_counter()
        t = threading.Thread(
            target=self._capture_worker,
            args=(on_done, on_error, on_cap_reached),
            daemon=True,
            name='gif-capture',
        )
        t.start()

    def stop(self) -> None:
        """Signal the capture loop to stop and begin encoding."""
        self._stop_event.set()

    def force_stop(self) -> None:
        """Abort, stop capture without encoding, discard frames."""
        self._stop_event.set()
        with self._lock:
            self._frames_rgb.clear()
        self._recording = False
        self._encoding  = False

    # ── Capture worker ────────────────────────────────────────────────────────

    def _capture_worker(self, on_done, on_error, on_cap_reached) -> None:
        cap = None
        try:
            cap = ScreenCapture(hwnd=self.hwnd, mon=self.mon)
            w_src, h_src = cap.size()

            # Compute output dimensions (maintain aspect ratio, even pixels)
            if w_src > self.max_width:
                scale  = self.max_width / w_src
                out_w  = self.max_width & ~1
                out_h  = int(h_src * scale) & ~1
            else:
                out_w = w_src & ~1
                out_h = h_src & ~1
            out_w  = max(2, out_w)
            out_h  = max(2, out_h)

            interval = 1.0 / self.fps
            t_start  = time.perf_counter()
            next_t   = t_start
            # Approx bytes per uncompressed RGB frame.
            bytes_per_frame = out_w * out_h * 3
            # How many frames we can hold before we'd exceed the cap.
            max_frames = max(1, self.max_total_bytes // bytes_per_frame)

            while not self._stop_event.is_set():
                # Max duration cap
                if time.perf_counter() - t_start >= self.max_duration_s:
                    if on_cap_reached:
                        try:
                            on_cap_reached()
                        except Exception:
                            pass
                    break

                # Max RAM cap (hard stop). Prevents the documented
                # ~12 GB OOM at 1280px × 15fps × 5min. We've already
                # recorded `max_frames` worth; stop and encode what we have.
                with self._lock:
                    n = len(self._frames_rgb)
                if n >= max_frames:
                    logger.warning(
                        f'GIF: RAM cap reached after {n} frames '
                        f'(~{n * bytes_per_frame // (1024*1024)} MB); '
                        f'stopping early to encode what we have.')
                    if on_cap_reached:
                        try: on_cap_reached()
                        except Exception: pass
                    break

                # Precise timing, wait until next frame slot
                wait = next_t - time.perf_counter()
                if wait > 0:
                    if self._stop_event.wait(timeout=wait):
                        break

                next_t += interval
                # Re-anchor if we fall more than one frame behind (overrun)
                if next_t < time.perf_counter() - interval:
                    next_t = time.perf_counter()

                bgr = cap.grab()
                rgb = bgr[:, :, ::-1]
                img = Image.fromarray(rgb).resize((out_w, out_h), Image.LANCZOS)
                with self._lock:
                    self._frames_rgb.append(img)

        except Exception as exc:
            self._error     = str(exc)
            self._recording = False
            self._encoding  = False
            if on_error:
                try:
                    on_error(str(exc))
                except Exception:
                    pass
            return
        finally:
            if cap is not None:
                try:
                    cap.close()
                except Exception:
                    pass

        # Capture done, encode
        self._recording = False
        self._encoding  = True
        self._encode(on_done, on_error)

    def _encode(self, on_done, on_error) -> None:
        """Quantize frames and write GIF to a temp file."""
        try:
            with self._lock:
                frames = list(self._frames_rgb)
                self._frames_rgb.clear()

            if not frames:
                self._error   = 'No frames captured'
                self._encoding = False
                if on_error:
                    on_error(self._error)
                return

            # Quantize all frames with FASTOCTREE
            quantized = [
                f.quantize(colors=256, method=Image.Quantize.FASTOCTREE, dither=1)
                for f in frames
            ]
            del frames   # free memory

            # Write to temp file
            tmp_dir = Path(appdata_dir()) / 'temp'
            tmp_dir.mkdir(parents=True, exist_ok=True)
            stamp   = int(time.time())
            tmp_path = str(tmp_dir / f'gif_tmp_{stamp}.gif')
            frame_ms = max(1, int(1000 / self.fps))

            quantized[0].save(
                tmp_path,
                save_all=True,
                append_images=quantized[1:],
                duration=frame_ms,
                loop=0,
                optimize=False,
            )
            del quantized

            self._output_path = tmp_path
            dur = time.perf_counter() - self._t0

            self._encoding = False
            if on_done:
                try:
                    on_done(tmp_path, dur)
                except Exception:
                    pass

        except Exception as exc:
            self._error    = str(exc)
            self._encoding = False
            if on_error:
                try:
                    on_error(str(exc))
                except Exception:
                    pass


# ── Setup Dialog ──────────────────────────────────────────────────────────────

class GifSetupDialog:
    """
    Pre-recording options dialog.

    Usage
    -----
    dlg = GifSetupDialog(parent)
    parent.wait_window(dlg.win)
    if dlg.result:
        hwnd          = dlg.result['hwnd']
        mon           = dlg.result['mon']    # (l,t,w,h) or None
        fps           = dlg.result['fps']
        max_width     = dlg.result['max_width']
        max_duration_s = dlg.result['max_duration_s']
    """

    def __init__(self, parent) -> None:
        self.result: dict | None = None
        self._parent  = parent
        self._windows = list_windows()
        self._region: tuple | None = None

        self._build()

    def _build(self) -> None:
        win = ctk.CTkToplevel(self._parent)
        win.title('Record GIF')
        win.configure(fg_color=BG)
        win.resizable(False, False)
        # Only set transient when parent is mapped, transient to a withdrawn
        # parent hides the dialog on Windows, blocking it indefinitely.
        if self._parent.winfo_ismapped():
            win.transient(self._parent)
        win.deiconify()   # force visible even if parent is withdrawn
        try:
            win.grab_set()
        except Exception:
            pass   # non-fatal if window isn't viewable yet
        self.win = win

        # Header
        hdr = ctk.CTkFrame(win, fg_color=SURFACE, corner_radius=0)
        hdr.pack(fill='x')
        ctk.CTkLabel(hdr, text='🎞  Record GIF',
                     font=(FONT_FAMILY, 16, 'bold'), text_color=TEXT_P
                     ).pack(anchor='w', padx=PAD, pady=PAD_SM)

        body = ctk.CTkFrame(win, fg_color=BG, corner_radius=0)
        body.pack(fill='both', expand=True, padx=PAD, pady=PAD)

        # ── Source ────────────────────────────────────────────────────────────
        ctk.CTkLabel(body, text='Source', font=FONT_SM_BOLD,
                     text_color=TEXT_S).grid(row=0, column=0, sticky='w', pady=(0, 4))
        self._src_var = tk.StringVar(value='screen')
        src_row = ctk.CTkFrame(body, fg_color='transparent')
        src_row.grid(row=1, column=0, sticky='w', pady=(0, PAD))
        for val, lbl in [('screen', 'Full Screen'), ('window', 'Window'), ('region', 'Region')]:
            ctk.CTkRadioButton(
                src_row, text=lbl, variable=self._src_var, value=val,
                fg_color=ACCENT, hover_color=ACCENTL,
                text_color=TEXT_P, font=(FONT_FAMILY, 13),
                command=self._on_src_change,
            ).pack(side='left', padx=(0, 12))

        # ── Window selector ───────────────────────────────────────────────────
        self._win_row = ctk.CTkFrame(body, fg_color='transparent')
        self._win_row.grid(row=2, column=0, sticky='ew', pady=(0, PAD))
        self._win_row.grid_remove()   # hidden until 'window' source chosen
        win_titles = [t for _, t in self._windows] or ['(no windows found)']
        self._win_var = tk.StringVar(value=win_titles[0])
        ctk.CTkLabel(self._win_row, text='Window', font=FONT_SM_BOLD,
                     text_color=TEXT_S).pack(anchor='w', pady=(0, 4))
        ctk.CTkComboBox(
            self._win_row, variable=self._win_var, values=win_titles, width=360,
            fg_color=SURFACE, border_color=BORDER2, border_width=1,
            text_color=TEXT_P, font=(FONT_FAMILY, 13),
            button_color=SURF2, button_hover_color=SURF3,
            corner_radius=RADIUS_SM,
        ).pack(anchor='w')

        # ── Region picker ─────────────────────────────────────────────────────
        self._region_row = ctk.CTkFrame(body, fg_color='transparent')
        self._region_row.grid(row=3, column=0, sticky='ew', pady=(0, PAD))
        self._region_row.grid_remove()
        ctk.CTkLabel(self._region_row, text='Region', font=FONT_SM_BOLD,
                     text_color=TEXT_S).pack(anchor='w', pady=(0, 4))
        region_inner = ctk.CTkFrame(self._region_row, fg_color='transparent')
        region_inner.pack(anchor='w')
        self._region_lbl = ctk.CTkLabel(
            region_inner, text='Click "Pick…" to select',
            font=(FONT_FAMILY, 13), text_color=TEXT_S)
        self._region_lbl.pack(side='left', padx=(0, 8))
        ctk.CTkButton(
            region_inner, text='Pick…', width=72, height=28,
            fg_color=SURF2, hover_color=SURF3, text_color=TEXT_P,
            corner_radius=RADIUS_SM, font=(FONT_FAMILY, 13),
            command=self._pick_region,
        ).pack(side='left')

        # ── FPS ───────────────────────────────────────────────────────────────
        ctk.CTkLabel(body, text='Frame rate', font=FONT_SM_BOLD,
                     text_color=TEXT_S).grid(row=4, column=0, sticky='w', pady=(0, 4))
        self._fps_var = tk.IntVar(value=10)
        fps_row = ctk.CTkFrame(body, fg_color='transparent')
        fps_row.grid(row=5, column=0, sticky='w', pady=(0, PAD))
        for fps in [5, 8, 10, 15]:
            ctk.CTkRadioButton(
                fps_row, text=f'{fps} fps', variable=self._fps_var, value=fps,
                fg_color=ACCENT, hover_color=ACCENTL,
                text_color=TEXT_P, font=(FONT_FAMILY, 13),
            ).pack(side='left', padx=(0, 12))

        # ── Max width ─────────────────────────────────────────────────────────
        ctk.CTkLabel(body, text='Max width', font=FONT_SM_BOLD,
                     text_color=TEXT_S).grid(row=6, column=0, sticky='w', pady=(0, 4))
        self._width_var = tk.IntVar(value=640)
        w_row = ctk.CTkFrame(body, fg_color='transparent')
        w_row.grid(row=7, column=0, sticky='w', pady=(0, PAD))
        for w, lbl in [(480, '480 px'), (640, '640 px'), (800, '800 px'), (1280, '1280 px (large)')]:
            ctk.CTkRadioButton(
                w_row, text=lbl, variable=self._width_var, value=w,
                fg_color=ACCENT, hover_color=ACCENTL,
                text_color=TEXT_P, font=(FONT_FAMILY, 13),
            ).pack(side='left', padx=(0, 12))

        # ── Max duration ──────────────────────────────────────────────────────
        ctk.CTkLabel(body, text='Max duration (auto-stop)', font=FONT_SM_BOLD,
                     text_color=TEXT_S).grid(row=8, column=0, sticky='w', pady=(0, 4))
        self._dur_var = tk.IntVar(value=30)
        dur_row = ctk.CTkFrame(body, fg_color='transparent')
        dur_row.grid(row=9, column=0, sticky='w', pady=(0, PAD))
        for sec, lbl in [(10, '10 s'), (30, '30 s'), (60, '60 s'), (300, '5 min')]:
            ctk.CTkRadioButton(
                dur_row, text=lbl, variable=self._dur_var, value=sec,
                fg_color=ACCENT, hover_color=ACCENTL,
                text_color=TEXT_P, font=(FONT_FAMILY, 13),
            ).pack(side='left', padx=(0, 12))

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = ctk.CTkFrame(body, fg_color='transparent')
        btn_row.grid(row=10, column=0, sticky='e', pady=(PAD, 0))

        ctk.CTkButton(
            btn_row, text='Cancel', width=88,
            fg_color=SURF2, hover_color=SURF3, text_color=TEXT_S,
            corner_radius=RADIUS_SM, font=(FONT_FAMILY, 13),
            command=win.destroy,
        ).pack(side='left', padx=(0, 8))

        self._start_btn = ctk.CTkButton(
            btn_row, text='▶  Start Recording', width=160,
            fg_color=ACCENT, hover_color=ACCENTL, text_color=TEXT_P,
            corner_radius=RADIUS_SM, font=(FONT_FAMILY, 13, 'bold'),
            command=self._start,
        )
        self._start_btn.pack(side='left')

        body.columnconfigure(0, weight=1)
        self._center(self._parent)

    def _on_src_change(self) -> None:
        src = self._src_var.get()
        if src == 'window':
            self._win_row.grid()
            self._region_row.grid_remove()
        elif src == 'region':
            self._win_row.grid_remove()
            self._region_row.grid()
        else:
            self._win_row.grid_remove()
            self._region_row.grid_remove()
        self._refresh_start_btn()

    def _pick_region(self) -> None:
        self.win.withdraw()
        # update_idletasks() instead of update(): no nested event-loop
        # dispatch, see screen_recorder.py for the same reasoning.
        self.win.update_idletasks()

        ov = RegionSelectorOverlay(self.win)
        self.win.wait_window(ov._win)

        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self.win.grab_set()

        if ov.region:
            self._region = ov.region
            l, t, w, h = ov.region
            self._region_lbl.configure(
                text=f'{w} × {h}  at ({l}, {t})', text_color=TEXT_P)
        self._refresh_start_btn()

    def _refresh_start_btn(self) -> None:
        src = self._src_var.get()
        ok  = src != 'region' or self._region is not None
        self._start_btn.configure(state='normal' if ok else 'disabled')

    def _start(self) -> None:
        src  = self._src_var.get()
        hwnd = 0
        mon  = None
        if src == 'window':
            sel   = self._win_var.get()
            match = [h for h, t in self._windows if t == sel]
            hwnd  = match[0] if match else 0
        elif src == 'region':
            if self._region is None:
                return
            mon = self._region   # (l, t, w, h)

        self.result = {
            'hwnd':           hwnd,
            'mon':            mon,
            'fps':            self._fps_var.get(),
            'max_width':      self._width_var.get(),
            'max_duration_s': float(self._dur_var.get()),
        }
        self.win.destroy()

    def _center(self, parent) -> None:
        self.win.update_idletasks()
        w  = self.win.winfo_reqwidth()
        h  = self.win.winfo_reqheight()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        if pw > 1 and ph > 1 and parent.winfo_ismapped():
            x = parent.winfo_rootx() + (pw - w) // 2
            y = parent.winfo_rooty() + (ph - h) // 2
        else:
            x = (sw - w) // 2
            y = (sh - h) // 2
        self.win.geometry(f'+{max(0, min(x, sw-w))}+{max(0, min(y, sh-h))}')


# ── Save Dialog ───────────────────────────────────────────────────────────────

def show_gif_save_dialog(
    parent,
    tmp_path: str,
    duration_s: float,
) -> str | None:
    """
    Show a save-as dialog for the completed GIF.

    Returns the final save path, or None if the user discarded.
    Moves the temp file to the chosen location.
    """
    size_bytes = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
    size_kb    = size_bytes / 1024
    dur_s      = int(duration_s)

    result: list[str | None] = [None]

    dlg = ctk.CTkToplevel(parent)
    dlg.title('Save GIF')
    dlg.configure(fg_color=BG)
    dlg.resizable(False, False)
    dlg.transient(parent)
    dlg.grab_set()

    hdr = ctk.CTkFrame(dlg, fg_color=SURFACE, corner_radius=0)
    hdr.pack(fill='x')
    ctk.CTkLabel(hdr, text='🎞  Save GIF',
                 font=(FONT_FAMILY, 16, 'bold'), text_color=TEXT_P
                 ).pack(anchor='w', padx=PAD, pady=PAD_SM)

    body = ctk.CTkFrame(dlg, fg_color=BG, corner_radius=0)
    body.pack(fill='both', expand=True, padx=PAD, pady=PAD)

    # Summary
    ctk.CTkLabel(
        body,
        text=f'Duration: {dur_s}s  ·  Size: {size_kb:.0f} KB',
        font=(FONT_FAMILY, 13), text_color=TEXT_P,
    ).pack(anchor='w', pady=(0, PAD))

    btn_row = ctk.CTkFrame(body, fg_color='transparent')
    btn_row.pack(anchor='e')

    def _discard():
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        dlg.destroy()

    def _save():
        # Default to gifs folder under AppData so the file shows in the library
        gif_dir = Path(appdata_dir()) / 'gifs'
        gif_dir.mkdir(parents=True, exist_ok=True)
        existing = {Path(f).stem for f in gif_dir.glob('*.gif')}
        n = 1
        while str(n) in existing:
            n += 1
        default_name = f'{n}.gif'
        dest = fd.asksaveasfilename(
            parent=dlg,
            title='Save GIF as…',
            defaultextension='.gif',
            filetypes=[('GIF files', '*.gif'), ('All files', '*.*')],
            initialfile=default_name,
            initialdir=str(gif_dir),
        )
        if not dest:
            return
        try:
            import shutil
            shutil.move(tmp_path, dest)
            result[0] = dest
        except Exception as exc:
            from dialogs import alert
            alert(dlg, 'Save failed', str(exc))
            return
        dlg.destroy()

    ctk.CTkButton(
        btn_row, text='Discard', width=88,
        fg_color=SURF2, hover_color=SURF3, text_color=TEXT_S,
        corner_radius=RADIUS_SM, font=(FONT_FAMILY, 13),
        command=_discard,
    ).pack(side='left', padx=(0, 8))

    ctk.CTkButton(
        btn_row, text='💾  Save As…', width=120,
        fg_color=ACCENT, hover_color=ACCENTL, text_color=TEXT_P,
        corner_radius=RADIUS_SM, font=(FONT_FAMILY, 13, 'bold'),
        command=_save,
    ).pack(side='left')

    # Center over parent
    dlg.update_idletasks()
    pw = parent.winfo_width()
    ph = parent.winfo_height()
    px = parent.winfo_rootx()
    py = parent.winfo_rooty()
    w  = dlg.winfo_reqwidth()
    h  = dlg.winfo_reqheight()
    x  = px + (pw - w) // 2
    y  = py + (ph - h) // 2
    dlg.geometry(f'+{x}+{y}')

    parent.wait_window(dlg)
    return result[0]


# ── GIF index (tracks files saved outside the default folder) ─────────────────

_gif_index_lock = threading.Lock()


def _gif_index_path() -> Path:
    return Path(appdata_dir()) / 'gifs_index.json'


def _write_gif_index_atomic(idx_file: Path, entries: list) -> None:
    import json
    tmp = idx_file.with_suffix('.tmp')
    try:
        tmp.write_text(json.dumps(entries, indent=2), encoding='utf-8')
        os.replace(str(tmp), str(idx_file))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def add_to_gif_index(path: str) -> None:
    """Persist a saved GIF path so it appears in the list regardless of save location."""
    import json
    idx_file = _gif_index_path()
    path = str(Path(path).resolve())
    with _gif_index_lock:
        try:
            entries: list = json.loads(idx_file.read_text(encoding='utf-8')) if idx_file.exists() else []
        except Exception:
            entries = []
        if path not in entries:
            entries.append(path)
            _write_gif_index_atomic(idx_file, entries)


def remove_from_gif_index(path: str) -> None:
    """Remove a path from the GIF index (e.g. after deleting the file)."""
    import json
    idx_file = _gif_index_path()
    if not idx_file.exists():
        return
    key = str(Path(path).resolve())
    with _gif_index_lock:
        try:
            entries: list = json.loads(idx_file.read_text(encoding='utf-8'))
        except Exception:
            return
        new_entries = [e for e in entries if str(Path(e).resolve()) != key]
        if len(new_entries) != len(entries):
            _write_gif_index_atomic(idx_file, new_entries)


def _load_gif_index_entries() -> list[str]:
    """Return all indexed GIF paths, pruning entries for deleted files."""
    import json
    idx_file = _gif_index_path()
    if not idx_file.exists():
        return []
    with _gif_index_lock:
        try:
            entries: list = json.loads(idx_file.read_text(encoding='utf-8'))
        except Exception:
            return []
        live = [p for p in entries if Path(p).exists()]
        if len(live) != len(entries):
            _write_gif_index_atomic(idx_file, live)
    return live


def list_gifs(gif_dir: str) -> list[dict]:
    """
    Return a list of dicts for every known .gif, sorted newest-first.
    Sources: the default gif_dir (scanned for *.gif) plus any paths
    persisted in the GIF index (files saved to arbitrary locations).
    Each dict has: path, name, size_kb, mtime.
    """
    seen: set[str] = set()
    candidates: list[Path] = []

    p = Path(gif_dir)
    if p.exists():
        for f in p.glob('*.gif'):
            key = str(f.resolve())
            if key not in seen:
                seen.add(key)
                candidates.append(f)

    for raw in _load_gif_index_entries():
        key = str(Path(raw).resolve())
        if key not in seen:
            seen.add(key)
            candidates.append(Path(raw))

    result = []
    for f in candidates:
        try:
            st = f.stat()
            result.append({
                'path':    str(f),
                'name':    f.name,
                'size_kb': st.st_size / 1024,
                'mtime':   st.st_mtime,
            })
        except Exception:
            pass
    result.sort(key=lambda d: d['mtime'], reverse=True)
    return result
