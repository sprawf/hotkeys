"""
Themed dialogs — replaces tkinter.messagebox throughout the app.

Usage:
    from dialogs import alert, confirm

    alert(parent, 'Title', 'Message text')

    if confirm(parent, 'Delete?', 'This cannot be undone.',
               action_label='Delete', action_color='#b03030'):
        ...
"""
import tkinter as tk
import customtkinter as ctk

from theme import (
    BG, SURFACE, SURF2, SURF3,
    ACCENT, ACCENTL, TEXT_P, TEXT_S,
    FONT_FAMILY, PAD, PAD_SM, RADIUS_SM,
)


class ThemedDialog(ctk.CTkToplevel):
    """
    Minimal dark-themed dialog.

    mode='alert'   — single OK button; result is always False
    mode='confirm' — Cancel + coloured action button; result True on confirm
    """

    def __init__(self, parent, title: str, message: str,
                 mode: str = 'alert',
                 action_label: str = 'OK',
                 action_color: str = ACCENT,
                 action_hover: str = ACCENTL) -> None:
        super().__init__(parent)
        self.withdraw()          # hide until positioned — avoids top-left flash
        self.title(title)
        self.configure(fg_color=BG)
        self.resizable(False, False)
        self.result = False

        # ── Header ────────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        hdr.pack(fill='x')
        ctk.CTkLabel(hdr, text=title,
                     font=(FONT_FAMILY, 14, 'bold'),
                     text_color=TEXT_P).pack(anchor='w', padx=PAD, pady=PAD_SM)

        # ── Body ──────────────────────────────────────────────────────────────
        ctk.CTkLabel(self, text=message,
                     font=(FONT_FAMILY, 13), text_color=TEXT_S,
                     wraplength=320, justify='left').pack(
                         padx=PAD, pady=PAD)

        # ── Footer ────────────────────────────────────────────────────────────
        foot = ctk.CTkFrame(self, fg_color=SURFACE, corner_radius=0)
        foot.pack(fill='x')

        def _btn(text, cmd, fg, hover):
            return ctk.CTkButton(
                foot, text=text, command=cmd, width=80,
                fg_color=fg, hover_color=hover,
                text_color=TEXT_P, corner_radius=RADIUS_SM,
                font=(FONT_FAMILY, 13),
            )

        if mode == 'confirm':
            _btn(action_label, self._confirm,
                 action_color, action_hover).pack(
                     side='right', padx=PAD, pady=PAD_SM)
            _btn('Cancel', self.destroy,
                 SURF2, SURF3).pack(side='right', pady=PAD_SM)
        else:
            _btn('OK', self.destroy,
                 ACCENT, ACCENTL).pack(side='right', padx=PAD, pady=PAD_SM)

        # ── Keyboard shortcuts ────────────────────────────────────────────────
        self.bind('<Escape>', lambda e: self.destroy())
        self.bind('<Return>',
                  lambda e: (self._confirm() if mode == 'confirm' else self.destroy()))

        self.after(50, lambda: self._show(parent))

    def _confirm(self) -> None:
        self.result = True
        self.destroy()

    def _show(self, parent) -> None:
        center_over_parent(self, parent)
        self.deiconify()
        self.grab_set()


# ── Shared geometry helper ────────────────────────────────────────────────────

def center_over_parent(dialog, parent) -> None:
    """Position *dialog* centered over *parent* widget, or screen if parent is hidden."""
    dialog.update_idletasks()
    try:
        # winfo_width/height gives actual rendered size after update_idletasks;
        # fall back to reqwidth/reqheight if the window hasn't been mapped yet.
        w = dialog.winfo_width()
        h = dialog.winfo_height()
        if w <= 1:
            w = dialog.winfo_reqwidth()
            h = dialog.winfo_reqheight()
        sw = dialog.winfo_screenwidth()
        sh = dialog.winfo_screenheight()
        # If parent is withdrawn/iconified fall back to screen centre.
        # A withdrawn CTkToplevel still reports width=200 so check ismapped() too.
        pw, ph = parent.winfo_width(), parent.winfo_height()
        if pw <= 1 or ph <= 1 or not parent.winfo_ismapped():
            x = (sw - w) // 2
            y = (sh - h) // 2
        else:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            x = px + (pw - w) // 2
            y = py + (ph - h) // 2
        # Clamp to screen bounds
        x = max(0, min(x, sw - w))
        y = max(0, min(y, sh - h))
        dialog.geometry(f'+{x}+{y}')
    except Exception:
        pass


# ── Convenience wrappers ──────────────────────────────────────────────────────

def alert(parent, title: str, message: str) -> None:
    """Show a themed alert and block until dismissed."""
    dlg = ThemedDialog(parent, title, message, mode='alert')
    parent.wait_window(dlg)


def confirm(parent, title: str, message: str,
            action_label: str = 'OK',
            action_color: str = ACCENT,
            action_hover: str = ACCENTL) -> bool:
    """Show a themed confirm dialog; return True if the action button was clicked."""
    dlg = ThemedDialog(parent, title, message, mode='confirm',
                       action_label=action_label,
                       action_color=action_color,
                       action_hover=action_hover)
    parent.wait_window(dlg)
    return dlg.result


# ── PopupMenu ────────────────────────────────────────────────────────────────

