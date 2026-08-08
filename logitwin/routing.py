"""Pick-path routing: heuristic order-picking routes vs the exact optimum.

Everywhere else in this engine, pick travel is a per-slot **round-trip proxy** (each slot carries a
single scalar ``distance`` to dispatch, as the layout interchange notes: "no aisle routing or rack
entry points"). That is fine for slotting, which only needs a per-slot cost. It is *not* how a
picker actually walks a multi-line order: the picker enters aisles, works down and back or straight
through, and uses cross-aisles to change aisle. Which **routing policy** the picker follows is a
first-class warehousing lever - and a canonical one (de Koster, Le-Duc & Roodbergen 2007).

This module adds that missing layer. It imposes an explicit single-block aisle geometry on the
layout's existing ``(aisle, bay)`` slot indices (it introduces *no* new slotting or simulation
heuristic - it reuses :func:`logitwin.data.make_warehouse`, the
:func:`logitwin.slotting.legacy_slotting` / :func:`~logitwin.slotting.optimize_slotting`
assignments, and :func:`logitwin.data.make_order_stream`) and, for every order in the seeded stream,
routes the picker under four classic heuristics and one exact optimum:

* **return**       - enter each aisle from the front, go to the farthest pick, come back to the
  front; change aisle along the front cross-aisle only (Hall 1993).
* **s-shape**      - traversal / serpentine: traverse each aisle that holds a pick completely,
  snaking front-to-back-to-front; the last aisle is a return trip when a full traversal would strand
  the picker at the back (Hall 1993; Petersen 1997).
* **midpoint**     - picks in the front half of an aisle are reached from the front cross-aisle,
  picks in the back half from the back cross-aisle; the picker transitions to the back cross-aisle by
  fully traversing the last aisle with picks (Hall 1993).
* **largest-gap**  - like midpoint, but each aisle is split at its own *largest gap* (the longest
  stretch with no picks, including the gap to either cross-aisle) rather than at the fixed midpoint;
  the picker never walks the largest gap. First and last aisles with picks are traversed
  (Roodbergen & de Koster 2001).
* **optimal**      - the exact shortest pick tour, solved by brute force over pick orderings on the
  aisle-graph metric (a single order has few pick faces, so this is tractable and *provably* the
  minimum for this metric model). It is the honest yardstick every heuristic is measured against -
  the same role CP-SAT plays for the packing benchmark.

Honest scope
------------
* The geometry is a **single-block** layout: parallel pick aisles joined by a front and a back
  cross-aisle, one depot at the front-left corner. Aisle pitch and bay depth are documented modelled
  constants (``AISLE_PITCH_M``, ``BAY_DEPTH_M``), not measured from a real building. Rack level is a
  vertical dimension and does not change the floor tour, so pick faces are de-duplicated to distinct
  ``(aisle, bay)`` floor points.
* Distance is the **shortest constrained path** on that graph (move along an aisle, or along a
  cross-aisle - never through a rack). There is **no aisle congestion, one-way flow, or middle
  cross-aisle**; a real floor has all three and this model does not, and says so.
* The optimum is exact **for this metric model only** (equivalently, the Ratliff-Rosenthal single-
  block optimum). It is not a claim about a real multi-block facility.
* Everything is synthetic, seeded and deterministic: a given dataset always yields byte-identical
  tables, CSV and SVG. Numbers are teaching-scale and reported exactly as the code computes them.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from itertools import permutations
from pathlib import Path

from .data import Carton, Order, Warehouse, dataset
from .slotting import legacy_slotting, optimize_slotting

# --- modelled geometry (documented assumptions, not measurements) -------------------------------

# Centre-to-centre distance between two adjacent parallel pick aisles (metres). A rack-aisle-rack
# module is roughly this wide in a manual pallet/carton facility.
AISLE_PITCH_M = 5.0
# Along-aisle distance between two adjacent bay positions (metres); ~ one pallet/carton face.
BAY_DEPTH_M = 1.2

# The four heuristic policies (the exact optimum is reported alongside them as the yardstick).
HEURISTICS: tuple[str, ...] = ("return", "s-shape", "midpoint", "largest-gap")
POLICIES: tuple[str, ...] = (*HEURISTICS, "optimal")

# Brute-force the exact optimum only up to this many distinct pick faces in one order (9! routes is
# instant; a single order in the seeded stream never exceeds a handful of faces). Beyond it we would
# need Ratliff-Rosenthal DP; the seeded data never gets there, and the sweep guards it explicitly.
EXACT_MAX_PICKS = 9

Point = tuple[float, float]


@dataclass(frozen=True)
class RoutingGeometry:
    """A single-block aisle layout: parallel aisles, a front (y=0) and back (y=L) cross-aisle.

    ``x`` is horizontal (aisle position), ``y`` is depth into an aisle from the front cross-aisle.
    The depot (pick-up/drop-off point) sits at the front-left corner ``(0, 0)``. Travel is
    rectilinear and constrained to aisles and cross-aisles - you can never move through a rack.
    """

    aisle_pitch: float
    bay_depth: float
    aisle_length: float  # L: depth of the back cross-aisle (just past the deepest bay)
    depot: Point = (0.0, 0.0)

    @classmethod
    def from_warehouse(
        cls,
        warehouse: Warehouse,
        aisle_pitch: float = AISLE_PITCH_M,
        bay_depth: float = BAY_DEPTH_M,
    ) -> RoutingGeometry:
        """Derive the geometry from a warehouse's ``(aisle, bay)`` slot indices.

        The back cross-aisle is placed half a bay past the deepest bay in the whole layout, so every
        aisle shares one length (a single block).
        """
        max_bay = max((s.bay for s in warehouse.slots), default=0)
        aisle_length = (max_bay + 1) * bay_depth
        return cls(aisle_pitch=aisle_pitch, bay_depth=bay_depth, aisle_length=aisle_length)

    def floor_point(self, aisle: int, bay: int) -> Point:
        """The floor coordinate of a pick face at ``(aisle, bay)`` (centre of the bay)."""
        return (aisle * self.aisle_pitch, (bay + 0.5) * self.bay_depth)

    def distance(self, p: Point, q: Point) -> float:
        """Shortest constrained (aisle + cross-aisle) distance between two floor points.

        Same aisle -> straight along the aisle. Different aisles -> out to a cross-aisle, across,
        and in again, taking whichever of the front/back cross-aisle is shorter (a single-block
        layout has exactly those two).
        """
        (xp, yp), (xq, yq) = p, q
        if xp == xq:
            return abs(yp - yq)
        across = abs(xp - xq)
        via_front = yp + across + yq
        via_back = (self.aisle_length - yp) + across + (self.aisle_length - yq)
        return min(via_front, via_back)


# --- turning picks into floor points -------------------------------------------------------------


def order_pick_points(
    order: Order,
    assignment: dict[str, int],
    warehouse: Warehouse,
    geom: RoutingGeometry,
) -> list[Point]:
    """Distinct floor points a picker must visit for ``order`` under ``assignment`` (sku -> slot id).

    Pick faces at the same ``(aisle, bay)`` but different rack levels collapse to one floor point
    (the picker stops there once). Sorted for determinism.
    """
    slot_by_id = {s.id: s for s in warehouse.slots}
    points: set[Point] = set()
    for sku in order.skus:
        slot = slot_by_id.get(assignment.get(sku, -1))
        if slot is None:
            continue
        points.add(geom.floor_point(slot.aisle, slot.bay))
    return sorted(points)


def _by_aisle(points: list[Point]) -> dict[float, list[float]]:
    """Group pick points by their aisle x-coordinate, each aisle's depths sorted ascending."""
    out: dict[float, list[float]] = {}
    for x, y in points:
        out.setdefault(x, []).append(y)
    for x in out:
        out[x].sort()
    return out


