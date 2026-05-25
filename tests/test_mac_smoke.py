"""
Headless macOS smoke test — verifies imports and platform guards.
Run by .github/workflows/test_mac.yml on every push.
"""
import sys, os, logging

logging.basicConfig(
    filename='smoke_test.log',
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s %(message)s',
)
log = logging.getLogger('smoke')
log.info(f'Python {sys.version}, platform={sys.platform}')

failures = []

def check(name, fn):
    try:
        fn()
        log.info(f'PASS  {name}')
        print(f'PASS  {name}')
    except Exception as e:
        log.error(f'FAIL  {name}: {e}')
        print(f'FAIL  {name}: {e}')
        failures.append(name)

# 1. Core platform-safe imports
check('import storage',      lambda: __import__('storage'))
check('import theme',        lambda: __import__('theme'))
check('import core.typer',   lambda: __import__('core.typer'))
check('import core.vad',     lambda: __import__('core.vad'))

# 2. screen_recorder imports without crashing (win32 must be conditional)
check('import screen_recorder',
      lambda: __import__('screen_recorder'))

# 3. mss available
check('import mss',          lambda: __import__('mss'))

# 4. PyAV available
check('import av',           lambda: __import__('av'))

# 5. keyboard available
check('import keyboard',     lambda: __import__('keyboard'))

# 6. pystray available
check('import pystray',      lambda: __import__('pystray'))

# 7. ScreenCapture instantiation (full screen, macOS path)
def _test_screencapture():
    from screen_recorder import ScreenCapture
    cap = ScreenCapture(hwnd=0)
    w, h = cap.size()
    assert w > 0 and h > 0, f'bad size {w}x{h}'
    frame = cap.grab()
    assert frame.shape == (h, w, 3), f'bad frame shape {frame.shape}'
    cap.close()
check('ScreenCapture full-screen grab', _test_screencapture)

# 8. list_windows (macOS path)
def _test_list_windows():
    from screen_recorder import list_windows
    wins = list_windows()
    # Should return a list (possibly empty in headless CI)
    assert isinstance(wins, list)
check('list_windows()', _test_list_windows)

# 9. core.typer macOS functions exist
def _test_typer():
    from core.typer import copy_to_clipboard, copy_selection, paste_from_clipboard, undo_last
    assert callable(copy_to_clipboard)
    assert callable(copy_selection)
check('core.typer functions', _test_typer)

# ── Result ────────────────────────────────────────────────────────────────────
if failures:
    log.error(f'FAILED: {failures}')
    print(f'\nFAILED ({len(failures)}): {failures}')
    sys.exit(1)
else:
    log.info('All checks passed.')
    print('\nAll checks passed.')
