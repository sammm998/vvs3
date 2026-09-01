"""Deterministic generator for CAD-like VVS test drawings.

These files stand in for a real CAD export: mono-chrome stroked geometry,
double-line pipes at true scale, annotation text drawn as single-stroke
polylines (no PDF text layer), a legend panel that repeats the pipe codes, a
title block, a scale bar, riser symbols with elevation notes, and - on one
variant - previous manual take-off stored as PDF annotations.

The generator also writes a ground-truth manifest.  That manifest is consumed
**only** by :mod:`vvs_pipe.evaluation` in the post-hoc comparison step.  The
blind pipeline never sees it, and ``tests/python/test_blind_leakage.py``
proves the import closure cannot reach it.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from .stroke_font import text_advance, text_strokes

PT_PER_MM = 72.0 / 25.4
BLACK = (0.0, 0.0, 0.0)


@dataclass
class PipeSpec:
    name: str
    designation: str
    dn_mm: float
    centerline: list[tuple[float, float]]


@dataclass
class DrawingSpec:
    file_stem: str
    width: float
    height: float
    scale_denominator: float
    pipes: list[PipeSpec]
    callouts: list[tuple[str, tuple[float, float], tuple[float, float]]]
    legend_entries: list[str]
    title_lines: list[str]
    room_labels: list[tuple[str, tuple[float, float]]]
    risers: list[dict]
    native_text: bool = False
    with_takeoff_annotations: bool = False
    text_size: float = 7.0

    @property
    def metres_per_point(self) -> float:
        return (25.4 / 72.0) * self.scale_denominator / 1000.0


def _offset_polyline(points: list[tuple[float, float]], d: float) -> list[tuple[float, float]]:
    """Mitre offset of an open polyline by signed distance ``d``."""
    n = len(points)
    normals: list[tuple[float, float]] = []
    for i in range(n - 1):
        ax, ay = points[i]
        bx, by = points[i + 1]
        ln = math.hypot(bx - ax, by - ay)
        ux, uy = (bx - ax) / ln, (by - ay) / ln
        normals.append((-uy, ux))
    out: list[tuple[float, float]] = []
    out.append((points[0][0] + normals[0][0] * d, points[0][1] + normals[0][1] * d))
    for i in range(1, n - 1):
        n1, n2 = normals[i - 1], normals[i]
        mx, my = n1[0] + n2[0], n1[1] + n2[1]
        ml = math.hypot(mx, my)
        if ml < 1e-9:
            mx, my, ml = n1[0], n1[1], 1.0
        cos_half = (n1[0] * mx + n1[1] * my) / ml
        scale = d / max(cos_half, 1e-6)
        out.append((points[i][0] + mx / ml * scale, points[i][1] + my / ml * scale))
    out.append((points[-1][0] + normals[-1][0] * d, points[-1][1] + normals[-1][1] * d))
    return out


class _Canvas:
    def __init__(self, page: "fitz.Page") -> None:
        self.page = page

    def polyline(self, pts, width: float, close: bool = False, fill=None) -> None:
        sh = self.page.new_shape()
        p = [fitz.Point(*q) for q in pts]
        for i in range(len(p) - 1):
            sh.draw_line(p[i], p[i + 1])
        if close and len(p) > 2:
            sh.draw_line(p[-1], p[0])
        sh.finish(color=BLACK, width=width, fill=fill, closePath=False)
        sh.commit()

    def rect(self, x0, y0, x1, y1, width: float, fill=None) -> None:
        sh = self.page.new_shape()
        sh.draw_rect(fitz.Rect(x0, y0, x1, y1))
        sh.finish(color=BLACK, width=width, fill=fill)
        sh.commit()

    def stroke_text(self, text: str, origin, size: float, width: float = 0.25, angle: float = 0.0) -> None:
        for poly in text_strokes(text, origin, size, angle):
            self.polyline(poly, width)

    def circle(self, cx, cy, r, width: float, segments: int = 24) -> None:
        pts = [
            (cx + r * math.cos(2 * math.pi * i / segments), cy + r * math.sin(2 * math.pi * i / segments))
            for i in range(segments)
        ]
        pts.append(pts[0])
        self.polyline(pts, width)


def _draw(spec: DrawingSpec, page: "fitz.Page") -> None:
    c = _Canvas(page)
    mpp = spec.metres_per_point

    # sheet frame
    c.rect(15, 15, spec.width - 15, spec.height - 15, 0.9)
    c.rect(25, 25, spec.width - 25, spec.height - 25, 0.5)

    # pipes as true-scale double lines
    for p in spec.pipes:
        half = (p.dn_mm / 1000.0) / mpp / 2.0
        for sgn in (+1.0, -1.0):
            c.polyline(_offset_polyline(p.centerline, sgn * half), 0.35)

    # riser symbols and their elevation notes
    for r in spec.risers:
        cx, cy = r["point"]
        c.circle(cx, cy, r.get("radius", 6.0), 0.35)
        c.polyline([(cx - 8.5, cy - 8.5), (cx + 8.5, cy + 8.5)], 0.35)
        for i, note in enumerate(r["elevations"]):
            c.stroke_text(note, (cx + 16.0, cy - 6.0 + i * 20.0), spec.text_size * 0.85)

    # designation callouts with leader lines
    for text, text_origin, target in spec.callouts:
        c.stroke_text(text, text_origin, spec.text_size)
        w = text_advance(text, spec.text_size)
        anchor = (text_origin[0] + w * 0.5, text_origin[1] + 1.6)
        c.polyline([anchor, target], 0.2)

    # room labels
    for label, origin in spec.room_labels:
        c.stroke_text(label, origin, spec.text_size * 1.15)

    # scale bar: 5 m, alternating filled cells
    bar_m = 5.0
    bar_len = bar_m / mpp
    bx, by = 60.0, spec.height - 70.0
    cell = bar_len / 5.0
    for i in range(5):
        c.rect(bx + i * cell, by, bx + (i + 1) * cell, by + 7.0, 0.3, fill=BLACK if i % 2 == 0 else None)
    c.stroke_text("0", (bx - 2.0, by - 4.0), spec.text_size * 0.8)
    c.stroke_text("5", (bx + bar_len - 3.0, by - 4.0), spec.text_size * 0.8)
    c.stroke_text("M", (bx + bar_len + 8.0, by - 4.0), spec.text_size * 0.8)

    # legend panel
    lx0, ly0 = spec.width - 300.0, 90.0
    lx1, ly1 = spec.width - 40.0, 100.0 + 22.0 * (len(spec.legend_entries) + 1)
    c.rect(lx0, ly0, lx1, ly1, 0.5)
    c.stroke_text("TECKENFORKLARING", (lx0 + 10.0, ly0 + 20.0), spec.text_size)
    for i, entry in enumerate(spec.legend_entries):
        y = ly0 + 44.0 + i * 22.0
        c.polyline([(lx0 + 10.0, y - 3.0), (lx0 + 44.0, y - 3.0)], 0.35)
        c.polyline([(lx0 + 10.0, y - 6.5), (lx0 + 44.0, y - 6.5)], 0.35)
        c.stroke_text(entry, (lx0 + 54.0, y), spec.text_size)

    # title block
    tx0, ty0 = spec.width - 300.0, spec.height - 150.0
    tx1, ty1 = spec.width - 40.0, spec.height - 40.0
    c.rect(tx0, ty0, tx1, ty1, 0.5)
    for i, line in enumerate(spec.title_lines):
        c.stroke_text(line, (tx0 + 10.0, ty0 + 24.0 + i * 20.0), spec.text_size)

    if spec.native_text:
        # A second variant of the same sheet keeps a real text layer, so the
        # engine's text-span path is exercised too.
        for text, origin, _target in spec.callouts:
            page.insert_text(fitz.Point(*origin), text, fontname="helv", fontsize=spec.text_size)


def _add_takeoff_annotations(page: "fitz.Page", spec: DrawingSpec) -> None:
    """Previous manual quantity take-off, stored the way a reviewer leaves it."""
    main = spec.pipes[0]
    a = page.add_polyline_annot([fitz.Point(*p) for p in main.centerline])
    a.set_colors(stroke=(1, 0, 0))
    a.set_border(width=2.0)
    a.set_info(content=f"{main.designation} 10.4 m")
    a.update()
    box = fitz.Rect(spec.width - 330.0, 60.0, spec.width - 20.0, 80.0)
    s = page.add_rect_annot(box)
    s.set_colors(stroke=(1, 0, 0))
    s.set_info(content="MANGDNING KLAR")
    s.update()


def build(spec: DrawingSpec, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def render(path: Path, annotations: bool) -> None:
        doc = fitz.open()
        page = doc.new_page(width=spec.width, height=spec.height)
        _draw(spec, page)
        if annotations:
            _add_takeoff_annotations(page, spec)
        doc.set_metadata({"producer": "vvs-fixture", "creator": "vvs-fixture"})
        doc.save(str(path), deflate=True, garbage=0, clean=False, pretty=False, no_new_id=True)
        doc.close()

    clean_path = out_dir / f"{spec.file_stem}_clean.pdf"
    render(clean_path, annotations=False)
    paths = {"clean": str(clean_path)}
    if spec.with_takeoff_annotations:
        marked_path = out_dir / f"{spec.file_stem}_with_takeoff.pdf"
        render(marked_path, annotations=True)
        paths["with_takeoff"] = str(marked_path)

    truth = ground_truth(spec)
    truth["files"] = paths
    (out_dir / f"{spec.file_stem}_truth.json").write_text(
        json.dumps(truth, indent=2, sort_keys=True), encoding="utf-8"
    )
    return truth


def ground_truth(spec: DrawingSpec) -> dict:
    """Post-hoc facit.  Never read by the pipeline."""
    mpp = spec.metres_per_point
    rows: dict[tuple[str, float], dict] = {}
    for p in spec.pipes:
        length_pt = sum(
            math.dist(p.centerline[i], p.centerline[i + 1]) for i in range(len(p.centerline) - 1)
        )
        key = (p.designation, p.dn_mm)
        row = rows.setdefault(
            key,
            {
                "designation": p.designation,
                "diameterMm": p.dn_mm,
                "horizontalM": 0.0,
                "verticalM": 0.0,
                "pipeCount": 0,
            },
        )
        row["horizontalM"] += length_pt * mpp
        row["pipeCount"] += 1
    for r in spec.risers:
        if r.get("resolved_length_m") is None:
            continue
        key = (r["designation"], r["dn_mm"])
        row = rows.setdefault(
            key,
            {
                "designation": r["designation"],
                "diameterMm": r["dn_mm"],
                "horizontalM": 0.0,
                "verticalM": 0.0,
                "pipeCount": 0,
            },
        )
        row["verticalM"] += float(r["resolved_length_m"])
    out_rows = []
    for key in sorted(rows):
        row = dict(rows[key])
        row["horizontalM"] = round(row["horizontalM"], 4)
        row["verticalM"] = round(row["verticalM"], 4)
        row["totalM"] = round(row["horizontalM"] + row["verticalM"], 4)
        out_rows.append(row)
    return {
        "schema": "vvs-pipe/ground-truth/1",
        "drawing": spec.file_stem,
        "scaleDenominator": spec.scale_denominator,
        "metresPerPoint": mpp,
        "designations": sorted({p.designation for p in spec.pipes}),
        "quantities": out_rows,
        "unresolvedVerticals": [r["id"] for r in spec.risers if r.get("resolved_length_m") is None],
    }


# --------------------------------------------------------------------------
# The two drawings
# --------------------------------------------------------------------------

DRAWING_A = DrawingSpec(
    file_stem="drawing_a",
    width=1190.55,
    height=841.89,
    scale_denominator=50.0,
    pipes=[
        PipeSpec("main", "S1-P2-160", 160.0, [(170.0, 620.0), (760.0, 620.0)]),
        PipeSpec("branch_b", "S1-P2-110", 110.0, [(330.0, 620.0), (330.0, 400.0)]),
        PipeSpec("branch_c", "S1-P2-110", 110.0, [(600.0, 620.0), (600.0, 330.0), (830.0, 330.0)]),
        PipeSpec("branch_d", "S1-P2-75", 75.0, [(330.0, 400.0), (200.0, 400.0)]),
    ],
    callouts=[
        ("S1-P2-160", (300.0, 585.0), (430.0, 615.0)),
        ("S1-P2-110", (352.0, 498.0), (334.0, 520.0)),
        ("S1-P2-110", (640.0, 300.0), (700.0, 326.0)),
        ("S1-P2-75", (232.0, 372.0), (262.0, 396.0)),
    ],
    legend_entries=["S1-P2-75", "S1-P2-110", "S1-P2-160"],
    title_lines=["W-50-1-A-0024", "SKALA 1:50", "PLAN 1 TR"],
    room_labels=[("WC", (430.0, 700.0)), ("TVATT", (640.0, 700.0))],
    risers=[
        {
            "id": "R1",
            "point": (200.0, 400.0),
            "elevations": ["VG+2.800", "VG+0.150"],
            "designation": "S1-P2-75",
            "dn_mm": 75.0,
            "resolved_length_m": 2.65,
        },
        {
            "id": "R2",
            "point": (830.0, 330.0),
            "elevations": ["VG+2.800"],
            "designation": "S1-P2-110",
            "dn_mm": 110.0,
            "resolved_length_m": None,
        },
    ],
    with_takeoff_annotations=True,
)

DRAWING_B = DrawingSpec(
    file_stem="drawing_b",
    width=841.89,
    height=595.28,
    scale_denominator=100.0,
    pipes=[
        PipeSpec("kv", "KV1-X7", 63.0, [(120.0, 430.0), (620.0, 430.0)]),
        PipeSpec("vv", "VV1-X7", 50.0, [(120.0, 400.0), (520.0, 400.0)]),
        PipeSpec("vvc", "VVC1-X7", 32.0, [(120.0, 372.0), (420.0, 372.0)]),
        PipeSpec("vs", "VS1-S13", 40.0, [(300.0, 430.0), (300.0, 240.0)]),
    ],
    callouts=[
        ("KV1-X7", (180.0, 455.0), (240.0, 428.0)),
        ("VV1-X7", (330.0, 392.0), (390.0, 402.0)),
        ("VVC1-X7", (200.0, 350.0), (260.0, 370.0)),
        ("VS1-S13", (315.0, 290.0), (302.0, 310.0)),
    ],
    legend_entries=["KV1-X7", "VV1-X7", "VVC1-X7", "VS1-S13"],
    title_lines=["W-100-2-B-0110", "SKALA 1:100", "PLAN 2 TR"],
    room_labels=[("KOK", (250.0, 500.0))],
    risers=[
        {
            "id": "R1",
            "point": (300.0, 240.0),
            "elevations": ["VG+5.400", "VG+2.700"],
            "designation": "VS1-S13",
            "dn_mm": 40.0,
            "resolved_length_m": 2.70,
        }
    ],
    native_text=True,
    with_takeoff_annotations=False,
)

ALL_SPECS = [DRAWING_A, DRAWING_B]


def build_all(out_dir: str | Path) -> dict[str, dict]:
    return {spec.file_stem: build(spec, Path(out_dir)) for spec in ALL_SPECS}


if __name__ == "__main__":  # pragma: no cover
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifacts/fixtures")
    for stem, truth in build_all(target).items():
        print(stem, json.dumps(truth["quantities"]))
