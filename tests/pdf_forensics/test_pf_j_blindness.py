"""The answer may not reach detection, by any route."""

from __future__ import annotations

import importlib
import pathlib
import pkgutil
import sys

import pytest

import pdf_forensics
from pdf_forensics.analyze import analyse
from pdf_forensics.loader import BlindnessError, load


def test_a_spreadsheet_is_refused(tmp_path):
    facit = tmp_path / "facit.xlsx"
    facit.write_bytes(b"not a drawing")
    with pytest.raises(BlindnessError):
        load(facit)


def test_a_file_whose_name_carries_an_answer_is_refused(marked_a, tmp_path):
    target = tmp_path / "drawing_marked.pdf"
    target.write_bytes(open(marked_a, "rb").read())
    with pytest.raises(BlindnessError):
        load(target)


def test_previous_take_off_annotations_cannot_enter_the_geometry(clean_a, marked_a, tmp_path):
    renamed = tmp_path / "sheet_with_review_layer.pdf"
    renamed.write_bytes(open(marked_a, "rb").read())
    clean_workspace, clean_report = analyse(clean_a)
    marked_workspace, marked_report = analyse(renamed)
    assert marked_report["stages"]["objects"]["inventory"]["byKind"].get("annotation", 0) > 0
    assert ([p.centerline for p in clean_workspace.physical_pipes]
            == [p.centerline for p in marked_workspace.physical_pipes])
    assert clean_report["quantities"] == marked_report["quantities"]


def test_the_engine_cannot_import_a_spreadsheet_reader():
    """The check runs in a fresh interpreter, so another test cannot mask it."""
    import subprocess
    import textwrap

    script = textwrap.dedent("""
        import importlib, pkgutil, sys
        import pdf_forensics
        for module in pkgutil.iter_modules(pdf_forensics.__path__):
            importlib.import_module("pdf_forensics." + module.name)
        forbidden = {"openpyxl", "xlrd", "pandas", "xlsxwriter"}
        reachable = sorted(forbidden & set(sys.modules))
        print(",".join(reachable))
    """)
    root = pathlib.Path(pdf_forensics.__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            cwd=str(root))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", \
        f"detection must not be able to read a facit: {result.stdout.strip()}"
