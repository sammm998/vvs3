"""The engine may not contain any drawing's codes."""

from __future__ import annotations

import ast
import pathlib
import re

ENGINE = pathlib.Path(__file__).resolve().parents[2] / "pdf_forensics"

# A designation is alphanumeric runs joined by a separator with a digit in it.
CODE_SHAPED = re.compile(r"^[A-ZÅÄÖ]{1,4}\d*[-/][A-ZÅÄÖ0-9]+([-/][A-ZÅÄÖ0-9]+)*$")


def _string_constants(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    # docstrings may discuss examples; executable constants may not
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    return [s for s in out if s not in docstrings]


def test_no_module_hardcodes_a_drawing_code():
    offenders = []
    for module in sorted(ENGINE.glob("*.py")):
        for constant in _string_constants(module):
            for token in constant.split():
                if CODE_SHAPED.match(token):
                    offenders.append((module.name, token))
    assert not offenders, f"drawing codes must not appear in the engine: {offenders}"


def test_the_same_engine_reads_two_disjoint_code_sets(analysis_a, analysis_b):
    _, report_a = analysis_a
    _, report_b = analysis_b
    codes_a = {row["designation"] for row in report_a["quantities"] if row["designation"]}
    codes_b = {row["designation"] for row in report_b["quantities"] if row["designation"]}
    assert codes_a and codes_b
    assert not (codes_a & codes_b), "the two sheets share no code, by construction"
