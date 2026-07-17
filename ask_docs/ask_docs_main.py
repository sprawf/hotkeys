"""Ask Docs — standalone entry point.

Run: python E:/Hotkeys/ask_docs/ask_docs_main.py

Later, when this gets embedded into Hotkeys as Shift+F11, main.py will
import AskDocsWindow from ui.py directly and pass its existing root
(no new tk.Tk() needed). This entry-point file isn't used in that case.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make the source directory importable as the script's local "."
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _setup_logging():
    log_path = Path.home() / 'AppData' / 'Roaming' / 'Hotkeys' / 'ask_docs.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s  %(levelname)-8s  %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(log_path, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main():
    _setup_logging()
    logger = logging.getLogger(__name__)
    logger.info('Ask Docs starting…')

    import customtkinter as ctk
    # Light theme matches the NotebookLM reference UI.
    ctk.set_appearance_mode('light')
    ctk.set_default_color_theme('blue')

    from ui import AskDocsWindow
    # Hidden root; AskDocsWindow is the visible Toplevel.
    root = ctk.CTk()
    root.withdraw()
    win = AskDocsWindow(root, on_close=root.quit)
    win.lift()
    win.focus_force()

    # Pre-warm the embedding model in the background so the first time the
    # user adds a source we're not paying the 1-2s model-load cost on top
    # of the actual embedding. Daemon thread = doesn't block app shutdown.
    import threading
    def _prewarm():
        try:
            import embed
            embed.prewarm()
        except Exception as e:
            logger.warning(f'embed prewarm thread failed: {e}')
    threading.Thread(target=_prewarm, daemon=True, name='askdocs-prewarm').start()

    root.mainloop()


if __name__ == '__main__':
    main()
