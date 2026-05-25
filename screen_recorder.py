"""
screen_recorder.py — Screen + audio recording engine for the Hotkeys app.

Public surface
--------------
ScreenCapture   — Win32 BitBlt capture context
Recorder        — threaded encoder (H.264 + AAC → MP4 via PyAV)
list_windows()  — enumerate capturable desktop windows
RecorderSetupDialog  — pre-recording options dialog (tkinter)
show_save_dialog()   — post-recording save-as dialog (tkinter)

No global state lives here; all orchestration is in main.py / library.py.
"""
from __future__ import annotations

import ctypes
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
from fractions import Fraction
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Callable

if sys.platform == 'win32':
    import win32con
    import win32gui
    import win32ui

import av
import numpy as np
import sounddevice as sd
from PIL import ImageTk

from theme import (
    BG, SURFACE, SURF2, SURF3, ACCENT, ACCENTL,
    TEXT_P, TEXT_S, FONT_FAMILY, PAD, PAD_SM, RADIUS_SM,
)

# ── Constants ─────────────────────────────────────────────────────────────────

AUDIO_RATE   = 44100
AUDIO_CHANS  = 2
AUDIO_CHUNK  = 1024           # samples per sounddevice callback
SIZE_LIMIT_B = 1 * 1024 ** 3  # 1 GB hard cap

RECORDINGS_DIR_NAME = 'recordings'   # sub-folder of appdata_dir()


# ── Screen / window helpers ───────────────────────────────────────────────────

