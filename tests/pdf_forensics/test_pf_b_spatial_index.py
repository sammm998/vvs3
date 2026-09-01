"""The index must be exact, backend-independent and cheaper than a scan."""

from __future__ import annotations

import random

from pdf_forensics.loader import load
from pdf_forensics.objects import extract
from pdf_forensics.paths import PathModel
from pdf_forensics.spatial_index import (SpatialIndex, bbox_distance, bbox_intersects)
from pdf_forensics.geometry_search import seg_bbox


def _entries(path):
    with load(path) as pdf:
        segments = PathModel(extract(pdf).drawing_objects()).segments
    return [(s.segment_id, s.page, seg_bbox(s)) for s in segments]


def test_backends_agree(clean_a):
    entries = _entries(clean_a)
    grid = SpatialIndex(entries, backend="grid")
    tree = SpatialIndex(entries, backend="strtree")
    rng = random.Random(7)
    for _ in range(50):
        x, y = rng.uniform(0, 1190), rng.uniform(0, 841)
        radius = rng.uniform(1, 80)
        assert grid.near_point(0, (x, y), radius) == tree.near_point(0, (x, y), radius)
        box = (x, y, x + radius, y + radius)
        assert grid.intersecting_bbox(0, box) == tree.intersecting_bbox(0, box)


def test_index_matches_a_brute_force_scan(clean_b):
    entries = _entries(clean_b)
    index = SpatialIndex(entries)
    rng = random.Random(11)
    for _ in range(25):
        x, y = rng.uniform(0, 841), rng.uniform(0, 595)
        radius = rng.uniform(2, 60)
        query = (x - radius, y - radius, x + radius, y + radius)
        expected = sorted(key for key, page, box in entries
                          if page == 0 and bbox_distance(box, (x, y, x, y)) <= radius + 1e-9)
        assert index.near_point(0, (x, y), radius) == expected


def test_results_do_not_depend_on_insertion_order(clean_a):
    entries = _entries(clean_a)
    forward = SpatialIndex(entries)
    backward = SpatialIndex(list(reversed(entries)))
    assert forward.near_point(0, (330, 620), 30) == backward.near_point(0, (330, 620), 30)