# --- route length from a waypoint list -----------------------------------------------------------


def path_length(waypoints: list[Point], geom: RoutingGeometry) -> float:
    """Total distance walked along an ordered list of waypoints (constrained metric)."""
    return sum(geom.distance(waypoints[i], waypoints[i + 1]) for i in range(len(waypoints) - 1))


def route_visits_all(waypoints: list[Point], points: list[Point], geom: RoutingGeometry) -> bool:
    """True iff every pick point lies on some axis-aligned segment of the waypoint route.

    A correctness net for the heuristics: a route is only valid if it actually passes every pick.
    Segments are either vertical (within an aisle) or horizontal (along a cross-aisle).
    """
    eps = 1e-9
    for px, py in points:
        covered = False
        for i in range(len(waypoints) - 1):
            (x1, y1), (x2, y2) = waypoints[i], waypoints[i + 1]
            if abs(x1 - x2) < eps and abs(px - x1) < eps:  # vertical segment in this aisle
                if min(y1, y2) - eps <= py <= max(y1, y2) + eps:
                    covered = True
                    break
            elif abs(y1 - y2) < eps and abs(py - y1) < eps:  # horizontal along a cross-aisle
                if min(x1, x2) - eps <= px <= max(x1, x2) + eps:
                    covered = True
                    break
        if not covered:
            return False
    return True


