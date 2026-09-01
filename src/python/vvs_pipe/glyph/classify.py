"""Character recognition from vector + raster + geometric + contextual evidence.

Four independent kinds of evidence are combined, as the specification requires:

* **vector/raster shape** - chamfer distance and dilated-skeleton Jaccard
  distance against the rendered prototype bank;
* **geometric** - aspect ratio of the *source* geometry, plus the glyph's
  height and vertical placement relative to its own text line;
* **topological** - enclosed holes and pruned-skeleton endpoint/junction counts;
* **context** - an optional per-character log-prior supplied by the token
  layer.  It re-ranks characters the shape evidence already produced; it can
  never introduce one, and it is derived from the drawing's own token
  structure, never from a list of known codes.

Confidence comes from the *margin* between the best and second-best character.
A genuinely ambiguous shape returns ``character=None`` so the caller records
UNRESOLVED_GLYPH instead of guessing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from ..canonical import qs
from .features import (
    GlyphRaster,
    chamfer,
    jaccard_distance,
    rasterise_polylines,
)
from .prototypes import REL_METRICS, prototype_bank

Pt = tuple[float, float]

# Shape-recognition weights.  They were fitted on *characters* - see
# tests/python/test_glyph_reconstruction.py, which measures accuracy on glyphs
# rendered by fonts that are not in the prototype bank - and never against any
# drawing's designations.
W_CHAMFER = 0.30
W_JACCARD = 0.50
W_ASPECT = 0.15
W_HOLES = 0.50
W_RELHEIGHT = 2.50
W_RELBASE = 1.50
W_ENDPOINTS = 0.10
W_JUNCTIONS = 0.10

ASPECT_CLAMP = 1.2
TOPOLOGY_CLAMP = 4

ACCEPT_SCORE = 0.06
ACCEPT_REL_MARGIN = 0.04


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    character: str | None
    confidence: float
    ranked: tuple[tuple[str, float], ...]
    holes: int
    endpoints: int
    junctions: int
    aspect: float
    raster: GlyphRaster

    @property
    def rel_margin(self) -> float:
        if len(self.ranked) < 2 or self.ranked[0][1] <= 0:
            return 1.0
        return (self.ranked[0][1] - self.ranked[1][1]) / self.ranked[0][1]


def glyph_distance_vector(
    raster: GlyphRaster,
    rel_h: float,
    rel_base: float,
) -> dict[str, float]:
    """Weighted distance from one glyph to every character in the bank.

    Exposed separately from :func:`classify_glyph` because the alphabet
    resolver in :mod:`vvs_pipe.glyph.alphabet` aggregates these vectors over
    shape clusters before deciding anything.
    """
    out: dict[str, float] = {}
    for proto in prototype_bank():
        ch = proto.character
        nom_h, nom_base = REL_METRICS.get(ch, (1.0, 0.0))
        d = (
            W_CHAMFER * chamfer(raster, proto.raster)
            + W_JACCARD * jaccard_distance(raster, proto.raster)
            + W_ASPECT
            * min(
                abs(math.log(max(raster.aspect, 1e-6) / max(proto.raster.aspect, 1e-6))),
                ASPECT_CLAMP,
            )
            + W_HOLES * abs(raster.holes - proto.holes)
            + W_RELHEIGHT * abs(rel_h - nom_h)
            + W_RELBASE * abs(rel_base - nom_base)
            + W_ENDPOINTS * min(abs(raster.endpoints - proto.endpoints), TOPOLOGY_CLAMP)
            + W_JUNCTIONS * min(abs(raster.junctions - proto.junctions), TOPOLOGY_CLAMP)
        )
        if d < out.get(ch, math.inf):
            out[ch] = d
    return out


def classify_glyph(
    polylines: Sequence[Sequence[Pt]],
    filled: bool,
    cap_height: float,
    baseline_y: float,
    bbox: tuple[float, float, float, float],
    prior: Mapping[str, float] | None = None,
) -> ClassificationResult:
    x0, y0, x1, y1 = bbox
    raster = rasterise_polylines(polylines, filled=filled)
    cap = max(cap_height, 1e-6)
    rel_h = (y1 - y0) / cap
    rel_base = (baseline_y - y1) / cap  # glyph bottom above the line baseline

    scores: dict[str, float] = {}
    for ch, d in glyph_distance_vector(raster, rel_h, rel_base).items():
        s = math.exp(-d)
        if prior:
            s *= math.exp(prior.get(ch, 0.0))
        scores[ch] = s

    if not scores:  # pragma: no cover - prototype bank is never empty
        return ClassificationResult(None, 0.0, (), raster.holes, raster.endpoints, raster.junctions, raster.aspect, raster)

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    ranked_q = tuple((c, qs(s)) for c, s in ranked[:6])
    top_char, top_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    rel_margin = (top_score - second) / top_score if top_score > 0 else 0.0

    if top_score < ACCEPT_SCORE or rel_margin < ACCEPT_REL_MARGIN:
        return ClassificationResult(
            None,
            qs(min(top_score / ACCEPT_SCORE, rel_margin / ACCEPT_REL_MARGIN, 1.0) * 0.5),
            ranked_q,
            raster.holes,
            raster.endpoints,
            raster.junctions,
            raster.aspect,
            raster,
        )

    confidence = min(
        1.0,
        0.5 * min(1.0, top_score / 0.35) + 0.5 * min(1.0, rel_margin / 0.30),
    )
    return ClassificationResult(
        top_char, qs(confidence), ranked_q, raster.holes, raster.endpoints, raster.junctions, raster.aspect, raster
    )
