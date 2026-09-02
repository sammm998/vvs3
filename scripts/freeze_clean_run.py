"""Freeze a clean-only run, then validate later runs against it.

    python scripts/freeze_clean_run.py freeze  CLEAN.pdf --out artifacts/frozen
    python scripts/freeze_clean_run.py check   CLEAN.pdf --out artifacts/frozen

``freeze`` analyses a clean drawing - no facit, no marked copy, nothing else -
and records the canonical digest, the association-chain census and the
quantities.  ``check`` re-runs the same drawing and reports every difference.

The order matters and is the point: the result is frozen *before* anything is
compared to it, so a comparison cannot quietly become an input to detection.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "python"))

from vvs_pipe.pipeline import analyse   # noqa: E402


def _record(pdf: Path) -> dict:
    result = analyse(pdf)
    payload = json.loads(json.dumps(result.to_canonical()))
    pages = payload["diagnostics"]["pages"]
    return {
        "file": pdf.name,
        "canonicalDigest": payload.get("forensicsDigest") or payload.get("canonicalDigest"),
        "analysisStatus": payload.get("analysisStatus"),
        "chain": [p.get("associationChain", {}) for p in pages],
        "quantities": payload.get("quantities", []),
        "physicalPipes": len(payload.get("physicalPipes", [])),
        "evidenceChains": [c for p in pages for c in p.get("evidenceChains", [])],
    }


def _compare(frozen: dict, fresh: dict) -> list[str]:
    problems: list[str] = []
    if frozen["physicalPipes"] != fresh["physicalPipes"]:
        problems.append(f"physical pipes {frozen['physicalPipes']} -> {fresh['physicalPipes']}")
    for index, (was, now) in enumerate(zip(frozen["chain"], fresh["chain"])):
        for key in sorted(set(was) | set(now)):
            if not isinstance(was.get(key), int):
                continue
            if was.get(key) != now.get(key):
                problems.append(f"page {index} {key}: {was.get(key)} -> {now.get(key)}")
    if frozen["quantities"] != fresh["quantities"]:
        problems.append("quantities differ")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("freeze", "check"))
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--out", type=Path, default=Path("artifacts/frozen"))
    parser.add_argument("--force", action="store_true", help="overwrite an existing freeze")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    target = args.out / f"{args.pdf.stem}.json"
    fresh = _record(args.pdf)

    if args.mode == "freeze":
        if target.exists() and not args.force:
            print(f"{target} already exists; pass --force to replace the frozen result")
            return 2
        target.write_text(json.dumps(fresh, indent=2, sort_keys=True), encoding="utf-8")
        print(f"frozen: {target}")
        for page in fresh["chain"]:
            print(f"  designations {page.get('designationOccurrences')}"
                  f" -> DN {page.get('designationsWithDn')}"
                  f" -> leaders {page.get('vectorLeaders')}"
                  f" -> attachments {page.get('verifiedAttachments')}")
        return 0

    if not target.exists():
        print(f"nothing frozen at {target}; run freeze first")
        return 2
    frozen = json.loads(target.read_text(encoding="utf-8"))
    problems = _compare(frozen, fresh)
    if not problems:
        print(f"unchanged against {target}")
        return 0
    print(f"changed against {target}:")
    for problem in problems:
        print(f"  {problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
