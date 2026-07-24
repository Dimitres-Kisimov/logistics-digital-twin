"""Slotting optimization.

Assign SKUs to storage slots so that total daily pick travel is minimised:

    total_travel = sum over SKUs of (daily_pick_demand[sku] * distance_to_its_slot)

With one SKU per slot this is a balanced linear assignment problem, solved exactly by the
Hungarian algorithm (:func:`scipy.optimize.linear_sum_assignment`). We compare a legacy
alphabetical/arbitrary layout against the optimized layout, and emit a re-shuffle plan (the moves
needed to get from the current layout to the optimized one) with a break-even analysis.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import linear_sum_assignment

from .data import VELOCITY_DEMAND, Carton, Warehouse


@dataclass
class SlottingResult:
    assignment: dict[str, int]  # sku -> slot id
    total_travel: float


@dataclass
class Move:
    sku: str
    from_slot: int
    to_slot: int


def _demand_vector(cartons: list[Carton]) -> dict[str, float]:
    """Daily pick demand per SKU, driven by its ABC velocity class."""
    return {c.sku: VELOCITY_DEMAND[c.velocity] for c in cartons}


def legacy_slotting(cartons: list[Carton], warehouse: Warehouse) -> SlottingResult:
    """Arbitrary/alphabetical slotting: SKUs placed in slot order as they arrive (no optimization)."""
    demand = _demand_vector(cartons)
    skus = sorted(c.sku for c in cartons)
    slots = warehouse.slots
    n = min(len(skus), len(slots))
    assignment: dict[str, int] = {}
    total = 0.0
    for i in range(n):
        sku = skus[i]
        slot = slots[i]
        assignment[sku] = slot.id
        total += demand[sku] * slot.distance
    return SlottingResult(assignment=assignment, total_travel=total)


def optimize_slotting(cartons: list[Carton], warehouse: Warehouse) -> SlottingResult:
    """Optimal one-SKU-per-slot assignment minimising demand-weighted travel (Hungarian)."""
    demand = _demand_vector(cartons)
    skus = [c.sku for c in cartons]
    slots = warehouse.slots
    n = min(len(skus), len(slots))
    skus = skus[:n]
    slots = slots[:n]

    # Cost matrix: cost[i, j] = demand[sku_i] * distance[slot_j]. Minimising the assignment puts
    # high-demand SKUs into low-distance slots (that is exactly slotting by velocity).
    d = np.array([demand[s] for s in skus], dtype=float)
    dist = np.array([sl.distance for sl in slots], dtype=float)
    cost = np.outer(d, dist)

    rows, cols = linear_sum_assignment(cost)
    assignment: dict[str, int] = {}
    total = 0.0
    for i, j in zip(rows, cols, strict=True):
        assignment[skus[i]] = slots[j].id
        total += cost[i, j]
    return SlottingResult(assignment=assignment, total_travel=total)


def reshuffle_plan(
    current: dict[str, int],
    target: dict[str, int],
) -> list[Move]:
    """Minimal set of moves to transform ``current`` slotting into ``target``.

    Only SKUs whose slot changes are moved; SKUs already in the right slot stay put. This is the
    minimal move count for a re-slotting (each mis-placed SKU is moved exactly once to its target).
    """
    moves: list[Move] = []
    for sku, tgt_slot in target.items():
        cur_slot = current.get(sku)
        if cur_slot is not None and cur_slot != tgt_slot:
            moves.append(Move(sku=sku, from_slot=cur_slot, to_slot=tgt_slot))
    # Deterministic ordering.
    moves.sort(key=lambda m: (m.sku, m.from_slot, m.to_slot))
    return moves


def apply_moves(current: dict[str, int], moves: list[Move]) -> dict[str, int]:
    """Apply a list of moves to a slotting map, returning the resulting map."""
    result = dict(current)
    for m in moves:
        result[m.sku] = m.to_slot
    return result


def slotting_report(
    cartons: list[Carton],
    warehouse: Warehouse,
    move_cost_seconds: float = 120.0,
    picker_speed_mps: float = 1.2,
) -> dict:
    """Full slotting comparison plus a re-shuffle break-even analysis.

    ``move_cost_seconds`` is the one-off labour cost of physically relocating one SKU; distances are
    a round-trip metre proxy, converted to seconds via ``picker_speed_mps`` to price the daily
    saving in the same unit as the move cost.
    """
    legacy = legacy_slotting(cartons, warehouse)
    optimized = optimize_slotting(cartons, warehouse)

    travel_saved = legacy.total_travel - optimized.total_travel
    reduction_pct = 100.0 * travel_saved / legacy.total_travel if legacy.total_travel > 0 else 0.0

    moves = reshuffle_plan(legacy.assignment, optimized.assignment)
    # Daily saving in seconds of picker time; one-off cost is moves * move_cost_seconds.
    daily_saving_seconds = travel_saved / picker_speed_mps
    one_off_cost_seconds = len(moves) * move_cost_seconds
    break_even_days = (
        one_off_cost_seconds / daily_saving_seconds if daily_saving_seconds > 0 else float("inf")
    )

    # Verify the plan actually reaches the target layout.
    reached = apply_moves(legacy.assignment, moves)
    plan_valid = reached == optimized.assignment

    return {
        "legacy_travel": legacy.total_travel,
        "optimized_travel": optimized.total_travel,
        "travel_saved": travel_saved,
        "reduction_pct": reduction_pct,
        "n_moves": len(moves),
        "daily_saving_seconds": daily_saving_seconds,
        "one_off_cost_seconds": one_off_cost_seconds,
        "break_even_days": break_even_days,
        "plan_valid": plan_valid,
        "legacy_result": legacy,
        "optimized_result": optimized,
        "moves": moves,
    }
