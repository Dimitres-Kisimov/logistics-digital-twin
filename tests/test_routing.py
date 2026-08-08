import math
import random
from itertools import permutations
from pathlib import Path

import pytest

from logitwin.data import make_cartons, make_order_stream, make_warehouse
from logitwin.routing import (
    EXACT_MAX_PICKS,
    HEURISTICS,
    POLICIES,
    RoutingGeometry,
    _assignment_for,
    heuristic_route,
    optimal_route_length,
    order_pick_points,
    recommended_policy,
    route_length,
    route_visits_all,
    routing_report,
    sweep_slotting,
    to_csv,
    to_svg,
)


def _fixture():
    cartons = make_cartons()
    wh = make_warehouse(n_slots=len(cartons))
    orders = make_order_stream(cartons)
    return cartons, wh, orders


def _geom():
    return RoutingGeometry.from_warehouse(make_warehouse(n_slots=60))


# --- geometry / distance (hand-checked) ---------------------------------------------------------


def test_geometry_derives_a_single_block_from_the_layout():
    geom = _geom()
    # make_warehouse packs bays 0..3 (pos // 3 over 10 positions), so L = 4 bays * 1.2 m.
    assert geom.aisle_length == pytest.approx(4.8)
    assert geom.floor_point(0, 0) == pytest.approx((0.0, 0.6))
    assert geom.floor_point(2, 1) == pytest.approx((10.0, 1.8))
    assert geom.floor_point(3, 3) == pytest.approx((15.0, 4.2))


def test_constrained_distance_is_rectilinear_via_the_cheaper_cross_aisle():
    geom = _geom()  # L = 4.8
    # Same aisle -> straight down the aisle.
    assert geom.distance((5.0, 0.6), (5.0, 3.0)) == pytest.approx(2.4)
    # Different aisles, picks near the front -> the front cross-aisle is cheaper.
    assert geom.distance((0.0, 0.6), (5.0, 1.8)) == pytest.approx(0.6 + 5.0 + 1.8)
    # Different aisles, picks near the back -> the back cross-aisle wins.
    assert geom.distance((0.0, 4.2), (5.0, 4.2)) == pytest.approx((4.8 - 4.2) + 5.0 + (4.8 - 4.2))


# --- collapse-to-base-case ----------------------------------------------------------------------


def test_single_pick_all_policies_equal_the_out_and_back_optimum():
    """One pick face: every policy must be the same simple out-and-back, and it is the optimum."""
    geom = _geom()
    pt = geom.floor_point(2, 1)  # (10, 1.8)
    expected = 2.0 * geom.distance(geom.depot, pt)  # 2 * 11.8 = 23.6
    assert expected == pytest.approx(23.6)
    for policy in POLICIES:
        assert route_length(policy, [pt], geom) == pytest.approx(expected)


def test_single_aisle_order_collapses_to_a_return_trip_for_every_policy():
    """All picks in one aisle: nothing to route between aisles, so every heuristic equals the
    optimum, which is 2*(aisle offset) + 2*(deepest pick) = a plain return trip."""
    geom = _geom()
    pts = [geom.floor_point(3, 0), geom.floor_point(3, 2)]  # x=15, depths 0.6 and 3.0
    expected = 2.0 * 15.0 + 2.0 * 3.0  # 36.0
    assert optimal_route_length(pts, geom) == pytest.approx(expected)
    for policy in HEURISTICS:
        assert route_length(policy, pts, geom) == pytest.approx(expected)