class PopupMenu:
    """Lightweight custom popup menu styled to match the app's dark theme.

    Usage:
        m = PopupMenu(parent_window)
        m.add('Cut',          cmd_cut,   enabled=has_sel)
        m.add('Copy',         cmd_copy,  enabled=has_sel)
        m.add('Paste',        cmd_paste)
        m.separator()
        m.add('Paste Image',  cmd_ocr)
        m.show(event.x_root, event.y_root)
    """

    _BG       = '#1c1c1c'
    _BORDER   = '#333333'
    _TEXT     = '#e8e8e8'
    _DIM      = '#484848'
    _HOVER_BG = ACCENT        # purple highlight — same as rest of app
    _HOVER_FG = '#ffffff'
    _SEP      = '#2a2a2a'
    _FONT     = (FONT_FAMILY, 11)
    _PAD_X    = 14
    _ITEM_PY  = 5
    _MIN_W    = 140

    def __init__(self, parent) -> None:
        self._parent = parent
        self._items: list = []
        self._win: tk.Toplevel | None = None
        self._alive = [False]

    def add(self, label: str, command, enabled: bool = True) -> 'PopupMenu':
        self._items.append(('item', label, command, enabled))
        return self

    def separator(self) -> 'PopupMenu':
        self._items.append(('sep',))
        return self

    def show(self, x: int, y: int) -> None:
        win = tk.Toplevel(self._parent)
        win.overrideredirect(True)
        win.attributes('-topmost', True)
        win.configure(bg=self._BORDER)   # 1 px border via outer bg colour

        inner = tk.Frame(win, bg=self._BG, bd=0)
        inner.pack(padx=1, pady=1)

        self._win   = win
        self._alive = [True]

        for item in self._items:
            if item[0] == 'sep':
                tk.Frame(inner, bg=self._SEP, height=1).pack(
                    fill='x', padx=6, pady=3)
            else:
                _, label, cmd, enabled = item
                fg  = self._TEXT if enabled else self._DIM
                lbl = tk.Label(
                    inner,
                    text=f'  {label}',
                    bg=self._BG, fg=fg,
                    font=self._FONT,
                    anchor='w',
                    padx=self._PAD_X,
                    pady=self._ITEM_PY,
                    cursor='arrow',
                )
                lbl.pack(fill='x')

                if enabled:
                    lbl.bind('<Enter>',
                             lambda e, w=lbl: w.configure(
                                 bg=self._HOVER_BG, fg=self._HOVER_FG))
                    lbl.bind('<Leave>',
                             lambda e, w=lbl: w.configure(
                                 bg=self._BG, fg=self._TEXT))
                    # return 'break' stops the event propagating to win's
                    # <ButtonRelease-1> handler so only the item fires
                    def _on_item_click(e, c=cmd):
                        self._dismiss()
                        c()
                        return 'break'
                    lbl.bind('<ButtonRelease-1>', _on_item_click)

        # Keep menu within screen bounds
        win.update_idletasks()
        mw = win.winfo_reqwidth()
        mh = win.winfo_reqheight()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry(f'+{min(x, sw - mw - 4)}+{min(y, sh - mh - 4)}')

        # grab_set() routes all in-app mouse events to this window so any
        # click outside the menu (on another widget) is redirected here and
        # triggers dismissal via the <ButtonRelease-1> binding on win.
        # This is far more reliable than <FocusOut> which fires immediately
        # on overrideredirect windows and kills the menu before it's seen.
        try:
            win.grab_set()
        except Exception:
            pass
        # Click on win background or redirected outside click → dismiss.
        # <ButtonRelease-3> is intentionally NOT bound here: grab_set routes
        # the release of the right-click that opened this menu to win, which
        # would immediately kill it before the user sees anything.
        win.bind('<ButtonRelease-1>', lambda e: self._dismiss())
        win.bind('<Escape>',          lambda e: self._dismiss())
        # Fallback: clicking outside the whole app (another application's
        # window) doesn't trigger grab_set, but does cause FocusOut.
        win.bind('<FocusOut>', lambda e: win.after(150, self._dismiss))
        win.focus_force()

    def _dismiss(self) -> None:
        if not self._alive[0]:
            return
        self._alive[0] = False
        try:
            self._win.grab_release()
        except Exception:
            pass
        try:
            self._win.destroy()
        except Exception:
            pass


# ── Tooltip ───────────────────────────────────────────────────────────────────

class Tooltip:
    """Hover tooltip for any tkinter or CustomTkinter widget.

    Usage:
        Tooltip(widget, 'Explain what the widget does')
    """

    def __init__(self, widget, text: str, delay: int = 450) -> None:
        self._widget = widget
        self._text   = text
        self._delay  = delay
        self._job    = None
        self._win    = None
        widget.bind('<Enter>',  self._on_enter, add='+')
        widget.bind('<Leave>',  self._on_leave, add='+')
        widget.bind('<Button>', self._on_leave, add='+')

    def _on_enter(self, event=None) -> None:
        self._cancel()
        self._job = self._widget.after(self._delay, self._show)

    def _on_leave(self, event=None) -> None:
        self._cancel()
        self._hide()

    def _cancel(self) -> None:
        if self._job:
            try:
                self._widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _show(self) -> None:
        if self._win:
            return
        try:
            wx = self._widget.winfo_rootx()
            wy = self._widget.winfo_rooty()
            wh = self._widget.winfo_height()
        except Exception:
            return
        self._win = tk.Toplevel(self._widget)
        self._win.overrideredirect(True)
        self._win.attributes('-topmost', True)
        lbl = tk.Label(
            self._win,
            text=self._text,
            bg=SURF2, fg=TEXT_P,
            font=(FONT_FAMILY, 11),
            padx=10, pady=6,
            justify='left',
            relief='flat',
        )
        lbl.pack()
        # Position below the widget, horizontally centred on it
        self._win.update_idletasks()
        tw = self._win.winfo_reqwidth()
        ww = self._widget.winfo_width()
        x  = wx + max(0, (ww - tw) // 2)
        y  = wy + wh + 4
        self._win.geometry(f'+{x}+{y}')

    def _hide(self) -> None:
        if self._win:
            try:
                self._win.destroy()
            except Exception:
                pass
            self._win = None
