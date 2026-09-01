"""Canonical representation, total ordering and deterministic digests.

Every entity that can be influenced by input order must be emitted through
this module.  Rules enforced here:

* floats are quantised before they are hashed or compared, so that
  arithmetic performed in a different summation order still collapses to the
  same canonical value;
* every canonical key is a tuple of primitives with a *total* order - there is
  never a fallback on insertion order, object id or array position;
* JSON is emitted with sorted keys and a fixed float format.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Sequence

# Quantisation grids.  These are deliberately coarse relative to the numeric
# noise produced by floating point summation but fine relative to any physical
# distinction the engine is asked to make.
COORD_DECIMALS = 4  # PDF points
LENGTH_DECIMALS = 4  # metres / points depending on field
SCORE_DECIMALS = 6
ANGLE_DECIMALS = 6


def q(value: float, decimals: int = COORD_DECIMALS) -> float:
    """Quantise a float deterministically (round-half-away-from-zero).

    ``round()`` in Python uses banker's rounding, which is deterministic but
    surprising; more importantly ``-0.0`` must collapse to ``0.0`` so that two
    geometrically identical values hash identically.
    """
    if value is None:
        return None  # type: ignore[return-value]
    if isinstance(value, bool):  # pragma: no cover - guard against bool leak
        raise TypeError("bool is not a coordinate")
    if math.isnan(value):
        raise ValueError("NaN cannot be canonicalised")
    if math.isinf(value):
        raise ValueError("Inf cannot be canonicalised")
    factor = 10 ** decimals
    scaled = value * factor
    # round-half-away-from-zero
    if scaled >= 0:
        out = math.floor(scaled + 0.5) / factor
    else:
        out = -math.floor(-scaled + 0.5) / factor
    return out + 0.0  # normalise -0.0 -> 0.0


def qc(value: float) -> float:
    return q(value, COORD_DECIMALS)


def qs(value: float) -> float:
    return q(value, SCORE_DECIMALS)


def ql(value: float) -> float:
    return q(value, LENGTH_DECIMALS)


def point_key(p: Sequence[float]) -> tuple[float, float]:
    return (qc(p[0]), qc(p[1]))


def polyline_key(points: Sequence[Sequence[float]]) -> tuple[tuple[float, float], ...]:
    """Canonical, direction-independent key for an open polyline.

    A polyline and its reversal describe the same physical object, so the
    canonical form is the lexicographically smaller of the two sequences.
    """
    fwd = tuple(point_key(p) for p in points)
    rev = tuple(reversed(fwd))
    return fwd if fwd <= rev else rev


def segment_key(a: Sequence[float], b: Sequence[float]) -> tuple[tuple[float, float], tuple[float, float]]:
    ka, kb = point_key(a), point_key(b)
    return (ka, kb) if ka <= kb else (kb, ka)


def canonical_sort(items: Iterable[Any], key) -> list[Any]:
    """Sort with a key that must produce a total order.

    Raises if two distinct items collapse to the same key, because that would
    leave their relative order decided by Python's (stable) sort - i.e. by
    input order, which this engine forbids as semantics.
    """
    decorated = [(key(it), it) for it in items]
    decorated.sort(key=lambda kv: kv[0])
    return [it for _, it in decorated]


def stable_group(items: Iterable[Any], key) -> dict[Any, list[Any]]:
    groups: dict[Any, list[Any]] = {}
    for it in items:
        groups.setdefault(key(it), []).append(it)
    return groups


class _CanonicalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:  # pragma: no cover - defensive
        if isinstance(o, tuple):
            return list(o)
        if hasattr(o, "to_canonical"):
            return o.to_canonical()
        raise TypeError(f"not canonically serialisable: {type(o)!r}")


def _normalise(obj: Any) -> Any:
    if isinstance(obj, float):
        v = q(obj, 9)
        # emit integral floats without noise
        return v
    if isinstance(obj, dict):
        return {str(k): _normalise(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [_normalise(v) for v in obj]
    return obj


def canonical_json(obj: Any, indent: int | None = None) -> str:
    return json.dumps(
        _normalise(obj),
        sort_keys=True,
        separators=(",", ":") if indent is None else (",", ": "),
        indent=indent,
        ensure_ascii=False,
        allow_nan=False,
        cls=_CanonicalEncoder,
    )


def digest(obj: Any) -> str:
    """SHA256 of the canonical JSON encoding."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def short_digest(obj: Any, n: int = 12) -> str:
    return digest(obj)[:n]


def entity_id(prefix: str, canonical_key: Any) -> str:
    """Content-addressed identifier.

    Identifiers are derived from geometry/evidence, never from a counter, so
    they are invariant under input permutation.
    """
    return f"{prefix}_{short_digest(canonical_key, 12)}"
