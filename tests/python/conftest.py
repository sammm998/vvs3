"""Shared fixtures.

The drawings are generated once per test session into a temporary directory.
The generator lives in ``tests/fixtures`` and is never importable from the
engine - ``test_blind_leakage`` proves that.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "python"))
sys.path.insert(0, str(ROOT))

from tests.fixtures.make_drawings import ALL_SPECS, build_all  # noqa: E402


@pytest.fixture(scope="session")
def drawings(tmp_path_factory) -> dict[str, dict]:
    out = tmp_path_factory.mktemp("drawings")
    truths = build_all(out)
    for stem, truth in truths.items():
        truth["dir"] = str(out)
    return truths


@pytest.fixture(scope="session")
def drawing_a(drawings) -> dict:
    return drawings["drawing_a"]


@pytest.fixture(scope="session")
def drawing_b(drawings) -> dict:
    return drawings["drawing_b"]


@pytest.fixture(scope="session")
def analysis_a(drawing_a):
    from vvs_pipe.pipeline import analyse

    return analyse(drawing_a["files"]["clean"], blind=True)


@pytest.fixture(scope="session")
def analysis_b(drawing_b):
    from vvs_pipe.pipeline import analyse

    return analyse(drawing_b["files"]["clean"], blind=True)


@pytest.fixture(scope="session")
def specs_by_stem() -> dict:
    return {s.file_stem: s for s in ALL_SPECS}
