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


@dataclass
class Step:
    """One physical action in the executable re-shuffle sequence.

    ``from_slot``/``to_slot`` of ``None`` mean the off-rack staging position: with every rack slot
    occupied there is no empty slot to move into, so each cycle of moves parks its first carton
    off-rack once. ``saving`` is the daily demand-weighted travel (unit-m/day) banked when this
    step lands its SKU in its final slot (0.0 for the park-to-staging step; the saving is credited
    when the staged carton lands).
    """

    seq: int
    sku: str
    from_slot: int | None
    to_slot: int | None
    cycle: int
    saving: float


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


# Tie-break bias for :func:`optimize_slotting`. Distances are rounded to 0.01 m and demands are
# whole picks/day, so any two genuinely different layouts differ by at least ~0.01 unit-m/day;
# n * _PREFER_EPS stays orders of magnitude below that, so the bias can only break exact ties.
_PREFER_EPS = 1e-6


def optimize_slotting(
    cartons: list[Carton],
    warehouse: Warehouse,
    prefer: dict[str, int] | None = None,
) -> SlottingResult:
    """Optimal one-SKU-per-slot assignment minimising demand-weighted travel (Hungarian).

    ``prefer`` (usually the current layout) breaks ties among equally good layouts toward keeping
    each SKU where it already is: every off-preference cell gets a tiny epsilon surcharge, so the
    solver only relocates a SKU when the move buys real travel. The biased solve is verified to
    reach the exact unbiased optimum and is discarded if it ever did not - travel is never traded
    for fewer moves.
    """
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
    best_total = float(cost[rows, cols].sum())

    if prefer is not None:
        slot_index = {sl.id: j for j, sl in enumerate(slots)}
        bias = np.full_like(cost, _PREFER_EPS)
        for i, sku in enumerate(skus):
            j = slot_index.get(prefer.get(sku))
            if j is not None:
                bias[i, j] = 0.0
        p_rows, p_cols = linear_sum_assignment(cost + bias)
        if abs(float(cost[p_rows, p_cols].sum()) - best_total) <= 1e-6 * max(1.0, best_total):
            rows, cols = p_rows, p_cols

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


def execution_sequence(
    current: dict[str, int],
    target: dict[str, int],
    cartons: list[Carton],
    warehouse: Warehouse,
) -> list[Step]:
    """Order the re-shuffle into physically executable steps: every step lands in an empty spot.

    The raw move list is a simultaneous swap set - in a full warehouse it decomposes into cycles
    of occupied slots, so *no* ordering of the raw moves alone is executable. Each cycle is run by
    parking its first carton in an off-rack staging position, shifting the remaining cartons along
    the freed chain in reverse, and finally landing the staged carton: k moves become k + 1 steps
    and the staging position never holds more than one carton. Open chains (possible when some
    slots are empty) need no staging and run purely in reverse.

    Chains and cycles are independent of each other, so they are ordered by bang-for-buck (net
    daily saving per physical step, descending): stopping after any completed cycle leaves a
    consistent warehouse and banks the largest savings first.
    """
    moves = reshuffle_plan(current, target)
    demand = _demand_vector(cartons)
    dist = {sl.id: sl.distance for sl in warehouse.slots}
    move_by_from = {m.from_slot: m for m in moves}
    into = {m.to_slot for m in moves}

    seen: set[str] = set()

    def walk(start: Move) -> list[Move]:
        chain = [start]
        seen.add(start.sku)
        nxt = move_by_from.get(start.to_slot)
        while nxt is not None and nxt.sku not in seen:
            chain.append(nxt)
            seen.add(nxt.sku)
            nxt = move_by_from.get(nxt.to_slot)
        return chain

    # Open chains first (their head slot receives nobody), then whatever remains forms cycles.
    # Both scans run in SKU order, so discovery is deterministic.
    chains: list[tuple[list[Move], bool]] = []
    for m in moves:
        if m.sku not in seen and m.from_slot not in into:
            chains.append((walk(m), False))
    for m in moves:
        if m.sku not in seen:
            chains.append((walk(m), True))

    def sku_saving(m: Move) -> float:
        return demand[m.sku] * (dist[m.from_slot] - dist[m.to_slot])

    def bang_for_buck(item: tuple[list[Move], bool]) -> tuple[float, str]:
        chain, is_cycle = item
        n_steps = len(chain) + (1 if is_cycle else 0)
        return (-sum(sku_saving(m) for m in chain) / n_steps, chain[0].sku)

    chains.sort(key=bang_for_buck)

    steps: list[Step] = []
    for c_id, (chain, is_cycle) in enumerate(chains, start=1):
        if is_cycle:
            first = chain[0]
            steps.append(
                Step(seq=len(steps) + 1, sku=first.sku, from_slot=first.from_slot, to_slot=None, cycle=c_id, saving=0.0)
            )
            rest = chain[1:]
        else:
            first = None
            rest = chain
        # Reverse order: each move's target slot was just vacated by the move executed before it.
        for m in reversed(rest):
            steps.append(
                Step(seq=len(steps) + 1, sku=m.sku, from_slot=m.from_slot, to_slot=m.to_slot, cycle=c_id, saving=sku_saving(m))
            )
        if first is not None:
            steps.append(
                Step(seq=len(steps) + 1, sku=first.sku, from_slot=None, to_slot=first.to_slot, cycle=c_id, saving=sku_saving(first))
            )
    return steps


