"""Third search: individual glyphs.

Two sources, treated the same way downstream.

* **text glyphs** - the PDF has a text layer and hands over characters with
  their boxes.  Nothing is inferred; the character is read off the file.
* **path glyphs** - the drawing was exported with its lettering as geometry, so
  there is no text at all.  Ink is grouped into strokes, strokes into
  characters and characters into lines *by position*, and each character is
  matched against a bank of shapes rendered at run time from the fonts the file
  itself carries plus the PDF base-14 faces.

The bank contains characters, never drawing codes: a sheet whose designations
have never been seen before goes through exactly the same path.  Every reading
keeps its runners-up in ``alternatives`` so a later stage can recover from one
wrong character instead of inheriting it silently.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

import numpy as np

from .canonical import canonical_json, entity_id, q, qa, qbbox, sort_canonical
from .model import Glyph, PdfObject, Segment
from .spatial_index import SpatialIndex, bbox_distance, expand

# The alphabet is a property of writing, not of this drawing.  Swedish sheets
# add the three national vowels and the diameter sign.
ALPHABET = tuple(
    list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + list("ÅÄÖ")
    + list("0123456789")
    + ["-", "+", ".", ",", ":", "/", "(", ")", "Ø", "%", "=", "*", "'", '"', "?", "!", "&", "#", "<", ">"]
)
BASE14 = ("helv", "cour", "tiro", "hebo", "tibo", "cobo")

# Characters whose nominal height differs from a capital's, expressed as a
# fraction of cap height and an offset above the baseline.  Typographic facts,
# used as evidence beside shape.
REL_METRICS: dict[str, tuple[float, float]] = {
    **{c: (1.0, 0.0) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZÅÄÖ0123456789Ø"},
    "-": (0.10, 0.40), "+": (0.55, 0.18), ".": (0.10, 0.0), ",": (0.22, -0.10),
    ":": (0.55, 0.15), "/": (1.05, -0.05), "(": (1.15, -0.08), ")": (1.15, -0.08),
    "%": (1.0, 0.0), "=": (0.35, 0.22), "*": (0.45, 0.5), "'": (0.30, 0.68),
    '"': (0.30, 0.68), "?": (1.0, 0.0), "!": (1.0, 0.0), "&": (1.0, 0.0),
    "#": (0.95, 0.02), "<": (0.55, 0.15), ">": (0.55, 0.15),
}

RASTER = 32          # side of the normalised glyph raster
MAX_ALTERNATIVES = 4
# Matching a mark to a shape asks two questions: is all of the mark's ink
# accounted for by the shape (forward), and is all of the shape's ink present
# in the mark (backward).  Both matter equally; what actually separates the
# pairs shape distance confuses - P from R, E from R, 8 from 0 - is the
# structural term below, not a thumb on the scale of one direction.
FORWARD_WEIGHT = 0.5
BACKWARD_WEIGHT = 0.5
METRIC_HEIGHT_WEIGHT = 2.2
METRIC_OFFSET_WEIGHT = 1.6
ASPECT_WEIGHT = 0.8
HOLE_WEIGHT = 0.9
ENDPOINT_WEIGHT = 0.30
JUNCTION_WEIGHT = 0.20


# ---------------------------------------------------------------------------
# rasterisation
# ---------------------------------------------------------------------------

def _blank() -> np.ndarray:
    return np.zeros((RASTER, RASTER), dtype=bool)


def _draw_line(mask: np.ndarray, a: tuple[float, float], b: tuple[float, float]) -> None:
    x0, y0 = int(round(a[0])), int(round(a[1]))
    x1, y1 = int(round(b[0])), int(round(b[1]))
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx - dy
    n = 0
    while n <= dx + dy + 2:
        if 0 <= x0 < RASTER and 0 <= y0 < RASTER:
            mask[y0, x0] = True
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            x0 += sx
        if e2 < dx:
            err += dx
            y0 += sy
        n += 1


def rasterise_strokes(polylines: Sequence[Sequence[tuple[float, float]]],
                      bbox: Sequence[float]) -> np.ndarray:
    """Normalise a set of strokes into the standard raster, aspect preserved."""
    mask = _blank()
    w = max(bbox[2] - bbox[0], 1e-6)
    h = max(bbox[3] - bbox[1], 1e-6)
    scale = (RASTER - 3) / max(w, h)
    ox = (RASTER - w * scale) / 2.0
    oy = (RASTER - h * scale) / 2.0
    for poly in polylines:
        for i in range(len(poly) - 1):
            a = ((poly[i][0] - bbox[0]) * scale + ox, (poly[i][1] - bbox[1]) * scale + oy)
            b = ((poly[i + 1][0] - bbox[0]) * scale + ox, (poly[i + 1][1] - bbox[1]) * scale + oy)
            _draw_line(mask, a, b)
    return mask


def rasterise_pixmap(samples: np.ndarray) -> np.ndarray:
    """Normalise a rendered grey image of one character into the standard raster."""
    ink = samples < 128
    ys, xs = np.nonzero(ink)
    if len(xs) == 0:
        return _blank()
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    crop = ink[y0:y1, x0:x1]
    h, w = crop.shape
    scale = (RASTER - 3) / max(h, w)
    out = _blank()
    th, tw = max(1, int(round(h * scale))), max(1, int(round(w * scale)))
    yi = (np.arange(th) * (h / th)).astype(int).clip(0, h - 1)
    xi = (np.arange(tw) * (w / tw)).astype(int).clip(0, w - 1)
    small = crop[np.ix_(yi, xi)]
    oy = (RASTER - th) // 2
    ox = (RASTER - tw) // 2
    out[oy:oy + th, ox:ox + tw] = small
    return out


def thin(mask: np.ndarray) -> np.ndarray:
    """Zhang-Suen thinning, so a filled outline can be compared with a stroke."""
    img = mask.copy()
    changed = True
    guard = 0
    while changed and guard < 40:
        guard += 1
        changed = False
        for step in (0, 1):
            p = np.pad(img, 1)
            p2 = p[0:-2, 1:-1]; p3 = p[0:-2, 2:]; p4 = p[1:-1, 2:]
            p5 = p[2:, 2:];     p6 = p[2:, 1:-1]; p7 = p[2:, 0:-2]
            p8 = p[1:-1, 0:-2]; p9 = p[0:-2, 0:-2]
            neighbours = (p2.astype(np.int8) + p3 + p4 + p5 + p6 + p7 + p8 + p9)
            seq = [p2, p3, p4, p5, p6, p7, p8, p9, p2]
            transitions = np.zeros_like(neighbours)
            for i in range(8):
                transitions += (~seq[i] & seq[i + 1]).astype(np.int8)
            if step == 0:
                cond = (~p2 | ~p4 | ~p6) & (~p4 | ~p6 | ~p8)
            else:
                cond = (~p2 | ~p4 | ~p8) & (~p2 | ~p6 | ~p8)
            remove = img & (neighbours >= 2) & (neighbours <= 6) & (transitions == 1) & cond
            if remove.any():
                img = img & ~remove
                changed = True
    return img


def outline(mask: np.ndarray) -> np.ndarray:
    """The boundary of a filled shape - what an outline-exported font draws."""
    if not mask.any():
        return mask.copy()
    padded = np.pad(mask, 1)
    eroded = (padded[0:-2, 1:-1] & padded[2:, 1:-1] & padded[1:-1, 0:-2] & padded[1:-1, 2:]
              & padded[0:-2, 0:-2] & padded[0:-2, 2:] & padded[2:, 0:-2] & padded[2:, 2:])
    return mask & ~eroded


def topology_features(mask: np.ndarray) -> tuple[int, int, int]:
    """(holes, endpoints, junctions) of a thin shape.

    These separate exactly the pairs that shape distance confuses: P from R
    (one endpoint against two), E from R (a hole against none), 8 from 0 (two
    holes against one).  They are properties of the letter's structure, so they
    survive a change of typeface far better than its outline does.
    """
    padded = np.pad(mask, 1)
    neighbours = np.zeros(mask.shape, dtype=np.int8)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            neighbours += padded[1 + dy: 1 + dy + mask.shape[0],
                                 1 + dx: 1 + dx + mask.shape[1]].astype(np.int8)
    endpoints = int(((neighbours == 1) & mask).sum())
    junctions = int(((neighbours >= 3) & mask).sum())
    holes = _count_holes(mask)
    return holes, endpoints, junctions


def _count_holes(mask: np.ndarray) -> int:
    """Background regions fully enclosed by ink."""
    h, w = mask.shape
    seen = np.zeros_like(mask)
    stack = [(y, x) for y in range(h) for x in (0, w - 1) if not mask[y, x]]
    stack += [(y, x) for x in range(w) for y in (0, h - 1) if not mask[y, x]]
    for y, x in stack:
        seen[y, x] = True
    while stack:
        y, x = stack.pop()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and not mask[ny, nx] and not seen[ny, nx]:
                seen[ny, nx] = True
                stack.append((ny, nx))
    holes = 0
    visited = seen.copy()
    for y in range(h):
        for x in range(w):
            if mask[y, x] or visited[y, x]:
                continue
            holes += 1
            frontier = [(y, x)]
            visited[y, x] = True
            while frontier:
                cy, cx = frontier.pop()
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < h and 0 <= nx < w and not mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        frontier.append((ny, nx))
    return holes


def chamfer(mask: np.ndarray) -> np.ndarray:
    """Chamfer distance transform: two sweeps, each vectorised over a row."""
    big = float(RASTER * 2)
    dist = np.where(mask, 0.0, big)
    h, w = dist.shape
    for y in range(h):
        row = dist[y]
        if y > 0:
            above = dist[y - 1]
            row = np.minimum(row, above + 1.0)
            row = np.minimum(row, np.concatenate(([big], above[:-1])) + 1.414)
            row = np.minimum(row, np.concatenate((above[1:], [big])) + 1.414)
        for x in range(1, w):
            if row[x - 1] + 1.0 < row[x]:
                row[x] = row[x - 1] + 1.0
        dist[y] = row
    for y in range(h - 1, -1, -1):
        row = dist[y]
        if y < h - 1:
            below = dist[y + 1]
            row = np.minimum(row, below + 1.0)
            row = np.minimum(row, np.concatenate(([big], below[:-1])) + 1.414)
            row = np.minimum(row, np.concatenate((below[1:], [big])) + 1.414)
        for x in range(w - 2, -1, -1):
            if row[x + 1] + 1.0 < row[x]:
                row[x] = row[x + 1] + 1.0
        dist[y] = row
    return dist


# ---------------------------------------------------------------------------
# the character bank
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Prototype:
    """One reference shape.

    Lettering reaches a drawing in two forms, and a bank that knows only one of
    them mis-reads the other.  A single-stroke CAD font draws the *centre line*
    of each character, so it is compared against the prototype's skeleton; a
    font exported as outlines draws the *boundary* of a filled shape, so it is
    compared against the prototype's outline.  Both variants are kept and the
    better one wins, which is why a filled full stop and a stroked one both
    read as a full stop.
    """

    character: str
    font: str
    source: str                    # base14 | embedded
    variant: str                   # skeleton | outline
    skeleton: np.ndarray
    distance: np.ndarray
    aspect: float
    holes: int = 0
    endpoints: int = 0
    junctions: int = 0

    def key(self) -> str:
        return f"{self.character}|{self.font}|{self.source}|{self.variant}"


class CharacterBank:
    """Reference shapes, rendered at run time.

    Rendering the sheet's *own* embedded face matters: matching a technical CAD
    face against Helvetica is what turns R into 9 and A into 4.  Embedded fonts
    are usually subsets, so the base-14 faces stay in the bank to cover the
    characters the subset never contained.
    """

    def __init__(self, embedded_fonts: Sequence[tuple[str, bytes]] = ()) -> None:
        self.prototypes: list[Prototype] = []
        self._cache: dict[tuple, tuple[tuple[str, float], ...]] = {}
        self._by_char: dict[str, list[Prototype]] = {}
        self.sources: dict[str, int] = {}
        for fontname in BASE14:
            self._render_font(fontname, None, "base14")
        for name, buffer in embedded_fonts:
            self._render_font(name, buffer, "embedded")
        self.prototypes = sort_canonical(self.prototypes, key=lambda p: p.key())
        for proto in self.prototypes:
            self._by_char.setdefault(proto.character, []).append(proto)
        self._stack = np.stack([p.skeleton for p in self.prototypes]) if self.prototypes else None
        self._dstack = np.stack([p.distance for p in self.prototypes]) if self.prototypes else None
        self._counts = self._stack.reshape(len(self.prototypes), -1).sum(axis=1) if self.prototypes else None

    def _render_font(self, fontname: str, buffer: Optional[bytes], source: str) -> None:
        import fitz

        try:
            font = fitz.Font(fontname=fontname) if buffer is None else fitz.Font(fontbuffer=buffer)
        except Exception:
            return
        rendered = 0
        for character in ALPHABET:
            if not _font_has(font, character):
                continue
            mask = _render_character(font, character, buffer, fontname)
            if mask is None or not mask.any():
                continue
            for variant, shape in (("skeleton", thin(mask)), ("outline", outline(mask))):
                ys, xs = np.nonzero(shape)
                if len(xs) == 0:
                    continue
                aspect = (xs.max() - xs.min() + 1) / max(1.0, (ys.max() - ys.min() + 1))
                holes, ends, joins = topology_features(shape)
                self.prototypes.append(
                    Prototype(character, fontname, source, variant, shape,
                              chamfer(shape), q(aspect), holes, ends, joins)
                )
            rendered += 1
        if rendered:
            self.sources[f"{source}:{fontname}"] = rendered

    def classify(self, mask: np.ndarray, rel_height: float, rel_offset: float,
                 aspect: float) -> tuple[tuple[str, float], ...]:
        """Rank characters for one ink shape.  Never returns a single answer.

        Results are memoised on the raster and its metrics: a sheet letters the
        same character hundreds of times, and the same ink must always produce
        the same reading anyway.
        """
        cache_key = (mask.tobytes(), round(rel_height, 2), round(rel_offset, 2), round(aspect, 2))
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        result = self._classify(mask, rel_height, rel_offset, aspect)
        self._cache[cache_key] = result
        return result

    def _classify(self, mask: np.ndarray, rel_height: float, rel_offset: float,
                  aspect: float) -> tuple[tuple[str, float], ...]:
        if self._stack is None or not mask.any():
            return ()
        cand_dist = chamfer(mask)
        cand_holes, cand_ends, cand_joins = topology_features(thin(mask))
        cand_flat = mask.reshape(-1)
        n_cand = max(1, int(cand_flat.sum()))
        protos = self._stack.reshape(len(self.prototypes), -1)
        pdist = self._dstack.reshape(len(self.prototypes), -1)
        # distance from the candidate's ink to each prototype, and back
        forward = pdist[:, cand_flat].mean(axis=1)
        backward = (protos * cand_dist.reshape(-1)).sum(axis=1) / np.maximum(self._counts, 1)
        shape_cost = FORWARD_WEIGHT * forward + BACKWARD_WEIGHT * backward
        scores: dict[str, float] = {}
        self.last_variant: dict[str, str] = {}
        for index, proto in enumerate(self.prototypes):
            nominal_h, nominal_off = REL_METRICS.get(proto.character, (1.0, 0.0))
            metric_cost = (METRIC_HEIGHT_WEIGHT * abs(rel_height - nominal_h)
                           + METRIC_OFFSET_WEIGHT * abs(rel_offset - nominal_off))
            aspect_cost = ASPECT_WEIGHT * abs(math.log((aspect + 1e-3) / (proto.aspect + 1e-3)))
            structure_cost = (
                HOLE_WEIGHT * abs(cand_holes - proto.holes)
                + ENDPOINT_WEIGHT * min(4, abs(cand_ends - proto.endpoints))
                + JUNCTION_WEIGHT * min(4, abs(cand_joins - proto.junctions))
            )
            cost = float(shape_cost[index]) + metric_cost + aspect_cost + structure_cost
            previous = scores.get(proto.character)
            if previous is None or cost < previous:
                scores[proto.character] = cost
                self.last_variant[proto.character] = f"{proto.font}:{proto.source}:{proto.variant}"
        ranked = sorted(scores.items(), key=lambda kv: (round(kv[1], 6), kv[0]))
        best = ranked[0][1]
        out = []
        for character, cost in ranked[:MAX_ALTERNATIVES]:
            out.append((character, q(math.exp(-(cost - best) / 0.8))))
        return tuple(out)

    def to_json(self) -> dict:
        return {"prototypes": len(self.prototypes),
                "characters": len(self._by_char),
                "fonts": {k: self.sources[k] for k in sorted(self.sources)}}


def _font_has(font, character: str) -> bool:
    try:
        return bool(font.has_glyph(ord(character)))
    except Exception:
        return False


def _render_character(font, character: str, buffer: Optional[bytes], fontname: str) -> Optional[np.ndarray]:
    import fitz

    try:
        doc = fitz.open()
        page = doc.new_page(width=120, height=120)
        if buffer is None:
            page.insert_text(fitz.Point(20, 90), character, fontname=fontname, fontsize=64)
        else:
            page.insert_font(fontname="EMB", fontbuffer=buffer)
            page.insert_text(fitz.Point(20, 90), character, fontname="EMB", fontsize=64)
        pix = page.get_pixmap(colorspace=fitz.csGRAY, alpha=False)
        samples = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        doc.close()
        return rasterise_pixmap(samples)
    except Exception:
        return None


@functools.lru_cache(maxsize=8)
def _cached_bank(fingerprint: tuple) -> CharacterBank:  # pragma: no cover - cache shim
    raise RuntimeError("use build_bank")


def build_bank(embedded_fonts: Sequence[tuple[str, bytes]] = ()) -> CharacterBank:
    return CharacterBank(embedded_fonts)


def embedded_font_buffers(pdf) -> list[tuple[str, bytes]]:
    """The fonts the file carries, extracted so the sheet can be matched to itself."""
    out: dict[str, bytes] = {}
    for i in range(pdf.page_count):
        for font in pdf.page(i).get_fonts(full=True):
            xref, ext = int(font[0]), str(font[1])
            if ext in ("", "n/a"):
                continue
            try:
                name, extension, _ftype, buffer = pdf.doc.extract_font(xref)
            except Exception:
                continue
            if buffer and extension not in ("n/a", ""):
                out.setdefault(str(name) or f"xref{xref}", bytes(buffer))
    return [(name, out[name]) for name in sorted(out)]


# ---------------------------------------------------------------------------
# ink components - strokes joined into marks
# ---------------------------------------------------------------------------

@dataclass
class InkComponent:
    component_id: str
    page: int
    bbox: tuple[float, float, float, float]
    segment_ids: tuple[str, ...]
    polylines: tuple[tuple[tuple[float, float], ...], ...]
    ink_length: float
    path_ids: tuple[str, ...]
    straight: bool = False
    longest_run: float = 0.0

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.bbox[0] + self.bbox[2]) / 2.0, (self.bbox[1] + self.bbox[3]) / 2.0)


class _Union:
    def __init__(self, keys: Iterable[str]) -> None:
        self.parent = {k: k for k in keys}

    def find(self, key: str) -> str:
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[key] != root:
            self.parent[key], key = root, self.parent[key]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # smaller id becomes the root, so the result never depends on the
            # order in which pairs were offered
            if rb < ra:
                ra, rb = rb, ra
            self.parent[rb] = ra

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key in sorted(self.parent):
            out.setdefault(self.find(key), []).append(key)
        return out


def ink_components(segments: Sequence[Segment], tolerance: float = 0.6) -> list[InkComponent]:
    """Join segments that touch into connected marks."""
    union = _Union(s.segment_id for s in segments)
    by_id = {s.segment_id: s for s in segments}
    endpoints = SpatialIndex(
        [(f"{s.segment_id}#a", s.page, (s.a[0], s.a[1], s.a[0], s.a[1])) for s in segments]
        + [(f"{s.segment_id}#b", s.page, (s.b[0], s.b[1], s.b[0], s.b[1])) for s in segments]
    )
    for seg in segments:
        for point in (seg.a, seg.b):
            for key in endpoints.near_point(seg.page, point, tolerance):
                union.union(seg.segment_id, key.split("#")[0])
    out: list[InkComponent] = []
    for root, members in union.groups().items():
        segs = [by_id[m] for m in members if m in by_id]
        if not segs:
            continue
        xs = [v for s in segs for v in (s.a[0], s.b[0])]
        ys = [v for s in segs for v in (s.a[1], s.b[1])]
        bbox = qbbox((min(xs), min(ys), max(xs), max(ys)))
        polys = tuple(sorted((s.a, s.b) for s in segs))
        angles = {s.angle for s in segs}
        straight = max(angles) - min(angles) <= 2.0 if angles else True
        payload = {"p": segs[0].page, "b": list(bbox), "n": len(segs)}
        out.append(
            InkComponent(
                component_id=entity_id("ink", payload),
                page=segs[0].page,
                bbox=bbox,
                segment_ids=tuple(sorted(s.segment_id for s in segs)),
                polylines=polys,
                ink_length=q(sum(s.length for s in segs)),
                path_ids=tuple(sorted({s.path_id for s in segs})),
                straight=straight,
                longest_run=q(max(s.length for s in segs)),
            )
        )
    return sort_canonical(out, key=lambda c: (c.page, c.bbox, c.component_id))


# ---------------------------------------------------------------------------
# path glyphs - lettering that was exported as geometry
# ---------------------------------------------------------------------------

@dataclass
class InkLine:
    """A run of marks that share a baseline.  The unit the reader works in."""

    line_id: str
    page: int
    rotation: float
    direction: tuple[float, float]
    origin: tuple[float, float]
    cap_height: float
    component_ids: tuple[str, ...]
    bbox: tuple[float, float, float, float]


def _rotate(point: Sequence[float], cos_a: float, sin_a: float,
            about: Sequence[float]) -> tuple[float, float]:
    dx, dy = point[0] - about[0], point[1] - about[1]
    return (about[0] + dx * cos_a + dy * sin_a, about[1] - dx * sin_a + dy * cos_a)


def _principal_direction(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Direction of greatest spread, sign-normalised so it points right/down."""
    if len(points) < 2:
        return (1.0, 0.0)
    arr = np.asarray(points, dtype=float)
    centred = arr - arr.mean(axis=0)
    cov = centred.T @ centred
    values, vectors = np.linalg.eigh(cov)
    vec = vectors[:, int(np.argmax(values))]
    if abs(vec[0]) < 1e-9 and abs(vec[1]) < 1e-9:
        return (1.0, 0.0)
    if vec[0] < 0 or (abs(vec[0]) < 1e-9 and vec[1] < 0):
        vec = -vec
    norm = math.hypot(vec[0], vec[1])
    return (float(vec[0] / norm), float(vec[1] / norm))