def test_two_aisle_s_shape_and_return_match_hand_computed_lengths():
    geom = _geom()  # L = 4.8
    pts = [geom.floor_point(0, 0), geom.floor_point(1, 0)]  # (0,0.6) and (5,0.6)
    # S-shape traverses both aisles fully: 2L + 2*x_last = 9.6 + 10 = 19.6.
    assert route_length("s-shape", pts, geom) == pytest.approx(2 * 4.8 + 2 * 5.0)
    # Return only dips to depth 0.6 in each aisle: 2*0.6 + 2*0.6 + 2*5 (out and back on the front).
    assert route_length("return", pts, geom) == pytest.approx(2 * 0.6 + 2 * 0.6 + 2 * 5.0)
    # At this density the return route is already optimal; s-shape is strictly worse.
    assert optimal_route_length(pts, geom) == pytest.approx(route_length("return", pts, geom))
    assert route_length("s-shape", pts, geom) > optimal_route_length(pts, geom)


# --- the two core invariants over the whole seeded stream ---------------------------------------


def test_no_heuristic_ever_beats_the_exact_optimum():
    """The hard guarantee: an exact optimum is a lower bound for every heuristic, per order, on both
    layouts. If any construction under-counts (a bug), this fails."""
    cartons, wh, orders = _fixture()
    geom = RoutingGeometry.from_warehouse(wh)
    for slotting in ("legacy", "optimized"):
        assignment = _assignment_for(slotting, cartons, wh)
        for o in orders:
            pts = order_pick_points(o, assignment, wh, geom)
            opt = optimal_route_length(pts, geom)
            for policy in HEURISTICS:
                assert route_length(policy, pts, geom) >= opt - 1e-9


def test_every_heuristic_route_visits_every_pick():
    """Validity: a route is only meaningful if it physically passes each pick face."""
    cartons, wh, orders = _fixture()
    geom = RoutingGeometry.from_warehouse(wh)
    for slotting in ("legacy", "optimized"):
        assignment = _assignment_for(slotting, cartons, wh)
        for o in orders:
            pts = order_pick_points(o, assignment, wh, geom)
            for policy in HEURISTICS:
                wps = heuristic_route(policy, pts, geom)
                assert route_visits_all(wps, pts, geom)


# --- the exact solver, cross-checked against an independent algorithm ----------------------------


def _held_karp(points, geom):
    """Independent exact TSP (Held-Karp DP) - a different algorithm from the module's brute force."""
    n = len(points)
    if n == 0:
        return 0.0
    d = geom.distance
    depot = geom.depot
    dp = {(1 << j, j): d(depot, points[j]) for j in range(n)}
    for mask in range(1 << n):
        for j in range(n):
            if not (mask >> j) & 1 or (mask, j) not in dp:
                continue
            base = dp[(mask, j)]
            for k in range(n):
                if (mask >> k) & 1:
                    continue
                nm = mask | (1 << k)
                nc = base + d(points[j], points[k])
                if nc < dp.get((nm, k), math.inf):
                    dp[(nm, k)] = nc
    full = (1 << n) - 1
    return min(dp[(full, j)] + d(points[j], depot) for j in range(n))


def test_optimal_matches_an_independent_held_karp_on_random_instances():
    geom = _geom()
    rng = random.Random(0)
    for _ in range(300):
        k = rng.randint(1, 6)
        pts = sorted(
            {(rng.randint(0, 5) * 5.0, (rng.randint(0, 3) + 0.5) * 1.2) for _ in range(k)}
        )
        assert optimal_route_length(pts, geom) == pytest.approx(_held_karp(pts, geom))


def test_optimal_matches_brute_force_permutation_sum_without_pruning():
    """A second, pruning-free reference so the early-exit in the solver cannot hide a bug."""
    geom = _geom()

    def naive(points):
        if not points:
            return 0.0
        best = math.inf
        for perm in permutations(points):
            t = geom.distance(geom.depot, perm[0])
            for i in range(len(perm) - 1):
                t += geom.distance(perm[i], perm[i + 1])
            t += geom.distance(perm[-1], geom.depot)
            best = min(best, t)
        return best

    rng = random.Random(7)
    for _ in range(100):
        pts = sorted({(rng.randint(0, 5) * 5.0, (rng.randint(0, 3) + 0.5) * 1.2) for _ in range(5)})
        assert optimal_route_length(pts, geom) == pytest.approx(naive(pts))


