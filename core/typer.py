import sys

# Suspend any active macro recording while we inject keystrokes on the user's
# behalf, otherwise the injection (Ctrl+C / Ctrl+V / Ctrl+Z) gets baked into
# the macro and replayed as garbage. Import is best-effort so this module
# stays usable in contexts where the macro package isn't available.
try:
    from macros.recorder import suspend_capture as _suspend_macro_capture
except Exception:
    from contextlib import contextmanager
    @contextmanager
    def _suspend_macro_capture():
        yield


# ── Windows ───────────────────────────────────────────────────────────────────

if sys.platform == 'win32':
    import ctypes
    import threading
    import time
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    KEYEVENTF_KEYUP   = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    INPUT_KEYBOARD    = 1

    VK_CONTROL = 0x11
    VK_C       = 0x43
    VK_V       = 0x56
    VK_Z       = 0x5A

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
        """Copy text to the Windows clipboard.

        Defensive against the (rare but severe) failure mode where
        OpenClipboard succeeds but Empty/SetClipboardText raises:
        without a try/finally we'd leave the clipboard OPENED, holding
        a global lock that makes every other app's clipboard ops fail
        until Hotkeys exits. Same defence on the ctypes fallback: free
        hMem on every path so we don't leak global heap."""
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(
                    text, win32clipboard.CF_UNICODETEXT)
            finally:
                try: win32clipboard.CloseClipboard()
                except Exception: pass
        except Exception:
            CF_UNICODETEXT = 13
            hMem = None
            opened = False
            try:
                data = (text + '\0').encode('utf-16-le')
                hMem = ctypes.windll.kernel32.GlobalAlloc(0x0042, len(data))
                if not hMem:
                    return
                pMem = ctypes.windll.kernel32.GlobalLock(hMem)
                ctypes.memmove(pMem, data, len(data))
                ctypes.windll.kernel32.GlobalUnlock(hMem)
                if user32.OpenClipboard(None):
                    opened = True
                    user32.EmptyClipboard()
                    if user32.SetClipboardData(CF_UNICODETEXT, hMem):
                        # Clipboard now owns hMem; do NOT free it.
                        hMem = None
            except Exception:
                pass
            finally:
                if opened:
                    try: user32.CloseClipboard()
                    except Exception: pass
                if hMem is not None:
                    # We allocated but ownership wasn't taken; free it.
                    try: ctypes.windll.kernel32.GlobalFree(hMem)
                    except Exception: pass

    def copy_selection():
        """Simulate Ctrl+C to copy the current selection into the clipboard."""
        with _suspend_macro_capture():
            inputs = [
                _make_key_input(VK_CONTROL, 0, 0),
                _make_key_input(VK_C,       0, 0),
                _make_key_input(VK_C,       0, KEYEVENTF_KEYUP),
                _make_key_input(VK_CONTROL, 0, KEYEVENTF_KEYUP),
            ]
            arr = (INPUT * len(inputs))(*inputs)
            user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

    def paste_from_clipboard():
        """Simulate Ctrl+V to paste the current clipboard into the focused window."""
        with _suspend_macro_capture():
            inputs = [
                _make_key_input(VK_CONTROL, 0, 0),
                _make_key_input(VK_V,       0, 0),
                _make_key_input(VK_V,       0, KEYEVENTF_KEYUP),
                _make_key_input(VK_CONTROL, 0, KEYEVENTF_KEYUP),
            ]
            _send_inputs(inputs)

    def undo_last():
        """Simulate Ctrl+Z via Win32 SendInput, avoids routing through the
        keyboard library's hook, which prevents modifier-state corruption."""
        with _suspend_macro_capture():
            inputs = [
                _make_key_input(VK_CONTROL, 0, 0),
                _make_key_input(VK_Z,       0, 0),
                _make_key_input(VK_Z,       0, KEYEVENTF_KEYUP),
                _make_key_input(VK_CONTROL, 0, KEYEVENTF_KEYUP),
            ]
            _send_inputs(inputs)

    # ── Editable-target detection + notepad fallback ─────────────────────────

    class _GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ('cbSize',        wintypes.DWORD),
            ('flags',         wintypes.DWORD),
            ('hwndActive',    wintypes.HWND),
            ('hwndFocus',     wintypes.HWND),
            ('hwndCapture',   wintypes.HWND),
            ('hwndMenuOwner', wintypes.HWND),
            ('hwndMoveSize',  wintypes.HWND),
            ('hwndCaret',     wintypes.HWND),
            ('rcCaret',       wintypes.RECT),
        ]

    def _win32_caret_present() -> bool:
        """True iff the foreground window's GUI thread has a Win32 caret.
        Reliable for classic apps (Notepad, Word, Explorer rename box) but
        always False for Chromium/Electron apps, which draw their own caret."""
        try:
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False
            tid = user32.GetWindowThreadProcessId(hwnd, None)
            gti = _GUITHREADINFO()
            gti.cbSize = ctypes.sizeof(_GUITHREADINFO)
            if not user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
                return False
            return bool(gti.hwndCaret)
        except Exception:
            return False

    # UIA control types
    _UIA_EDIT, _UIA_COMBOBOX, _UIA_GROUP, _UIA_DOCUMENT = 50004, 50003, 50026, 50030
    # Focused control types that can never receive pasted text. Deliberately
    # NOT exhaustive: anything unlisted is treated as unknown -> paste.
    _UIA_NEVER_EDITABLE = {
        50000,  # Button
        50001,  # Calendar
        50002,  # CheckBox
        50005,  # Hyperlink
        50006,  # Image
        50007,  # ListItem
        50008,  # List
        50011,  # MenuItem
        50012,  # ProgressBar
        50013,  # RadioButton
        50015,  # Slider
        50018,  # TabItem
        50020,  # Text (static)
        50023,  # Tree
        50024,  # TreeItem
        50032,  # Window
        50033,  # Pane
        50030,  # Document (browser read mode / PDF; Word has a Win32 caret
                #           so it is caught by the fast path before this)
    }
    _uia_tls = threading.local()
    _uia_seen_pids: set = set()   # processes whose a11y tree we already woke

    def _uia_focused_editable():
        """Three-state UIA verdict on the focused element.

        True  = definitely editable (paste will land)
        False = confidently NOT editable (notepad fallback is right)
        None  = unknown -> caller must fail open to paste

        Needed because Chromium/Electron apps (Claude, Chrome, Discord,
        VS Code...) never create a Win32 caret; UIA is the only API that
        sees their text inputs. Observed mappings: <input>/<textarea>/
        role=textbox -> Edit; bare contenteditable (Claude's prompt) ->
        Group with TextPattern; read-mode page body -> Document.
        """
        try:
            import comtypes
            import comtypes.client
            # CoInitialize once per thread (lives behind a TLS flag).
            # Subsequent calls return S_FALSE but still bump COM's
            # ref count, so guarding avoids needless churn.
            if not getattr(_uia_tls, 'co_initialized', False):
                try: comtypes.CoInitialize()
                except Exception: pass
                _uia_tls.co_initialized = True
            uia = getattr(_uia_tls, 'uia', None)
            if uia is None:
                comtypes.client.GetModule('UIAutomationCore.dll')
                from comtypes.gen.UIAutomationClient import (
                    CUIAutomation, IUIAutomation)
                uia = comtypes.client.CreateObject(
                    CUIAutomation, interface=IUIAutomation)
                # Cache request: fetch every property we need in ONE
                # cross-process round trip instead of one per property.
                req = uia.CreateCacheRequest()
                for pid in (30003,   # ControlType
                            30040,   # IsTextPatternAvailable
                            30043,   # IsValuePatternAvailable
                            30046,   # Value.IsReadOnly
                            30095,   # LegacyIAccessible.Role
                            30096,   # LegacyIAccessible.State
                            30101,   # AriaRole
                            30115):  # IsTextEditPatternAvailable — the
                                     # cleanest editable-vs-readonly signal
                                     # for Chromium/Electron. Read-only
                                     # text (Claude message history, VS
                                     # Code output pane, Discord scroll)
                                     # exposes only TextPattern (30040);
                                     # editable text (input, textarea,
                                     # contenteditable=true) exposes both
                                     # TextPattern AND TextEditPattern.
                    req.AddProperty(pid)
                _uia_tls.uia = uia
                _uia_tls.req = req
            el = uia.GetFocusedElementBuildCache(_uia_tls.req)
            if el is None:
                return None
            ct = int(el.GetCachedPropertyValue(30003) or 0)    # ControlType
            if ct in (_UIA_EDIT, _UIA_COMBOBOX):
                return True
            # Writable ValuePattern = editable regardless of control type
            if (bool(el.GetCachedPropertyValue(30043))         # ValuePattern avail
                    and not el.GetCachedPropertyValue(30046)):   # not read-only
                return True
            aria = str(el.GetCachedPropertyValue(30101) or '').lower()
            if aria in ('textbox', 'searchbox', 'combobox'):
                return True
            legacy_role  = int(el.GetCachedPropertyValue(30095) or 0)
            legacy_state = int(el.GetCachedPropertyValue(30096) or 0)
            if legacy_role == 42 and not (legacy_state & 0x40):  # TEXT, not RO
                return True
            if ct == _UIA_GROUP:
                # Chromium exposes bare contenteditable as Group+TextPattern.
                # Prefer TextEditPattern (30115) as a strong "definitely
                # editable" signal, but fall back to TextPattern because
                # many rich-text editors (ProseMirror in Claude Code, some
                # Slack/Discord inputs) don't expose TextEditPattern even
                # though they ARE editable. Post-verify will catch the
                # false-positive case (read-only message area with
                # TextPattern) via caret/UIA-delta measurement.
                if bool(el.GetCachedPropertyValue(30115)):     # TextEditPattern
                    return True
                if bool(el.GetCachedPropertyValue(30040)):     # TextPattern only
                    return None   # unknown — let post-verify decide
                return None
            if ct in _UIA_NEVER_EDITABLE:
                return False
            return None
        except Exception:
            return None

    def focused_text_snapshot() -> str | None:
        """Read the focused element's current text content via UIA.

        Returns a string if the element exposes a ValuePattern
        (CurrentValue) or a TextPattern (DocumentRange.GetText), else
        None. The caller uses this for POST-paste verification: snapshot
        before Ctrl+V, snapshot again ~300 ms after, and if both
        snapshots are equal the paste didn't visibly land — fall back
        to opening MiniNotepad with the text.

        Truncated at 64 kB so we don't haul a whole novel across COM
        for what's a delta check.
        """
        try:
            import comtypes
            import comtypes.client
            if not getattr(_uia_tls, 'co_initialized', False):
                try: comtypes.CoInitialize()
                except Exception: pass
                _uia_tls.co_initialized = True
            uia = getattr(_uia_tls, 'uia', None)
            if uia is None:
                comtypes.client.GetModule('UIAutomationCore.dll')
                from comtypes.gen.UIAutomationClient import (
                    CUIAutomation, IUIAutomation)
                uia = comtypes.client.CreateObject(
                    CUIAutomation, interface=IUIAutomation)
                _uia_tls.uia = uia
            el = uia.GetFocusedElement()
            if el is None:
                return None
            # ValuePattern (10002): cheapest, covers classic Edit,
            # browser <input>, WhatsApp's chat input, address bars.
            try:
                vp = el.GetCurrentPattern(10002)
                if vp:
                    from comtypes.gen.UIAutomationClient import (
                        IUIAutomationValuePattern)
                    vp = vp.QueryInterface(IUIAutomationValuePattern)
                    v = vp.CurrentValue
                    if v is not None:
                        return v
            except Exception:
                pass
            # TextPattern (10014): contenteditable in Chromium / Word body.
            try:
                tp = el.GetCurrentPattern(10014)
                if tp:
                    from comtypes.gen.UIAutomationClient import (
                        IUIAutomationTextPattern)
                    tp = tp.QueryInterface(IUIAutomationTextPattern)
                    rng = tp.DocumentRange
                    if rng is not None:
                        return rng.GetText(65536)
            except Exception:
                pass
            return None
        except Exception:
            return None

    def has_editable_focus_in_foreground() -> bool:
        """Heuristic: True iff an editable text input has keyboard focus in
        the foreground window, i.e. a Ctrl+V paste will actually land.
        Used by Refine / Chain to decide paste vs notepad fallback.

        Fail-open by design: only a *confident* UIA "not editable" verdict
        returns False (browser read mode, PDF viewer, image viewer...).
        Any doubt -> True, so the historical paste behavior is never
        stolen by the notepad fallback.
        """
        if _win32_caret_present():
            return True
        verdict = _uia_focused_editable()
        if verdict is False:
            # Chromium builds its accessibility tree lazily on the FIRST
            # UIA query against a process, so a first-look "not editable"
            # can be stale. Re-ask once after a beat, but only the first
            # time we meet this process; afterwards its tree is warm and
            # the verdict is trustworthy immediately.
            try:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(
                    user32.GetForegroundWindow(), ctypes.byref(pid))
                fresh = pid.value not in _uia_seen_pids
                _uia_seen_pids.add(pid.value)
            except Exception:
                fresh = True
            if fresh:
                time.sleep(0.15)
                verdict = _uia_focused_editable()
            if verdict is False:
                return False
        return True

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

    def copy_selection():
        """Simulate Cmd+C on macOS via osascript."""
        with _suspend_macro_capture():
            try:
                subprocess.run(
                    ['osascript', '-e',
                     'tell application "System Events" to keystroke "c" using command down'],
                    check=True,
                )
            except Exception:
                try:
                    import keyboard
                    keyboard.send('command+c')
                except Exception:
                    pass

    def paste_from_clipboard():
        """Simulate Cmd+V on macOS via osascript."""
        with _suspend_macro_capture():
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

    def undo_last():
        """Simulate Cmd+Z on macOS via osascript."""
        with _suspend_macro_capture():
            try:
                subprocess.run(
                    ['osascript', '-e',
                     'tell application "System Events" to keystroke "z" using command down'],
                    check=True,
                )
            except Exception:
                try:
                    import keyboard
                    keyboard.send('command+z')
                except Exception:
                    pass

    def has_editable_focus_in_foreground() -> bool:
        # No cross-platform caret API on macOS/Linux — assume editable so
        # the existing Ctrl+V path runs (matches prior behavior).
        return True