def _cluster(components: Sequence[InkComponent]) -> list[list[InkComponent]]:
    """Link marks that are close relative to their own size."""
    if not components:
        return []
    index = SpatialIndex([(c.component_id, c.page, c.bbox) for c in components])
    by_id = {c.component_id: c for c in components}
    union = _Union(by_id)
    for comp in sort_canonical(components, key=lambda c: (c.page, c.bbox, c.component_id)):
        reach = max(1.0, 0.75 * max(comp.height, comp.width * 0.35))
        for key in index.within_distance(comp.page, comp.bbox, reach):
            if key == comp.component_id:
                continue
            other = by_id[key]
            tall = max(comp.height, other.height)
            if bbox_distance(comp.bbox, other.bbox) <= max(1.0, 0.75 * tall):
                union.union(comp.component_id, key)
    return [[by_id[m] for m in members] for members in union.groups().values()]


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _drop_outliers(group: Sequence[InkComponent]) -> list[InkComponent]:
    """Remove ink that is too big, or too straight and too long, to be a letter.

    A leader line that happens to end beside a label, a riser circle, a legend
    rule: all of them sit close enough to lettering to be linked to it, and all
    of them are decided *by shape against the group's own scale* - not against
    any absolute size, because a drawing may letter at any size.
    """
    heights = [c.height for c in group if c.height > 0.0]
    if not heights:
        return list(group)
    scale = _median(heights)
    if scale <= 0.0:
        scale = max(heights)
    kept = []
    for comp in group:
        too_tall = comp.height > 2.6 * scale
        too_wide = comp.width > 4.0 * scale
        rule = comp.straight and comp.longest_run > 3.0 * scale
        if too_tall or too_wide or rule:
            continue
        kept.append(comp)
    return kept


