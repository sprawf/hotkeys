"""
Shared design tokens, single source of truth for all UI files.
Inspired by Linear / Raycast / Vercel design systems.
"""

# ── Palette ──────────────────────────────────────────────────────────────────
BG       = '#0a0a0a'   # deep background
SURFACE  = '#141414'   # cards, panels
SURF2    = '#1e1e1e'   # hover / active surface
SURF3    = '#282828'   # pressed / selected
BORDER   = '#2a2a2a'   # subtle separator
BORDER2  = '#383838'   # stronger border

ACCENT   = '#7c3aed'   # primary purple (Linear-inspired)
ACCENTL  = '#9f67fa'   # light purple
ACCENTS  = '#4c1d95'   # dark purple

TEXT_P   = '#f0f0f0'   # primary text
# TEXT_S is the colour used for hint text, descriptions, captions, and
# secondary labels across the entire app. It was #909090, which read as
# washed-out grey on the dark surfaces and made splash cards / op
# descriptions hard to scan. #c8c8c8 keeps it visibly secondary (still
# clearly weaker than TEXT_P) while staying readable on any dark surface.
TEXT_S   = '#c8c8c8'   # secondary / muted
TEXT_D   = '#7a7a7a'   # disabled

OK       = '#22c55e'   # success green
WARN     = '#f59e0b'   # amber
ERR      = '#ef4444'   # red
INFO     = '#3b82f6'   # blue (used for whisper recording)

# ── Sticky note card colours (warm contrast on dark BG) ───────────────────
CARD_COLORS = [
    '#FFF9C4',   # yellow
    '#DCEDC8',   # green
    '#BBDEFB',   # blue
    '#F8BBD0',   # pink
    '#FFE0B2',   # orange
    '#E1BEE7',   # purple
    '#D7CCC8',   # warm grey
    '#B2DFDB',   # teal
]
CARD_TEXT   = '#1a1a1a'
CARD_TEXT_S = '#444444'

# ── Typography ────────────────────────────────────────────────────────────────
FONT_FAMILY  = 'Segoe UI'
FONT_MONO    = 'Consolas'

FONT_XS  = (FONT_FAMILY, 11)
FONT_SM  = (FONT_FAMILY, 12)
FONT_MD  = (FONT_FAMILY, 13)
FONT_LG  = (FONT_FAMILY, 14)
FONT_XL  = (FONT_FAMILY, 15, 'bold')
FONT_2XL = (FONT_FAMILY, 18, 'bold')

FONT_SM_BOLD = (FONT_FAMILY, 12, 'bold')
FONT_MD_BOLD = (FONT_FAMILY, 13, 'bold')
FONT_LG_BOLD = (FONT_FAMILY, 14, 'bold')

FONT_MONO_MD = (FONT_MONO, 13)
FONT_MONO_LG = (FONT_MONO, 14)

# ── Color helpers ────────────────────────────────────────────────────────────

def _darken(hex_color: str, factor: float = 0.72) -> str:
    """Return a darker shade of *hex_color* by multiplying each channel by factor."""
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f'#{int(r * factor):02x}{int(g * factor):02x}{int(b * factor):02x}'


# ── Geometry ──────────────────────────────────────────────────────────────────
RADIUS       = 10    # card corner radius (CTk)
RADIUS_SM    = 6
RADIUS_LG    = 14
PAD          = 16
PAD_SM       = 8
PAD_LG       = 24