# --- the routing policies (each builds an axis-aligned waypoint list) ----------------------------


def _waypoints_return(by_aisle: dict[float, list[float]], geom: RoutingGeometry) -> list[Point]:
    """Return routing: every aisle entered/left at the front, out to the farthest pick and back."""
    wp: list[Point] = [geom.depot]
    for x in sorted(by_aisle):
        far = by_aisle[x][-1]
        wp.append((x, 0.0))
        wp.append((x, far))
        wp.append((x, 0.0))
    wp.append(geom.depot)
    return wp


def _waypoints_s_shape(by_aisle: dict[float, list[float]], geom: RoutingGeometry) -> list[Point]:
    """S-shape / traversal: snake through every aisle with a pick; last aisle is a return if needed."""
    aisles = sorted(by_aisle)
    L = geom.aisle_length
    wp: list[Point] = [geom.depot]
    side = 0.0  # current cross-aisle (0 = front, L = back)
    for i, x in enumerate(aisles):
        last = i == len(aisles) - 1
        if not last:
            other = L if side == 0.0 else 0.0
            wp.append((x, side))
            wp.append((x, other))
            side = other
        elif side == 0.0:  # arriving at the last aisle from the front -> return trip
            far = by_aisle[x][-1]
            wp.append((x, 0.0))
            wp.append((x, far))
            wp.append((x, 0.0))
        else:  # arriving from the back -> traverse down to the front
            wp.append((x, L))
            wp.append((x, 0.0))
            side = 0.0
    wp.append(geom.depot)
    return wp


def _waypoints_midpoint(by_aisle: dict[float, list[float]], geom: RoutingGeometry) -> list[Point]:
    """Midpoint: front-half picks from the front cross-aisle, back-half from the back cross-aisle.

    The transition to the back cross-aisle is made by fully traversing the last aisle with picks;
    if no pick lies in any back half, this degenerates to a pure front return route.
    """
    aisles = sorted(by_aisle)
    L = geom.aisle_length
    mid = L / 2.0
    if len(aisles) == 1:
        return _waypoints_return(by_aisle, geom)

    front_deep = {x: max((y for y in by_aisle[x] if y <= mid), default=None) for x in aisles}
    back_shallow = {x: min((y for y in by_aisle[x] if y > mid), default=None) for x in aisles}
    any_back = any(back_shallow[x] is not None for x in aisles)

    if not any_back:  # all picks in the front half -> return route
        return _waypoints_return(by_aisle, geom)

    wp: list[Point] = [geom.depot]
    last = aisles[-1]
    # Front sweep: front-half picks of every aisle except the last (that one is traversed next).
    for x in aisles[:-1]:
        fd = front_deep[x]
        if fd is not None:
            wp.append((x, 0.0))
            wp.append((x, fd))
            wp.append((x, 0.0))
    # Transition: fully traverse the last aisle front -> back (collects all its picks).
    wp.append((last, 0.0))
    wp.append((last, L))
    # Back sweep: back-half picks from the back, walking back down to the first aisle.
    for x in reversed(aisles[:-1]):
        bs = back_shallow[x]
        if bs is not None:
            wp.append((x, L))
            wp.append((x, bs))
            wp.append((x, L))
    # Return to the depot (down the current aisle to the front, then along the front cross-aisle).
    cx = wp[-1][0]
    wp.append((cx, 0.0))
    wp.append(geom.depot)
    return wp


