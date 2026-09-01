"""Twenty-second and nineteenth searches: measurement, including verticals.

Lengths come from the reconstructed geometry and the sheet's scale, and from
nothing else - not from a rendered image, not from a marked drawing, not from a
model's opinion.  Without a scale there are no metres at all: the analysis
reports points and says ``SCALE_UNKNOWN``.

Vertical pipe is not measurable from a plan at all unless the sheet states the
heights, so a riser contributes a length only when two elevations can be read
near it.  One elevation is ``VERTICAL_HEIGHT_UNKNOWN``, which is a fact about
the drawing rather than a failure to be smoothed over with an assumption.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .canonical import entity_id, q, qs, sort_canonical
from .model import PhysicalPipe, Reason, State, TextItem
from .spatial_index import SpatialIndex

# A level: an optional prefix, a sign, and a decimal number.  Structure, not a
# list of the words a particular office uses.
_ELEVATION = re.compile(r"^(?P<prefix>[A-ZÅÄÖ]{0,4})\s*(?P<sign>[+-])\s*(?P<value>\d{1,3}[.,]\d{1,3})$")


@dataclass(frozen=True)
class Elevation:
    text_id: str
    page: int
    bbox: tuple[float, float, float, float]
    text: str
    metres: float
    prefix: str

    def to_json(self) -> dict:
        return {"textId": self.text_id, "page": self.page, "bbox": list(self.bbox),
                "text": self.text, "metres": self.metres, "prefix": self.prefix}


def find_elevations(text_items: Sequence[TextItem]) -> list[Elevation]:
    out: list[Elevation] = []
    for item in sort_canonical(text_items, key=lambda t: (t.page, t.bbox, t.text_id)):
        for word in item.text.split():
            match = _ELEVATION.match(word.strip())
            if not match:
                continue
            value = float(match.group("value").replace(",", "."))
            if match.group("sign") == "-":
                value = -value
            out.append(
                Elevation(text_id=item.text_id, page=item.page, bbox=item.bbox,
                          text=word, metres=q(value), prefix=match.group("prefix"))
            )
    return out


@dataclass(frozen=True)
class Riser:
    """A pipe that leaves the plane, and what the sheet says about its height."""

    riser_id: str
    page: int
    point: tuple[float, float]
    pipe_id: str
    elevations: tuple[float, ...]
    text_ids: tuple[str, ...]
    height_metres: Optional[float]
    state: str
    reasons: tuple[str, ...]

    def to_json(self) -> dict:
        return {"riserId": self.riser_id, "page": self.page, "point": list(self.point),
                "pipeId": self.pipe_id, "elevations": list(self.elevations),
                "textIds": list(self.text_ids), "heightMetres": self.height_metres,
                "state": self.state, "reasons": list(self.reasons)}


def find_risers(pipes: Sequence[PhysicalPipe], elevations: Sequence[Elevation],
                default_reach: float = 34.0) -> list[Riser]:
    """Look for height evidence at the ends of pipes."""
    if not elevations:
        return []
    index = SpatialIndex([(f"{i}", e.page, e.bbox) for i, e in enumerate(elevations)])
    risers: list[Riser] = []
    for pipe in pipes:
        if not pipe.centerline:
            continue
        ends = [pipe.centerline[0], pipe.centerline[-1]]
        for point in ends:
            keys = index.near_point(pipe.page, point, default_reach)
            found = sorted({elevations[int(k)].metres for k in keys})
            text_ids = tuple(sorted({elevations[int(k)].text_id for k in keys}))
            if not found:
                continue
            if len(found) >= 2:
                height = q(max(found) - min(found))
                state, reasons = State.CONFIRMED, ("TWO_ELEVATIONS_AT_THE_RISER",)
            else:
                height, state = None, State.UNRESOLVED
                reasons = (Reason.VERTICAL_HEIGHT_UNKNOWN, "ONE_ELEVATION_ONLY")
            risers.append(
                Riser(
                    riser_id=entity_id("riser", {"p": pipe.page, "x": q(point[0]),
                                                 "y": q(point[1]), "i": pipe.pipe_id}),
                    page=pipe.page,
                    point=(q(point[0]), q(point[1])),
                    pipe_id=pipe.pipe_id,
                    elevations=tuple(found),
                    text_ids=text_ids,
                    height_metres=height,
                    state=state,
                    reasons=reasons,
                )
            )
    return sort_canonical(risers, key=lambda r: (r.page, r.point, r.pipe_id, r.riser_id))


@dataclass
class PipeMeasurement:
    pipe_id: str
    horizontal_points: float
    horizontal_metres: Optional[float]
    vertical_metres: Optional[float]
    total_metres: Optional[float]
    state: str
    reasons: tuple[str, ...]
    calculation: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"pipeId": self.pipe_id, "horizontalPoints": self.horizontal_points,
                "horizontalMetres": self.horizontal_metres,
                "verticalMetres": self.vertical_metres, "totalMetres": self.total_metres,
                "state": self.state, "reasons": list(self.reasons),
                "calculation": self.calculation}


def measure(pipes: Sequence[PhysicalPipe], risers: Sequence[Riser],
            metres_per_point: Optional[float], scale_state: str) -> list[PipeMeasurement]:
    """Horizontal, vertical and total length for every pipe."""
    risers_by_pipe: dict[str, list[Riser]] = {}
    for riser in risers:
        risers_by_pipe.setdefault(riser.pipe_id, []).append(riser)
    out: list[PipeMeasurement] = []
    for pipe in pipes:
        reasons: list[str] = []
        horizontal_m: Optional[float] = None
        if metres_per_point is not None:
            horizontal_m = qs(pipe.horizontal_points * metres_per_point)
        else:
            reasons.append(Reason.SCALE_UNKNOWN)
        mine = risers_by_pipe.get(pipe.pipe_id, [])
        heights = [r.height_metres for r in mine if r.height_metres is not None]
        vertical_m = qs(sum(heights)) if heights else None
        if mine and not heights:
            reasons.append(Reason.VERTICAL_HEIGHT_UNKNOWN)
        total: Optional[float] = None
        if horizontal_m is not None:
            total = qs(horizontal_m + (vertical_m or 0.0))
        state = State.CONFIRMED if (horizontal_m is not None and not reasons) else (
            State.AMBIGUOUS if horizontal_m is not None else State.UNRESOLVED)
        out.append(
            PipeMeasurement(
                pipe_id=pipe.pipe_id,
                horizontal_points=pipe.horizontal_points,
                horizontal_metres=horizontal_m,
                vertical_metres=vertical_m,
                total_metres=total,
                state=state,
                reasons=tuple(sorted(set(reasons))),
                calculation={
                    "metresPerPoint": metres_per_point,
                    "scaleState": scale_state,
                    "runIds": list(pipe.run_ids),
                    "risers": [r.riser_id for r in mine],
                    "formula": "horizontal_points * metres_per_point + sum(riser heights)",
                },
            )
        )
    return sort_canonical(out, key=lambda m: (m.pipe_id,))


def aggregate(pipes: Sequence[PhysicalPipe], measurements: Sequence[PipeMeasurement]) -> list[dict]:
    """The take-off: one row per designation and size, with what is not known."""
    by_pipe = {m.pipe_id: m for m in measurements}
    rows: dict[tuple, dict] = {}
    for pipe in pipes:
        measurement = by_pipe.get(pipe.pipe_id)
        key = (pipe.designation or "", pipe.diameter_mm if pipe.diameter_mm is not None else -1.0)
        row = rows.setdefault(key, {
            "designation": pipe.designation,
            "designationState": pipe.designation_state,
            "diameterMm": pipe.diameter_mm,
            "diameterState": pipe.diameter_state,
            "pipeCount": 0,
            "horizontalMetres": 0.0,
            "verticalMetres": 0.0,
            "totalMetres": 0.0,
            "measurableCount": 0,
            "notMeasurableCount": 0,
            "pipeIds": [],
        })
        row["pipeCount"] += 1
        row["pipeIds"].append(pipe.pipe_id)
        if measurement and measurement.horizontal_metres is not None:
            row["horizontalMetres"] += measurement.horizontal_metres
            row["verticalMetres"] += measurement.vertical_metres or 0.0
            row["totalMetres"] += measurement.total_metres or 0.0
            row["measurableCount"] += 1
        else:
            row["notMeasurableCount"] += 1
    out = []
    for key in sorted(rows, key=lambda k: (str(k[0]), k[1])):
        row = dict(rows[key])
        row["horizontalMetres"] = qs(row["horizontalMetres"])
        row["verticalMetres"] = qs(row["verticalMetres"])
        row["totalMetres"] = qs(row["totalMetres"])
        row["pipeIds"] = sorted(row["pipeIds"])
        out.append(row)
    return out


def to_json(measurements: Sequence[PipeMeasurement], risers: Sequence[Riser],
            rows: Sequence[dict]) -> dict:
    measurable = [m for m in measurements if m.horizontal_metres is not None]
    return {
        "pipesMeasured": len(measurable),
        "pipesNotMeasurable": len(measurements) - len(measurable),
        "risers": len(risers),
        "risersWithHeight": len([r for r in risers if r.height_metres is not None]),
        "totalHorizontalMetres": qs(sum(m.horizontal_metres or 0.0 for m in measurements)),
        "totalVerticalMetres": qs(sum(m.vertical_metres or 0.0 for m in measurements)),
        "quantityRows": len(rows),
    }
