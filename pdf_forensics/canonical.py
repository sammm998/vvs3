"""Determinism foundation.

Everything in this package is addressed by *content*, never by the order in
which PyMuPDF happened to hand it to us.  Two rules follow from that and are
enforced everywhere:

* coordinates are quantised before they take part in an identity or a sort, so
  that a value that differs only in floating-point noise cannot produce two
  identities;
* every collection that leaves a stage is sorted by a canonical key built from
  the content itself.  ``array order`` is never a tie-breaker, and neither is
  a PDF object number.

``tests/pdf_forensics/test_determinism.py`` runs the whole pipeline over the
original, the reversed and two seeded permutations of every input collection
and requires byte-identical output.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Sequence

# One thousandth of a PDF point: far below anything a drawing can express, far
# above the noise of a matrix multiplication.
QUANT = 1000.0
# Angles are compared at a tenth of a degree.
ANGLE_QUANT = 10.0
# Quantities that are not coordinates - a scale factor, a length in metres -
# need far more resolution than a thousandth: a scale of 0.0176389 metres per
# point rounded to 0.018 would put every length on the sheet 2 % out.
SCALAR_QUANT = 1_000_000_000.0


def q(value: float) -> float:
    """Quantise a coordinate.  ``-0.0`` and ``0.0`` collapse to the same value."""
    if value is None or not math.isfinite(value):
        return 0.0
    return round(value * QUANT) / QUANT + 0.0


def qs(value: float) -> float:
    """Quantise a scalar that is not a coordinate (scale, metres, ratios)."""
    if value is None or not math.isfinite(value):
        return 0.0
    return round(value * SCALAR_QUANT) / SCALAR_QUANT + 0.0


def qa(angle_deg: float) -> float:
    """Quantise an angle in degrees into ``[0, 360)``."""
    if angle_deg is None or not math.isfinite(angle_deg):
        return 0.0
    a = round(float(angle_deg) * ANGLE_QUANT) / ANGLE_QUANT
    return (a % 360.0) + 0.0


def qpoint(p: Sequence[float]) -> tuple[float, float]:
    return (q(p[0]), q(p[1]))


def qbbox(b: Sequence[float]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = (q(v) for v in b)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def qpoly(points: Iterable[Sequence[float]]) -> tuple[tuple[float, float], ...]:
    return tuple(qpoint(p) for p in points)


def undirected(points: Sequence[Sequence[float]]) -> tuple[tuple[float, float], ...]:
    """A polyline's direction-independent form.

    A drawing does not care which end of a line was emitted first, so identity
    must not either.  The smaller of the two orientations wins.
    """
    fwd = qpoly(points)
    rev = tuple(reversed(fwd))
    return fwd if fwd <= rev else rev


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def _default(obj: Any) -> Any:
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=lambda v: canonical_json(v))
    if isinstance(obj, tuple):
        return list(obj)
    if isinstance(obj, float):
        return q(obj)
    if hasattr(obj, "to_json"):
        return obj.to_json()
    raise TypeError(f"cannot serialise {type(obj)!r}")


def digest(payload: Any, length: int = 12) -> str:
    return hashlib.sha1(canonical_json(payload).encode("utf-8")).hexdigest()[:length]


def entity_id(prefix: str, payload: Any, occurrence: int = 0) -> str:
    """A content-addressed identifier.

    ``occurrence`` distinguishes objects a PDF genuinely contains more than
    once.  Identical content in the same occurrence slot is the same entity in
    every run, which is what makes the whole pipeline order-independent.
    """
    return f"{prefix}:{digest(payload)}:{occurrence}"


def sort_canonical(items: Iterable[Any], key) -> list[Any]:
    """Sort by a content key, with the key itself as the final tie-breaker.

    Deliberately not ``sorted(items)``: an item's position in the input must
    never influence its position in the output.
    """
    decorated = [(canonical_json(key(item)), item) for item in items]
    decorated.sort(key=lambda pair: pair[0])
    return [item for _, item in decorated]


def stable_group(items: Iterable[Any], key) -> list[tuple[Any, list[Any]]]:
    """Group by ``key`` and return the groups in canonical key order."""
    buckets: dict[str, tuple[Any, list[Any]]] = {}
    for item in items:
        k = key(item)
        kk = canonical_json(k)
        buckets.setdefault(kk, (k, []))[1].append(item)
    return [buckets[kk] for kk in sorted(buckets)]