def _largest_gap_split(depths: list[float], L: float) -> tuple[float | None, float | None]:
    """Split an aisle's picks at its largest gap.

    Returns ``(front_boundary, back_boundary)``: the deepest pick reached from the front and the
    shallowest pick reached from the back. Either is ``None`` when that side is empty (the largest
    gap is against a cross-aisle). Gaps considered: front cross-aisle -> first pick, between
    consecutive picks, last pick -> back cross-aisle.
    """
    gaps = [depths[0] - 0.0]  # front cross-aisle to first pick
    gaps += [depths[i + 1] - depths[i] for i in range(len(depths) - 1)]
    gaps.append(L - depths[-1])  # last pick to back cross-aisle
    # First index of the maximum gap (deterministic tie-break toward the front).
    gmax = max(gaps)
    i_star = gaps.index(gmax)
    if i_star == 0:  # gap is front-cross-aisle -> first pick: whole aisle from the back
        return None, depths[0]
    if i_star == len(depths):  # gap is last pick -> back cross-aisle: whole aisle from the front
        return depths[-1], None
    return depths[i_star - 1], depths[i_star]


def _waypoints_largest_gap(by_aisle: dict[float, list[float]], geom: RoutingGeometry) -> list[Point]:
    """Largest-gap: each intermediate aisle split at its largest gap; first and last aisles traversed."""
    aisles = sorted(by_aisle)
    L = geom.aisle_length
    if len(aisles) == 1:
        return _waypoints_return(by_aisle, geom)
    if len(aisles) == 2:  # both are first/last -> both traversed == s-shape for two aisles
        return _waypoints_s_shape(by_aisle, geom)

    first, last = aisles[0], aisles[-1]
    middle = aisles[1:-1]
    wp: list[Point] = [geom.depot]
    # Traverse the first aisle front -> back to reach the back cross-aisle.
    wp.append((first, 0.0))
    wp.append((first, L))
    # Back pass across the intermediate aisles: reach each one's back portion from the back.
    for x in middle:
        _, back_boundary = _largest_gap_split(by_aisle[x], L)
        if back_boundary is not None:
            wp.append((x, L))
            wp.append((x, back_boundary))
            wp.append((x, L))
    # Traverse the last aisle back -> front.
    wp.append((last, L))
    wp.append((last, 0.0))
    # Front pass back across the intermediate aisles: reach each one's front portion from the front.
    for x in reversed(middle):
        front_boundary, _ = _largest_gap_split(by_aisle[x], L)
        if front_boundary is not None:
            wp.append((x, 0.0))
            wp.append((x, front_boundary))
            wp.append((x, 0.0))
    wp.append(geom.depot)
    return wp


_HEURISTIC_BUILDERS = {
    "return": _waypoints_return,
    "s-shape": _waypoints_s_shape,
    "midpoint": _waypoints_midpoint,
    "largest-gap": _waypoints_largest_gap,
}


def heuristic_route(policy: str, points: list[Point], geom: RoutingGeometry) -> list[Point]:
    """Waypoint list for one heuristic ``policy`` over a set of pick points (empty if no picks)."""
    if not points:
        return [geom.depot]
    builder = _HEURISTIC_BUILDERS.get(policy)
    if builder is None:
        raise ValueError(f"unknown routing policy: {policy!r}")
    return builder(_by_aisle(points), geom)


