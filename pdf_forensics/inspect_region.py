"""Look at one place on the sheet: ``python -m pdf_forensics.inspect_region``.

    python -m pdf_forensics.inspect_region sheet.pdf --page 0 --bbox 300 570 360 590
    python -m pdf_forensics.inspect_region sheet.pdf --near 412 380 --radius 60 --crop out.png
    python -m pdf_forensics.inspect_region sheet.pdf --object seg:1a2b3c4d5e6f:0

This is ``inspect_neighbourhood`` on the command line: everything the drawing
has in one region, grouped by what it is, with an optional rendered crop so the
vector answer can be checked against the picture.  The vectors are the evidence;
the crop is only a second opinion.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

from .analyze import analyse
from .canonical import q
from .neighbourhood import inspect_neighbourhood


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pdf_forensics.inspect_region",
        description="Everything the drawing contains around a point, a box or an object.")
    parser.add_argument("pdf")
    parser.add_argument("--page", type=int, default=0)
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"))
    parser.add_argument("--near", nargs=2, type=float, metavar=("X", "Y"))
    parser.add_argument("--object", metavar="ID", help="an id from any stage")
    parser.add_argument("--radius", type=float, default=None)
    parser.add_argument("--crop", metavar="PNG", help="render the region to this file")
    parser.add_argument("--detail", action="store_true", help="print every entity, not counts")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    workspace, _ = analyse(args.pdf)
    neighbourhood = inspect_neighbourhood(
        workspace,
        object_id=args.object,
        page=args.page,
        point=tuple(args.near) if args.near else None,
        bbox=tuple(args.bbox) if args.bbox else None,
        radius=args.radius,
    )
    payload = neighbourhood.to_json(detail=args.detail or args.json)
    if args.crop:
        from .render import render_crop
        payload["crop"] = render_crop(workspace.pdf, neighbourhood.page, neighbourhood.bbox,
                                      args.crop, pad=neighbourhood.radius)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        box = " ".join(f"{v:.2f}" for v in neighbourhood.bbox)
        print(f"page {neighbourhood.page}  bbox [{box}]  radius {neighbourhood.radius}")
        for name, count in payload["counts"].items():
            print(f"  {name:<24}{count}")
        if args.detail:
            for name, items in sorted(payload["contents"].items()):
                print(f"\n{name}")
                for entity in items[:20]:
                    label = (entity.get("text") or entity.get("character")
                             or entity.get("designation") or entity.get("kind") or "")
                    ids = (entity.get("objectId") or entity.get("segmentId")
                           or entity.get("textId") or entity.get("pipeId")
                           or entity.get("glyphId") or entity.get("candidateId") or "")
                    bbox = entity.get("bbox")
                    box = " ".join(f"{v:8.2f}" for v in bbox) if bbox else ""
                    print(f"  {str(label)[:26]:<28}[{box}]  {ids}")
        if args.crop:
            print(f"\ncrop written to {payload['crop']['file']}")
    workspace.pdf.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
