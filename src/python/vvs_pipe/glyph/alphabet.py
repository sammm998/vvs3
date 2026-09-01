"""Unsupervised alphabet resolution.

Matching each glyph against a font bank independently leaves systematic
confusions that a human reader resolves instantly: a single-stroke ``S`` and a
single-stroke ``5`` may both sit closest to the bank's ``5``.

This module removes that class of error without any knowledge of what the
drawing says, by exploiting a property of the drawing itself:

1. **the drawing uses one typeface**, so every occurrence of a character has
   the same normalised shape - glyphs are therefore clustered by shape and
   each cluster is decided once, using evidence aggregated over all of its
   instances;
2. **distinct shapes are distinct characters** - so the clusters are matched
   to the alphabet by a globally optimal *one-to-one* assignment rather than
   independent nearest-neighbour lookups.  If the ``S``-shaped cluster and the
   ``5``-shaped cluster both prefer ``5``, the assignment gives ``5`` to
   whichever fits it better and the other cluster falls to its next best
   character.

A cluster whose best cost exceeds ``REJECT_COST`` is left unassigned and its
glyphs are reported UNRESOLVED_GLYPH rather than forced onto a character.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ..canonical import canonical_sort, qs
from .classify import glyph_distance_vector
from .features import GlyphRaster, chamfer, jaccard_distance

CLUSTER_CHAMFER_MAX = 0.55
CLUSTER_JACCARD_MAX = 0.22
CLUSTER_RELHEIGHT_MAX = 0.18
REJECT_COST = 3.4


@dataclass(frozen=True, slots=True)
class GlyphObservation:
    """One glyph instance, ready for alphabet resolution."""

    key: tuple
    raster: GlyphRaster
    rel_height: float
    rel_base: float


@dataclass(frozen=True, slots=True)
class AlphabetAssignment:
    cluster_of: dict[tuple, int]
    character_of_cluster: dict[int, str | None]
    confidence_of_cluster: dict[int, float]
    ranked_of_cluster: dict[int, tuple[tuple[str, float], ...]]
    cluster_sizes: dict[int, int]

    def character(self, key: tuple) -> str | None:
        c = self.cluster_of.get(key)
        return None if c is None else self.character_of_cluster.get(c)

    def confidence(self, key: tuple) -> float:
        c = self.cluster_of.get(key)
        return 0.0 if c is None else self.confidence_of_cluster.get(c, 0.0)

    def ranked(self, key: tuple) -> tuple[tuple[str, float], ...]:
        c = self.cluster_of.get(key)
        return () if c is None else self.ranked_of_cluster.get(c, ())

    def cluster_size(self, key: tuple) -> int:
        c = self.cluster_of.get(key)
        return 0 if c is None else self.cluster_sizes.get(c, 0)


def _same_shape(a: GlyphObservation, b: GlyphObservation) -> bool:
    if a.raster.holes != b.raster.holes:
        return False
    if abs(a.rel_height - b.rel_height) > CLUSTER_RELHEIGHT_MAX:
        return False
    if jaccard_distance(a.raster, b.raster) > CLUSTER_JACCARD_MAX:
        return False
    return chamfer(a.raster, b.raster) <= CLUSTER_CHAMFER_MAX


def cluster_glyphs(observations: Sequence[GlyphObservation]) -> list[list[int]]:
    """Agglomerate glyph instances by normalised shape.

    Blocking on the hole count and the relative height keeps this far below the
    O(n^2) worst case on real sheets; within a block the pairwise test is
    symmetric, so the resulting partition does not depend on input order.
    """
    from ..geometry.index import connected_components

    n = len(observations)
    blocks: dict[tuple[int, int], list[int]] = {}
    for i, o in enumerate(observations):
        blocks.setdefault((o.raster.holes, int(round(o.rel_height / CLUSTER_RELHEIGHT_MAX))), []).append(i)
    # A glyph near a block boundary must be comparable with its neighbours, so
    # each glyph is also tested against the adjacent height bucket.
    edges: list[tuple[int, int]] = []
    for (holes, bucket), members in sorted(blocks.items()):
        neighbours = sorted(set(members) | set(blocks.get((holes, bucket + 1), [])))
        for ai in range(len(neighbours)):
            for bi in range(ai + 1, len(neighbours)):
                i, j = neighbours[ai], neighbours[bi]
                if _same_shape(observations[i], observations[j]):
                    edges.append((i, j))
    return connected_components(n, edges)


def resolve_alphabet(
    observations: Sequence[GlyphObservation],
    singletons: Sequence[GlyphObservation] = (),
) -> AlphabetAssignment:
    """Resolve the drawing's alphabet.

    ``observations`` are glyphs that sit in a text line of two or more glyphs -
    those are certainly text, and only they take part in the exclusive
    assignment.  ``singletons`` are lone marks (a scale-bar label, but also a
    riser circle or any other isolated symbol): they are matched *against the
    resolved clusters* afterwards, so a symbol that resembles nothing in the
    drawing's own alphabet stays unresolved instead of consuming a character
    that real text needs.
    """
    from scipy.optimize import linear_sum_assignment

    obs = canonical_sort(observations, key=lambda o: o.key)
    if not obs:
        return AlphabetAssignment({}, {}, {}, {}, {})

    comps = cluster_glyphs(obs)
    cluster_of: dict[tuple, int] = {}
    for ci, members in enumerate(comps):
        for m in members:
            cluster_of[obs[m].key] = ci

    # Aggregate the per-character distance over every instance of a cluster.
    per_cluster: list[dict[str, float]] = []
    for members in comps:
        acc: dict[str, float] = {}
        for m in members:
            o = obs[m]
            for ch, d in glyph_distance_vector(o.raster, o.rel_height, o.rel_base).items():
                acc[ch] = acc.get(ch, 0.0) + d
        n = float(len(members))
        per_cluster.append({ch: v / n for ch, v in acc.items()})

    chars = sorted({ch for row in per_cluster for ch in row})
    n_clusters = len(per_cluster)
    n_chars = len(chars)
    # Real characters, then one reject column per cluster so that a cluster is
    # allowed to stay unassigned instead of being forced onto a character.
    cost = np.full((n_clusters, n_chars + n_clusters), REJECT_COST, dtype=np.float64)
    for i, row in enumerate(per_cluster):
        for j, ch in enumerate(chars):
            cost[i, j] = row.get(ch, REJECT_COST)
    rows, cols = linear_sum_assignment(cost)

    character_of: dict[int, str | None] = {}
    confidence_of: dict[int, float] = {}
    ranked_of: dict[int, tuple[tuple[str, float], ...]] = {}
    sizes: dict[int, int] = {i: len(comps[i]) for i in range(n_clusters)}
    for i, j in zip(rows.tolist(), cols.tolist()):
        row = per_cluster[i]
        ranked = sorted(row.items(), key=lambda kv: (kv[1], kv[0]))
        ranked_of[i] = tuple((c, qs(d)) for c, d in ranked[:6])
        if j >= n_chars or cost[i, j] >= REJECT_COST:
            character_of[i] = None
            confidence_of[i] = 0.0
            continue
        ch = chars[j]
        assigned_cost = row.get(ch, REJECT_COST)
        # Margin against the best *other* character for this cluster.
        others = [d for c, d in ranked if c != ch]
        runner_up = others[0] if others else REJECT_COST
        margin = max(0.0, runner_up - assigned_cost)
        fit = max(0.0, 1.0 - assigned_cost / REJECT_COST)
        character_of[i] = ch
        confidence_of[i] = qs(min(1.0, 0.55 * fit + 0.45 * min(1.0, margin / 0.35)))

    # Lone marks: adopt the character of the cluster whose shape they match.
    next_cluster = n_clusters
    for so in canonical_sort(singletons, key=lambda o: o.key):
        matched: int | None = None
        for ci, members in enumerate(comps):
            if character_of.get(ci) is None:
                continue
            if _same_shape(so, obs[members[0]]):
                matched = ci
                break
        if matched is None:
            cluster_of[so.key] = next_cluster
            character_of[next_cluster] = None
            confidence_of[next_cluster] = 0.0
            row = glyph_distance_vector(so.raster, so.rel_height, so.rel_base)
            ranked_of[next_cluster] = tuple(
                (c, qs(d)) for c, d in sorted(row.items(), key=lambda kv: (kv[1], kv[0]))[:6]
            )
            sizes[next_cluster] = 1
            next_cluster += 1
        else:
            cluster_of[so.key] = matched
            sizes[matched] = sizes.get(matched, 0) + 1
    return AlphabetAssignment(cluster_of, character_of, confidence_of, ranked_of, sizes)
