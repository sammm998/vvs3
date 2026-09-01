"""Q. Blind-test leakage test.

The blind pipeline is defined as the transitive import closure of
``vvs_pipe.pipeline``.  If anything in that closure could reach the evaluator,
the fixture generator, or a spreadsheet reader, "blind" would be a claim rather
than a property.  This test walks the closure and proves it.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

# The package under test, as the repository lays it out.
SRC = Path(__file__).resolve().parents[2] / "src" / "python"

ENGINE_ROOT = Path(__file__).resolve().parents[2] / "src" / "python"
PACKAGE = "vvs_pipe"

FORBIDDEN_MODULES = {
    "vvs_pipe.evaluation",
    "openpyxl",
    "xlrd",
    "pandas",
    "tests",
    "tests.fixtures",
}
FORBIDDEN_PREFIXES = ("vvs_pipe.evaluation", "openpyxl", "xlrd", "tests.")


def _module_path(name: str) -> Path | None:
    rel = Path(*name.split("."))
    for cand in (ENGINE_ROOT / rel.with_suffix(".py"), ENGINE_ROOT / rel / "__init__.py"):
        if cand.exists():
            return cand
    return None


def _imports_of(path: Path, package: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                out.add(f"{base}.{node.module}" if node.module else base)
            elif node.module:
                out.add(node.module)
    return out


def _closure(entry: str) -> tuple[set[str], dict[str, str]]:
    seen: set[str] = set()
    parents: dict[str, str] = {}
    stack = [entry]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        path = _module_path(name)
        if path is None:
            continue
        package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
        for imported in sorted(_imports_of(path, package)):
            if imported not in seen:
                parents.setdefault(imported, name)
                stack.append(imported)
    return seen, parents


def test_blind_pipeline_has_no_ground_truth_dependency():
    closure, parents = _closure("vvs_pipe.pipeline")
    offenders = [
        f"{m} (imported by {parents.get(m, '?')})"
        for m in sorted(closure)
        if m in FORBIDDEN_MODULES or m.startswith(FORBIDDEN_PREFIXES)
    ]
    assert not offenders, "blind pipeline can reach: " + "; ".join(offenders)


def test_the_cli_only_reaches_the_evaluator_lazily():
    """``vvs_pipe.cli`` may compare against a facit, but only on request."""
    path = _module_path("vvs_pipe.cli")
    assert path is not None
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module)
        elif isinstance(node, ast.Import):
            top_level.update(a.name for a in node.names)
    assert not any(m.startswith("vvs_pipe.evaluation") for m in top_level)


def test_importing_the_pipeline_does_not_load_a_spreadsheet_library():
    """A fresh interpreter, not this one.

    Evicting ``vvs_pipe`` from ``sys.modules`` in-process and re-importing gives
    a second generation of every class in the package, so a later test holding a
    first-generation enum finds that ``x is SomeEnum.VALUE`` is false against an
    object from the second - which silently turns assertions in unrelated tests
    into passes.  A subprocess also makes the claim stronger: the import is
    measured from nothing, rather than from whatever this session had already
    loaded.
    """
    source = "import sys; import vvs_pipe.pipeline; print(' '.join(sorted(sys.modules)))"
    proc = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    loaded = set(proc.stdout.split())
    assert "openpyxl" not in loaded
    assert "vvs_pipe.evaluation" not in loaded


def test_blind_analysis_records_that_no_facit_was_used(analysis_a):
    payload = analysis_a.to_canonical()
    assert payload["blind"]["facitUsedDuringDetection"] is False
    assert payload["blind"]["mode"] == "blind"


def test_the_evaluator_cannot_change_a_result(analysis_a, drawing_a):
    """Running the comparison leaves the analysis byte-identical."""
    from vvs_pipe.evaluation import compare_with_ground_truth

    before = analysis_a.canonical_digest()
    truth = Path(drawing_a["dir"]) / "drawing_a_truth.json"
    compare_with_ground_truth(analysis_a, truth)
    assert analysis_a.canonical_digest() == before
