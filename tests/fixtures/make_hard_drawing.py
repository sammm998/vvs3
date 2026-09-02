"""A drawing that carries the failure modes the production sheet exposed.

The A/B fixtures prove the happy path.  This one exists to prove the *refusals*:
a sheet where several strings are shaped exactly like pipe designations and sit
exactly where a proximity rule would bind them, and where the only difference
between the ones that name a pipe and the ones that do not is whether the
draughtsman drew a leader to pipe geometry.

What it contains, by design:

* pipes drawn as double lines on a declared pipe layer, and building fabric
  drawn as parallel pairs at a *pipe-like* separation on an architectural
  layer, so the layer gate has something real to decide;
* callouts whose leaders are drawn the way CAD draws them - a shoulder and a
  slant, as two separate objects - which the old single-object leader rule
  could not see at all;
* an inline label sitting on a pipe with no leader, which must not be
  confirmed however close it is;
* a code-shaped note beside a pipe, a date, two `ENL. PM-n` references and a
  title block, none of which may become a designation.

The manifest it writes is a facit: post-hoc only, never read by detection.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import fitz

from .stroke_font import text_advance, text_strokes

PT_PER_MM = 72.0 / 25.4
BLACK = (0.0, 0.0, 0.0)
SCALE_DENOMINATOR = 50.0
METRES_PER_POINT = (25.4 / 72.0) * SCALE_DENOMINATOR / 1000.0

WIDTH, HEIGHT = 1190.55, 841.89

PIPE_LAYER = "W50-VVS-FE-S3"
FABRIC_LAYER = "W50-ARK-VAGG"
TEXT_LAYER = "W50-VVS-TEXT"

# designation, DN, centreline.  A network, not a set of parallel rules: a
# drawing whose pipes never meet is a grid, and the role classifier is right to
# say so, so the fixture has to be a real network for the test to be about
# association at all.
PIPES = [
    ("S3-R8-110", 110.0, [(160.0, 600.0), (700.0, 600.0)]),
    ("S3-R8-75", 75.0, [(300.0, 600.0), (300.0, 380.0), (430.0, 380.0)]),
    ("S3-K2-160", 160.0, [(160.0, 700.0), (620.0, 700.0)]),
    ("S3-K2-160", 160.0, [(620.0, 700.0), (620.0, 600.0)]),
    ("S3-R8-110", 110.0, [(500.0, 600.0), (500.0, 480.0)]),
]

# label origin, leader shoulder, leader tip: drawn as two objects, as CAD does
CALLOUTS = [
    ("S3-R8-110", (380.0, 520.0), (420.0, 545.0), (430.0, 596.0)),
    ("S3-R8-75", (200.0, 430.0), (250.0, 440.0), (297.0, 450.0)),
    ("S3-K2-160", (700.0, 745.0), (660.0, 730.0), (600.0, 703.0)),
]

# A callout whose leader ends on building fabric, not on a pipe: the chain must
# refuse it rather than binding it to the pipe that happens to run past.
FABRIC_CALLOUT = ("S3-X9-50", (760.0, 300.0), (740.0, 280.0), (700.0, 262.0))

# Text that a proximity rule would bind to a pipe, and that must stay text.
INLINE_LABEL = ("S3-R8-160", (350.0, 604.0))          # sits on the DN110 pipe
NEARBY_NOTE = ("ENL. PM-2", (500.0, 612.0))           # beside the same pipe
DATE_NOTE = ("2024-04-19", (200.0, 660.0))            # beside the DN160 pipe
DUCT_NOTE = ("B2-SDLV94NN4 1000X150", (240.0, 726.0))
TITLE_LINES = ["W-50-1-A-0024", "SKALA 1:50", "ENL. PM-1"]


def _layer(doc: fitz.Document, name: str) -> int:
    return doc.add_ocg(name, on=1)


def _polyline(page, pts, width, oc, fill=None, close=False):
    shape = page.new_shape()
    points = [fitz.Point(*p) for p in pts]
    for a, b in zip(points, points[1:]):
        shape.draw_line(a, b)
    if close and len(points) > 2:
        shape.draw_line(points[-1], points[0])
    shape.finish(color=BLACK, width=width, fill=fill, closePath=False, oc=oc)
    shape.commit()


def _rect(page, box, width, oc):
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(*box))
    shape.finish(color=BLACK, width=width, oc=oc)
    shape.commit()


def _text(page, text, origin, size, oc, width=0.25):
    for poly in text_strokes(text, origin, size):
        _polyline(page, poly, width, oc)


def _offset(points, d):
    out = []
    normals = []
    for a, b in zip(points, points[1:]):
        length = math.dist(a, b)
        ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
        normals.append((-uy, ux))
    out.append((points[0][0] + normals[0][0] * d, points[0][1] + normals[0][1] * d))
    for i in range(1, len(points) - 1):
        n1, n2 = normals[i - 1], normals[i]
        mx, my = n1[0] + n2[0], n1[1] + n2[1]
        ml = math.hypot(mx, my) or 1.0
        cos_half = (n1[0] * mx + n1[1] * my) / ml
        scale = d / max(cos_half, 1e-6)
        out.append((points[i][0] + mx / ml * scale, points[i][1] + my / ml * scale))
    out.append((points[-1][0] + normals[-1][0] * d, points[-1][1] + normals[-1][1] * d))
    return out


def _quantities() -> list[dict]:
    rows: dict[tuple[str, float], float] = {}
    for name, dn, centre in PIPES:
        length = sum(math.dist(centre[i], centre[i + 1]) for i in range(len(centre) - 1))
        rows[(name, dn)] = rows.get((name, dn), 0.0) + length * METRES_PER_POINT
    return [
        {"designation": name, "diameterMm": dn, "horizontalM": round(metres, 4)}
        for (name, dn), metres in sorted(rows.items())
    ]


def build(out_dir: str | Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=WIDTH, height=HEIGHT)
    pipe_oc = _layer(doc, PIPE_LAYER)
    fabric_oc = _layer(doc, FABRIC_LAYER)
    text_oc = _layer(doc, TEXT_LAYER)

    _rect(page, (15, 15, WIDTH - 15, HEIGHT - 15), 0.9, fabric_oc)

    # pipes, at true scale, on the pipe layer
    for _name, dn, centre in PIPES:
        half = (dn / 1000.0) / METRES_PER_POINT / 2.0
        for sign in (+1.0, -1.0):
            _polyline(page, _offset(centre, sign * half), 0.35, pipe_oc)

    # building fabric: parallel pairs at a pipe-like separation, on its own
    # layer.  Geometry alone cannot tell these from pipes; the layer can.
    for y in (240.0, 300.0):
        for offset in (0.0, 6.2):
            _polyline(page, [(120.0, y + offset), (880.0, y + offset)], 0.35, fabric_oc)

    # callouts: label, shoulder, slant - the shoulder and the slant are two
    # separate objects, as a CAD leader normally is
    for text, origin, shoulder, tip in CALLOUTS + [FABRIC_CALLOUT]:
        _text(page, text, origin, 7.0, text_oc)
        anchor = (origin[0] + text_advance(text, 7.0) * 0.5, origin[1] + 1.6)
        _polyline(page, [anchor, shoulder], 0.2, text_oc)
        _polyline(page, [shoulder, tip], 0.2, text_oc)

    for text, origin in (INLINE_LABEL, NEARBY_NOTE, DATE_NOTE, DUCT_NOTE):
        _text(page, text, origin, 7.0, text_oc)

    _rect(page, (WIDTH - 300.0, HEIGHT - 150.0, WIDTH - 40.0, HEIGHT - 40.0), 0.5, fabric_oc)
    for i, line in enumerate(TITLE_LINES):
        _text(page, line, (WIDTH - 290.0, HEIGHT - 126.0 + i * 20.0), 7.0, text_oc)

    # a scale bar, so the sheet can be measured at all
    bar = 5.0 / METRES_PER_POINT
    cell = bar / 5.0
    for i in range(5):
        shape = page.new_shape()
        shape.draw_rect(fitz.Rect(60.0 + i * cell, HEIGHT - 70.0, 60.0 + (i + 1) * cell,
                                  HEIGHT - 63.0))
        shape.finish(color=BLACK, width=0.3, fill=BLACK if i % 2 == 0 else None, oc=fabric_oc)
        shape.commit()
    _text(page, "0", (58.0, HEIGHT - 74.0), 5.6, text_oc)
    _text(page, "5", (60.0 + bar - 3.0, HEIGHT - 74.0), 5.6, text_oc)
    _text(page, "M", (60.0 + bar + 8.0, HEIGHT - 74.0), 5.6, text_oc)

    path = out_dir / "drawing_c_clean.pdf"
    doc.set_metadata({"producer": "vvs-fixture", "creator": "vvs-fixture"})
    doc.save(str(path), deflate=True, garbage=0, clean=False, pretty=False, no_new_id=True)
    doc.close()

    truth = {
        "schema": "vvs-pipe/ground-truth/2",
        "drawing": "drawing_c",
        "scaleDenominator": SCALE_DENOMINATOR,
        "metresPerPoint": METRES_PER_POINT,
        "pipeLayer": PIPE_LAYER,
        "expectedConfirmed": sorted({c[0] for c in CALLOUTS}),
        "expectedRefused": sorted([
            FABRIC_CALLOUT[0], INLINE_LABEL[0], NEARBY_NOTE[0], DATE_NOTE[0],
            DUCT_NOTE[0].split()[0], *TITLE_LINES,
        ]),
        "quantities": _quantities(),
        "files": {"clean": str(path)},
    }
    (out_dir / "drawing_c_truth.json").write_text(json.dumps(truth, indent=2, sort_keys=True),
                                                  encoding="utf-8")
    return truth


if __name__ == "__main__":  # pragma: no cover
    import sys
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifacts/fixtures")
    print(json.dumps(build(target)["expectedConfirmed"]))
