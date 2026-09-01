"""The microscope: ``python -m pdf_forensics.inspect input.pdf``.

The first command anybody runs.  It walks the whole file and reports what is
actually in it - objects by kind, per page, the fonts, the pens, the angles the
linework runs at - together with the conservation check that says whether the
model holds everything the PDF reported.

It interprets nothing.  There is no pipe, no designation and no measurement
here; this is the ground truth about the file that every later claim has to be
consistent with.

    python -m pdf_forensics.inspect sheet.pdf                 # the inventory
    python -m pdf_forensics.inspect sheet.pdf --glyphs        # read the lettering too
    python -m pdf_forensics.inspect sheet.pdf --json --dump out/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from .canonical import q
from .search import Microscope


def inspect_file(path: str | Path, with_glyphs: bool = False) -> dict[str, Any]:
    scope = Microscope(path)
    payload: dict[str, Any] = {
        "schema": "pdf-forensics/inspect/1",
        "file": scope.pdf.to_json(),
        "objects": scope.store.to_json(),
        "paths": scope.paths.to_json(),
        "geometry": scope.geometry.to_json(),
        "transformedObjects": [o.object_id for o in scope.store.transformed_objects()][:200],
    }
    if with_glyphs:
        payload["glyphs"] = scope.glyphs.to_json()
        payload["text"] = {
            "items": len(scope.text),
            "sample": [t.to_json() for t in scope.text[:50]],
        }
    payload["_scope"] = scope
    return payload


def summarise(payload: dict[str, Any]) -> str:
    objects = payload["objects"]
    inventory = objects["inventory"]
    conservation = objects["conservation"]
    file_info = payload["file"]
    lines = [
        f"file                {file_info['file']}   {file_info['bytes']} bytes"
        f"   sha1 {file_info['sha1'][:12]}",
        f"pages               {file_info['pageCount']}"
        + "".join(f"\n  page {p['page']}         {p['width']:.1f} x {p['height']:.1f} pt"
                 f"   ({p['width'] * 25.4 / 72:.0f} x {p['height'] * 25.4 / 72:.0f} mm)"
                 f"   rotation {p['rotation']}" for p in file_info["pages"]),
        "",
        "OBJECTS",
        f"  total             {inventory['totalObjects']}",
    ]
    for kind, count in inventory["byKind"].items():
        lines.append(f"    {kind:<16}{count}")
    lines += [
        f"  drawing objects   {inventory['drawingObjects']}"
        f"   (clips/groups excluded: {inventory['clipsAndGroups']})",
        f"  transformed       {inventory['transformedObjects']}",
        f"  fonts             {inventory['fonts']} references,"
        f" {inventory['distinctFonts']} distinct, {inventory['embeddedFonts']} embedded",
        "",
        "CONSERVATION      (what the file reports vs what the model holds)",
    ]
    for key in sorted(conservation["reported"]):
        reported = conservation["reported"][key]
        modelled = conservation["modelled"][key]
        mark = "ok" if reported == modelled else "LOST"
        lines.append(f"    {key:<16}{reported:>8} -> {modelled:<8} {mark}")
    lines.append(f"  result            {'OK' if conservation['ok'] else 'FAILED'}")
    if conservation["warnings"]:
        for warning in conservation["warnings"]:
            lines.append(f"  warning           {warning}")
    paths = payload["paths"]
    lines += [
        "",
        "LINEWORK",
        f"  segments          {paths['segments']}   ink length {paths['inkLength']:.0f} pt",
        f"  dashed paths      {paths['dashedPaths']}"
        f"   filled {paths['filledPaths']}   closed {paths['closedPaths']}",
        "  pens              " + ", ".join(f"{w}pt x{c}" for w, c in paths["strokeWidths"].items()),
        f"  vertical/horizontal {payload['geometry']['vertical']} / {payload['geometry']['horizontal']}",
        f"  index             {payload['geometry']['index']['backend']}"
        f" ({payload['geometry']['index']['entries']} entries, cell {payload['geometry']['index']['cell']})",
    ]
    if "glyphs" in payload:
        glyphs = payload["glyphs"]
        lines += [
            "",
            "GLYPHS",
            f"  glyphs            {glyphs['glyphs']}   {glyphs['bySource']}",
            f"  unresolved        {glyphs['unresolved']}"
            f"   mean confidence {glyphs['meanConfidence']}",
            f"  text items        {payload['text']['items']}",
        ]
        for item in payload["text"]["sample"][:20]:
            lines.append(f"    {item['text'][:40]:<42}{item['source']:<14}"
                         f"conf {item['confidence']:.2f}")
    fonts = payload["objects"]["fonts"]
    if fonts:
        lines += ["", "FONTS"]
        for font in fonts[:20]:
            lines.append(f"    {font['basefont'][:40]:<42}{font['type']:<12}"
                         f"{'embedded' if font['embedded'] else 'not embedded'}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pdf_forensics.inspect",
        description="Count and describe everything a vector PDF contains.")
    parser.add_argument("pdf")
    parser.add_argument("--glyphs", action="store_true",
                        help="also segment and read the lettering (slower)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dump", metavar="DIR", help="write the full inventory as JSON")
    args = parser.parse_args(argv)

    payload = inspect_file(args.pdf, with_glyphs=args.glyphs)
    scope = payload.pop("_scope")
    if args.dump:
        out = Path(args.dump)
        out.mkdir(parents=True, exist_ok=True)
        (out / "inspect.json").write_text(json.dumps(payload, indent=2, sort_keys=True),
                                          encoding="utf-8")
        (out / "objects.json").write_text(
            json.dumps([o.to_json() for o in scope.store.objects], indent=2, sort_keys=True),
            encoding="utf-8")
        (out / "segments.json").write_text(
            json.dumps([s.to_json() for s in scope.geometry.segments], indent=2, sort_keys=True),
            encoding="utf-8")
        if args.glyphs:
            (out / "glyphs.json").write_text(
                json.dumps([g.to_json() for g in scope.glyphs.glyphs], indent=2, sort_keys=True),
                encoding="utf-8")
            (out / "text.json").write_text(
                json.dumps([t.to_json() for t in scope.text], indent=2, sort_keys=True),
                encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(summarise(payload))
        if args.dump:
            print(f"\nwritten:            {args.dump}")
    scope.close()
    return 0 if payload["objects"]["conservation"]["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