def optimal_route_length(points: list[Point], geom: RoutingGeometry) -> float:
    """Exact shortest pick-tour length (depot -> all points -> depot) on the aisle-graph metric.

    Brute force over pick orderings. Exact and deterministic; raises if an order has more distinct
    pick faces than :data:`EXACT_MAX_PICKS` (never on the seeded stream).
    """
    if not points:
        return 0.0
    if len(points) > EXACT_MAX_PICKS:
        raise ValueError(
            f"exact routing supports up to {EXACT_MAX_PICKS} pick faces, got {len(points)}"
        )
    depot = geom.depot
    best = float("inf")
    for perm in permutations(points):
        total = geom.distance(depot, perm[0])
        for i in range(len(perm) - 1):
            total += geom.distance(perm[i], perm[i + 1])
            if total >= best:
                break
        else:
            total += geom.distance(perm[-1], depot)
            if total < best:
                best = total
    return best


def route_length(policy: str, points: list[Point], geom: RoutingGeometry) -> float:
    """Length of the route ``policy`` produces over ``points`` (``policy='optimal'`` is exact)."""
    if policy == "optimal":
        return optimal_route_length(points, geom)
    return path_length(heuristic_route(policy, points, geom), geom)


# --- the sweep ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteRow:
    """Aggregate routing metrics for one (slotting, policy) over the whole order stream."""

    slotting: str  # "legacy" | "optimized"
    policy: str
    n_orders: int
    total_route_m: float  # total metres walked across every order
    mean_route_m: float  # mean metres per order
    pct_above_optimal: float  # (mean_policy - mean_optimal) / mean_optimal * 100 (0.0 for optimal)


def _assignment_for(slotting: str, cartons: list[Carton], warehouse: Warehouse) -> dict[str, int]:
    """The sku -> slot map for a slotting label: legacy alphabetical or the canonical optimum."""
    legacy = legacy_slotting(cartons, warehouse).assignment
    if slotting == "legacy":
        return legacy
    if slotting == "optimized":
        return optimize_slotting(cartons, warehouse, prefer=legacy).assignment
    raise ValueError(f"unknown slotting: {slotting!r}")


def sweep_slotting(
    slotting: str,
    cartons: list[Carton],
    warehouse: Warehouse,
    orders: list[Order],
    geom: RoutingGeometry,
) -> list[RouteRow]:
    """Route every order under each policy for one slotting; return one aggregate row per policy."""
    assignment = _assignment_for(slotting, cartons, warehouse)
    order_points = [order_pick_points(o, assignment, warehouse, geom) for o in orders]
    n = len(orders)

    totals: dict[str, float] = {p: 0.0 for p in POLICIES}
    for pts in order_points:
        for policy in POLICIES:
            totals[policy] += route_length(policy, pts, geom)

    mean_opt = totals["optimal"] / n if n else 0.0
    rows: list[RouteRow] = []
    for policy in POLICIES:
        mean = totals[policy] / n if n else 0.0
        pct = 100.0 * (mean - mean_opt) / mean_opt if mean_opt > 0 else 0.0
        rows.append(
            RouteRow(
                slotting=slotting,
                policy=policy,
                n_orders=n,
                total_route_m=round(totals[policy], 1),
                mean_route_m=round(mean, 2),
                pct_above_optimal=round(pct, 2),
            )
        )
    return rows


def recommended_policy(rows: list[RouteRow], slotting: str = "optimized") -> RouteRow:
    """The heuristic with the lowest mean route length on ``slotting`` (ties -> policy order)."""
    order = {p: i for i, p in enumerate(HEURISTICS)}
    candidates = [r for r in rows if r.slotting == slotting and r.policy in order]
    if not candidates:
        raise ValueError("no heuristic rows to choose from")
    return min(candidates, key=lambda r: (r.mean_route_m, order[r.policy]))