def list_windows() -> list[tuple[int, str]]:
    """Return [(hwnd, title), …] for all visible, non-trivial windows."""
    if sys.platform == 'win32':
        results: list[tuple[int, str]] = []

        def _cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            if not win32gui.IsWindowEnabled(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title or len(title) < 2:
                return
            cls = win32gui.GetClassName(hwnd)
            if cls in ('Shell_TrayWnd', 'Progman', 'WorkerW'):
                return
            results.append((hwnd, title))

        win32gui.EnumWindows(_cb, None)
        return results
    else:
        # macOS: use Quartz
        try:
            from Quartz import CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly, kCGNullWindowID, kCGWindowListExcludeDesktopElements
            import Quartz
            opts = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
            wl = CGWindowListCopyWindowInfo(opts, kCGNullWindowID)
            results = []
            for w in wl:
                title = w.get('kCGWindowName') or w.get('kCGWindowOwnerName', '')
                if not title or len(title) < 2:
                    continue
                layer = w.get('kCGWindowLayer', 0)
                if layer != 0:
                    continue   # skip menu bar, dock, etc.
                wid = w.get('kCGWindowNumber', 0)
                results.append((wid, title))
            return results
        except Exception:
            return []


def list_input_devices() -> list[tuple[int | None, str]]:
    """Return [(device_index, label), …] for all audio input devices.

    The first entry is always (None, 'Default microphone') so the user can
    choose the OS default without having to know which index it is.
    Subsequent entries are the real devices sorted by index.
    """
    entries: list[tuple[int | None, str]] = [(None, 'Default microphone')]
    try:
        for i, dev in enumerate(sd.query_devices()):
            if dev['max_input_channels'] > 0:
                name = dev['name'].strip()
                entries.append((i, name))
    except Exception:
        pass
    return entries


if sys.platform == 'win32':
    class ScreenCapture:
        """
        Capture a region of the screen using Win32 BitBlt.

        Parameters
        ----------
        hwnd  : 0 = primary monitor, else a specific window handle.
        mon   : (left, top, width, height). Pass None to auto-detect.
        """

        def __init__(self, hwnd: int = 0, mon=None):
            self.hwnd = hwnd
            if mon is not None:
                self._l, self._t, self._w, self._h = mon
            elif hwnd:
                rect = win32gui.GetWindowRect(hwnd)
                self._l = rect[0]
                self._t = rect[1]
                self._w = max(2, rect[2] - rect[0])
                self._h = max(2, rect[3] - rect[1])
            else:
                user32 = ctypes.windll.user32
                self._l = 0
                self._t = 0
                self._w = user32.GetSystemMetrics(0)
                self._h = user32.GetSystemMetrics(1)

            # yuv420p requires even dimensions
            self._w -= self._w % 2
            self._h -= self._h % 2

            self._desk = win32gui.GetDesktopWindow()
            self._hdcSrc = None
            self._open()

        def _open(self):
            src = self.hwnd if self.hwnd else self._desk
            self._hdcSrc = win32gui.GetWindowDC(src)
            self._mfcDC  = win32ui.CreateDCFromHandle(self._hdcSrc)
            self._saveDC = self._mfcDC.CreateCompatibleDC()
            self._bmp    = win32ui.CreateBitmap()
            self._bmp.CreateCompatibleBitmap(self._mfcDC, self._w, self._h)
            self._saveDC.SelectObject(self._bmp)

        def grab(self) -> np.ndarray:
            """Return (H, W, 3) uint8 BGR array."""
            src = self.hwnd if self.hwnd else self._desk
            try:
                self._saveDC.BitBlt(
                    (0, 0), (self._w, self._h),
                    self._mfcDC, (self._l, self._t),
                    win32con.SRCCOPY,
                )
            except Exception:
                return np.zeros((self._h, self._w, 3), dtype=np.uint8)
            raw = self._bmp.GetBitmapBits(True)
            arr = np.frombuffer(raw, dtype=np.uint8).reshape(self._h, self._w, 4)
            return arr[:, :, :3].copy()

        def size(self) -> tuple[int, int]:
            return self._w, self._h

        def close(self):
            try:
                win32gui.DeleteObject(self._bmp.GetHandle())
                self._saveDC.DeleteDC()
                self._mfcDC.DeleteDC()
                src = self.hwnd if self.hwnd else self._desk
                win32gui.ReleaseDC(src, self._hdcSrc)
            except Exception:
                pass

else:
    class ScreenCapture:
        """macOS screen capture using mss."""
        def __init__(self, hwnd: int = 0, mon=None):
            import mss as _mss
            self._sct = _mss.mss()
            if mon is not None:
                self._l, self._t, self._w, self._h = mon
            elif hwnd:
                # hwnd is a Quartz window ID; get its bounds
                try:
                    from Quartz import CGWindowListCopyWindowInfo, kCGWindowListOptionIncludingWindow, kCGNullWindowID
                    wl = CGWindowListCopyWindowInfo(
                        0x00000001,  # kCGWindowListOptionIncludingWindow
                        hwnd)
                    if wl:
                        b = wl[0].get('kCGWindowBounds', {})
                        self._l = int(b.get('X', 0))
                        self._t = int(b.get('Y', 0))
                        self._w = max(2, int(b.get('Width', 100)))
                        self._h = max(2, int(b.get('Height', 100)))
                    else:
                        raise ValueError('window not found')
                except Exception:
                    m = self._sct.monitors[1]
                    self._l, self._t = m['left'], m['top']
                    self._w, self._h = m['width'], m['height']
            else:
                m = self._sct.monitors[1]   # [0]=all monitors combined, [1]=primary
                self._l = m['left']
                self._t = m['top']
                self._w = m['width']
                self._h = m['height']
            self._w -= self._w % 2
            self._h -= self._h % 2
            self._monitor = {'left': self._l, 'top': self._t,
                             'width': self._w, 'height': self._h}

        def grab(self) -> 'np.ndarray':
            try:
                img = self._sct.grab(self._monitor)
                # mss returns BGRA; drop alpha → BGR
                arr = np.frombuffer(img.bgra, dtype=np.uint8).reshape(img.height, img.width, 4)
                return arr[:, :, :3].copy()
            except Exception:
                return np.zeros((self._h, self._w, 3), dtype=np.uint8)

        def size(self) -> tuple:
            return self._w, self._h

        def close(self):
            try:
                self._sct.close()
            except Exception:
                pass


# ── Recorder ─────────────────────────────────────────────────────────────────

class Recorder:
    """
    Threaded screen + audio recorder → MP4 temp file.

    Usage:
        r = Recorder(hwnd=0, mic=True, fps=20,
                     on_size_update=cb, on_cap_reached=cb)
        r.start()
        ...
        r.stop()           # blocks until encoder flushed
        path = r.output_path    # temp file; move to destination
        err  = r.error          # None on success
    """

    def __init__(self, *,
                 hwnd: int = 0,
                 mon=None,
                 mic: bool = False,
                 mic_device: int | None = None,
                 fps: int = 20,
                 on_size_update: Callable[[int], None] | None = None,
                 on_cap_reached: Callable[[], None] | None = None):
        self._hwnd       = hwnd
        self._mon        = mon
        self._mic        = mic
        self._mic_device = mic_device   # None = OS default
        self._fps        = fps
        self._on_size    = on_size_update
        self._on_cap     = on_cap_reached

        self._stop_evt   = threading.Event()
        self._aq: queue.Queue = queue.Queue()
        self._vid_thread: threading.Thread | None = None
        self._sd_stream  = None

        self.output_path: str | None = None
        self.error:       str | None = None
        self.mic_warning: str | None = None   # non-fatal mic setup failure
        self._started_at: float      = 0.0
        self.bytes_written: int      = 0

    # ── Public ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        fd, path = tempfile.mkstemp(suffix='.mp4', prefix='rec_')
        os.close(fd)
        self.output_path = path
        self._stop_evt.clear()
        self._started_at = time.perf_counter()

        if self._mic:
            try:
                self._sd_stream = sd.InputStream(
                    device=self._mic_device,   # None = OS default
                    samplerate=AUDIO_RATE,
                    channels=AUDIO_CHANS,
                    blocksize=AUDIO_CHUNK,
                    dtype='float32',
                    callback=self._audio_cb,
                )
            except Exception as exc:
                # Store as a warning — mic failed but video recording can still proceed
                self.mic_warning = f'Mic unavailable: {exc}'
                self._mic = False
                # Release any partial WASAPI handle before continuing
                if self._sd_stream is not None:
                    try:
                        self._sd_stream.stop()
                        self._sd_stream.close()
                    except Exception:
                        pass
                    self._sd_stream = None

        self._vid_thread = threading.Thread(
            target=self._record_loop, daemon=True, name='rec-video')
        self._vid_thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._vid_thread:
            self._vid_thread.join(timeout=20)

    def elapsed(self) -> float:
        return time.perf_counter() - self._started_at

    # ── Internal ─────────────────────────────────────────────────────────────

    def _audio_cb(self, indata, frames, t, status):
        self._aq.put(indata.copy())

    def _record_loop(self) -> None:
        import logging as _logging
        _log = _logging.getLogger(__name__)

        cap       = None
        container = None
        vstream   = None
        astream   = None

        try:
            cap      = ScreenCapture(self._hwnd, self._mon)
            w, h     = cap.size()
            fps      = self._fps
            interval = 1.0 / fps

            # frag_keyframe+empty_moov: write each keyframe fragment to disk
            # immediately so a failed close() loses at most the last fragment.
            container = av.open(self.output_path, 'w',
                                options={'movflags': 'frag_keyframe+empty_moov+default_base_moof'})

            # Video stream
            vstream = container.add_stream('libx264', rate=fps)
            vstream.width   = w
            vstream.height  = h
            vstream.pix_fmt = 'yuv420p'
            vstream.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'crf': '23'}

            # Audio stream — mic failures are non-fatal: fall back to video-only
            apts_samples = 0
            if self._mic and self._sd_stream is not None:
                try:
                    astream = container.add_stream('aac', rate=AUDIO_RATE)
                    # Use layout only — avoids the deprecated .channels setter
                    # which raises AttributeError in PyAV ≥ 12.
                    astream.layout = 'stereo'
                    self._sd_stream.start()
                except Exception as exc:
                    _log.warning(f'Mic setup failed (recording video-only): {exc}')
                    astream = None

            frame_idx = 0
            next_time = time.perf_counter()

            while not self._stop_evt.is_set():
                now = time.perf_counter()
                if now < next_time:
                    time.sleep(next_time - now)
                next_time += interval
                # If we've fallen more than one frame behind (e.g. due to a slow
                # encode or system load spike), re-anchor so we don't schedule a
                # burst of back-to-back frames trying to catch up.
                if next_time < time.perf_counter() - interval:
                    next_time = time.perf_counter()

                # ── Video frame ──────────────────────────────────────────────
                bgr = cap.grab()
                vf  = av.VideoFrame.from_ndarray(bgr, format='bgr24')
                vf.pts       = frame_idx
                vf.time_base = Fraction(1, fps)
                for pkt in vstream.encode(vf):
                    container.mux(pkt)
                    self.bytes_written += pkt.size
                frame_idx += 1

                # ── Audio flush ──────────────────────────────────────────────
                if astream:
                    while True:
                        try:
                            chunk = self._aq.get_nowait()
                        except queue.Empty:
                            break
                        # chunk: float32 (frames, channels) from sounddevice.
                        # fltp (float planar) expects (channels, frames) float32 —
                        # matches chunk.T exactly and is the AAC encoder's native fmt.
                        af = av.AudioFrame.from_ndarray(
                            chunk.T.copy(), format='fltp', layout='stereo')
                        af.sample_rate = AUDIO_RATE
                        af.pts         = apts_samples
                        apts_samples  += chunk.shape[0]
                        for pkt in astream.encode(af):
                            container.mux(pkt)
                            self.bytes_written += pkt.size

                # ── Size cap ─────────────────────────────────────────────────
                if self._on_size:
                    try:
                        self._on_size(self.bytes_written)
                    except Exception:
                        pass
                if self.bytes_written >= SIZE_LIMIT_B:
                    if self._on_cap:
                        try:
                            self._on_cap()
                        except Exception:
                            pass
                    break

        except Exception as exc:
            self.error = str(exc)
            _log.error(f'Recording loop error: {exc}', exc_info=True)
        finally:
            # Stop mic capture first so no more audio chunks arrive
            try:
                if self._sd_stream:
                    self._sd_stream.stop()
                    self._sd_stream.close()
            except Exception:
                pass
            # Flush encoder queues — errors here are non-fatal (partial flush)
            if vstream is not None:
                try:
                    for pkt in vstream.encode():
                        container.mux(pkt)
                        self.bytes_written += pkt.size
                except Exception:
                    pass
            if astream is not None:
                try:
                    for pkt in astream.encode():
                        container.mux(pkt)
                        self.bytes_written += pkt.size
                except Exception:
                    pass
            # close() writes the final fragment / moov trailer.
            if container is not None:
                try:
                    container.close()
                except Exception as _close_exc:
                    if not self.error:
                        self.error = f'Failed to finalise recording: {_close_exc}'
            elif self.output_path and os.path.exists(self.output_path):
                # container was never opened (ScreenCapture or av.open failed) —
                # remove the empty temp file so caller sees no file rather than a 0-byte stub.
                try:
                    os.unlink(self.output_path)
                except Exception:
                    pass
            if cap is not None:
                cap.close()