def group_ink_lines(components: Sequence[InkComponent], page_height: float,
                    max_mark_height: Optional[float] = None) -> list[InkLine]:
    """Group small marks into baselines, by position only.

    No array order is consulted anywhere: marks are linked when they are close
    relative to their own size, the group's direction comes from the spread of
    its ink, and everything downstream is ordered by projection onto that
    direction.  Clusters are cleaned of ink that cannot be lettering and then
    rebuilt, because a single leader line can otherwise weld two labels into
    one nonsense baseline.
    """
    limit = max_mark_height if max_mark_height is not None else max(6.0, page_height * 0.03)
    small = [c for c in components if c.height <= limit and c.width <= limit * 3.0]
    if not small:
        return []
    survivors: list[InkComponent] = []
    for group in _cluster(small):
        survivors.extend(_drop_outliers(group))
    lines: list[InkLine] = []
    for comps in _cluster(sort_canonical(survivors, key=lambda c: (c.page, c.bbox, c.component_id))):
        comps = _drop_outliers(comps)
        if not comps:
            continue
        points = [c.centre for c in comps]
        direction = _principal_direction(points) if len(points) > 1 else (1.0, 0.0)
        cos_a, sin_a = direction
        about = (
            sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points),
        )
        rotated = [
            _rotate(corner, cos_a, sin_a, about)
            for c in comps
            for corner in ((c.bbox[0], c.bbox[1]), (c.bbox[2], c.bbox[3]),
                           (c.bbox[0], c.bbox[3]), (c.bbox[2], c.bbox[1]))
        ]
        vs = [p[1] for p in rotated]
        cap = max(vs) - min(vs)
        xs = [c.bbox[0] for c in comps] + [c.bbox[2] for c in comps]
        ys = [c.bbox[1] for c in comps] + [c.bbox[3] for c in comps]
        payload = {"p": comps[0].page, "b": list(qbbox((min(xs), min(ys), max(xs), max(ys)))),
                   "n": len(comps)}
        lines.append(
            InkLine(
                line_id=entity_id("inkline", payload),
                page=comps[0].page,
                rotation=qa(math.degrees(math.atan2(-direction[1], direction[0]))),
                direction=(q(direction[0]), q(direction[1])),
                origin=(q(about[0]), q(about[1])),
                cap_height=q(cap),
                component_ids=tuple(sorted(c.component_id for c in comps)),
                bbox=qbbox((min(xs), min(ys), max(xs), max(ys))),
            )
        )
    return sort_canonical(lines, key=lambda l: (l.page, l.bbox, l.line_id))


