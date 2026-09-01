"""Order of arrival may not change any answer."""

from __future__ import annotations

from pdf_forensics.analyze import analyse
from pdf_forensics.validation import determinism_check, result_digest


def test_permuted_input_order_gives_an_identical_result(clean_b):
    def run(order: str) -> dict:
        _, report = analyse(clean_b, order=order)
        return report

    result = determinism_check(run, orders=("normal", "reversed", "permuted:1"))
    assert result["identical"], result["orders"]


def test_repeated_runs_are_byte_identical(clean_a):
    _, first = analyse(clean_a)
    _, second = analyse(clean_a)
    assert result_digest(first) == result_digest(second)


def test_canonical_ordering_is_content_based():
    from pdf_forensics.canonical import sort_canonical, undirected
    items = [{"b": 2}, {"a": 1}, {"c": 3}]
    assert sort_canonical(items, key=lambda i: i) == sort_canonical(list(reversed(items)),
                                                                    key=lambda i: i)
    assert undirected([(3.0, 4.0), (1.0, 2.0)]) == undirected([(1.0, 2.0), (3.0, 4.0)])