# ── Region selector overlay ───────────────────────────────────────────────────

_REG_BORDER  = '#ff0000'   # OBS-style red border
_REG_FILL    = '#ff000022' # very faint red tint inside region (approx — canvas has no alpha)
_REG_DIM     = 0.45        # outside-region brightness factor
_REG_HANDLE  = 6           # handle square half-size px
_REG_DASH    = (6, 3)


class RegionSelectorOverlay:
    """Full-screen overlay that lets the user drag-resize a red rectangle.

    Result is stored in self.region as (left, top, width, height) in screen
    coordinates, or None if the user cancelled.

    Usage (blocking — run on the main thread with its own mainloop):
        ov = RegionSelectorOverlay(root)
        root.wait_window(ov._win)
        region = ov.region   # (l, t, w, h) or None
    """

    def __init__(self, root) -> None:
        self.region: tuple | None = None
        self._root  = root
        self._sx = self._sy = self._cx = self._cy = 0
        self._dragging   = False
        self._resizing   = False   # dragging a handle
        self._res_handle = None    # which handle: 'nw','n','ne','e','se','s','sw','w'
        self._res_sx = self._res_sy = 0
        self._res_orig   = (0, 0, 0, 0)  # (sx,sy,cx,cy) at resize start
        self._has_sel    = False
        self._build()

    def _build(self) -> None:
        if sys.platform == 'win32':
            user32 = ctypes.windll.user32
            vx = user32.GetSystemMetrics(76)
            vy = user32.GetSystemMetrics(77)
            vw = user32.GetSystemMetrics(78)
            vh = user32.GetSystemMetrics(79)
        else:
            # macOS: use mss to get bounding box of all monitors
            try:
                import mss as _mss
                with _mss.mss() as sct:
                    all_mon = sct.monitors[0]  # index 0 = all monitors combined
                    vx = all_mon['left']
                    vy = all_mon['top']
                    vw = all_mon['width']
                    vh = all_mon['height']
            except Exception:
                vx, vy = 0, 0
                vw = self._root.winfo_screenwidth()
                vh = self._root.winfo_screenheight()
        self._vx, self._vy, self._vw, self._vh = vx, vy, vw, vh

        from PIL import ImageGrab, ImageEnhance
        shot    = ImageGrab.grab(all_screens=True)
        dim_img = ImageEnhance.Brightness(shot).enhance(_REG_DIM)

        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.geometry(f'{vw}x{vh}+{vx}+{vy}')
        win.configure(bg='black')
        self._win = win

        canvas = tk.Canvas(win, bg='black', highlightthickness=0,
                           width=vw, height=vh, cursor='crosshair')
        canvas.pack(fill='both', expand=True)
        self._canvas = canvas
        self._shot   = shot

        self._dim_photo = ImageTk.PhotoImage(dim_img, master=canvas)
        canvas.create_image(0, 0, anchor='nw', image=self._dim_photo, tags=('bg',))

        # Instruction label
        self._instr = canvas.create_text(
            vw // 2, 28, text='Drag to select recording region  —  Enter to confirm  —  Esc to cancel',
            fill='#ffffff', font=(FONT_FAMILY, 12), tags=('instr',))

        canvas.bind('<ButtonPress-1>',   self._on_down)
        canvas.bind('<B1-Motion>',       self._on_drag)
        canvas.bind('<ButtonRelease-1>', self._on_up)
        canvas.bind('<Motion>',          self._on_motion)
        win.bind('<Escape>', lambda e: self._cancel())
        win.bind('<Return>', lambda e: self._confirm())
        win.focus_force()

    # ── Selection helpers ─────────────────────────────────────────────────────

    def _sel(self):
        x1, y1 = min(self._sx, self._cx), min(self._sy, self._cy)
        x2, y2 = max(self._sx, self._cx), max(self._sy, self._cy)
        if x2 - x1 < 4 or y2 - y1 < 4:
            return None
        return x1, y1, x2, y2

    def _handles(self, x1, y1, x2, y2):
        mx, my = (x1 + x2) // 2, (y1 + y2) // 2
        return {
            'nw': (x1, y1), 'n': (mx, y1), 'ne': (x2, y1),
            'e':  (x2, my),
            'se': (x2, y2), 's': (mx, y2), 'sw': (x1, y2),
            'w':  (x1, my),
        }

    def _hit_handle(self, x, y, x1, y1, x2, y2):
        """Return handle name if (x,y) is within _REG_HANDLE px of a handle, else None."""
        for name, (hx, hy) in self._handles(x1, y1, x2, y2).items():
            if abs(x - hx) <= _REG_HANDLE + 2 and abs(y - hy) <= _REG_HANDLE + 2:
                return name
        return None

    # ── Redraw ────────────────────────────────────────────────────────────────

    def _redraw(self) -> None:
        c = self._canvas
        c.delete('sel', 'handle', 'badge', 'btn')
        s = self._sel()
        if not s:
            self._has_sel = False
            return
        self._has_sel = True
        x1, y1, x2, y2 = s

        # Bright crop inside selection
        crop = self._shot.crop((x1, y1, x2, y2))
        self._sel_photo = ImageTk.PhotoImage(crop, master=c)
        c.create_image(x1, y1, anchor='nw', image=self._sel_photo, tags=('sel',))

        # Red dashed border
        c.create_rectangle(x1, y1, x2, y2,
                           outline=_REG_BORDER, width=2,
                           dash=_REG_DASH, fill='', tags=('sel',))

        # 8 handles
        for name, (hx, hy) in self._handles(x1, y1, x2, y2).items():
            c.create_rectangle(hx - _REG_HANDLE, hy - _REG_HANDLE,
                               hx + _REG_HANDLE, hy + _REG_HANDLE,
                               fill=_REG_BORDER, outline='#800000',
                               width=1, tags=('handle',))

        # Size badge top-left
        w, h   = x2 - x1, y2 - y1
        txt    = f'{w} × {h}'
        bx, by = x1, max(0, y1 - 22)
        tw     = len(txt) * 7 + 14
        c.create_rectangle(bx, by, bx + tw, by + 18,
                           fill='#1a1a1a', outline='', tags=('badge',))
        c.create_text(bx + 7, by + 9, text=txt, fill='#ffffff',
                      font=('Segoe UI', 9), anchor='w', tags=('badge',))

        # Confirm button below selection
        btn_y = min(y2 + 10, self._vh - 36)
        btn_cx = (x1 + x2) // 2
        bw = 160
        bx1, bx2 = btn_cx - bw // 2, btn_cx + bw // 2
        by1, by2 = btn_y, btn_y + 28
        btn_bg = c.create_rectangle(bx1, by1, bx2, by2,
                                    fill=_REG_BORDER, outline='#800000',
                                    width=1, tags=('btn',))
        btn_txt = c.create_text(btn_cx, (by1 + by2) // 2,
                                text='⏺  Record this region',
                                fill='#ffffff', font=(FONT_FAMILY, 11, 'bold'),
                                tags=('btn',))
        for item in (btn_bg, btn_txt):
            c.tag_bind(item, '<ButtonPress-1>', lambda e: self._confirm())
            c.tag_bind(item, '<Enter>', lambda e: c.config(cursor='hand2'))
            c.tag_bind(item, '<Leave>', lambda e: c.config(cursor='crosshair'))

        c.tag_raise('handle')
        c.tag_raise('badge')
        c.tag_raise('btn')
        c.tag_raise('instr')

    # ── Cursor ────────────────────────────────────────────────────────────────

    _HANDLE_CURSORS = {
        'nw': 'size_nw_se', 'se': 'size_nw_se',
        'ne': 'size_ne_sw', 'sw': 'size_ne_sw',
        'n':  'size_ns',    's':  'size_ns',
        'e':  'size_we',    'w':  'size_we',
    }

    def _on_motion(self, event) -> None:
        if not self._has_sel:
            return
        s = self._sel()
        if not s:
            return
        h = self._hit_handle(event.x, event.y, *s)
        if h:
            self._canvas.config(cursor=self._HANDLE_CURSORS.get(h, 'fleur'))
        elif s[0] <= event.x <= s[2] and s[1] <= event.y <= s[3]:
            self._canvas.config(cursor='fleur')
        else:
            self._canvas.config(cursor='crosshair')

    # ── Mouse handlers ────────────────────────────────────────────────────────

    def _on_down(self, event) -> None:
        s = self._sel()
        if s:
            h = self._hit_handle(event.x, event.y, *s)
            if h:
                self._resizing   = True
                self._res_handle = h
                self._res_sx     = event.x
                self._res_sy     = event.y
                self._res_orig   = (self._sx, self._sy, self._cx, self._cy)
                return
            # Click inside existing selection — move it
            if s[0] <= event.x <= s[2] and s[1] <= event.y <= s[3]:
                self._resizing   = True
                self._res_handle = 'move'
                self._res_sx     = event.x
                self._res_sy     = event.y
                self._res_orig   = (self._sx, self._sy, self._cx, self._cy)
                return
        # New selection
        self._sx = self._cx = event.x
        self._sy = self._cy = event.y
        self._dragging = True
        self._canvas.delete('sel', 'handle', 'badge', 'btn')
        self._has_sel  = False

    def _on_drag(self, event) -> None:
        ex = max(self._vx, min(event.x, self._vx + self._vw - 1))
        ey = max(self._vy, min(event.y, self._vy + self._vh - 1))
        if self._resizing:
            dx, dy = ex - self._res_sx, ey - self._res_sy
            osx, osy, ocx, ocy = self._res_orig
            h = self._res_handle
            if h == 'move':
                self._sx = osx + dx; self._cx = ocx + dx
                self._sy = osy + dy; self._cy = ocy + dy
            elif h == 'nw': self._sx = osx + dx; self._sy = osy + dy
            elif h == 'n':  self._sy = osy + dy
            elif h == 'ne': self._cx = ocx + dx; self._sy = osy + dy
            elif h == 'e':  self._cx = ocx + dx
            elif h == 'se': self._cx = ocx + dx; self._cy = ocy + dy
            elif h == 's':  self._cy = ocy + dy
            elif h == 'sw': self._sx = osx + dx; self._cy = ocy + dy
            elif h == 'w':  self._sx = osx + dx
            self._redraw()
        elif self._dragging:
            self._cx = ex
            self._cy = ey
            self._redraw()

    def _on_up(self, event) -> None:
        self._dragging = False
        self._resizing = False

    # ── Actions ───────────────────────────────────────────────────────────────

    def _confirm(self) -> None:
        s = self._sel()
        if not s:
            return
        x1, y1, x2, y2 = s
        # Ensure even dimensions (required by yuv420p encoder)
        w = (x2 - x1) & ~1
        h = (y2 - y1) & ~1
        if w < 2 or h < 2:
            return
        self.region = (x1, y1, w, h)
        self._win.destroy()

    def _cancel(self) -> None:
        self.region = None
        self._win.destroy()