def _split_characters(line: InkLine, comps: Sequence[InkComponent]) -> list[list[InkComponent]]:
    """Cut a baseline into characters where the ink stops, not where a list ends."""
    cos_a, sin_a = line.direction
    spans = []
    for comp in comps:
        corners = [(comp.bbox[0], comp.bbox[1]), (comp.bbox[2], comp.bbox[1]),
                   (comp.bbox[0], comp.bbox[3]), (comp.bbox[2], comp.bbox[3])]
        us = [_rotate(c, cos_a, sin_a, line.origin)[0] for c in corners]
        spans.append((q(min(us)), q(max(us)), comp))
    spans.sort(key=lambda s: (s[0], s[1], s[2].component_id))
    cap = max(line.cap_height, 1.0)
    # Strokes belong to the same character when they occupy the same place
    # along the baseline, not when they are merely close: adjacent characters
    # in a CAD font are separated by a gap far smaller than a character.
    gap_limit = 0.10 * cap
    overlap_ratio = 0.25
    groups: list[list[tuple[float, float, InkComponent]]] = []
    for span in spans:
        start, end, _ = span
        if groups:
            g_start = min(s[0] for s in groups[-1])
            g_end = max(s[1] for s in groups[-1])
            overlap = min(end, g_end) - max(start, g_start)
            narrower = max(1e-6, min(end - start, g_end - g_start))
            if overlap > overlap_ratio * narrower or start - g_end <= gap_limit:
                groups[-1].append(span)
                continue
        groups.append([span])
    groups = _resplit_by_pitch(groups, gap_limit)
    return [[s[2] for s in group] for group in groups]


