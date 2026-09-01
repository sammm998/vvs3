"""Fixtures for the pdf_forensics suite.

The drawings are generated, not committed, and the *truth* manifest beside them
is only ever read by tests that say so in their name.  No test that exercises
detection may look at it - that is the blindness rule, and
``test_j_blindness.py`` enforces it on the engine's import closure too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.make_drawings import build_all

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def drawings(tmp_path_factory) -> dict[str, dict]:
    out = tmp_path_factory.mktemp("drawings")
    return build_all(out)


@pytest.fixture(scope="session")
def clean_a(drawings) -> str:
    return drawings["drawing_a"]["files"]["clean"]


@pytest.fixture(scope="session")
def clean_b(drawings) -> str:
    return drawings["drawing_b"]["files"]["clean"]


@pytest.fixture(scope="session")
def marked_a(drawings) -> str:
    return drawings["drawing_a"]["files"]["with_takeoff"]


@pytest.fixture(scope="session")
def analysis_a(clean_a):
    from pdf_forensics.analyze import analyse
    return analyse(clean_a)


@pytest.fixture(scope="session")
def analysis_b(clean_b):
    from pdf_forensics.analyze import analyse
    return analyse(clean_b)
