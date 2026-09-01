"""Order-independence harness.

The engine's contract is that a permutation of the *input order* cannot change
the output.  These helpers generate the permutations the tests use - original,
reversed, and two seeded shuffles - and reduce any stage's output to a digest.

The shuffles are seeded from a fixed constant, so the test suite itself is
reproducible; nothing in ``vvs_pipe`` outside this module uses randomness.
"""

from __future__ import annotations

import random
from typing import Any, Callable, Iterable, Sequence

from ..canonical import digest

PERMUTATION_SEEDS = (20240917, 811)


def permutations_of(items: Sequence[Any]) -> list[tuple[str, list[Any]]]:
    base = list(items)
    out: list[tuple[str, list[Any]]] = [
        ("original", list(base)),
        ("reversed", list(reversed(base))),
    ]
    for seed in PERMUTATION_SEEDS:
        rng = random.Random(seed)
        shuffled = list(base)
        rng.shuffle(shuffled)
        out.append((f"shuffled-{seed}", shuffled))
    return out


def digest_of_stage(items: Iterable[Any], to_canonical: Callable[[Any], Any] | None = None) -> str:
    conv = to_canonical or (lambda x: x.to_canonical())
    return digest([conv(i) for i in items])
