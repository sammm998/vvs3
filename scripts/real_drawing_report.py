"""Post-hoc comparison of a blind run against the real facit.

Reports two things separately, because they fail independently:

* **geometry** - how much pipe the engine reconstructed per drawing layer,
  against the facit's per-system totals.  This is measured without using any
  designation, so a mis-read label cannot flatter or spoil it;
* **naming** - which designations the engine read, against the facit's.

Run:  PYTHONPATH=src/python .venv/bin/python scripts/real_drawing_report.py RUN_DIR
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FACIT = Path("artifacts/real/facit_W501A0011.xlsx")
# The drawing's own layer names carry the system token as their last field;
# the facit's subjects carry it as their first.  Both are read from the files.
LAYER_SYSTEM = re.compile(r"-(?P<system>[A-Z]{1,3}\d{0,2})-*$")


def facit_rows():
    from vvs_pipe.evaluation import load_ground_truth

    return load_ground_truth(FACIT)


def main() -> int:
    run = Path(sys.argv[1] if len(sys.argv) > 1 else "artifacts/real/run3")
    report = json.loads((run / "analysis.json").read_text(encoding="utf-8"))
    truth = facit_rows()

    scale = report["scale"][0]
    mpp = scale.get("metresPerPoint")
    print(f"scale            : {scale['state']} 1:{scale.get('ratioDenominator')}  mpp={mpp}")
    print(f"vector objects   : {report['drawing']['vectorObjectCount']}")
    print(f"glyphs           : {len(report['glyphs'])} "
          f"({sum(1 for g in report['glyphs'] if g['character'] is None)} unresolved)")
    print(f"designations     : {len(report['designations'])}")
    print(f"pipe candidates  : {len(report['pipeCandidates'])}  runs {len(report['pipeRuns'])}  "
          f"physical {len(report['physicalPipes'])}")
    print(f"reconciliation   : {'OK' if report['diagnostics']['reconciliation']['ok'] else report['diagnostics']['reconciliation']['problems']}")

    # --- geometry, by the drawing's own layers -----------------------------
    by_layer: dict[str, float] = collections.defaultdict(float)
    for c in report["pipeCandidates"]:
        notes = " ".join(c["provenance"].get("notes", []))
        m = re.search(r"layer=(.*)", notes)
        layer = m.group(1) if m else "(none)"
        if mpp:
            by_layer[layer] += c["lengthPt"] * mpp
    print("\nreconstructed length by layer (m):")
    fe_total = 0.0
    for layer, metres in sorted(by_layer.items(), key=lambda kv: -kv[1])[:12]:
        mark = ""
        if "-FE-" in layer:
            fe_total += metres
            mark = "  <- pipe layer"
        print(f"  {metres:8.1f}  {layer}{mark}")

    truth_h = sum(r.horizontal_m or 0.0 for r in truth)
    truth_v = sum(r.vertical_m or 0.0 for r in truth)
    print(f"\npipe-layer total : {fe_total:.1f} m")
    print(f"facit horizontal : {truth_h:.1f} m   (difference {fe_total - truth_h:+.1f} m,"
          f" {100 * (fe_total - truth_h) / truth_h:+.1f}%)")
    print(f"facit vertical   : {truth_v:.1f} m   (risers; not derivable from this sheet)")

    # --- naming ------------------------------------------------------------
    found = collections.Counter(
        d["designation"] for d in report["designations"] if d["role"] == "PIPE_DESIGNATION"
    )
    want = {r.designation for r in truth}
    print(f"\nfacit designations ({len(want)}): {sorted(want)}")
    print("engine read as PIPE_DESIGNATION (top 15):")
    for text, n in found.most_common(15):
        print(f"  {n:>4}  {text!r}{'  <- exact facit match' if text in want else ''}")
    exact = sorted(set(found) & want)
    print(f"\nexact designation matches: {len(exact)} of {len(want)}  {exact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
