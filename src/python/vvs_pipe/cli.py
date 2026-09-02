"""Command line entry point.

    vvs-pipe analyse DRAWING.pdf --out artifacts/run

Environment switches required by the specification:

* ``RUN_BLIND_TEST=1``  - blind mode; the engine is given the PDF and nothing
  else.  (The engine cannot read ground truth in any mode; the flag records the
  intent in the report and is asserted by the leakage test.)
* ``RUN_FORENSICS=1``   - also export every intermediate: raw vectors, glyph
  candidates, designation candidates, pipe candidates, centerlines, the graph,
  runs, physical pipes, associations, dimensions, verticals and the debug
  drawing.
* ``POST_TEST_COMPARE=1`` - after the blind run, invoke the separate evaluator
  against a ground-truth file.  The evaluator is a different module and cannot
  influence the pipeline.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .canonical import canonical_json
from .pipeline import analyse
from .rendering import render_debug, render_marked


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip() == "1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vvs-pipe", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("analyse", help="run the blind analysis pipeline")
    run.add_argument("pdf", type=Path)
    run.add_argument("--out", type=Path, default=Path("artifacts/run"))
    run.add_argument("--forensics", action="store_true", help="export all intermediates")
    run.add_argument("--ground-truth", type=Path, default=None, help="post-hoc comparison only")
    run.add_argument(
        "--debug-crops",
        action="store_true",
        help="render a local crop for every stage of the evidence chain that failed",
    )

    args = parser.parse_args(argv)
    if args.command != "analyse":  # pragma: no cover - argparse enforces this
        parser.error("unknown command")

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    result = analyse(args.pdf, blind=_truthy("RUN_BLIND_TEST"))
    result.forensics.write(out / "forensics.json")
    result.write_json(out / "analysis.json")
    marked = render_marked(result, out / "marked.pdf")
    quantities_csv = _write_quantities_csv(result, out / "quantities.csv")

    debug_path = None
    if args.forensics or _truthy("RUN_FORENSICS"):
        debug_path = render_debug(result, out / "debug.pdf")
        _write_forensic_dump(result, out / "forensics")

    crops = []
    if args.debug_crops or _truthy("RUN_DEBUG_CROPS"):
        from .rendering.crops import render_crops, requests_from_result

        cap = result.pages[0].text_cap_height if result.pages else 7.0
        crops = render_crops(result.source_path, requests_from_result(result),
                             out / "debug_crops", cap_height=cap)

    print(f"forensics      : {out / 'forensics.json'}")
    print(f"analysis       : {out / 'analysis.json'}")
    print(f"marked drawing : {marked}")
    print(f"quantities     : {quantities_csv}")
    _print_chain_census(result)
    if crops:
        print(f"debug crops    : {out / 'debug_crops'}  ({len(crops)} failed stages)")
    if debug_path:
        print(f"debug drawing  : {debug_path}")
    print(f"canonicalDigest: {result.canonical_digest()}")
    print(f"reconciliation : {'OK' if result.reconciliation.ok else result.reconciliation.problems}")

    gt = args.ground_truth
    if gt is None and _truthy("POST_TEST_COMPARE"):
        parser.error("POST_TEST_COMPARE=1 requires --ground-truth")
    if gt is not None:
        # Imported lazily and only here: the evaluator must never be part of
        # the pipeline's import closure.
        from .evaluation import compare_with_ground_truth

        report = compare_with_ground_truth(result, gt)
        (out / "post_test_comparison.json").write_text(
            canonical_json(report, indent=2), encoding="utf-8"
        )
        print(f"post-test      : {out / 'post_test_comparison.json'}")
        print(canonical_json(report["summary"], indent=2))
    return 0


def _print_chain_census(result) -> None:
    """The stage-by-stage census of the association chain.

    Printed on every run because it is the number that says whether the engine
    is *identifying* pipes or merely measuring them: geometry can be perfect
    while every label fails to attach, and a single "designations: n" line
    hides exactly that.
    """
    for page_result in result.pages:
        chain = page_result.diagnostics.get("associationChain")
        if not chain:
            continue
        layers = chain.get("pipeLayers", {})
        print(
            f"chain page {page_result.page}   "
            f"designations {chain.get('designationOccurrences', 0)}"
            f" -> with DN {chain.get('designationsWithDn', 0)}"
            f" -> vector leaders {chain.get('vectorLeaders', 0)}"
            f" -> verified attachments {chain.get('verifiedAttachments', 0)}"
        )
        print(
            f"               physical pipes {chain.get('physicalPipes', 0)},"
            f" designated {chain.get('physicalPipesDesignated', 0)};"
            f" pipe layers {'active ' + ','.join(layers.get('layers', [])) if layers.get('active') else 'not declared by this file'};"
            f" proximity hints not used {len(chain.get('proximityHintsNotUsed', []))}"
        )


def _write_quantities_csv(result, path: Path) -> Path:
    import csv

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "designation",
                "diameterMm",
                "horizontalM",
                "verticalM",
                "totalM",
                "pipeCount",
                "state",
                "reasons",
                "confidence",
            ]
        )
        for q in result.quantities:
            w.writerow(
                [
                    q.designation or "",
                    "" if q.diameter_mm is None else q.diameter_mm,
                    "" if q.horizontal_m is None else q.horizontal_m,
                    "" if q.vertical_m is None else q.vertical_m,
                    "" if q.total_m is None else q.total_m,
                    q.pipe_count,
                    q.state.value,
                    ";".join(r.value for r in q.reasons),
                    q.confidence.overall,
                ]
            )
    return path


def _write_forensic_dump(result, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    sections = {
        "raw_vectors": [o.to_canonical() for o in result.document.objects],
        "text_spans": [s.to_canonical() for s in result.document.text_spans],
        "glyph_candidates": [g.to_canonical() for p in result.pages for g in p.glyphs],
        "text_items": [t.to_canonical() for p in result.pages for t in p.text_items],
        "designation_candidates": [d.to_canonical() for p in result.pages for d in p.designations],
        "pipe_candidates": [c.to_canonical() for p in result.pages for c in p.candidates],
        "centerlines": [
            {"candidateId": c.candidate_id, "centerline": [[x, y] for x, y in c.centerline]}
            for p in result.pages
            for c in p.candidates
        ],
        "graph": [p.graph.to_canonical() for p in result.pages],
        "pipe_runs": [r.to_canonical() for p in result.pages for r in p.runs],
        "physical_pipes": [pp.to_canonical() for p in result.pages for pp in p.physical_pipes],
        "verticals": [v.to_canonical() for p in result.pages for v in p.verticals],
        "dimensions": [p.diagnostics["dimensionReconciliation"] for p in result.pages],
        "associations": [p.diagnostics["associationDiagnostics"] for p in result.pages],
        "measurement_segments": [
            {
                "physicalPipeId": pp.physical_pipe_id,
                "lengthPt": pp.length_pt,
                "horizontalM": pp.horizontal_length_m,
                "verticalM": pp.vertical_length_m,
                "totalM": pp.total_length_m,
            }
            for p in result.pages
            for pp in p.physical_pipes
        ],
        "rejected_geometry": [
            {"page": p.page, "excludedObjectIds": sorted(p.diagnostics.get("excludedObjectIds", []))}
            for p in result.pages
        ],
    }
    for name, payload in sections.items():
        (directory / f"{name}.json").write_text(canonical_json(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
