"""Tests for the honest 1D bin-packing benchmark (logitwin/benchmark.py).

These pin the *real* engine results on the embedded instances: exact optimum where FFD should hit
it, and the known suboptimal gap where FFD loses. They also check determinism, schema/optimum
validation, and that CP-SAT reaches the proven optimum on every instance.
"""

from __future__ import annotations

import copy
import json
import math

import pytest

from logitwin.benchmark import (
    SCHEMA,
    BenchmarkError,
    evaluate_all,
    evaluate_instance,
    load_instances,
    render_table,
    run_benchmark,
    validate_instance,
)

# Expected engine results per instance, pinned to real runs:
#   name -> (n_items, ffd_bins, cpsat_bins, known_optimum, ffd_abs_gap, ffd_pct_gap)
EXPECTED = {
    "perfect-pairs-8": (8, 4, 4, 4, 0, 0.0),
    "even-split-12": (12, 6, 6, 6, 0, 0.0),
    "triplet-9": (9, 4, 3, 3, 1, 33.33),
    "triplet-18": (18, 7, 6, 6, 1, 16.67),
    "ffd-suboptimal-8": (8, 4, 3, 3, 1, 33.33),
    "ffd-suboptimal-10": (10, 5, 4, 4, 1, 25.0),
}


def _results_by_name() -> dict:
    return {r.name: r for r in evaluate_all()}


def _valid_raw() -> dict:
    """A minimal, valid raw instance dict for mutation in schema tests."""
    return {
        "name": "unit-test",
        "dimension": "1D",
        "bin_capacity": 100,
        "items": [60, 40, 55, 45],
        "known_optimum": 2,
        "optimum_source": "constructed",
        "optimum_proof": "sum=200, cap=100, LB=2, witness achieves 2.",
        "optimal_packing": [[60, 40], [55, 45]],
        "citation": "constructed for this test.",
        "note": "test fixture.",
    }


def test_load_instances_returns_all_with_unique_names():
    instances = load_instances()
    names = [i.name for i in instances]
    assert set(names) == set(EXPECTED)
    assert len(names) == len(set(names))
    assert all(i.dimension == "1D" for i in instances)


def test_ffd_optimal_where_expected():
    results = _results_by_name()
    for name in ("perfect-pairs-8", "even-split-12"):
        r = results[name]
        assert r.ffd_bins == r.known_optimum
        assert r.ffd_abs_gap == 0
        assert r.ffd_pct_gap == 0.0
        assert r.note == "FFD optimal"


def test_ffd_suboptimal_gaps_are_pinned_to_known_numbers():
    """Do NOT cherry-pick wins: assert the exact gap where FFD is worse than optimal."""
    results = _results_by_name()
    for name in ("triplet-9", "triplet-18", "ffd-suboptimal-8", "ffd-suboptimal-10"):
        r = results[name]
        _, ffd_bins, _, opt, abs_gap, pct_gap = EXPECTED[name]
        assert r.ffd_bins == ffd_bins
        assert r.known_optimum == opt
        assert r.ffd_abs_gap == abs_gap == r.ffd_bins - r.known_optimum
        assert r.ffd_pct_gap == pct_gap
        assert r.ffd_bins > r.known_optimum  # genuinely suboptimal


def test_all_pinned_engine_numbers_match():
    results = _results_by_name()
    for name, (n, ffd_bins, cpsat_bins, opt, abs_gap, pct_gap) in EXPECTED.items():
        r = results[name]
        assert (r.n_items, r.ffd_bins, r.cpsat_bins, r.known_optimum, r.ffd_abs_gap, r.ffd_pct_gap) == (
            n,
            ffd_bins,
            cpsat_bins,
            opt,
            abs_gap,
            pct_gap,
        )


def test_cpsat_reaches_proven_optimum_on_every_instance():
    for r in evaluate_all():
        assert r.cpsat_bins == r.known_optimum
        assert r.cpsat_abs_gap == 0
        assert r.cpsat_pct_gap == 0.0


def test_reported_optimum_matches_its_proof_for_every_instance():
    """The known/optimal value must equal the volume lower bound AND the exhibited packing."""
    for inst in load_instances():
        volume_lb = math.ceil(sum(inst.items) / inst.bin_capacity)
        assert volume_lb == inst.known_optimum  # a valid lower bound
        assert len(inst.optimal_packing) == inst.known_optimum  # achievable upper bound
        assert all(sum(b) <= inst.bin_capacity for b in inst.optimal_packing)
        assert sorted(x for b in inst.optimal_packing for x in b) == sorted(inst.items)
        # FFD can never beat the true optimum.
        assert evaluate_instance(inst).ffd_bins >= inst.known_optimum


def test_evaluate_all_is_deterministic():
    assert evaluate_all() == evaluate_all()


def test_run_benchmark_without_runtime_is_deterministic():
    assert run_benchmark(with_runtime=False) == run_benchmark(with_runtime=False)


def test_run_benchmark_summary_counts():
    report = run_benchmark(with_runtime=False)
    assert report["schema"] == SCHEMA
    assert report["n_instances"] == len(EXPECTED)
    assert report["summary"]["ffd_optimal_instances"] == 2
    assert report["summary"]["ffd_suboptimal_instances"] == 4
    assert report["summary"]["cpsat_all_optimal"] is True
    assert report["summary"]["worst_ffd_pct_gap"] == 33.33


def test_validate_instance_accepts_a_valid_instance():
    inst = validate_instance(_valid_raw())
    assert inst.name == "unit-test"
    assert inst.known_optimum == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("items"),
        lambda d: d.update(dimension="3D"),
        lambda d: d.update(items=[60, 40, 55, 145]),  # item > capacity
        lambda d: d.update(optimal_packing=[[60, 40], [55, 45, 30]]),  # bin over capacity
        lambda d: d.update(optimal_packing=[[60, 40]]),  # bins != known_optimum
        lambda d: d.update(known_optimum=3),  # volume LB (2) != claimed optimum
        lambda d: d.update(items=[60, 40, 55, 46]),  # packing no longer covers items
        lambda d: d.update(known_optimum=True),  # bool is not an int optimum
    ],
)
def test_validate_instance_rejects_malformed(mutate):
    raw = _valid_raw()
    mutate(raw)
    with pytest.raises(BenchmarkError):
        validate_instance(raw)


def test_load_instances_rejects_wrong_schema(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "not-this", "instances": []}), encoding="utf-8")
    with pytest.raises(BenchmarkError):
        load_instances(bad)


def test_load_instances_rejects_duplicate_names(tmp_path):
    raw = _valid_raw()
    dup = tmp_path / "dup.json"
    dup.write_text(
        json.dumps({"schema": SCHEMA, "instances": [copy.deepcopy(raw), copy.deepcopy(raw)]}),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError):
        load_instances(dup)


def test_render_table_is_honest_about_losses():
    table = render_table(run_benchmark(with_runtime=False))
    for name in EXPECTED:
        assert name in table
    assert "FFD +1 bin over optimum" in table  # losses shown plainly
    assert "optimal" in table
    assert "11/9" in table and "Dosa" in table  # worst-case bound cited
    assert "Falkenauer" in table  # triplet provenance named


def test_run_benchmark_with_runtime_is_json_serializable():
    report = run_benchmark(with_runtime=True)
    text = json.dumps(report)
    assert '"runtime_ms"' in text
    for row in report["instances"]:
        assert row["runtime_ms"]["ffd"] >= 0.0
        assert row["runtime_ms"]["cpsat"] >= 0.0