def _resplit_by_pitch(groups: list[list[tuple[float, float, "InkComponent"]]],
                      gap_limit: float) -> list[list[tuple[float, float, "InkComponent"]]]:
    """Cut a unit that is much wider than its neighbours at its widest gap.

    Two characters whose ink happens to touch would otherwise be read as one
    shape and mis-classified; the line's own character pitch says when that has
    happened.
    """
    widths = [max(s[1] for s in g) - min(s[0] for s in g) for g in groups]
    if len(widths) < 3:
        return groups
    typical = _median([w for w in widths if w > 0.0])
    if typical <= 0.0:
        return groups
    out: list[list[tuple[float, float, InkComponent]]] = []
    for group, width in zip(groups, widths):
        if width <= 1.7 * typical or len(group) < 2:
            out.append(group)
            continue
        ordered = sorted(group, key=lambda s: (s[0], s[1], s[2].component_id))
        best_index, best_gap = None, gap_limit
        end = ordered[0][1]
        for i in range(1, len(ordered)):
            gap = ordered[i][0] - end
            if gap > best_gap:
                best_index, best_gap = i, gap
            end = max(end, ordered[i][1])
        if best_index is None:
            out.append(group)
        else:
            out.append(ordered[:best_index])
            out.append(ordered[best_index:])
    return out


