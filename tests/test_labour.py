from pathlib import Path

from logitwin.data import make_cartons, make_order_stream, make_warehouse
from logitwin.labour import (
    DEFAULT_CREW_SIZES,
    labour_report,
    recommended_crew,
    sweep_labour,
    to_csv,
    to_svg,
)
from logitwin.simulate import simulate, simulate_assignment
from logitwin.slotting import legacy_slotting, optimize_slotting


def _fixture():
    cartons = make_cartons()
    wh = make_warehouse(n_slots=len(cartons))
    orders = make_order_stream(cartons)
    return cartons, wh, orders


def test_single_picker_reproduces_the_canonical_regime_numbers():
    """crew=1 must reproduce the engine's existing single-picker simulation exactly - the labour
    sweep only adds parallel pickers, it does not change the base model."""
    cartons, wh, orders = _fixture()
    for regime in ("legacy", "modern"):
        rows = sweep_labour(cartons, wh, orders, regime, 1 if regime == "legacy" else 4)
        base = rows[0]
        canonical = simulate(regime, cartons, wh, orders)
        assert base.n_pickers == 1
        assert base.mean_cycle_s == round(canonical.mean_cycle_time_s, 2)
        assert base.sim_travel_m == round(canonical.total_travel_m, 1)
        assert base.cycle_reduction_pct == 0.0  # crew=1 is its own baseline


def test_more_pickers_never_raise_mean_cycle_time():
    """Adding crew can only lower (or hold) mean cycle time - no congestion is modelled, so the
    curve is monotone non-increasing and its reduction-vs-baseline is monotone non-decreasing."""
    cartons, wh, orders = _fixture()
    for regime, batch in (("legacy", 1), ("modern", 4)):
        rows = sweep_labour(cartons, wh, orders, regime, batch)
        means = [r.mean_cycle_s for r in rows]
        assert means == sorted(means, reverse=True)
        reductions = [r.cycle_reduction_pct for r in rows]
        assert reductions == sorted(reductions)
        assert reductions[0] == 0.0


def test_completion_count_and_makespan_are_arrival_bound():
    """The honest finding: every crew size completes the same orders within the same horizon, so
    the labour lever shows up as responsiveness/utilization, not as a higher completion rate."""
    cartons, wh, orders = _fixture()
    rows = sweep_labour(cartons, wh, orders, "modern", 4)
    assert {r.orders_completed for r in rows} == {len(orders)}
    assert len({r.makespan_s for r in rows}) == 1  # makespan identical across crew sizes


def test_total_travel_is_flat_under_the_legacy_regime():
    """Legacy picks one line at a time (no batching), so parallelizing pickers cannot change the
    total distance walked - it is byte-identical across every crew size."""
    cartons, wh, orders = _fixture()
    rows = sweep_labour(cartons, wh, orders, "legacy", 1)
    assert len({r.sim_travel_m for r in rows}) == 1


def test_utilization_falls_as_crew_grows():
    cartons, wh, orders = _fixture()
    rows = sweep_labour(cartons, wh, orders, "modern", 4)
    utils = [r.utilization_pct for r in rows]
    assert utils == sorted(utils, reverse=True)  # each added picker is less loaded


def test_parallel_pickers_beat_a_single_picker_on_cycle_time():
    """Two pickers on the same layout must strictly beat one on mean cycle time (real queueing)."""
    cartons, wh, orders = _fixture()
    legacy = legacy_slotting(cartons, wh).assignment
    one = simulate_assignment(legacy, cartons, wh, orders, batch_size=1, n_pickers=1)
    two = simulate_assignment(legacy, cartons, wh, orders, batch_size=1, n_pickers=2)
    assert two.mean_cycle_time_s < one.mean_cycle_time_s
    assert two.orders_completed == one.orders_completed  # same work, parallelized


def test_recommended_crew_captures_target_share_with_less_than_full_crew():
    cartons, wh, orders = _fixture()
    rows = sweep_labour(cartons, wh, orders, "legacy", 1)
    rec = recommended_crew(rows, capture=0.90)
    full = rows[-1]
    assert rec.cycle_reduction_pct >= 0.90 * full.cycle_reduction_pct
    assert rec.n_pickers < full.n_pickers  # the knee is cheaper than a fully-staffed crew


def test_sweep_is_deterministic():
    cartons, wh, orders = _fixture()
    a = sweep_labour(cartons, wh, orders, "modern", 4)
    b = sweep_labour(cartons, wh, orders, "modern", 4)
    assert a == b


def test_labour_report_pins_the_seeded_numbers():
    """Exact figures the README quotes - synthetic, seeded; wins and the flat tail alike."""
    rep = labour_report()
    lg = rep["by_regime"]["legacy"]
    md = rep["by_regime"]["modern"]
    # Single-picker floor: modern reproduces the canonical 158.0s modern cycle time.
    assert md[0].mean_cycle_s == 158.00
    assert lg[0].mean_cycle_s == 662.75
    # A second picker banks the bulk of the responsiveness.
    assert lg[1].mean_cycle_s == 247.74
    assert md[1].mean_cycle_s == 118.69
    # Both curves flatten to a fixed floor by the fourth picker.
    assert lg[-1].mean_cycle_s == 229.14
    assert md[-1].mean_cycle_s == 115.25
    # Recommended crew (>=90% of the cycle-time saving) is 2 pickers per regime.
    assert rep["recommended"]["legacy"].n_pickers == 2
    assert rep["recommended"]["modern"].n_pickers == 2
    # Utilization at a single picker: legacy near-saturated, modern well under.
    assert lg[0].utilization_pct == 81.14
    assert md[0].utilization_pct == 40.57


def test_csv_and_svg_are_deterministic_and_well_formed():
    rep = labour_report()
    csv1, csv2 = to_csv(rep), to_csv(rep)
    assert csv1 == csv2
    assert csv1.startswith("regime,n_pickers,")
    # header + one line per (regime, crew size)
    assert csv1.rstrip().count("\n") == 2 * len(DEFAULT_CREW_SIZES)

    svg1, svg2 = to_svg(rep), to_svg(rep)
    assert svg1 == svg2
    assert svg1.startswith("<svg") and svg1.rstrip().endswith("</svg>")
    # No wall-clock / RNG leakage into the committed artefact.
    for token in ("2026", "random", "NaN", "Infinity"):
        assert token not in svg1


def test_committed_docs_artifacts_are_in_sync_with_the_generator():
    """The README-referenced docs/ CSV + SVG must match freshly generated output byte-for-byte, so
    they can never silently drift from the seeded engine and stay deterministic."""
    docs = Path(__file__).resolve().parent.parent / "docs"
    rep = labour_report()
    assert (docs / "labour_sensitivity.csv").read_text(encoding="utf-8") == to_csv(rep)
    assert (docs / "labour_sensitivity.svg").read_text(encoding="utf-8") == to_svg(rep)


def test_optimized_layout_matches_the_canonical_modern_slotting():
    """The modern sweep must use the same tie-broken optimum as the rest of the engine, so its
    crew=1 point is consistent with the slotting report and the 'modern' simulation regime."""
    cartons, wh, _ = _fixture()
    from logitwin.labour import _assignment_for

    legacy = legacy_slotting(cartons, wh).assignment
    expected = optimize_slotting(cartons, wh, prefer=legacy).assignment
    assert _assignment_for("modern", cartons, wh) == expected
    assert _assignment_for("legacy", cartons, wh) == legacy
