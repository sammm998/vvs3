"""Structural comparison of an association-chain run against a reference.

    python scripts/chain_regression_report.py RUN_DIR
    python scripts/chain_regression_report.py RUN_DIR --reference 77 77 68 64

The four numbers are, in order:

    designations, designations carrying a DN, vector leaders, verified
    leader -> FE attachments

They are a *regression reference*, not a target: they describe what a previous
pipeline reported on one clean sheet.  They are passed in on the command line
and live in this script, which the engine never imports - nothing in the
detector can read them, and no threshold anywhere is derived from them.

The report is structural.  It says at which stage the chain loses labels, which
is the only useful thing to know when the count is short: losing them at the
leader stage is a tracing problem, losing them at the attachment stage is a
geometry or layer problem, and losing them at the reading stage is a text
problem.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(run_dir: Path) -> list[dict]:
    report = json.loads((run_dir / "analysis.json").read_text(encoding="utf-8"))
    return [page["associationChain"] for page in report["diagnostics"]["pages"]
            if "associationChain" in page]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument(
        "--reference", nargs=4, type=int, default=None,
        metavar=("DESIGNATIONS", "WITH_DN", "LEADERS", "ATTACHMENTS"),
        help="a previous pipeline's counts on the same clean sheet",
    )
    args = parser.parse_args()

    chains = load(args.run_dir)
    if not chains:
        print("no association chain in this run")
        return 2
    total = {
        "designations": sum(c["designationOccurrences"] for c in chains),
        "withDn": sum(c["designationsWithDn"] for c in chains),
        "leaders": sum(c["vectorLeaders"] for c in chains),
        "attachments": sum(c["verifiedAttachments"] for c in chains),
        "physicalPipes": sum(c["physicalPipes"] for c in chains),
        "designated": sum(c["physicalPipesDesignated"] for c in chains),
    }
    print("association chain")
    print(f"  designations reconstructed : {total['designations']}")
    print(f"  ... carrying a DN          : {total['withDn']}")
    print(f"  vector leaders traced      : {total['leaders']}")
    print(f"  verified FE attachments    : {total['attachments']}")
    print(f"  physical pipes             : {total['physicalPipes']}"
          f"  ({total['designated']} designated, "
          f"{100.0 * total['designated'] / max(1, total['physicalPipes']):.0f}%)")

    layers = chains[0].get("pipeLayers", {})
    if layers.get("active"):
        print(f"  pipe layers (discovered)   : {', '.join(layers['layers'])}")
    else:
        print("  pipe layers                : the file declares none; the gate is inactive")

    print("\nwhere the chain stops")
    by_stage: dict[tuple[str, str], int] = {}
    for chain in chains:
        for failure in chain.get("chainFailures", []):
            key = (failure["stage"], failure["reason"])
            by_stage[key] = by_stage.get(key, 0) + 1
    for (stage, reason), count in sorted(by_stage.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {count:>5}  {stage:<16}{reason}")
    hints = sum(len(c.get("proximityHintsNotUsed", [])) for c in chains)
    print(f"  {hints:>5}  proximity        hints measured and deliberately unused")

    if args.reference:
        ref = dict(zip(("designations", "withDn", "leaders", "attachments"), args.reference))
        print("\nagainst the reference run (structural, not a target)")
        for key in ("designations", "withDn", "leaders", "attachments"):
            got, want = total[key], ref[key]
            delta = got - want
            share = 100.0 * got / want if want else 0.0
            print(f"  {key:<14}{got:>6}   reference {want:>5}   {delta:+d}  ({share:.0f}%)")
        print("\n  read it as a chain: a shortfall first appears at the stage that")
        print("  lost it, and every stage after it inherits the loss.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
