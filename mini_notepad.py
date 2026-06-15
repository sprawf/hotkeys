"""Minimal Notepad-like Toplevel used as a paste fallback when no editable
text input has focus (browser read mode, PDF viewer, image, etc.).

Matches the user's actual Notepad look: font family/size/weight are read from
HKCU/Software/Microsoft/Notepad (the same values Notepad persists from its
Format > Font dialog), falling back to Notepad's factory default Consolas 11pt.
White bg, black text, word wrap, no menu bar, no toolbar. The whole UI is a
single Text widget.

Optimised for sub-second perceived open time:
- A single instance is created hidden at app boot (_prewarm_mini_notepad)
- show_text(text) reuses that instance: deiconify + replace text + focus
- Falls back to a fresh Toplevel only if the prewarmed one was destroyed
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import tempfile
import tkinter as tk


_WIDTH    = 800
_HEIGHT   = 500
_BG       = '#ffffff'
_FG       = '#000000'


# Win32 signatures for the right-click popup menu. Configured once at
# module import so each TrackPopupMenu invocation doesn't reset them.
# restype = c_void_p is load-bearing on 64-bit: without it HMENU/HWND
# results truncate to 32-bit and the wrong handles get passed back in.
if sys.platform == 'win32':
    _u32 = ctypes.windll.user32
    _u32.CreatePopupMenu.restype  = ctypes.c_void_p
    _u32.GetParent.restype        = ctypes.c_void_p
    _u32.GetParent.argtypes       = (ctypes.c_void_p,)
    _u32.GetForegroundWindow.restype = ctypes.c_void_p
    _u32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
    _u32.AppendMenuW.argtypes = (ctypes.c_void_p, ctypes.c_uint,
                                 ctypes.c_size_t, ctypes.c_wchar_p)
    _u32.TrackPopupMenu.argtypes = (ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_void_p,
                                    ctypes.c_void_p)
    _u32.DestroyMenu.argtypes = (ctypes.c_void_p,)
    _u32.PostMessageW.argtypes = (ctypes.c_void_p, ctypes.c_uint,
                                  ctypes.c_size_t, ctypes.c_ssize_t)


def _notepad_font():
    """Font tuple matching the user's Windows Notepad (Format > Font).

    Notepad persists its font in HKCU\\Software\\Microsoft\\Notepad:
    lfFaceName (family), iPointSize (tenths of a point), lfWeight
    (LOGFONT weight, 700+ means bold). Missing keys or any registry
    error fall back to Notepad's factory default, Consolas 11pt.
    """
    family, size, weight = 'Consolas', 11, 400
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Notepad') as k:
            try:
                # Notepad stores lfFaceName as REG_SZ. Be defensive
                # against a hand-edited REG_BINARY: bytes here would
                # blow up Tk later with a cryptic font error.
                val = winreg.QueryValueEx(k, 'lfFaceName')[0]
                if isinstance(val, str) and val:
                    family = val
            except OSError:
                pass
            try:
                size = max(1, int(winreg.QueryValueEx(k, 'iPointSize')[0]) // 10)
            except OSError:
                pass
            try:
                weight = int(winreg.QueryValueEx(k, 'lfWeight')[0])
            except OSError:
                pass
    except Exception:
        pass
    return (family, size, 'bold') if weight >= 700 else (family, size)


_FONT = _notepad_font()


def _blank_ico_path() -> str:
    """Path to a fully transparent 16x16 32-bit .ico, generated once into
    %TEMP%. Used to hide the title bar icon (Tk always reserves the icon
    slot; a transparent icon is the only way to make it look empty)."""
    path = os.path.join(tempfile.gettempdir(), 'hotkeys_blank_icon.ico')
    if not os.path.exists(path):
        xor = b'\x00' * (16 * 16 * 4)          # BGRA, alpha 0 everywhere
        and_mask = b'\xff' * (16 * 4)          # 16 rows, padded to 32 bits
        bmp = struct.pack('<IiiHHIIiiII', 40, 16, 32, 1, 32,
                          0, 0, 0, 0, 0, 0) + xor + and_mask
        ico = (struct.pack('<HHH', 0, 1, 1)
               + struct.pack('<BBBBHHII', 16, 16, 0, 0, 1, 32, len(bmp), 22)
               + bmp)
        with open(path, 'wb') as f:
            f.write(ico)
    return path


class MiniNotepad(tk.Toplevel):

    def __init__(self, parent):
        super().__init__(parent)
        self.title('')
        # Center on the primary screen. `winfo_screenwidth/height` is
        # read at __init__ time only, so if the user later drags the
        # window, show_text leaves it where they put it.
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x, y = max(0, (sw - _WIDTH) // 2), max(0, (sh - _HEIGHT) // 2)
        self.geometry(f'{_WIDTH}x{_HEIGHT}+{x}+{y}')
        self.configure(bg=_BG)
        # Blank title + transparent icon = minimal chrome. iconbitmap
        # per-window overrides the app-wide brand icon installed via
        # root.iconbitmap(default=...) in main._start_tray.
        try:
            self.iconbitmap(_blank_ico_path())
        except Exception:
            pass
        self._txt = tk.Text(
            self, font=_FONT, bg=_BG, fg=_FG,
            insertbackground=_FG, wrap='word',
            undo=True, maxundo=200,
            bd=0, padx=8, pady=6, highlightthickness=0,
        )
        self._txt.pack(fill='both', expand=True)
        self.bind('<Control-s>', self._save)
        self.bind('<Control-S>', self._save)
        self.bind('<Escape>',   lambda e: self.withdraw())
        self.protocol('WM_DELETE_WINDOW', self.withdraw)
        self._build_context_menu()
        # Track pending `after` ids so destroy doesn't leave callbacks
        # that fire after the widget is gone ("invalid command name").
        self._after_ids: list[str] = []

    # ── Standard edit context menu (right click / Shift+F10 / menu key) ──
    #
    # Native Win32 TrackPopupMenu instead of tk.Menu.tk_popup: a Tk popup
    # whose owner is not the foreground window never receives the outside
    # click and stays stuck on screen on top of every app. The native call
    # with the documented SetForegroundWindow + WM_NULL recipe makes
    # Windows itself own dismissal.

    def _build_context_menu(self):
        t = self._txt
        t.bind('<Button-3>',  self._popup_context_menu)
        t.bind('<Shift-F10>', self._popup_context_menu)
        t.bind('<App>',       self._popup_context_menu)

    @staticmethod
    def _edit_quiet(fn):
        """Run an edit op that may raise TclError when there is nothing to
        do (empty undo stack, no selection). Standard menus just no-op."""
        try:
            fn()
        except tk.TclError:
            pass

    def _select_all(self):
        t = self._txt
        t.tag_add('sel', '1.0', 'end-1c')
        t.mark_set('insert', 'end-1c')

    def _popup_context_menu(self, event):
        t = self._txt
        u32 = _u32   # module-level handle, signatures already configured
        has_sel = bool(t.tag_ranges('sel'))
        try:
            has_clip = bool(self.clipboard_get())
        except tk.TclError:
            has_clip = False

        def _can(op):
            try:
                return bool(int(t.tk.call(t._w, 'edit', op)))
            except tk.TclError:
                return True   # old Tk without canundo/canredo: leave enabled

        MF_GRAYED, MF_SEPARATOR = 0x0001, 0x0800
        menu = u32.CreatePopupMenu()

        def add(cmd_id, label, enabled=True):
            u32.AppendMenuW(menu, 0 if enabled else MF_GRAYED, cmd_id, label)

        add(1, 'Undo\tCtrl+Z', _can('canundo'))
        add(2, 'Redo\tCtrl+Y', _can('canredo'))
        u32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        add(3, 'Cut\tCtrl+X',    has_sel)
        add(4, 'Copy\tCtrl+C',   has_sel)
        add(5, 'Paste\tCtrl+V',  has_clip)
        add(6, 'Delete\tDel',    has_sel)
        u32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        add(7, 'Select All\tCtrl+A')

        # Keyboard invocation (Shift+F10 / menu key) has no mouse coords;
        # anchor at the insert cursor instead.
        if getattr(event, 'num', None) == 3:
            x, y = event.x_root, event.y_root
        else:
            bx, by, _, bh = t.bbox('insert') or (0, 0, 0, 20)
            x = t.winfo_rootx() + bx
            y = t.winfo_rooty() + by + bh

        hwnd = u32.GetParent(self.winfo_id())
        # MSDN TrackPopupMenu remarks: owner must be foreground or an
        # outside click will not dismiss the menu, and WM_NULL must be
        # posted afterwards for the next popup to behave.
        u32.SetForegroundWindow(hwnd)
        TPM_RETURNCMD, TPM_RIGHTBUTTON = 0x0100, 0x0002
        cmd = u32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                                 int(x), int(y), 0, hwnd, None)
        u32.PostMessageW(hwnd, 0x0000, 0, 0)   # WM_NULL
        u32.DestroyMenu(menu)

        if cmd == 1:
            self._edit_quiet(t.edit_undo)
        elif cmd == 2:
            self._edit_quiet(t.edit_redo)
        elif cmd == 3:
            t.event_generate('<<Cut>>')
        elif cmd == 4:
            t.event_generate('<<Copy>>')
        elif cmd == 5:
            t.event_generate('<<Paste>>')
        elif cmd == 6:
            self._edit_quiet(lambda: t.delete('sel.first', 'sel.last'))
        elif cmd == 7:
            self._select_all()
        return 'break'

    def show_text(self, text: str) -> None:
        """Replace contents with `text`, show + raise + focus the window.
        Cheap: no widget recreation, just a delete + insert.

        Cursor lands at the start (1.0): the user came here to read or
        keep the result, so showing the top is the right default. Avoids
        Tk's slow O(n) see('end') layout walk for multi-thousand-char
        inputs.
        """
        self._txt.delete('1.0', 'end')
        if text:
            self._txt.insert('1.0', text)
            self._txt.mark_set('insert', '1.0')
            self._txt.see('1.0')
        self.deiconify()
        self.lift()
        self.attributes('-topmost', True)
        self._after_ids.append(
            self.after(150, lambda: self.attributes('-topmost', False)))
        self._txt.focus_set()

    def destroy(self) -> None:
        # Cancel pending afters so test teardown (and any future explicit
        # destroy in main app) doesn't surface "invalid command name"
        # noise from callbacks fired after the widget is gone.
        for aid in getattr(self, '_after_ids', ()):
            try: self.after_cancel(aid)
            except Exception: pass
        super().destroy()

    def _save(self, _evt=None):
        from tkinter import filedialog, messagebox
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension='.txt',
            filetypes=[('Text', '*.txt'), ('All files', '*.*')],
            initialfile='Note.txt',
        )
        if not path:
            return 'break'
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(self._txt.get('1.0', 'end-1c'))
        except Exception as e:
            messagebox.showerror('Save failed', str(e), parent=self)
        return 'break'


def prewarm(parent) -> MiniNotepad:
    """Create a hidden MiniNotepad instance at app boot. Subsequent calls
    to show_text() on this instance are near-instant since the Toplevel,
    fonts, and Text widget are already realised."""
    win = MiniNotepad(parent)
    win.withdraw()
    return win
