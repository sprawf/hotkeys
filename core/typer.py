import sys

# ── Windows ───────────────────────────────────────────────────────────────────

if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    KEYEVENTF_KEYUP   = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    INPUT_KEYBOARD    = 1

    VK_CONTROL = 0x11
    VK_V       = 0x56

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ('wVk',         wintypes.WORD),
            ('wScan',       wintypes.WORD),
            ('dwFlags',     wintypes.DWORD),
            ('time',        wintypes.DWORD),
            ('dwExtraInfo', ctypes.c_ulonglong),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [
            ('ki',   KEYBDINPUT),
            ('_pad', ctypes.c_byte * 32),
        ]

    class INPUT(ctypes.Structure):
        _anonymous_ = ('u',)
        _fields_ = [('type', wintypes.DWORD), ('u', _INPUT_UNION)]

    def _make_key_input(vk, scan, flags):
        ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.ki = ki
        return inp

    def _send_inputs(inputs):
        arr = (INPUT * len(inputs))(*inputs)
        user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

    def copy_to_clipboard(text: str):
        """Copy text to the Windows clipboard."""
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            win32clipboard.CloseClipboard()
        except Exception:
            try:
                CF_UNICODETEXT = 13
                data = (text + '\0').encode('utf-16-le')
                hMem = ctypes.windll.kernel32.GlobalAlloc(0x0042, len(data))
                pMem = ctypes.windll.kernel32.GlobalLock(hMem)
                ctypes.memmove(pMem, data, len(data))
                ctypes.windll.kernel32.GlobalUnlock(hMem)
                if user32.OpenClipboard(None):
                    user32.EmptyClipboard()
                    user32.SetClipboardData(CF_UNICODETEXT, hMem)
                    user32.CloseClipboard()
            except Exception:
                pass

    def paste_from_clipboard():
        """Simulate Ctrl+V to paste the current clipboard into the focused window."""
        inputs = [
            _make_key_input(VK_CONTROL, 0, 0),
            _make_key_input(VK_V,       0, 0),
            _make_key_input(VK_V,       0, KEYEVENTF_KEYUP),
            _make_key_input(VK_CONTROL, 0, KEYEVENTF_KEYUP),
        ]
        arr = (INPUT * len(inputs))(*inputs)
        user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

# ── macOS / Linux ─────────────────────────────────────────────────────────────

else:
    import subprocess

    def copy_to_clipboard(text: str):
        """Copy text to the macOS clipboard via pbcopy."""
        try:
            subprocess.run(['pbcopy'], input=text.encode('utf-8'), check=True)
        except Exception:
            try:
                import pyperclip
                pyperclip.copy(text)
            except Exception:
                pass

    def paste_from_clipboard():
        """Simulate Cmd+V on macOS via osascript."""
        try:
            subprocess.run(
                ['osascript', '-e',
                 'tell application "System Events" to keystroke "v" using command down'],
                check=True,
            )
        except Exception:
            try:
                import keyboard
                keyboard.send('command+v')
            except Exception:
                pass