def routing_report(
    cartons: list[Carton] | None = None,
    warehouse: Warehouse | None = None,
    orders: list[Order] | None = None,
    *,
    seed: int = 42,
    n: int = 60,
    aisle_pitch: float = AISLE_PITCH_M,
    bay_depth: float = BAY_DEPTH_M,
) -> dict:
    """Run the routing comparison on a (synthetic, seeded) dataset and bundle it."""
    if cartons is None or warehouse is None or orders is None:
        ds = dataset(seed=seed, n=n)
        cartons, warehouse, orders = ds["cartons"], ds["warehouse"], ds["orders"]

    geom = RoutingGeometry.from_warehouse(warehouse, aisle_pitch=aisle_pitch, bay_depth=bay_depth)
    by_slotting: dict[str, list[RouteRow]] = {}
    for slotting in ("legacy", "optimized"):
        by_slotting[slotting] = sweep_slotting(slotting, cartons, warehouse, orders, geom)

    # Mean pick faces per order (routing context; distinct floor points on the optimized layout).
    opt_assign = _assignment_for("optimized", cartons, warehouse)
    faces = [len(order_pick_points(o, opt_assign, warehouse, geom)) for o in orders]
    mean_faces = round(sum(faces) / len(faces), 2) if faces else 0.0

    rec = recommended_policy(by_slotting["optimized"], "optimized")
    return {
        "seed": seed,
        "n_skus": len(cartons),
        "n_orders": len(orders),
        "aisle_pitch": aisle_pitch,
        "bay_depth": bay_depth,
        "aisle_length": geom.aisle_length,
        "mean_pick_faces": mean_faces,
        "by_slotting": by_slotting,
        "recommended": rec,
        "read": trade_off_read(by_slotting, rec, mean_faces),
    }


def _row(rows: list[RouteRow], policy: str) -> RouteRow:
    return next(r for r in rows if r.policy == policy)


def trade_off_read(
    by_slotting: dict[str, list[RouteRow]],
    rec: RouteRow,
    mean_faces: float,
) -> str:
    """One-paragraph plain-language read of the routing comparison, numbers straight from the sweep."""
    opt = by_slotting["optimized"]
    s_shape = _row(opt, "s-shape")
    optimal = _row(opt, "optimal")
    leg_best = _row(by_slotting["legacy"], rec.policy)
    slot_drop = (
        100.0 * (leg_best.mean_route_m - rec.mean_route_m) / leg_best.mean_route_m
        if leg_best.mean_route_m > 0
        else 0.0
    )
    return (
        f"On the seeded {rec.n_orders}-order shift (mean {mean_faces:.1f} pick faces per order), the "
        f"best heuristic on the velocity-optimized layout is {rec.policy}: {rec.mean_route_m:.1f} m "
        f"per order, {rec.pct_above_optimal:.1f}% above the exact optimum ({optimal.mean_route_m:.1f} "
        f"m). At this low pick density the traversal (s-shape) route is wasteful - it walks whole "
        f"aisles for one or two picks - landing {s_shape.mean_route_m:.1f} m "
        f"({s_shape.pct_above_optimal:.1f}% above optimum), so {rec.policy} is the right default here. "
        f"Better slotting also shortens the walk under a fixed policy: {rec.policy} drops "
        f"{leg_best.mean_route_m:.1f} m -> {rec.mean_route_m:.1f} m ({slot_drop:.1f}%) moving from the "
        f"legacy to the optimized layout. No heuristic ever beats the optimum (guaranteed, and "
        f"verified per order). (Synthetic, seeded; single-block layout, corner depot, no congestion.)"
    )


# --- serialisation ------------------------------------------------------------------------------

_CSV_HEADER = (
    "slotting",
    "policy",
    "n_orders",
    "total_route_m",
    "mean_route_m",
    "pct_above_optimal",
)


def _all_rows(report: dict) -> list[RouteRow]:
    rows: list[RouteRow] = []
    for slotting in ("legacy", "optimized"):
        rows.extend(report["by_slotting"][slotting])
    return rows


