"""Post-hoc evaluation.

**This package must never be imported by the pipeline.**  It is the only place
in the repository allowed to read a facit - a ground-truth JSON manifest or an
Excel take-off - and it runs strictly *after* a blind analysis has finished, on
that analysis's finished output.  ``tests/python/test_blind_leakage.py``
asserts that ``vvs_pipe.pipeline``'s import closure cannot reach this module.
"""

from .facit import compare_with_ground_truth, load_ground_truth

__all__ = ["compare_with_ground_truth", "load_ground_truth"]
