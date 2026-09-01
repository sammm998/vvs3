"""Glyph normalisation and shape features.

The engine has to recognise characters exported as *stroked polylines*
(single-stroke CAD fonts) and compare them against reference glyphs that are
*filled outlines* from ordinary PDF fonts.  Comparing those directly is
hopeless, so both sides are reduced to a one-pixel skeleton and matched with
several independent measures:

* symmetric chamfer distance between the skeletons;
* Jaccard distance between the dilated skeletons (area agreement);
* enclosed-hole count (topological);
* endpoint / junction counts of a *pruned* skeleton - pruning removes the
  spurious spurs that thinning a filled outline always produces, which is what
  makes this feature usable at all;
* aspect ratio of the source geometry.

Everything is pure numpy / OpenCV and deterministic: no random seeds, no
iteration over unordered containers, fixed iteration budgets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ..canonical import qs

RASTER_N = 48
PAD = 4
PRUNE_STEPS = 5
DILATE_K = 3

_NEIGHBOUR_OFFSETS = ((-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1))


@dataclass(frozen=True, slots=True)
class GlyphRaster:
    mask: np.ndarray
    skeleton: np.ndarray
    pruned: np.ndarray
    dilated: np.ndarray
    distance: np.ndarray
    aspect: float
    ink_ratio: float
    holes: int
    endpoints: int
    junctions: int

    def signature(self) -> tuple[float, ...]:
        """Coarse 4x4 ink-density descriptor, reported in the provenance."""
        n = RASTER_N // 4
        cells = self.mask.reshape(4, n, 4, n).sum(axis=(1, 3)) / float(n * n)
        return tuple(qs(float(v)) for v in cells.flatten())

    @property
    def complexity(self) -> float:
        return float(self.skeleton.sum()) / float(RASTER_N)


def _degree_map(skel: np.ndarray) -> np.ndarray:
    p = np.pad(skel, 1)
    deg = np.zeros(skel.shape, dtype=np.int16)
    for dy, dx in _NEIGHBOUR_OFFSETS:
        deg += p[1 + dy : 1 + dy + skel.shape[0], 1 + dx : 1 + dx + skel.shape[1]].astype(np.int16)
    return deg


def prune(skel: np.ndarray, steps: int = PRUNE_STEPS) -> np.ndarray:
    """Shorten every skeleton branch by ``steps`` pixels.

    Branches shorter than ``steps`` disappear entirely, which is exactly the
    thinning noise we want gone before counting endpoints and junctions.
    """
    img = (skel > 0).astype(np.uint8)
    for _ in range(steps):
        deg = _degree_map(img)
        img = np.where((img > 0) & (deg <= 1), 0, img).astype(np.uint8)
    return img


def skeleton_topology(skel: np.ndarray) -> tuple[int, int]:
    """(endpoints, junctions) counted as *clusters*, not as pixels.

    In an 8-connected skeleton a single Y-junction shows up as several
    degree>=3 pixels, and thinning a thick filled outline produces whole
    ridges of them.  Counting connected components of the endpoint set and of
    the junction set gives the topological quantity that is actually
    comparable between a hairline CAD stroke and a filled font outline.
    """
    import cv2

    deg = _degree_map(skel)
    on = skel > 0
    ends = ((deg == 1) & on).astype(np.uint8)
    joins = ((deg >= 3) & on).astype(np.uint8)
    n_end = cv2.connectedComponents(ends, connectivity=8)[0] - 1 if ends.any() else 0
    n_join = cv2.connectedComponents(joins, connectivity=8)[0] - 1 if joins.any() else 0
    return int(max(0, n_end)), int(max(0, n_join))


def euler_holes(mask: np.ndarray) -> int:
    """Enclosed holes: background components that do not touch the border."""
    import cv2

    inv = (mask == 0).astype(np.uint8)
    n, labels = cv2.connectedComponents(inv, connectivity=4)
    border = (
        set(labels[0, :].tolist())
        | set(labels[-1, :].tolist())
        | set(labels[:, 0].tolist())
        | set(labels[:, -1].tolist())
    )
    return max(0, sum(1 for lbl in range(1, n) if lbl not in border))


def _stroke_half_width(mask: np.ndarray) -> float:
    """Half the typical stroke thickness, in pixels, from the distance map."""
    from scipy import ndimage

    if not mask.any():
        return 0.0
    dt = ndimage.distance_transform_edt(mask > 0)
    vals = dt[mask > 0]
    return float(np.percentile(vals, 75))


def _build(mask: np.ndarray, aspect: float) -> GlyphRaster:
    import cv2
    from scipy import ndimage

    mask = (mask > 0).astype(np.uint8)
    skel = thin(mask)
    if not skel.any():
        skel = mask
    # Prune proportionally to the stroke thickness: a filled outline needs more
    # pruning than a hairline stroke to reach the same topological skeleton.
    steps = int(min(12, max(PRUNE_STEPS, round(_stroke_half_width(mask)) + PRUNE_STEPS - 2)))
    pruned = prune(skel, steps)
    ep, jn = skeleton_topology(pruned if pruned.any() else skel)
    dilated = cv2.dilate(skel, np.ones((DILATE_K, DILATE_K), np.uint8))
    distance = ndimage.distance_transform_edt(skel == 0).astype(np.float32)
    return GlyphRaster(
        mask=mask,
        skeleton=skel,
        pruned=pruned,
        dilated=dilated,
        distance=distance,
        aspect=float(aspect),
        ink_ratio=float(mask.sum()) / float(RASTER_N * RASTER_N),
        holes=euler_holes(mask),
        endpoints=ep,
        junctions=jn,
    )


def _empty() -> GlyphRaster:
    return _build(np.zeros((RASTER_N, RASTER_N), dtype=np.uint8), 1.0)


def rasterise_polylines(
    polylines: Sequence[Sequence[tuple[float, float]]],
    filled: bool = False,
    bbox: tuple[float, float, float, float] | None = None,
) -> GlyphRaster:
    """Normalise polylines into a fixed raster with the aspect ratio preserved.

    The glyph is scaled so its longer side spans ``RASTER_N - 2*PAD`` and is
    centred; the aspect ratio is carried as a separate feature rather than
    being destroyed by stretching.
    """
    import cv2

    pts_all = [p for poly in polylines for p in poly]
    if len(pts_all) < 2:
        return _empty()
    if bbox is None:
        xs = [p[0] for p in pts_all]
        ys = [p[1] for p in pts_all]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    else:
        x0, y0, x1, y1 = bbox
    w = x1 - x0
    h = y1 - y0
    span = max(w, h, 1e-9)
    inner = RASTER_N - 2 * PAD
    s = inner / span
    ox = PAD + (inner - w * s) / 2.0
    oy = PAD + (inner - h * s) / 2.0

    mask = np.zeros((RASTER_N, RASTER_N), dtype=np.uint8)
    for poly in polylines:
        if len(poly) < 2:
            continue
        arr = np.round(
            np.array([[(p[0] - x0) * s + ox, (p[1] - y0) * s + oy] for p in poly], dtype=np.float64)
        ).astype(np.int32).reshape(-1, 1, 2)
        if filled:
            cv2.fillPoly(mask, [arr], 1)
        cv2.polylines(mask, [arr], isClosed=bool(filled), color=1, thickness=1)

    # A hairline stroke has zero measurable height; give the aspect feature a
    # floor of one raster pixel so it stays finite and comparable.
    floor = span / float(inner)
    aspect = max(w, floor) / max(h, floor)
    return _build(mask, aspect)


def rasterise_mask(mask: np.ndarray) -> GlyphRaster:
    """Normalise an already-rasterised binary mask (font prototypes)."""
    import cv2

    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return _empty()
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    crop = (mask[y0 : y1 + 1, x0 : x1 + 1] > 0).astype(np.uint8)
    h, w = crop.shape
    inner = RASTER_N - 2 * PAD
    s = inner / max(w, h)
    nw, nh = max(1, int(round(w * s))), max(1, int(round(h * s)))
    resized = (cv2.resize(crop, (nw, nh), interpolation=cv2.INTER_AREA) > 0).astype(np.uint8)
    out = np.zeros((RASTER_N, RASTER_N), dtype=np.uint8)
    ox = PAD + (inner - nw) // 2
    oy = PAD + (inner - nh) // 2
    out[oy : oy + nh, ox : ox + nw] = resized
    return _build(out, float(w) / float(h))


def thin(mask: np.ndarray) -> np.ndarray:
    """Zhang-Suen thinning.

    Implemented here rather than taken from ``cv2.ximgproc`` so the engine has
    no opencv-contrib dependency and the result is identical on every platform.
    """
    img = (mask > 0).astype(np.uint8).copy()
    changed = True
    guard = 0
    while changed and guard < 100:
        guard += 1
        changed = False
        for step in (0, 1):
            p = np.pad(img, 1)
            p2 = p[0:-2, 1:-1]
            p3 = p[0:-2, 2:]
            p4 = p[1:-1, 2:]
            p5 = p[2:, 2:]
            p6 = p[2:, 1:-1]
            p7 = p[2:, 0:-2]
            p8 = p[1:-1, 0:-2]
            p9 = p[0:-2, 0:-2]
            neighbours = [p2, p3, p4, p5, p6, p7, p8, p9]
            b = sum(neighbours)
            seq = neighbours + [p2]
            a = sum(((seq[i] == 0) & (seq[i + 1] == 1)).astype(np.uint8) for i in range(8))
            if step == 0:
                c1, c2 = p2 * p4 * p6, p4 * p6 * p8
            else:
                c1, c2 = p2 * p4 * p8, p2 * p6 * p8
            cond = (img == 1) & (b >= 2) & (b <= 6) & (a == 1) & (c1 == 0) & (c2 == 0)
            if cond.any():
                img[cond] = 0
                changed = True
    return img


CHAMFER_TRIM = 1.00


def _trimmed_mean(values: np.ndarray) -> float:
    """Mean of the closest ``CHAMFER_TRIM`` fraction of the distances.

    Trimming the tail tolerates local differences between two renderings of the
    same character.  It is disabled by default (``CHAMFER_TRIM = 1.0``): with
    trimming on, visually adjacent characters such as C and G fall inside the
    shape-clustering radius and merge, which costs more than the tolerance
    gains.
    """
    if values.size == 0:
        return float(RASTER_N)
    k = max(1, int(values.size * CHAMFER_TRIM))
    return float(np.sort(values)[:k].mean())


def chamfer(a: GlyphRaster, b: GlyphRaster) -> float:
    """Symmetric trimmed chamfer distance between two skeletons, in pixels."""
    sa, sb = a.skeleton, b.skeleton
    if not sa.any() or not sb.any():
        return float(RASTER_N)
    return 0.5 * (_trimmed_mean(b.distance[sa > 0]) + _trimmed_mean(a.distance[sb > 0]))


def jaccard_distance(a: GlyphRaster, b: GlyphRaster) -> float:
    """1 - IoU of the dilated skeletons: how much of the ink actually agrees."""
    inter = float(np.logical_and(a.dilated, b.dilated).sum())
    union = float(np.logical_or(a.dilated, b.dilated).sum())
    if union <= 0:
        return 1.0
    return 1.0 - inter / union


def glyph_features(raster: GlyphRaster) -> dict[str, float]:
    return {
        "aspect": raster.aspect,
        "holes": float(raster.holes),
        "endpoints": float(raster.endpoints),
        "junctions": float(raster.junctions),
        "inkRatio": raster.ink_ratio,
        "complexity": raster.complexity,
    }
