"""H. Topology test, I. PipeRun test, J. PhysicalPipe test."""

from __future__ import annotations

import pytest

from vvs_pipe.topology import build_graph, build_runs


def test_branches_create_junction_nodes(analysis_a):
    page = analysis_a.pages[0]
    kinds = {n.node_id: n.kind for n in page.graph.nodes}
    assert sorted(kinds.values()).count("junction") == 2, kinds
    assert any(k == "endpoint" for k in kinds.values())


def test_corner_healing_restores_the_true_corner_length(analysis_a, specs_by_stem):
    """Offsetting two walls round a mitre loses length; the graph must recover it."""
    spec = specs_by_stem["drawing_a"]
    branch = next(p for p in spec.pipes if p.name == "branch_c")
    expected_pt = sum(
        ((branch.centerline[i][0] - branch.centerline[i + 1][0]) ** 2
         + (branch.centerline[i][1] - branch.centerline[i + 1][1]) ** 2) ** 0.5
        for i in range(len(branch.centerline) - 1)
    )
    page = analysis_a.pages[0]
    pipe = next(
        p
        for p in page.physical_pipes
        if p.designation == branch.designation and p.diameter_mm == branch.dn_mm
        and len(p.pipe_run_ids) >= 2
    )
    assert pipe.length_pt == pytest.approx(expected_pt, abs=0.05)


def test_runs_are_canonically_oriented(analysis_a):
    for page in analysis_a.pages:
        for run in page.runs:
            fwd = tuple((round(x, 4), round(y, 4)) for x, y in run.centerline)
            assert fwd <= tuple(reversed(fwd))


def test_runs_are_order_independent(analysis_a):
    from vvs_pipe.validation.determinism import digest_of_stage, permutations_of

    page = analysis_a.pages[0]
    baseline = None
    for _name, permuted in permutations_of(list(page.candidates)):
        graph = build_graph(permuted, page.page)
        runs = build_runs(graph, page.page)
        d = digest_of_stage(runs)
        if baseline is None:
            baseline = d
        else:
            assert d == baseline


def test_every_run_belongs_to_exactly_one_physical_pipe(analysis_a, analysis_b):
    for analysis in (analysis_a, analysis_b):
        for page in analysis.pages:
            owners: dict[str, int] = {}
            for pipe in page.physical_pipes:
                for rid in pipe.pipe_run_ids:
                    owners[rid] = owners.get(rid, 0) + 1
            assert sorted(owners) == sorted(r.pipe_run_id for r in page.runs)
            assert all(v == 1 for v in owners.values())


def test_physical_pipe_merges_the_two_runs_of_one_bent_pipe(analysis_a, specs_by_stem):
    spec = specs_by_stem["drawing_a"]
    branch = next(p for p in spec.pipes if p.name == "branch_c")
    page = analysis_a.pages[0]
    merged = [
        p
        for p in page.physical_pipes
        if p.designation == branch.designation and len(p.pipe_run_ids) > 1
    ]
    assert merged, "the bent branch should be one physical pipe made of two runs"