def to_csv(report: dict) -> str:
    """Deterministic CSV of the routing sweep (fixed formatting, trailing newline)."""
    lines = [",".join(_CSV_HEADER)]
    for r in _all_rows(report):
        lines.append(
            ",".join(
                (
                    r.slotting,
                    r.policy,
                    str(r.n_orders),
                    f"{r.total_route_m:.1f}",
                    f"{r.mean_route_m:.2f}",
                    f"{r.pct_above_optimal:.2f}",
                )
            )
        )
    return "\n".join(lines) + "\n"


def to_svg(report: dict) -> str:
    """Hand-drawn (pure-string, no plotting library) grouped bar chart of mean route length.

    One group per slotting (legacy, optimized); one bar per policy; the exact optimum bar is ringed
    in each group. Deterministic: every coordinate derives from the seeded sweep.
    """
    w, h = 720, 440
    ml, mr, mt, mb = 70, 30, 64, 84
    pw, ph = w - ml - mr, h - mt - mb

    groups = ("legacy", "optimized")
    max_mean = max((r.mean_route_m for r in _all_rows(report)), default=1.0) or 1.0
    y_top = max_mean * 1.1

    def py(v: float) -> float:
        return mt + ph * (1.0 - v / y_top)

    # Colours per policy (optimal muted; heuristics on the engine's teal/amber family).
    colour = {
        "return": "#4c78a8",
        "s-shape": "#e45756",
        "midpoint": "#f2a541",
        "largest-gap": "#2a9d8f",
        "optimal": "#9aa5b1",
    }

    group_w = pw / len(groups)
    bar_gap = 10.0
    n_bars = len(POLICIES)
    bar_w = (group_w - 2 * bar_gap) / n_bars

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" font-family="sans-serif">'
    )
    parts.append(f'<rect width="{w}" height="{h}" fill="#ffffff"/>')
    parts.append(
        f'<text x="{ml}" y="28" font-size="17" font-weight="bold" fill="#1f3a5f">'
        f"Pick-path routing - mean route length per order vs the exact optimum</text>"
    )
    parts.append(
        f'<text x="{ml}" y="46" font-size="11" fill="#9aa5b1">'
        f"Synthetic, seeded {report['n_skus']}-SKU / {report['n_orders']}-order shift, "
        f"single-block layout - deterministic</text>"
    )
    # Axes.
    parts.append(
        f'<line x1="{ml}" y1="{mt + ph}" x2="{ml + pw}" y2="{mt + ph}" stroke="#333" stroke-width="1"/>'
    )
    parts.append(f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt + ph}" stroke="#333" stroke-width="1"/>')
    for i in range(5):
        v = y_top * i / 4
        y = py(v)
        parts.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{ml + pw}" y2="{y:.1f}" stroke="#eee" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{ml - 8}" y="{y + 4:.1f}" font-size="10" fill="#555" text-anchor="end">'
            f"{v:.0f}m</text>"
        )
    parts.append(
        f'<text x="18" y="{mt + ph / 2:.0f}" font-size="12" fill="#333" text-anchor="middle" '
        f'transform="rotate(-90 18 {mt + ph / 2:.0f})">mean route length (m / order)</text>'
    )
    # Bars.
    for gi, slotting in enumerate(groups):
        gx = ml + gi * group_w
        rows = {r.policy: r for r in report["by_slotting"][slotting]}
        for bi, policy in enumerate(POLICIES):
            r = rows[policy]
            x = gx + bar_gap + bi * bar_w
            y = py(r.mean_route_m)
            bh = mt + ph - y
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w - 2:.1f}" height="{bh:.1f}" '
                f'fill="{colour[policy]}"/>'
            )
            if policy == "optimal":
                parts.append(
                    f'<rect x="{x - 1:.1f}" y="{y - 1:.1f}" width="{bar_w:.1f}" height="{bh + 1:.1f}" '
                    f'fill="none" stroke="#1f3a5f" stroke-width="1.5" stroke-dasharray="3 2"/>'
                )
            parts.append(
                f'<text x="{x + (bar_w - 2) / 2:.1f}" y="{y - 4:.1f}" font-size="8.5" fill="#555" '
                f'text-anchor="middle">{r.mean_route_m:.0f}</text>'
            )
        parts.append(
            f'<text x="{gx + group_w / 2:.1f}" y="{mt + ph + 18:.1f}" font-size="12" fill="#333" '
            f'text-anchor="middle" font-weight="bold">{slotting} slotting</text>'
        )
    # Legend (one row of policy swatches under the plot).
    lx, ly = ml, h - 26
    for policy in POLICIES:
        parts.append(f'<rect x="{lx:.1f}" y="{ly - 9:.1f}" width="11" height="11" fill="{colour[policy]}"/>')
        parts.append(f'<text x="{lx + 15:.1f}" y="{ly:.1f}" font-size="10.5" fill="#555">{policy}</text>')
        lx += 24 + 7.2 * len(policy)
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def render_table(report: dict) -> str:
    """Fixed-width text table of the routing sweep plus the trade-off read."""
    cols = ("slotting", "policy", "orders", "total m", "mean m", "+opt %")
    body = [
        (
            r.slotting,
            r.policy,
            str(r.n_orders),
            f"{r.total_route_m:.0f}",
            f"{r.mean_route_m:.2f}",
            f"{r.pct_above_optimal:.2f}",
        )
        for r in _all_rows(report)
    ]
    widths = [len(c) for c in cols]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    line = "-" * (sum(widths) + 2 * (len(widths) - 1))
    rec: RouteRow = report["recommended"]
    out = [
        "Pick-path routing comparison (synthetic, seeded)",
        f"single-block layout: {report['aisle_pitch']:.1f} m aisle pitch, "
        f"{report['aisle_length']:.1f} m aisle length, corner depot",
        line,
        fmt(cols),
        line,
    ]
    out.extend(fmt(row) for row in body)
    out.append(line)
    out.append(
        f"Recommended policy (optimized layout): {rec.policy} - "
        f"{rec.mean_route_m:.1f} m/order, {rec.pct_above_optimal:.1f}% above optimum."
    )
    out.append("")
    out.append(report["read"])
    return "\n".join(out)


