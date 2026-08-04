from pathlib import Path

from logitwin.data import make_cartons, make_order_stream, make_warehouse
from logitwin.sensitivity import (
    DEFAULT_FRACTIONS,
    frontier_report,
    golden_zone_occupancy_pct,
    partial_reslot,
    recommended_operating_point,
    sweep,
    to_csv,
    to_svg,
)
from logitwin.slotting import legacy_slotting, optimize_slotting


def _fixture():
    cartons = make_cartons()
    wh = make_warehouse(n_slots=len(cartons))
    orders = make_order_stream(cartons)
    return cartons, wh, orders


def test_partial_reslot_endpoints_are_legacy_and_full_optimum():
    cartons, wh, _ = _fixture()
    legacy = legacy_slotting(cartons, wh).assignment
    opt = optimize_slotting(cartons, wh, prefer=legacy).assignment

    at_zero = partial_reslot(cartons, wh, 0.0)
    at_one = partial_reslot(cartons, wh, 1.0)

    assert at_zero == legacy  # f=0 leaves everything in place
    # f=1 lets every SKU participate -> same travel as the full one-SKU-per-slot optimum.
    dist = {s.id: s.distance for s in wh.slots}
    demand = {c.sku: {"A": 40.0, "B": 12.0, "C": 3.0}[c.velocity] for c in cartons}
    t_one = sum(demand[k] * dist[v] for k, v in at_one.items())
    t_opt = sum(demand[k] * dist[v] for k, v in opt.items())
    assert abs(t_one - t_opt) < 1e-6


def test_partial_reslot_is_a_valid_permutation_at_every_fraction():
    cartons, wh, _ = _fixture()
    skus = {c.sku for c in cartons}
    for f in DEFAULT_FRACTIONS:
        a = partial_reslot(cartons, wh, f)
        assert set(a.keys()) == skus
        slots = list(a.values())
        assert len(slots) == len(set(slots))  # one SKU per slot


def test_travel_reduction_is_monotone_non_decreasing_in_the_lever():
    cartons, wh, orders = _fixture()
    rows = sweep(cartons, wh, orders)
    reductions = [r.travel_reduction_pct for r in rows]
    assert reductions == sorted(reductions)  # committing more SKUs never raises travel
    assert reductions[0] == 0.0  # legacy baseline
    assert reductions[-1] > 40.0  # full re-slot lands the headline saving


def test_golden_zone_occupancy_rises_from_legacy_to_optimum():
    cartons, wh, _ = _fixture()
    legacy = legacy_slotting(cartons, wh).assignment
    opt = optimize_slotting(cartons, wh, prefer=legacy).assignment
    occ_legacy = golden_zone_occupancy_pct(legacy, cartons, wh)
    occ_opt = golden_zone_occupancy_pct(opt, cartons, wh)
    assert occ_opt > occ_legacy
    assert occ_opt == 100.0  # the near slots end up entirely A-movers


def test_sweep_is_deterministic():
    cartons, wh, orders = _fixture()
    a = sweep(cartons, wh, orders)
    b = sweep(cartons, wh, orders)
    assert a == b


def test_recommended_point_captures_target_share_with_less_than_full_effort():
    cartons, wh, orders = _fixture()
    rows = sweep(cartons, wh, orders)
    rec = recommended_operating_point(rows, capture=0.90)
    full = rows[-1]
    assert rec.travel_reduction_pct >= 0.90 * full.travel_reduction_pct
    assert rec.n_moves <= full.n_moves
    # On the seeded 60-SKU dataset the knee is strictly cheaper than the full re-slot.
    assert rec.n_moves < full.n_moves


def test_frontier_report_pins_the_seeded_numbers():
    """Exact figures the README quotes - synthetic, seeded; wins and the flat part alike."""
    rep = frontier_report()
    rows = rep["rows"]
    assert [r.n_moves for r in rows] == [0, 0, 0, 6, 16, 16, 24, 33, 36, 36, 36]
    assert rep["full_reslot"].travel_reduction_pct == 44.18
    assert rep["recommended"].promote_fraction == 0.7
    assert rep["recommended"].n_moves == 33
    assert rep["recommended"].travel_reduction_pct == 43.71
    # f=1 reproduces the canonical modern cycle time reported elsewhere in the engine.
    assert rep["full_reslot"].mean_cycle_s == 158.0


def test_csv_and_svg_are_deterministic_and_well_formed():
    rep = frontier_report()
    csv1, csv2 = to_csv(rep["rows"]), to_csv(rep["rows"])
    assert csv1 == csv2
    assert csv1.startswith("promote_fraction,")
    assert csv1.rstrip().count("\n") == len(rep["rows"])  # header + one line per row

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
    rep = frontier_report()
    assert (docs / "slotting_sensitivity.csv").read_text(encoding="utf-8") == to_csv(rep["rows"])
    assert (docs / "slotting_sensitivity.svg").read_text(encoding="utf-8") == to_svg(rep)
