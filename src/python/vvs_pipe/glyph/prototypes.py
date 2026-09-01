"""Reference glyph bank.

Prototypes are rendered at run time from the PDF base-14 fonts that every
viewer ships, so the bank contains *characters*, not drawing codes.  Nothing
here knows which designations a drawing might contain; adding a new drawing
with entirely new codes requires no change.

Several typefaces are rendered per character and the classifier takes the best
match, which absorbs the difference between a filled serif outline and a
single-stroke CAD font.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

import numpy as np

from .features import GlyphRaster, rasterise_mask

ALPHABET = tuple(
    list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + list("0123456789")
    + ["-", "+", ".", ",", ":", "/", "(", ")", "Ø"]
)

FONTS = ("helv", "cour", "tiro", "hebo")

# Nominal height of the character relative to the cap height of its text line,
# and the nominal offset of its bottom above the baseline.  These are typographic
# facts about the characters themselves, used as evidence alongside shape.
REL_METRICS: dict[str, tuple[float, float]] = {
    **{c: (1.0, 0.0) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789Ø"},
    "-": (0.12, 0.40),
    "+": (0.55, 0.18),
    ".": (0.12, 0.0),
    ",": (0.22, -0.10),
    ":": (0.55, 0.15),
    "/": (1.0, 0.0),
    "(": (1.15, -0.08),
    ")": (1.15, -0.08),
}


@dataclass(frozen=True, slots=True)
class Prototype:
    character: str
    font: str
    raster: GlyphRaster

    @property
    def holes(self) -> int:
        return self.raster.holes

    @property
    def endpoints(self) -> int:
        return self.raster.endpoints

    @property
    def junctions(self) -> int:
        return self.raster.junctions


def _render_char(ch: str, font: str, px: int = 160) -> np.ndarray | None:
    import fitz

    doc = fitz.open()
    try:
        page = doc.new_page(width=px, height=px)
        try:
            page.insert_text(
                fitz.Point(px * 0.12, px * 0.78),
                ch,
                fontname=font,
                fontsize=px * 0.62,
                color=(0, 0, 0),
            )
        except Exception:  # pragma: no cover - font lacks the glyph
            return None
        pm = page.get_pixmap(colorspace=fitz.csGRAY, alpha=False)
        arr = np.frombuffer(pm.samples, dtype=np.uint8).reshape(pm.height, pm.width)
        mask = (arr < 128).astype(np.uint8)
        return mask if mask.any() else None
    finally:
        doc.close()


@functools.lru_cache(maxsize=1)
def prototype_bank() -> tuple[Prototype, ...]:
    """Render the character bank once per process.

    Deterministic: the fonts are the PDF base-14 set embedded in the renderer,
    the raster size is fixed, and the iteration order is the declared
    ``ALPHABET``/``FONTS`` order.
    """
    out: list[Prototype] = []
    for ch in ALPHABET:
        for font in FONTS:
            mask = _render_char(ch, font)
            if mask is None:
                continue
            out.append(Prototype(character=ch, font=font, raster=rasterise_mask(mask)))
    return tuple(out)
