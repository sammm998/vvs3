"""D. Unknown designation test - the open-world requirement.

Two things are checked:

1. the engine finds a drawing's codes without ever being told them, and the
   *same* engine finds a completely different drawing's completely different
   codes with no code change;
2. no designation string is written into the engine's source at all - a
   whitelist would make (1) meaningless.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from vvs_pipe.states import TextRole

ENGINE_ROOT = Path(__file__).resolve().parents[2] / "src" / "python" / "vvs_pipe"
# Anything shaped like an engineering code: letter and digit runs joined by
# separators, containing at least one digit.  ("TAKE-OFF" is not a code.)
CODE_SHAPED = re.compile(r"\b(?=[A-Z0-9-]*\d)[A-Z]{1,4}\d{0,3}-[A-Z0-9]{1,6}(?:-[A-Z0-9]{1,6})*\b")


def _discovered(analysis) -> set[str]:
    return {
        d.text
        for p in analysis.pages
        for d in p.designations
        if d.role is TextRole.PIPE_DESIGNATION and not d.is_legend
    }


def test_engine_discovers_drawing_a_codes_it_was_never_given(analysis_a, drawing_a):
    found = _discovered(analysis_a)
    assert set(drawing_a["designations"]) <= found, (found, drawing_a["designations"])


def test_same_engine_discovers_a_different_drawings_codes(analysis_b, drawing_b):
    found = _discovered(analysis_b)
    assert set(drawing_b["designations"]) <= found, (found, drawing_b["designations"])


def test_the_two_drawings_share_no_codes(drawing_a, drawing_b):
    """If the code sets overlapped, test D would prove nothing."""
    assert not set(drawing_a["designations"]) & set(drawing_b["designations"])


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids() of the string constants that are docstrings, not data."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    out.add(id(body[0].value))
    return out


def test_no_designation_literal_is_hardcoded_in_the_engine():
    """No executable string constant in the engine looks like a drawing code.

    Prose in docstrings may of course name example codes; what is forbidden is
    a code reaching the running program as data - a whitelist, a lookup table
    or an equality test.
    """
    offenders: list[str] = []
    for path in sorted(ENGINE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            for match in CODE_SHAPED.finditer(node.value):
                offenders.append(
                    f"{path.relative_to(ENGINE_ROOT)}:{node.lineno}: {match.group(0)}"
                )
    assert not offenders, "code-shaped literals found in the engine: " + "; ".join(offenders)


def test_dimension_parsing_is_positional_not_lexical():
    """A never-before-seen code still yields its trailing nominal size."""
    from vvs_pipe.designations.discovery import _dimension_from_structure
    from vvs_pipe.text_reconstruction import token_structure

    for text, expected in (
        ("ABC-17-X-250", 250.0),
        ("VP-003-A", None),
        ("QQ9-ZZ-63", 63.0),
        ("KV1-X7", None),  # 7 mm is not a plausible nominal size
    ):
        parts, _pattern = token_structure(text)
        size, _reason, _system = _dimension_from_structure(parts, None)
        assert size == expected, (text, size, expected)