def extract_path_glyphs(lines: Sequence[InkLine], components: Sequence[InkComponent],
                        bank: CharacterBank) -> list[Glyph]:
    """Read the characters out of lettering that has no text layer."""
    by_id = {c.component_id: c for c in components}
    out: list[Glyph] = []
    for line in lines:
        comps = [by_id[cid] for cid in line.component_ids if cid in by_id]
        if not comps:
            continue
        cos_a, sin_a = line.direction
        rotated_v = [
            _rotate((x, y), cos_a, sin_a, line.origin)[1]
            for c in comps
            for x, y in ((c.bbox[0], c.bbox[1]), (c.bbox[2], c.bbox[3]))
        ]
        baseline_v = max(rotated_v)
        cap = max(line.cap_height, 1e-3)
        for group in _split_characters(line, comps):
            xs = [v for c in group for v in (c.bbox[0], c.bbox[2])]
            ys = [v for c in group for v in (c.bbox[1], c.bbox[3])]
            bbox = qbbox((min(xs), min(ys), max(xs), max(ys)))
            # normalise the mark into its own upright frame before matching
            about = ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
            polys = []
            for comp in group:
                for poly in comp.polylines:
                    polys.append([_rotate(p, cos_a, sin_a, about) for p in poly])
            rxs = [p[0] for poly in polys for p in poly]
            rys = [p[1] for poly in polys for p in poly]
            rbbox = (min(rxs), min(rys), max(rxs), max(rys))
            mask = rasterise_strokes(polys, rbbox)
            corners_v = [_rotate((x, y), cos_a, sin_a, line.origin)[1]
                         for x, y in ((bbox[0], bbox[1]), (bbox[2], bbox[3]))]
            char_top, char_bottom = min(corners_v), max(corners_v)
            rel_height = q((char_bottom - char_top) / cap)
            rel_offset = q((baseline_v - char_bottom) / cap)
            width = max(rbbox[2] - rbbox[0], 1e-6)
            height = max(rbbox[3] - rbbox[1], 1e-6)
            alternatives = bank.classify(mask, rel_height, rel_offset, q(width / height))
            character = alternatives[0][0] if alternatives else ""
            runner_up = alternatives[1][1] if len(alternatives) > 1 else 0.0
            confidence = q(max(0.0, 1.0 - runner_up)) if alternatives else 0.0
            payload = {"p": line.page, "b": list(bbox), "r": line.rotation}
            out.append(
                Glyph(
                    glyph_id=entity_id("gly", payload),
                    page=line.page,
                    character=character,
                    bbox=bbox,
                    origin=(q(bbox[0]), q(_unrotate_v(line, bbox, baseline_v))),
                    width=q(bbox[2] - bbox[0]),
                    height=q(bbox[3] - bbox[1]),
                    baseline=q(baseline_v),
                    rotation=line.rotation,
                    font="",
                    size=q(cap),
                    transform=(q(cos_a), q(sin_a), q(-sin_a), q(cos_a), q(bbox[0]), q(bbox[3])),
                    source="path",
                    source_object_ids=tuple(sorted({pid for c in group for pid in c.path_ids})),
                    alternatives=alternatives,
                    confidence=confidence,
                )
            )
    return sort_canonical(out, key=lambda g: (g.page, g.bbox, g.glyph_id))