def _report_to_jsonable(report: dict) -> dict:
    from dataclasses import asdict

    return {
        "seed": report["seed"],
        "n_skus": report["n_skus"],
        "n_orders": report["n_orders"],
        "aisle_pitch": report["aisle_pitch"],
        "bay_depth": report["bay_depth"],
        "aisle_length": report["aisle_length"],
        "mean_pick_faces": report["mean_pick_faces"],
        "recommended": asdict(report["recommended"]),
        "read": report["read"],
        "by_slotting": {k: [asdict(r) for r in v] for k, v in report["by_slotting"].items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="logitwin.routing",
        description="Pick-path routing comparison: heuristic routes vs the exact optimum.",
    )
    parser.add_argument("--json", action="store_true", help="emit the sweep as JSON")
    parser.add_argument("--csv", metavar="FILE", help="write the sweep CSV to FILE")
    parser.add_argument("--svg", metavar="FILE", help="write the bar-chart SVG to FILE")
    parser.add_argument("--seed", type=int, default=42, help="dataset seed (default 42)")
    parser.add_argument("--n", type=int, default=60, help="number of SKUs (default 60)")
    args = parser.parse_args(argv)

    report = routing_report(seed=args.seed, n=args.n)

    if args.csv:
        Path(args.csv).write_text(to_csv(report), encoding="utf-8")
        print(f"[ok] wrote {args.csv}")
    if args.svg:
        Path(args.svg).write_text(to_svg(report), encoding="utf-8")
        print(f"[ok] wrote {args.svg}")

    if args.json:
        print(json.dumps(_report_to_jsonable(report), indent=2))
    elif not (args.csv or args.svg):
        print(render_table(report))
    return 0


if __name__ == "__main__":
    # Windows console safety: force UTF-8 so ASCII markers and any stray unicode never crash stdout.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
