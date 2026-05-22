"""
Themed dialogs — replaces tkinter.messagebox throughout the app.

Usage:
    from dialogs import alert, confirm

    alert(parent, 'Title', 'Message text')

    if confirm(parent, 'Delete?', 'This cannot be undone.',
               action_label='Delete', action_color='#b03030'):
        ...
"""
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
        self.title(title)
        self.configure(fg_color=BG)
        self.resizable(False, False)
        self.grab_set()
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

        self._center(parent)

    def _confirm(self) -> None:
        self.result = True
        self.destroy()

    def _center(self, parent) -> None:
        center_over_parent(self, parent)


# ── Shared geometry helper ────────────────────────────────────────────────────

def center_over_parent(dialog, parent) -> None:
    """Position *dialog* centered over *parent* widget, or screen if parent is hidden."""
    dialog.update_idletasks()
    try:
        w, h = dialog.winfo_reqwidth(), dialog.winfo_reqheight()
        # If parent is withdrawn/iconified its dimensions are 1x1 at 0,0 — fall back to screen centre
        pw, ph = parent.winfo_width(), parent.winfo_height()
        if pw <= 1 or ph <= 1:
            sw = dialog.winfo_screenwidth()
            sh = dialog.winfo_screenheight()
            dialog.geometry(f'+{(sw - w) // 2}+{(sh - h) // 2}')
        else:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            dialog.geometry(f'+{px + (pw - w) // 2}+{py + (ph - h) // 2}')
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
