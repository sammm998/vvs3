"""The marked drawing shows what was found, and nothing else."""

from __future__ import annotations

import math

import fitz

from pdf_forensics.render import mark_drawing, render_crop


def _marked_lines(path) -> set[tuple]:
    doc = fitz.open(str(path))
    lines = set()
    for page in doc:
        for drawing in page.get_drawings():
            colour = drawing.get("color")
            if not colour or not (colour[0] > 0.5 and colour[1] < 0.3):
                continue
            for item in drawing["items"]:
                if item[0] == "l":
                    a = (round(item[1].x, 3), round(item[1].y, 3))
                    b = (round(item[2].x, 3), round(item[2].y, 3))
                    lines.add(tuple(sorted((a, b))))
    doc.close()
    return lines


def test_the_overlay_draws_only_reconstructed_geometry(analysis_a, tmp_path):
    workspace, _ = analysis_a
    target = tmp_path / "marked.pdf"
    named = {p.pipe_id: p.designation for p in workspace.physical_pipes if p.designation}
    mark_drawing(workspace.pdf, target, workspace.physical_pipes,
                 list(workspace.leaders_by_text.values()), [], named)
    expected = set()
    for pipe in workspace.physical_pipes:
        if not pipe.designation:
            continue
        for part in pipe.parts:
            for a, b in zip(part, part[1:]):
                expected.add(tuple(sorted(((round(a[0], 3), round(a[1], 3)),
                                           (round(b[0], 3), round(b[1], 3))))))
    drawn = _marked_lines(target)
    assert drawn == expected, "the overlay invented or lost a line"


def test_a_crop_renders_the_area_asked_for(analysis_a, tmp_path):
    workspace, _ = analysis_a
    pipe = workspace.physical_pipes[0]
    xs = [p[0] for p in pipe.centerline]
    ys = [p[1] for p in pipe.centerline]
    result = render_crop(workspace.pdf, pipe.page, (min(xs), min(ys), max(xs), max(ys)),
                         tmp_path / "crop.png")
    assert result["width"] > 0 and result["height"] > 0
    assert (tmp_path / "crop.png").exists()
