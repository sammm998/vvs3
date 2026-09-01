"""Scalability and behaviour probe on a deliberately dense sheet.

Generates a sheet with 300 short pipes, 300 callouts crammed between them and
sub-4 pt leaders - a layout considerably harsher than the reference drawings -
and reports timing plus what the engine does with it.  The point is not that
the engine reads this sheet perfectly (it does not); the point is that it
degrades by *refusing to assign*, not by inventing quantities.

Run:  PYTHONPATH=src/python .venv/bin/python scripts/stress_probe.py
"""

from __future__ import annotations

import collections
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tests.fixtures.make_drawings import DrawingSpec, PipeSpec, build  # noqa: E402
from vvs_pipe.pdf_forensics import forensic_report  # noqa: E402
from vvs_pipe.pipeline import analyse  # noqa: E402

ROWS, COLS = 20, 15
DIAMETERS = (75, 110, 160)


def build_sheet(out_dir: pathlib.Path) -> tuple[pathlib.Path, dict]:
    pipes: list[PipeSpec] = []
    callouts: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    n = 0
    for row in range(ROWS):
        for col in range(COLS):
            x, y = 60 + col * 75.0, 60 + row * 38.0
            dn = DIAMETERS[(row + col) % len(DIAMETERS)]
            code = f"S{row % 7 + 1}-P{col % 5 + 1}-{dn}"
            pipes.append(PipeSpec(f"p{n}", code, float(dn), [(x, y), (x + 55, y)]))
            callouts.append((code, (x + 2, y - 6.0), (x + 28, y - 1.0)))
            n += 1
    spec = DrawingSpec(
        file_stem="stress",
        width=1400.0,
        height=900.0,
        scale_denominator=50.0,
        pipes=pipes,
        callouts=callouts,
        legend_entries=["S1-P1-75", "S1-P1-110"],
        title_lines=["STRESS-1", "SKALA 1:50"],
        room_labels=[],
        risers=[],
    )
    truth = build(spec, out_dir)
    return out_dir / "stress_clean.pdf", truth


def main() -> int:
    out = pathlib.Path(tempfile.mkdtemp(prefix="vvs-stress-"))
    pdf, truth = build_sheet(out)
    forensics = forensic_report(pdf).data
    started = time.time()
    result = analyse(pdf, blind=True)
    elapsed = time.time() - started
    page = result.pages[0]

    named = [q for q in result.quantities if q.designation]
    print(f"sheet            : {pdf}")
    print(f"vector objects   : {forensics['vectorObjectCount']} ({forensics['pathItemCount']} path items)")
    print(f"elapsed          : {elapsed:.1f} s")
    print(
        "stages           : "
        f"glyphs={len(page.glyphs)} designations={len(page.designations)} "
        f"candidates={len(page.candidates)} runs={len(page.runs)} "
        f"physical={len(page.physical_pipes)}"
    )
    print(f"unresolved glyphs: {page.diagnostics['unresolvedGlyphs']}")
    print(f"scale            : {page.scale.state.value}")
    print(f"reconciliation   : {'OK' if result.reconciliation.ok else result.reconciliation.problems}")
    print(f"named rows       : {len(named)} (ground truth has {len(truth['quantities'])})")
    print(
        "reason histogram : "
        + str(
            collections.Counter(
                r.value for p in page.physical_pipes for r in p.reasons
            ).most_common()
        )
    )
    invented = [q for q in named if q.total_m is not None and page.scale.metres_per_point is None]
    print(f"invented metres  : {len(invented)} (must be 0)")

    # Post-hoc only, and only to quantify how far short of the facit a sheet
    # this harsh falls - never to adjust anything.
    from vvs_pipe.evaluation import compare_with_ground_truth

    truth_file = pdf.parent / "stress_truth.json"
    if truth_file.exists():
        summary = compare_with_ground_truth(result, truth_file)["summary"]
        print(
            "post-hoc         : "
            f"precision={summary['precision']} recall={summary['recall']} "
            f"f1={summary['f1']} measurementAccuracy={summary['measurementAccuracy']} "
            f"designationCoverage={summary['designationCoverage']}"
        )
    return 0 if not invented and result.reconciliation.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
