"""Brand icon generator, used by:
  • App._save_brand_ico()  → writes %APPDATA%/Hotkeys/app_icon.ico at runtime
  • hotkeys.spec           → writes <project>/build_icon.ico at build time so
                             PyInstaller can embed it as the .exe's native
                             icon resource.

Self-contained: no dependency on main.py or the App class, so the PyInstaller
spec can import it without booting any of the runtime modules.
"""
from __future__ import annotations
from PIL import Image, ImageDraw, ImageFilter


def make_icon() -> Image.Image:
    """Render the brand mark at 512px and downsample to 64×64 with clean
    anti-aliased edges. Returns an RGBA PIL Image."""
    S = 8
    B = 64 * S  # 512 px working canvas

    def _hex(h: str) -> tuple[int, int, int]:
        h = h.lstrip('#')
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore

    def _grad_mask(mask: Image.Image, c1: str, c2: str) -> Image.Image:
        """Apply a top→bottom gradient through a white-on-black mask."""
        r1, g1, b1 = _hex(c1)
        r2, g2, b2 = _hex(c2)
        grad = Image.new('RGBA', (B, B))
        dg = ImageDraw.Draw(grad)
        for y in range(B):
            t = y / (B - 1)
            dg.line(
                [(0, y), (B, y)],
                fill=(int(r1 + (r2 - r1) * t),
                      int(g1 + (g2 - g1) * t),
                      int(b1 + (b2 - b1) * t), 255),
            )
        out = Image.new('RGBA', (B, B), (0, 0, 0, 0))
        out.paste(grad, mask=mask.split()[0])
        return out

    # Background: purple border + dark fill
    base = Image.new('RGBA', (B, B), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    d.rounded_rectangle([0, 0, B - 1, B - 1], radius=13 * S, fill='#7c3aed')
    d.rounded_rectangle(
        [3 * S, 3 * S, B - 1 - 3 * S, B - 1 - 3 * S],
        radius=11 * S, fill='#080f1a',
    )

    # Lightning bolt polygon
    BOLT = [(x * S, y * S) for x, y in
            [(42, 4), (10, 34), (28, 34), (22, 60), (52, 26), (36, 26)]]
    bolt_mask = Image.new('RGBA', (B, B), (0, 0, 0, 0))
    ImageDraw.Draw(bolt_mask).polygon(BOLT, fill='white')

    # Glow layer (soft halo behind the bolt)
    glow = _grad_mask(
        bolt_mask.filter(ImageFilter.GaussianBlur(12)),
        '#7dd3fc', '#1e40af',
    )
    base = Image.alpha_composite(base, glow)

    # Sharp bolt, sky blue top → deep navy bottom
    base = Image.alpha_composite(
        base, _grad_mask(bolt_mask, '#bae6fd', '#0f2a6e'),
    )

    return base.resize((64, 64), Image.LANCZOS)


_ICO_SIZES = [(16, 16), (20, 20), (24, 24),
              (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def save_ico(path: str) -> str:
    """Render and save a multi-resolution Windows .ico to `path`. Returns
    the path so callers can chain. Includes sizes for every Windows fall-
    back slot, Alt+Tab thumbnails (16/20), title bars (16), taskbar
    (32/48), jump list large (64), shell file info (128/256)."""
    img = make_icon()
    img.save(path, format='ICO', sizes=_ICO_SIZES)
    return path


if __name__ == '__main__':
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else 'brand_icon.ico'
    print(f'saved: {save_ico(out)}')