def test_optimal_rejects_orders_larger_than_the_exact_cap():
    geom = _geom()
    too_many = [(float(i), 0.6) for i in range(EXACT_MAX_PICKS + 1)]
    with pytest.raises(ValueError):
        optimal_route_length(too_many, geom)


# --- the sweep / report --------------------------------------------------------------------------


def test_sweep_is_deterministic():
    cartons, wh, orders = _fixture()
    geom = RoutingGeometry.from_warehouse(wh)
    a = sweep_slotting("optimized", cartons, wh, orders, geom)
    b = sweep_slotting("optimized", cartons, wh, orders, geom)
    assert a == b


def test_optimal_is_the_minimum_mean_in_each_group_and_recommended_is_a_heuristic():
    rep = routing_report()
    for slotting in ("legacy", "optimized"):
        rows = rep["by_slotting"][slotting]
        means = {r.policy: r.mean_route_m for r in rows}
        assert means["optimal"] == min(means.values())
        assert next(r for r in rows if r.policy == "optimal").pct_above_optimal == 0.0
    rec = rep["recommended"]
    assert rec.policy in HEURISTICS  # never recommends the (non-executable) optimum itself
    assert rec.slotting == "optimized"


def test_better_slotting_shortens_the_walk_under_every_fixed_policy():
    """Slotting and routing interact: for each policy, the velocity-optimized layout routes shorter
    than the legacy layout - the same lever the rest of the engine measures, now under real routing."""
    rep = routing_report()
    legacy = {r.policy: r.mean_route_m for r in rep["by_slotting"]["legacy"]}
    optimized = {r.policy: r.mean_route_m for r in rep["by_slotting"]["optimized"]}
    for policy in POLICIES:
        assert optimized[policy] < legacy[policy]


def test_routing_report_pins_the_seeded_numbers():
    """Exact figures the README/docs quote - synthetic, seeded; the canonical low-density result."""
    rep = routing_report()
    assert rep["n_orders"] == 101
    assert rep["mean_pick_faces"] == 2.86
    opt = {r.policy: r for r in rep["by_slotting"]["optimized"]}
    leg = {r.policy: r for r in rep["by_slotting"]["legacy"]}
    # On the optimized layout, return is closest to optimum and traversal (s-shape) is wasteful.
    assert opt["return"].mean_route_m == 27.46
    assert opt["return"].pct_above_optimal == 3.03
    assert opt["s-shape"].mean_route_m == 28.55
    assert opt["s-shape"].pct_above_optimal == 7.13
    assert opt["largest-gap"].mean_route_m == 28.13
    assert opt["midpoint"].mean_route_m == 29.59
    assert opt["optimal"].mean_route_m == 26.65
    assert leg["return"].mean_route_m == 51.39
    assert leg["optimal"].mean_route_m == 49.83
    assert rep["recommended"].policy == "return"


def test_recommended_policy_selects_lowest_mean_heuristic():
    cartons, wh, orders = _fixture()
    geom = RoutingGeometry.from_warehouse(wh)
    rows = sweep_slotting("optimized", cartons, wh, orders, geom)
    rec = recommended_policy(rows, "optimized")
    heuristic_means = [r.mean_route_m for r in rows if r.policy in HEURISTICS]
    assert rec.mean_route_m == min(heuristic_means)


# --- serialisation -------------------------------------------------------------------------------


def test_csv_and_svg_are_deterministic_and_well_formed():
    rep = routing_report()
    csv1, csv2 = to_csv(rep), to_csv(rep)
    assert csv1 == csv2
    assert csv1.startswith("slotting,policy,")
    # header + one row per (slotting, policy)
    assert csv1.rstrip().count("\n") == 2 * len(POLICIES)

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
    rep = routing_report()
    assert (docs / "routing_comparison.csv").read_text(encoding="utf-8") == to_csv(rep)
    assert (docs / "routing_comparison.svg").read_text(encoding="utf-8") == to_svg(rep)
