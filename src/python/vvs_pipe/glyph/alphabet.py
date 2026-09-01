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

Exclusivity is a statement about the drawing's *alphabet*, not a bijection over
every shape on the sheet.  A real drawing splits into far more clusters than
there are characters - 872 against an alphabet of 49 on the reference sheet,
because the same letter appears at several sizes, weights and rotations - and
forcing a global one-to-one match there starves the alphabet: 827 of those
clusters, including a 205-instance ``R``, were left with no character at all.
So the exclusive match runs over the clusters carrying the most evidence, which
are the ones that realise the alphabet, and every remaining cluster then takes
its own best character with duplicates allowed.

A cluster whose best cost exceeds ``REJECT_COST`` is left unassigned and its
glyphs are reported UNRESOLVED_GLYPH rather than forced onto a character.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ..canonical import canonical_sort, qs
from .classify import glyph_distance_vector
from .features import GlyphRaster, chamfer, jaccard_distance

CLUSTER_CHAMFER_MAX = 0.55
CLUSTER_JACCARD_MAX = 0.22
CLUSTER_RELHEIGHT_MAX = 0.18
# Cheap pre-filter before the expensive shape measures: two renderings of the
# same character have almost identical coarse ink distributions.
SIGNATURE_L1_MAX = 0.35
REJECT_COST = 3.4
# How much a maximally-supported cluster's regret is amplified, used only to
# decide otherwise-tied assignments deterministically by weight of evidence.
REGRET_EVIDENCE_GAIN = 0.05


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

    Exact transitive clustering (two glyphs join one cluster if a chain of
    matches links them), which is what makes the partition independent of the
    order the glyphs arrive in.  The cost is kept down by a two-stage filter
    rather than by weakening the criterion:

    1. glyphs are blocked by enclosed-hole count;
    2. inside a block a cheap 4x4 ink-density signature distance is evaluated
       for all pairs at once with numpy, and only the survivors pay for the
       chamfer and Jaccard measures.

    On a sheet with a few thousand glyphs this turns a quadratic number of
    expensive comparisons into a quadratic number of *vectorised* ones plus a
    linear number of expensive ones.
    """
    from ..geometry.index import connected_components

    n = len(observations)
    if n == 0:
        return []

    signatures = np.array([o.raster.signature() for o in observations], dtype=np.float32)
    blocks: dict[int, list[int]] = {}
    for i, o in enumerate(observations):
        blocks.setdefault(o.raster.holes, []).append(i)

    edges: list[tuple[int, int]] = []
    for holes in sorted(blocks):
        members = blocks[holes]
        if len(members) < 2:
            continue
        sub = signatures[np.array(members, dtype=np.int64)]
        for a_pos in range(len(members)):
            deltas = np.abs(sub[a_pos + 1 :] - sub[a_pos]).sum(axis=1)
            for offset in np.nonzero(deltas <= SIGNATURE_L1_MAX)[0].tolist():
                i, j = members[a_pos], members[a_pos + 1 + offset]
                if _same_shape(observations[i], observations[j]):
                    edges.append((i, j) if i < j else (j, i))
    return connected_components(n, edges)


def _assign(
    character_of: dict[int, str | None],
    confidence_of: dict[int, float],
    cluster: int,
    ch: str,
    row: dict[str, float],
) -> None:
    """Record a cluster's character and how confident that decision is."""
    assigned_cost = row.get(ch, REJECT_COST)
    others = [d for c, d in sorted(row.items(), key=lambda kv: (kv[1], kv[0])) if c != ch]
    runner_up = others[0] if others else REJECT_COST
    margin = max(0.0, runner_up - assigned_cost)
    fit = max(0.0, 1.0 - assigned_cost / REJECT_COST)
    character_of[cluster] = ch
    confidence_of[cluster] = qs(min(1.0, 0.55 * fit + 0.45 * min(1.0, margin / 0.35)))


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

    # One distance vector per *cluster*, not per instance.  Every member of a
    # cluster has the same normalised shape by construction, so the shape
    # evidence is identical; the typographic metrics that do vary between
    # instances are averaged first.  This is what stops a sheet with thousands
    # of glyphs from paying for thousands of prototype sweeps.
    per_cluster: list[dict[str, float]] = []
    for members in comps:
        rep = obs[members[0]]
        rel_h = sum(obs[m].rel_height for m in members) / len(members)
        rel_b = sum(obs[m].rel_base for m in members) / len(members)
        per_cluster.append(glyph_distance_vector(rep.raster, rel_h, rel_b))

    chars = sorted({ch for row in per_cluster for ch in row})
    n_chars = len(chars)
    n_clusters = len(per_cluster)

    # The clusters that realise the alphabet are the ones with the most
    # evidence behind them; they compete exclusively.  The rest are variants of
    # the same characters at other sizes, and take their best match directly.
    order = sorted(range(n_clusters), key=lambda i: (-len(comps[i]), i))
    competing = sorted(order[:n_chars])
    competing_pos = {ci: k for k, ci in enumerate(competing)}

    character_of: dict[int, str | None] = {}
    confidence_of: dict[int, float] = {}
    ranked_of: dict[int, tuple[tuple[str, float], ...]] = {}
    sizes: dict[int, int] = {i: len(comps[i]) for i in range(n_clusters)}

    for i in range(n_clusters):
        ranked_of[i] = tuple(
            (c, qs(d)) for c, d in sorted(per_cluster[i].items(), key=lambda kv: (kv[1], kv[0]))[:6]
        )

    if competing:
        cost = np.full((len(competing), n_chars + len(competing)), REJECT_COST, dtype=np.float64)
        for k, ci in enumerate(competing):
            for j, ch in enumerate(chars):
                cost[k, j] = per_cluster[ci].get(ch, REJECT_COST)

        # Exact ties are common here and the solver would otherwise break them
        # arbitrarily.  "Give 1 to the cluster of 369 glyphs and I to the
        # cluster of one" and its reverse can score exactly the same total, and
        # picking either at random is the arbitrary tie-break this engine
        # forbids - with 369 characters riding on it.
        #
        # So what is minimised is *regret* - how much worse than its own best
        # match a cluster is forced to accept - with each cluster's regret
        # amplified by how much evidence stands behind it.
        sizes_arr = np.array([len(comps[ci]) for ci in competing], dtype=np.float64)
        span = math.log1p(float(sizes_arr.max())) if sizes_arr.size else 1.0
        weight = (np.log1p(sizes_arr) / span) if span > 0 else np.zeros_like(sizes_arr)
        best_cost = cost[:, :n_chars].min(axis=1) if n_chars else np.zeros(len(competing))
        regret = cost - best_cost[:, None]
        scaled = best_cost[:, None] + regret * (1.0 + REGRET_EVIDENCE_GAIN * weight[:, None])
        rows, cols = linear_sum_assignment(scaled)
        for k, j in zip(rows.tolist(), cols.tolist()):
            ci = competing[k]
            row = per_cluster[ci]
            if j >= n_chars or cost[k, j] >= REJECT_COST:
                character_of[ci] = None
                confidence_of[ci] = 0.0
                continue
            ch = chars[j]
            _assign(character_of, confidence_of, ci, ch, row)

    for ci in range(n_clusters):
        if ci in competing_pos:
            continue
        row = per_cluster[ci]
        ranked = sorted(row.items(), key=lambda kv: (kv[1], kv[0]))
        if not ranked or ranked[0][1] >= REJECT_COST:
            character_of[ci] = None
            confidence_of[ci] = 0.0
            continue
        _assign(character_of, confidence_of, ci, ranked[0][0], row)

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