def slotting_report(
    cartons: list[Carton],
    warehouse: Warehouse,
    move_cost_seconds: float = 120.0,
    picker_speed_mps: float = 1.2,
) -> dict:
    """Full slotting comparison plus a re-shuffle break-even analysis.

    ``move_cost_seconds`` is the one-off labour cost of physically handling one carton once (touch,
    walk it over, put it away); it prices every step of the executable sequence, including each
    cycle's park-to-staging step, not just the raw move count. Distances are a round-trip metre
    proxy, converted to seconds via ``picker_speed_mps`` to price the daily saving in the same unit
    as the move cost. The optimizer ties are broken toward keeping SKUs in place (see
    :func:`optimize_slotting`), so only moves that buy real travel appear in the plan.
    """
    legacy = legacy_slotting(cartons, warehouse)
    optimized = optimize_slotting(cartons, warehouse, prefer=legacy.assignment)

    travel_saved = legacy.total_travel - optimized.total_travel
    reduction_pct = 100.0 * travel_saved / legacy.total_travel if legacy.total_travel > 0 else 0.0

    moves = reshuffle_plan(legacy.assignment, optimized.assignment)
    sequence = execution_sequence(legacy.assignment, optimized.assignment, cartons, warehouse)
    n_cycles = max((st.cycle for st in sequence), default=0)
    # Daily saving in seconds of picker time; one-off cost prices every physical step (incl. the
    # staging park-and-retrieve of each cycle) at move_cost_seconds.
    daily_saving_seconds = travel_saved / picker_speed_mps
    one_off_cost_seconds = len(sequence) * move_cost_seconds
    break_even_days = (
        one_off_cost_seconds / daily_saving_seconds if daily_saving_seconds > 0 else float("inf")
    )

    # Verify the plan actually reaches the target layout.
    reached = apply_moves(legacy.assignment, moves)
    plan_valid = reached == optimized.assignment

    # Verify the sequence is physically executable: every step lands in an empty spot, the staging
    # position never holds more than one carton, and the final state is the optimized layout.
    occupant = {slot: sku for sku, slot in legacy.assignment.items()}
    staged: str | None = None
    sequence_valid = True
    for st in sequence:
        if st.from_slot is None:
            sequence_valid = sequence_valid and staged == st.sku
            staged = None
        else:
            sequence_valid = sequence_valid and occupant.pop(st.from_slot, None) == st.sku
        if st.to_slot is None:
            sequence_valid = sequence_valid and staged is None
            staged = st.sku
        else:
            sequence_valid = sequence_valid and st.to_slot not in occupant
            occupant[st.to_slot] = st.sku
    end_state = {sku: slot for slot, sku in occupant.items()}
    sequence_valid = sequence_valid and staged is None and end_state == optimized.assignment

    return {
        "legacy_travel": legacy.total_travel,
        "optimized_travel": optimized.total_travel,
        "travel_saved": travel_saved,
        "reduction_pct": reduction_pct,
        "n_moves": len(moves),
        "n_steps": len(sequence),
        "n_cycles": n_cycles,
        "daily_saving_seconds": daily_saving_seconds,
        "one_off_cost_seconds": one_off_cost_seconds,
        "break_even_days": break_even_days,
        "plan_valid": plan_valid,
        "sequence_valid": sequence_valid,
        "legacy_result": legacy,
        "optimized_result": optimized,
        "moves": moves,
        "sequence": sequence,
    }
