"""Notebooks — Markdown → Tk Text widget renderer.

Used to format assistant chat messages and per-source content with NotebookLM-
style rich text: bold key terms, headings, bullets, code blocks, and inline
numbered citation chips (clickable, with a popup showing the source passage).

This is deliberately a tiny custom parser — we only support the markdown
features NotebookLM actually emits, in the order it emits them:
  • # / ## / ### headings
  • **bold**  *italic*
  • `inline code`
  • - / * bullet lists
  • [N] citation markers tied to a list of citation dicts
  • Plain paragraphs separated by blank lines

A full markdown lib (markdown-it-py, etc) would be ~3 MB and overkill — every
non-trivial element below comes straight from a NotebookLM observed answer.
"""
from __future__ import annotations

import re
import tkinter as tk
from typing import Callable


# Token pattern: match any of the inline markup primitives at once.
# Order matters — `**bold**` must be matched before `*italic*` so the bold
# stars don't get eaten as italic. Citations `[N]` are also matched here
# because they live inline within a paragraph.
_INLINE_RE = re.compile(
    r'(\*\*[^*\n]+?\*\*'      # **bold**
    r'|\*[^*\n]+?\*'          # *italic*
    r'|`[^`\n]+?`'            # `inline code`
    r'|\[\d+(?:\s*,\s*\d+)*\])'   # [1] or [1, 2] citations
)


def install_tags(tw: tk.Text, *,
                 body_font: tuple, mono_font: tuple,
                 fg: str, accent: str, accent_bg: str,
                 code_bg: str, heading_font_family: str) -> None:
    """Configure the standard formatting tags on a Tk Text widget. Call once
    per widget before insert calls. Citation chip tags are added dynamically
    in render() as needed."""
    body_size = body_font[1]
    fam = body_font[0]
    head_fam = heading_font_family

    tw.tag_config('h1',
                  font=(head_fam, body_size + 8, 'bold'),
                  foreground=fg,
                  spacing1=10, spacing3=6)
    tw.tag_config('h2',
                  font=(head_fam, body_size + 4, 'bold'),
                  foreground=fg,
                  spacing1=8, spacing3=4)
    tw.tag_config('h3',
                  font=(head_fam, body_size + 2, 'bold'),
                  foreground=fg,
                  spacing1=6, spacing3=2)

    tw.tag_config('bold',
                  font=(fam, body_size, 'bold'),
                  foreground=fg)
    tw.tag_config('italic',
                  font=(fam, body_size, 'italic'),
                  foreground=fg)
    tw.tag_config('code',
                  font=mono_font,
                  background=code_bg,
                  foreground=fg)
    # Indented bullet
    tw.tag_config('bullet',
                  lmargin1=18, lmargin2=36,
                  spacing1=2, spacing3=2)
    # Body paragraph spacing
    tw.tag_config('paragraph',
                  spacing1=4, spacing3=6, lmargin1=0, lmargin2=0)


def render(tw: tk.Text, markdown: str, *,
           citations: list[dict] | None = None,
           on_citation_click: Callable[[dict], None] | None = None,
           accent: str = '#1a73e8',
           accent_bg: str = '#e8f0fe') -> None:
    """Clear the widget and re-render `markdown` into it.

    citations:           list of {id, source_id, source_name, chunk_idx, text, score}
    on_citation_click:   called with the citation dict when a chip is clicked
    """
    citations = citations or []
    cite_by_id = {c['id']: c for c in citations}

    # Make widget editable while we write, then lock it again at the end so
    # the user can select + copy but not modify.
    tw.config(state='normal')
    tw.delete('1.0', 'end')

    # Install per-citation tags up front so the insertion code can just
    # reference them by name. We do this lazily — only configure tags for
    # citation IDs we actually have a payload for.
    for cid, c in cite_by_id.items():
        tag = f'cite_{cid}'
        tw.tag_config(tag,
                      background=accent_bg,
                      foreground=accent,
                      relief='flat',
                      borderwidth=0,
                      lmargin1=2, lmargin2=2)
        if on_citation_click:
            tw.tag_bind(tag, '<Button-1>',
                        lambda _e, cc=c: on_citation_click(cc))
        # Pointer hint on hover. Use Enter/Leave on the tag.
        tw.tag_bind(tag, '<Enter>', lambda _e, _t=tw: _t.config(cursor='hand2'))
        tw.tag_bind(tag, '<Leave>', lambda _e, _t=tw: _t.config(cursor='arrow'))

    # Walk lines, decide per-line whether it's a heading / bullet / paragraph.
    # Code fences would be nice but NotebookLM rarely returns triple-backtick
    # blocks in chat — skipping for now; can extend later.
    for raw_line in markdown.split('\n'):
        line = raw_line.rstrip()

        if not line:
            tw.insert('end', '\n')
            continue

        if line.startswith('### '):
            tw.insert('end', line[4:] + '\n', 'h3')
            continue
        if line.startswith('## '):
            tw.insert('end', line[3:] + '\n', 'h2')
            continue
        if line.startswith('# '):
            tw.insert('end', line[2:] + '\n', 'h1')
            continue

        # Bullets: lines starting with "- " or "* " (but not "**" which is bold)
        m = re.match(r'^([*\-])\s+(.+)$', line)
        if m and not line.startswith('**'):
            tw.insert('end', '  •  ', 'bullet')
            _render_inline(tw, m.group(2) + '\n', cite_by_id, tag_prefix='bullet')
            continue

        # Numbered lists: "1. " / "2. " etc
        m = re.match(r'^(\d+)\.\s+(.+)$', line)
        if m:
            tw.insert('end', f'  {m.group(1)}. ', 'bullet')
            _render_inline(tw, m.group(2) + '\n', cite_by_id, tag_prefix='bullet')
            continue

        # Regular paragraph
        _render_inline(tw, line + '\n', cite_by_id)

    tw.config(state='disabled')


def _render_inline(tw: tk.Text, text: str, cite_by_id: dict,
                   tag_prefix: str = '') -> None:
    """Walk a single line of text, inserting each inline token with the
    right tag. Bold / italic / code / citation get their own tags; plain
    text falls through with no tag (uses the widget's default style)."""
    parts = _INLINE_RE.split(text)
    for part in parts:
        if not part:
            continue
        if part.startswith('**') and part.endswith('**'):
            tw.insert('end', part[2:-2], 'bold')
        elif part.startswith('`') and part.endswith('`'):
            tw.insert('end', part[1:-1], 'code')
        elif part.startswith('*') and part.endswith('*') and len(part) >= 3:
            tw.insert('end', part[1:-1], 'italic')
        elif part.startswith('[') and part.endswith(']'):
            # Citation: "[1]" or "[1, 2]" — render each ID as its own chip.
            ids_str = part[1:-1]
            for raw in ids_str.split(','):
                raw = raw.strip()
                if not raw.isdigit():
                    continue
                cid = int(raw)
                if cid in cite_by_id:
                    # Padding spaces make the chip visually distinct
                    tw.insert('end', f' {cid} ', f'cite_{cid}')
                else:
                    # Citation referenced in answer but not in our list —
                    # surface as plain text so we don't pretend it doesn't
                    # exist.
                    tw.insert('end', f'[{cid}]')
        else:
            tw.insert('end', part)


def estimate_line_count(tw: tk.Text) -> int:
    """Return the number of displayed lines in a Text widget — used to size
    a wrap='word' widget to its content so it fits inside a scrollable
    parent without becoming its own scroll surface."""
    try:
        end_index = tw.index('end-1c')
        return max(1, int(end_index.split('.')[0]))
    except Exception:
        return 1
