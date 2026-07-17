"""ask_docs — Ask Docs, Hotkeys' NotebookLM-style document Q&A subpackage.

Entry point when embedded in Hotkeys:
    from ask_docs.ui import AskDocsWindow
    win = AskDocsWindow(root)   # pass Hotkeys' Tk root

Internal modules use RELATIVE imports (`from . import storage`) so we
never collide with Hotkeys' top-level `storage.py` / `engine.py`. Cross-
imports of Hotkeys' own modules (vision, engine, storage) are plain
absolute imports and resolve to the Hotkeys app package because ask_docs
is always run inside Hotkeys — the standalone `ask_docs_main.py`
launcher is dev-only.
"""