def _unrotate_v(line: InkLine, bbox: Sequence[float], baseline_v: float) -> float:
    """Baseline height of a glyph expressed back in page coordinates."""
    cos_a, sin_a = line.direction
    u = _rotate(((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0), cos_a, sin_a, line.origin)[0]
    # inverse rotation of (u, baseline_v) about the line origin
    dx, dy = u - line.origin[0], baseline_v - line.origin[1]
    return line.origin[1] + dx * sin_a + dy * cos_a


# ---------------------------------------------------------------------------
# text glyphs
# ---------------------------------------------------------------------------

def extract_text_glyphs(objects: Sequence[PdfObject]) -> list[Glyph]:
    """Characters the PDF hands over directly."""
    out: list[Glyph] = []
    for obj in objects:
        if obj.kind != "glyph":
            continue
        character = str(obj.source.get("character", ""))
        bbox = obj.bbox
        rotation = qa(float(obj.style.get("rotation", 0.0)))
        origin = tuple(obj.coordinates)
        out.append(
            Glyph(
                glyph_id=entity_id("gly", {"p": obj.page, "b": list(bbox), "c": character,
                                           "r": rotation}),
                page=obj.page,
                character=character,
                bbox=bbox,
                origin=(q(origin[0]), q(origin[1])),
                width=q(bbox[2] - bbox[0]),
                height=q(bbox[3] - bbox[1]),
                baseline=q(origin[1]),
                rotation=rotation,
                font=str(obj.style.get("font", "")),
                size=q(float(obj.style.get("size", 0.0))),
                transform=obj.transform,
                source="text",
                source_object_ids=(obj.object_id,),
                alternatives=((character, 1.0),) if character else (),
                confidence=1.0,
            )
        )
    return sort_canonical(out, key=lambda g: (g.page, g.bbox, g.glyph_id))


class GlyphModel:
    """Every glyph in the document, with the searches the spec asks for."""

    def __init__(self, glyphs: Sequence[Glyph]) -> None:
        self.glyphs = sort_canonical(glyphs, key=lambda g: (g.page, g.bbox, g.glyph_id))
        self.by_id = {g.glyph_id: g for g in self.glyphs}
        self.index = SpatialIndex([(g.glyph_id, g.page, g.bbox) for g in self.glyphs])

    def search_character(self, character: str) -> list[Glyph]:
        return [g for g in self.glyphs if g.character == character]

    def search_region(self, page: int, bbox: Sequence[float]) -> list[Glyph]:
        return [self.by_id[k] for k in self.index.intersecting_bbox(page, bbox)]

    def search_near_point(self, page: int, point: Sequence[float], radius: float) -> list[Glyph]:
        return [self.by_id[k] for k in self.index.near_point(page, point, radius)]

    def search_near_geometry(self, page: int, bbox: Sequence[float], distance: float) -> list[Glyph]:
        return [self.by_id[k] for k in self.index.within_distance(page, bbox, distance)]

    def unresolved(self) -> list[Glyph]:
        return [g for g in self.glyphs if not g.character or g.confidence < 0.25]

    def to_json(self) -> dict:
        by_source: dict[str, int] = {}
        for g in self.glyphs:
            by_source[g.source] = by_source.get(g.source, 0) + 1
        return {
            "glyphs": len(self.glyphs),
            "bySource": {k: by_source[k] for k in sorted(by_source)},
            "unresolved": len(self.unresolved()),
            "meanConfidence": q(sum(g.confidence for g in self.glyphs) / max(1, len(self.glyphs))),
        }
