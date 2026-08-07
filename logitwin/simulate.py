"""Hand-rolled discrete-event simulation of order picking through a shift.

No SimPy - the event queue is a plain ``heapq`` of ``(time, seq, kind, payload)`` tuples processed
in time order. We model two regimes:

* **legacy**  - arbitrary (alphabetical) slotting, one-carton-per-container packing, a single picker
  working orders strictly sequentially with a round trip to dispatch for every pick line.
* **modern**  - velocity-optimized slotting, FFD packing, and batched picks (one consolidated route
  per batch of orders).

The simulation is fully seeded/deterministic: the same inputs always produce identical KPIs.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass

from .data import Carton, Order, Warehouse
from .packing import ffd_pack, naive_pack
from .slotting import legacy_slotting, optimize_slotting

WALK_SPEED_MPS = 1.2  # picker walking speed
PICK_HANDLING_S = 18.0  # per-line handling time (grab, scan, place)
MODERN_BATCH_SIZE = 4


@dataclass
class SimResult:
    regime: str
    orders_completed: int
    mean_cycle_time_s: float
    total_travel_m: float
    container_utilization: float
    p95_cycle_time_s: float
    makespan_s: float
    # Crew size the run modelled and the total picker busy-time it accumulated. Both default so the
    # single-picker call sites and any code that reads SimResult by attribute stay unchanged; the
    # labour sweep reads them to compute per-picker utilization. ``picker_busy_s`` is the sum of
    # every service duration (one picker occupied for that long), so busy/(n_pickers*makespan) is
    # the mean utilization over the active horizon.
    n_pickers: int = 1
    picker_busy_s: float = 0.0


def _distance_map(assignment: dict[str, int], warehouse: Warehouse) -> dict[str, float]:
    slot_dist = {s.id: s.distance for s in warehouse.slots}
    return {sku: slot_dist[slot_id] for sku, slot_id in assignment.items()}


def _legacy_service(order: Order, dist: dict[str, float]) -> tuple[float, float]:
    """Return (service_time_s, travel_m) for one order under the legacy regime.

    Round trip to dispatch for every pick line (no route consolidation).
    """
    travel_m = sum(2.0 * dist[sku] for sku in order.skus)
    time_s = travel_m / WALK_SPEED_MPS + len(order.skus) * PICK_HANDLING_S
    return time_s, travel_m


def _modern_service(batch: list[Order], dist: dict[str, float]) -> tuple[float, float]:
    """Return (service_time_s, travel_m) for a batch under the modern regime.

    One consolidated route: walk out to the farthest slot and back once, plus a reduced lateral
    penalty for the remaining lines instead of a full round trip each.
    """
    lines = [dist[sku] for order in batch for sku in order.skus]
    if not lines:
        return 0.0, 0.0
    far = max(lines)
    others = sum(lines) - far
    travel_m = 2.0 * far + 0.5 * others
    time_s = travel_m / WALK_SPEED_MPS + len(lines) * PICK_HANDLING_S
    return time_s, travel_m


def _simulate_core(
    label: str,
    assignment: dict[str, int],
    batch_size: int,
    utilization: float,
    cartons: list[Carton],
    warehouse: Warehouse,
    orders: list[Order],
    n_pickers: int = 1,
) -> SimResult:
    """Run the discrete-event picking simulation for a fixed slotting ``assignment``.

    The regime-agnostic core: the caller supplies the slotting (sku -> slot), the batch size
    (1 = legacy sequential, >1 = modern batched), the container utilization to report, a ``label``
    for the result, and the crew size ``n_pickers`` (parallel pickers drawing from one shared
    backlog). The queueing model is identical for every regime, so isolating it here lets the
    slotting sensitivity and the labour sweep feed in arbitrary layouts / crew sizes without
    duplicating it.

    Crew model (honest scope): ``n_pickers`` pickers pull work from a single FIFO backlog; a free
    picker is dispatched the next order (legacy) or batch (modern) the instant it falls idle. There
    is **no** aisle congestion or pick-face contention - pickers never block one another - so more
    crew is pure parallelism with diminishing queueing returns, never a physical slow-down. At
    ``n_pickers = 1`` the loop is byte-identical to the original single-picker simulation.
    """
    dist = _distance_map(assignment, warehouse)

    # Event queue. Kinds: 0 = ARRIVAL, 1 = PICK_DONE. seq breaks ties deterministically.
    pq: list[tuple[float, int, int, object]] = []
    seq = 0
    for o in orders:
        heapq.heappush(pq, (o.arrival, seq, 0, o))
        seq += 1

    backlog: list[Order] = []
    busy = 0  # pickers currently occupied (0 .. n_pickers)
    total_travel = 0.0
    busy_seconds = 0.0  # summed service time across all pickers (for utilization)
    completions: list[float] = []  # cycle times
    arrival_of: dict[int, float] = {o.id: o.arrival for o in orders}
    makespan = 0.0

    def dispatch(now: float) -> None:
        nonlocal busy, total_travel, busy_seconds, seq
        # Fill every free picker from the shared backlog. With n_pickers == 1 this loop runs at
        # most once per call, reproducing the original "one picker, one dispatch" behaviour exactly.
        while busy < n_pickers and backlog:
            if batch_size == 1:
                order = backlog.pop(0)
                service, travel = _legacy_service(order, dist)
                payload: list[Order] = [order]
            else:
                payload = backlog[:batch_size]
                del backlog[:batch_size]
                service, travel = _modern_service(payload, dist)
            total_travel += travel
            busy_seconds += service
            busy += 1
            heapq.heappush(pq, (now + service, seq, 1, payload))
            seq += 1

    while pq:
        now, _, kind, payload = heapq.heappop(pq)
        if kind == 0:  # ARRIVAL
            backlog.append(payload)  # type: ignore[arg-type]
            dispatch(now)
        else:  # PICK_DONE
            batch = payload  # type: ignore[assignment]
            for o in batch:  # type: ignore[union-attr]
                completions.append(now - arrival_of[o.id])
            makespan = max(makespan, now)
            busy -= 1
            dispatch(now)

    n = len(completions)
    mean_cycle = sum(completions) / n if n else 0.0
    if n:
        s = sorted(completions)
        idx = min(n - 1, int(round(0.95 * (n - 1))))
        p95 = s[idx]
    else:
        p95 = 0.0

    return SimResult(
        regime=label,
        orders_completed=n,
        mean_cycle_time_s=mean_cycle,
        total_travel_m=total_travel,
        container_utilization=utilization,
        p95_cycle_time_s=p95,
        makespan_s=makespan,
        n_pickers=n_pickers,
        picker_busy_s=busy_seconds,
    )


def simulate(
    regime: str,
    cartons: list[Carton],
    warehouse: Warehouse,
    orders: list[Order],
) -> SimResult:
    """Run the discrete-event simulation for ``regime`` in {"legacy", "modern"}."""
    if regime == "legacy":
        assignment = legacy_slotting(cartons, warehouse).assignment
        utilization = naive_pack(cartons).fill_rate
        batch_size = 1
    elif regime == "modern":
        # Same canonical optimized layout as the slotting report: tie-broken toward keeping SKUs
        # in place, so the simulated layout is the one the re-shuffle plan actually produces.
        legacy_assignment = legacy_slotting(cartons, warehouse).assignment
        assignment = optimize_slotting(cartons, warehouse, prefer=legacy_assignment).assignment
        utilization = ffd_pack(cartons).fill_rate
        batch_size = MODERN_BATCH_SIZE
    else:
        raise ValueError(f"unknown regime: {regime!r}")

    return _simulate_core(regime, assignment, batch_size, utilization, cartons, warehouse, orders)


def simulate_assignment(
    assignment: dict[str, int],
    cartons: list[Carton],
    warehouse: Warehouse,
    orders: list[Order],
    *,
    batch_size: int = MODERN_BATCH_SIZE,
    utilization: float | None = None,
    label: str = "what-if",
    n_pickers: int = 1,
) -> SimResult:
    """Simulate an arbitrary slotting ``assignment`` under a chosen batch size and crew size.

    Used by the slotting sensitivity sweep (which varies the layout at a fixed single picker) and by
    the labour sweep (which fixes the layout and varies ``n_pickers``). Packing (hence
    ``utilization``) is independent of slotting, so it defaults to the same FFD fill the modern
    regime reports; only the slotting / crew size varies across a sweep.
    """
    if utilization is None:
        utilization = (
            ffd_pack(cartons).fill_rate if batch_size != 1 else naive_pack(cartons).fill_rate
        )
    return _simulate_core(
        label, assignment, batch_size, utilization, cartons, warehouse, orders, n_pickers=n_pickers
    )


def simulation_report(
    cartons: list[Carton],
    warehouse: Warehouse,
    orders: list[Order],
) -> dict:
    """Run both regimes and compute the modern-vs-legacy deltas."""
    legacy = simulate("legacy", cartons, warehouse, orders)
    modern = simulate("modern", cartons, warehouse, orders)

    def pct_drop(old: float, new: float) -> float:
        return 100.0 * (old - new) / old if old > 0 else 0.0

    return {
        "legacy": legacy,
        "modern": modern,
        "cycle_time_reduction_pct": pct_drop(
            legacy.mean_cycle_time_s, modern.mean_cycle_time_s
        ),
        "travel_reduction_pct": pct_drop(legacy.total_travel_m, modern.total_travel_m),
        "utilization_gain_pct": 100.0
        * (modern.container_utilization - legacy.container_utilization)
        / legacy.container_utilization
        if legacy.container_utilization > 0
        else 0.0,
        "orders": len(orders),
    }