# ── Setup Dialog ─────────────────────────────────────────────────────────────

class RecorderSetupDialog:
    """
    Pre-recording options dialog (source, mic, fps).

    Usage:
        dlg = RecorderSetupDialog(parent)
        parent.wait_window(dlg.win)
        if dlg.result:
            hwnd   = dlg.result['hwnd']    # 0 = screen / region
            mon    = dlg.result['mon']     # (l,t,w,h) for region, None otherwise
            mic    = dlg.result['mic']
            fps    = dlg.result['fps']
    """

    def __init__(self, parent) -> None:
        self.result: dict | None = None
        self._parent  = parent          # stored so _center always uses the right window
        self._windows = list_windows()
        self._region: tuple | None = None   # (l,t,w,h) from RegionSelectorOverlay

        self.win = tk.Toplevel(parent)
        self.win.title('Start Recording')
        self.win.resizable(False, False)
        self.win.configure(bg=BG)
        # Only set transient when the parent is actually mapped — on Windows,
        # a transient child of a withdrawn/hidden parent is itself hidden,
        # making the dialog invisible and preventing the user from closing it.
        if parent.winfo_ismapped():
            self.win.transient(parent)
        # Ensure the dialog is always visible even when parent is withdrawn.
        self.win.deiconify()
        try:
            self.win.grab_set()
        except Exception:
            pass   # grab can fail if window isn't viewable yet; non-fatal
        self._build()
        self._center(parent)
        self.win.bind('<Escape>', lambda e: self.win.destroy())

    def _build(self) -> None:
        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(self.win, bg=SURFACE)
        hdr.pack(fill='x')
        tk.Label(hdr, text='🎥  Screen Recording',
                 bg=SURFACE, fg=TEXT_P,
                 font=(FONT_FAMILY, 14, 'bold')).pack(anchor='w', padx=PAD, pady=PAD_SM)

        tk.Frame(self.win, bg='#2a2a3e', height=1).pack(fill='x')

        body = tk.Frame(self.win, bg=BG, padx=PAD, pady=PAD)
        body.pack(fill='both')

        # ── Source ────────────────────────────────────────────────────────────
        tk.Label(body, text='Source', bg=BG, fg=TEXT_S,
                 font=(FONT_FAMILY, 11, 'bold')).pack(anchor='w', pady=(0, 4))

        self._src_var = tk.StringVar(value='screen')

        row1 = tk.Frame(body, bg=BG)
        row1.pack(anchor='w')
        tk.Radiobutton(
            row1, text='Entire screen', variable=self._src_var, value='screen',
            bg=BG, fg=TEXT_P, selectcolor=BG, activebackground=BG,
            activeforeground=TEXT_P, font=(FONT_FAMILY, 12),
            command=self._on_src_change,
        ).pack(side='left')

        row2 = tk.Frame(body, bg=BG)
        row2.pack(anchor='w', pady=(2, 0))
        tk.Radiobutton(
            row2, text='Window', variable=self._src_var, value='window',
            bg=BG, fg=TEXT_P, selectcolor=BG, activebackground=BG,
            activeforeground=TEXT_P, font=(FONT_FAMILY, 12),
            command=self._on_src_change,
        ).pack(side='left')
        win_titles = [t for _, t in self._windows]
        self._win_var = tk.StringVar(value=win_titles[0] if win_titles else '')
        style = ttk.Style()
        style.theme_use('default')
        style.configure('Dark.TCombobox',
                        fieldbackground=SURF2, background=SURF2,
                        foreground=TEXT_P, selectbackground=ACCENT,
                        selectforeground=TEXT_P)
        self._win_combo = ttk.Combobox(
            row2, textvariable=self._win_var,
            values=win_titles, state='disabled',
            width=26, font=(FONT_FAMILY, 11),
        )
        self._win_combo.pack(side='left', padx=(6, 0))

        row3 = tk.Frame(body, bg=BG)
        row3.pack(anchor='w', pady=(2, PAD))
        tk.Radiobutton(
            row3, text='Region', variable=self._src_var, value='region',
            bg=BG, fg=TEXT_P, selectcolor=BG, activebackground=BG,
            activeforeground=TEXT_P, font=(FONT_FAMILY, 12),
            command=self._on_src_change,
        ).pack(side='left')
        self._region_btn = tk.Button(
            row3, text='Select…', state='disabled',
            bg=SURF2, fg=TEXT_S,
            activebackground=SURF3, activeforeground=TEXT_P,
            relief='flat', font=(FONT_FAMILY, 11),
            padx=8, pady=2, cursor='hand2',
            command=self._pick_region,
        )
        self._region_btn.pack(side='left', padx=(6, 0))
        self._region_lbl = tk.Label(
            row3, text='', bg=BG, fg=TEXT_S, font=(FONT_FAMILY, 10))
        self._region_lbl.pack(side='left', padx=(6, 0))

        # ── Audio ─────────────────────────────────────────────────────────────
        tk.Label(body, text='Audio', bg=BG, fg=TEXT_S,
                 font=(FONT_FAMILY, 11, 'bold')).pack(anchor='w', pady=(0, 4))

        self._mic_devices = list_input_devices()   # [(idx|None, label), …]
        self._mic_var     = tk.BooleanVar(value=False)

        mic_row = tk.Frame(body, bg=BG)
        mic_row.pack(anchor='w', fill='x', pady=(0, 2))
        tk.Checkbutton(
            mic_row, text='Record microphone', variable=self._mic_var,
            bg=BG, fg=TEXT_P, selectcolor=BG, activebackground=BG,
            activeforeground=TEXT_P, font=(FONT_FAMILY, 12),
            command=self._on_mic_toggle,
        ).pack(side='left')

        # Device picker — visible only when checkbox is ticked
        dev_labels = [lbl for _, lbl in self._mic_devices]
        self._mic_dev_var   = tk.StringVar(value=dev_labels[0] if dev_labels else '')
        self._mic_dev_combo = ttk.Combobox(
            mic_row, textvariable=self._mic_dev_var,
            values=dev_labels, state='disabled',
            width=28, font=(FONT_FAMILY, 11),
        )
        self._mic_dev_combo.pack(side='left', padx=(8, 0))
        # Start hidden; shown when user ticks the checkbox
        self._mic_dev_combo.pack_forget()

        # Spacer below audio row
        tk.Frame(body, bg=BG, height=PAD).pack()

        # ── FPS ───────────────────────────────────────────────────────────────
        tk.Label(body, text='Frame rate', bg=BG, fg=TEXT_S,
                 font=(FONT_FAMILY, 11, 'bold')).pack(anchor='w', pady=(0, 4))
        fps_row = tk.Frame(body, bg=BG)
        fps_row.pack(anchor='w', pady=(0, PAD))
        self._fps_var = tk.IntVar(value=20)
        for val, lbl in [(15, '15 fps'), (20, '20 fps'), (30, '30 fps')]:
            tk.Radiobutton(
                fps_row, text=lbl, variable=self._fps_var, value=val,
                bg=BG, fg=TEXT_P, selectcolor=BG, activebackground=BG,
                activeforeground=TEXT_P, font=(FONT_FAMILY, 12),
            ).pack(side='left', padx=(0, 8))

        # ── Footer ────────────────────────────────────────────────────────────
        tk.Frame(self.win, bg='#2a2a3e', height=1).pack(fill='x')
        foot = tk.Frame(self.win, bg=SURFACE)
        foot.pack(fill='x')

        self._start_btn = tk.Button(
            foot, text='Start Recording',
            bg=ACCENT, fg='#ffffff',
            activebackground=ACCENTL, activeforeground='#ffffff',
            relief='flat', font=(FONT_FAMILY, 12, 'bold'),
            padx=16, pady=6, cursor='hand2',
            command=self._start,
        )
        self._start_btn.pack(side='right', padx=PAD, pady=PAD_SM)

        tk.Button(
            foot, text='Cancel',
            bg=SURF2, fg=TEXT_S,
            activebackground=SURF3, activeforeground=TEXT_P,
            relief='flat', font=(FONT_FAMILY, 12),
            padx=12, pady=6, cursor='hand2',
            command=self.win.destroy,
        ).pack(side='right', pady=PAD_SM)

    def _on_mic_toggle(self) -> None:
        """Show/hide the device picker when the mic checkbox is toggled."""
        if self._mic_var.get():
            self._mic_dev_combo.pack(side='left', padx=(8, 0))
            if len(self._mic_devices) > 1:
                self._mic_dev_combo.configure(state='readonly')
            else:
                self._mic_dev_combo.configure(state='disabled')
        else:
            self._mic_dev_combo.pack_forget()
        # Re-center after layout change
        self._center(self._parent)

    def _on_src_change(self) -> None:
        src = self._src_var.get()
        if src == 'window':
            self._windows = list_windows()
            titles = [t for _, t in self._windows]
            self._win_combo['values'] = titles
            if titles:
                self._win_var.set(titles[0])
            self._win_combo.configure(state='readonly')
        else:
            self._win_combo.configure(state='disabled')

        if src == 'region':
            self._region_btn.configure(state='normal', fg=TEXT_P)
        else:
            self._region_btn.configure(state='disabled', fg=TEXT_S)

        # Disable Start if region mode but no region selected yet
        self._refresh_start_btn()

    def _refresh_start_btn(self) -> None:
        if self._src_var.get() == 'region' and self._region is None:
            self._start_btn.configure(state='disabled',
                                      bg=SURF2, fg=TEXT_S, cursor='arrow')
        else:
            self._start_btn.configure(state='normal',
                                      bg=ACCENT, fg='#ffffff', cursor='hand2')

    def _pick_region(self) -> None:
        """Hide dialog, show full-screen region selector, restore dialog."""
        self.win.withdraw()
        self.win.update()

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
                text=f'{w} × {h}  at ({l}, {t})', fg=TEXT_P)
        self._refresh_start_btn()

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
            l, t, w, h = self._region
            mon = (l, t, w, h)
        # Resolve selected mic device index (None = OS default)
        mic_device = None
        if self._mic_var.get() and self._mic_devices:
            sel_label = self._mic_dev_var.get()
            for idx, lbl in self._mic_devices:
                if lbl == sel_label:
                    mic_device = idx
                    break

        self.result = {
            'hwnd':       hwnd,
            'mon':        mon,
            'mic':        self._mic_var.get(),
            'mic_device': mic_device,
            'fps':        self._fps_var.get(),
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
            # Centre over the visible parent window
            x = parent.winfo_rootx() + (pw - w) // 2
            y = parent.winfo_rooty() + (ph - h) // 2
        else:
            # Parent is withdrawn/hidden — centre on screen instead
            x = (sw - w) // 2
            y = (sh - h) // 2
        x = max(0, min(x, sw - w))
        y = max(0, min(y, sh - h))
        self.win.geometry(f'+{x}+{y}')


# ── Save Dialog ───────────────────────────────────────────────────────────────

def show_save_dialog(parent, tmp_path: str, dur: int, size_mb: float,
                     default_dir: str = '') -> str | None:
    """
    Show a save-as dialog for the completed recording.

    Returns the final destination path, or None if discarded.
    Moves the temp file to the chosen destination.
    Caller must not rely on tmp_path after this call.
    """
    if not default_dir:
        from storage import appdata_dir
        default_dir = str(Path(appdata_dir()) / RECORDINGS_DIR_NAME)
    os.makedirs(default_dir, exist_ok=True)

    import datetime
    ts   = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    name = f'recording_{ts}.mp4'

    dest = filedialog.asksaveasfilename(
        parent=parent,
        defaultextension='.mp4',
        filetypes=[('MP4 video', '*.mp4'), ('All files', '*.*')],
        title='Save recording as…',
        initialdir=default_dir,
        initialfile=name,
    )
    if not dest:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return None

    try:
        shutil.move(tmp_path, dest)
    except Exception as exc:
        from dialogs import alert
        alert(parent, 'Save failed', str(exc))
        return None

    return dest


# ── Recordings index (tracks files saved outside the default folder) ──────────

_index_lock = threading.Lock()   # guards concurrent read-modify-write on recordings_index.json


def _index_path() -> Path:
    from storage import appdata_dir
    return Path(appdata_dir()) / 'recordings_index.json'


def _write_index_atomic(idx_file: Path, entries: list) -> None:
    """Write entries to idx_file atomically via a .tmp sibling, then os.replace()."""
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


def add_to_recordings_index(path: str) -> None:
    """Persist a saved recording path so it appears in the list regardless of location."""
    import json
    idx_file = _index_path()
    path = str(Path(path).resolve())
    with _index_lock:
        try:
            entries: list = json.loads(idx_file.read_text(encoding='utf-8')) if idx_file.exists() else []
        except Exception:
            entries = []
        if path not in entries:
            entries.append(path)
            _write_index_atomic(idx_file, entries)


def remove_from_recordings_index(path: str) -> None:
    """Remove a path from the recordings index (e.g. after deleting the file)."""
    import json
    idx_file = _index_path()
    if not idx_file.exists():
        return
    key = str(Path(path).resolve())
    with _index_lock:
        try:
            entries: list = json.loads(idx_file.read_text(encoding='utf-8'))
        except Exception:
            return
        new_entries = [e for e in entries if str(Path(e).resolve()) != key]
        if len(new_entries) != len(entries):
            _write_index_atomic(idx_file, new_entries)


def _load_index_entries() -> list[str]:
    """Return all indexed paths, pruning stale entries and saving back if any were removed."""
    import json
    idx_file = _index_path()
    if not idx_file.exists():
        return []
    with _index_lock:
        try:
            entries: list = json.loads(idx_file.read_text(encoding='utf-8'))
        except Exception:
            return []
        live = [p for p in entries if Path(p).exists()]
        if len(live) != len(entries):
            _write_index_atomic(idx_file, live)
    return live


# ── Recordings list helper ────────────────────────────────────────────────────

def list_recordings(recordings_dir: str) -> list[dict]:
    """
    Return a list of dicts for every known .mp4, sorted newest-first.
    Sources: the default recordings_dir (scanned for *.mp4) plus any paths
    persisted in the recordings index (files saved to arbitrary locations).
    Each dict has: path, name, size_mb, mtime.
    """
    seen: set[str] = set()
    candidates: list[Path] = []

    # Default folder scan
    p = Path(recordings_dir)
    if p.exists():
        for f in p.glob('*.mp4'):
            key = str(f.resolve())
            if key not in seen:
                seen.add(key)
                candidates.append(f)

    # Index entries (recordings saved elsewhere)
    for raw in _load_index_entries():
        key = str(Path(raw).resolve())
        if key not in seen:
            seen.add(key)
            candidates.append(Path(raw))

    result = []
    for f in candidates:
        try:
            stat = f.stat()
            result.append({
                'path':    str(f),
                'name':    f.name,
                'size_mb': stat.st_size / (1024 ** 2),
                'mtime':   stat.st_mtime,
            })
        except Exception:
            pass

    result.sort(key=lambda d: d['mtime'], reverse=True)
    return result
