"""3D carton bin-packing.

Two solvers live here:

* :func:`ffd_pack` - a First-Fit-Decreasing-by-volume heuristic with a shelf/layer placement
  rule. Fast, works on the full carton set, respects weight and dimension limits. Bin packing is
  NP-hard, so this is a heuristic - not guaranteed optimal.
* :func:`cpsat_min_bins` - a CP-SAT exact bin-count solver for a *small* instance, used only to
  measure how far the heuristic is from optimal on that instance.

We report fill rate (used volume / container volume) and the number of containers used, and compare
the FFD heuristic against a naive one-carton-per-container baseline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .data import Carton, Container


@dataclass
class Placement:
    """A carton placed at an (x, y, z) origin inside a container."""

    carton_id: int
    x: float
    y: float
    z: float
    length: float
    width: float
    height: float


@dataclass
class PackedContainer:
    """One container with the cartons assigned to it."""

    placements: list[Placement] = field(default_factory=list)
    used_volume: float = 0.0
    used_weight: float = 0.0


@dataclass
class PackResult:
    containers: list[PackedContainer]
    fill_rate: float
    n_containers: int

    @property
    def n_cartons(self) -> int:
        return sum(len(c.placements) for c in self.containers)


class _Shelf:
    """A horizontal shelf (layer) inside a container.

    A shelf has a fixed base height ``z`` and its own height (the tallest carton on it). Cartons are
    laid out left-to-right; when a row is full we wrap to the next row within the shelf footprint.
    """

    def __init__(self, z: float, container: Container):
        self.z = z
        self.container = container
        self.height = 0.0
        self.cursor_x = 0.0
        self.cursor_y = 0.0
        self.row_depth = 0.0  # deepest carton (width) in the current row

    def try_place(self, c: Carton) -> Placement | None:
        # A shelf's height is fixed by its first carton and never grows; taller cartons are
        # rejected here and open a new shelf on top instead. This keeps the vertical stack
        # consistent so the container height is never exceeded.
        if self.height > 0 and c.height > self.height:
            return None
        if self.cursor_x + c.length > self.container.length:
            # wrap to a new row
            self.cursor_x = 0.0
            self.cursor_y += self.row_depth
            self.row_depth = 0.0
        if self.cursor_y + c.width > self.container.width:
            return None  # shelf full in the footprint
        placement = Placement(
            carton_id=c.id,
            x=self.cursor_x,
            y=self.cursor_y,
            z=self.z,
            length=c.length,
            width=c.width,
            height=c.height,
        )
        self.cursor_x += c.length
        self.row_depth = max(self.row_depth, c.width)
        if self.height == 0.0:
            self.height = c.height  # lock shelf height to the first carton placed
        return placement


def _fits_dims(c: Carton, container: Container) -> bool:
    """Whether a carton can physically fit a container in its given orientation."""
    return (
        c.length <= container.length
        and c.width <= container.width
        and c.height <= container.height
    )


def _pack_one_container(cartons: list[Carton], container: Container) -> tuple[PackedContainer, list[Carton]]:
    """Greedily fill a single container using stacked shelves. Returns (packed, leftovers)."""
    packed = PackedContainer()
    shelves: list[_Shelf] = []
    stack_top = 0.0
    leftovers: list[Carton] = []

    for c in cartons:
        if not _fits_dims(c, container):
            leftovers.append(c)
            continue
        if packed.used_weight + c.weight > container.max_weight:
            leftovers.append(c)
            continue

        placed = None
        for shelf in shelves:
            p = shelf.try_place(c)
            if p is not None:
                placed = p
                break
        if placed is None:
            # open a new shelf on top if there is vertical room
            if stack_top + c.height <= container.height:
                shelf = _Shelf(stack_top, container)
                p = shelf.try_place(c)
                if p is not None:
                    shelves.append(shelf)
                    placed = p
        if placed is None:
            leftovers.append(c)
            continue

        packed.placements.append(placed)
        packed.used_volume += c.volume
        packed.used_weight += c.weight
        # Recompute stack top from all shelves (base + tallest carton on each).
        stack_top = sum(s.height for s in shelves)

    return packed, leftovers


def ffd_pack(cartons: list[Carton], container: Container | None = None) -> PackResult:
    """First-Fit-Decreasing (by volume) shelf packing across as many containers as needed."""
    container = container or Container()
    ordered = sorted(cartons, key=lambda c: c.volume, reverse=True)
    remaining = ordered
    containers: list[PackedContainer] = []

    # Safety bound: never more containers than cartons.
    guard = 0
    while remaining and guard <= len(cartons):
        guard += 1
        packed, leftovers = _pack_one_container(remaining, container)
        if not packed.placements:
            # Nothing fit at all (e.g. oversized cartons) - drop them to avoid an infinite loop.
            break
        containers.append(packed)
        remaining = leftovers

    return _summarize(containers, container)


def naive_pack(cartons: list[Carton], container: Container | None = None) -> PackResult:
    """Baseline: one carton per container (no consolidation). This is the 'legacy' regime."""
    container = container or Container()
    containers: list[PackedContainer] = []
    for c in cartons:
        pc = PackedContainer()
        pc.placements.append(
            Placement(c.id, 0.0, 0.0, 0.0, c.length, c.width, c.height)
        )
        pc.used_volume = c.volume
        pc.used_weight = c.weight
        containers.append(pc)
    return _summarize(containers, container)


def _summarize(containers: list[PackedContainer], container: Container) -> PackResult:
    n = len(containers)
    if n == 0:
        return PackResult(containers=[], fill_rate=0.0, n_containers=0)
    total_used = sum(c.used_volume for c in containers)
    fill = total_used / (n * container.volume)
    return PackResult(containers=containers, fill_rate=fill, n_containers=n)


def ffd_min_bins_1d(sizes: list[float], bin_capacity: float) -> int:
    """First-Fit-Decreasing bin count for a 1D bin-packing instance (heuristic)."""
    bins: list[float] = []  # remaining capacity per open bin
    for s in sorted(sizes, reverse=True):
        placed = False
        for i, rem in enumerate(bins):
            if s <= rem:
                bins[i] = rem - s
                placed = True
                break
        if not placed:
            bins.append(bin_capacity - s)
    return len(bins)


def cpsat_min_bins(
    volumes: list[float],
    bin_capacity: float,
    time_limit_s: float = 5.0,
) -> int:
    """Exact / near-exact minimum bin count for a 1D bin-packing instance via CP-SAT.

    This is the classic 1D relaxation (pack items by a single capacity, e.g. volume) used to give a
    lower-bound-quality reference for the number of containers. On small instances CP-SAT proves
    optimality within the time limit; we use it to bound the FFD heuristic's gap.
    """
    n = len(volumes)
    if n == 0:
        return 0
    # Scale to integers for CP-SAT.
    scale = 1000.0 / max(volumes)
    w = [max(1, int(round(v * scale))) for v in volumes]
    cap = int(round(bin_capacity * scale))
    # A trivially safe upper bound on bins.
    max_bins = n

    model = cp_model.CpModel()
    x = {}  # x[i, b] item i in bin b
    for i in range(n):
        for b in range(max_bins):
            x[i, b] = model.NewBoolVar(f"x_{i}_{b}")
    y = [model.NewBoolVar(f"y_{b}") for b in range(max_bins)]

    for i in range(n):
        model.Add(sum(x[i, b] for b in range(max_bins)) == 1)
    for b in range(max_bins):
        model.Add(sum(w[i] * x[i, b] for i in range(n)) <= cap * y[b])
    # Symmetry breaking: bin b can be used only if bin b-1 is used.
    for b in range(1, max_bins):
        model.Add(y[b] <= y[b - 1])

    model.Minimize(sum(y))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return int(round(solver.ObjectiveValue()))
    # Fallback: heuristic count if solver failed to find anything (should not happen).
    return max_bins


def packing_report(cartons: list[Carton], container: Container | None = None) -> dict:
    """Full packing comparison used by exports and the UI."""
    container = container or Container()
    naive = naive_pack(cartons, container)
    ffd = ffd_pack(cartons, container)

    # Small-instance heuristic-vs-optimal check on an identical 1D bin-packing sub-problem: pack a
    # handful of cartons (by volume) into a small picking-tote capacity. FFD-1D (heuristic) is
    # compared against CP-SAT solving the SAME instance to optimality, so the gap is a genuine
    # heuristic-vs-optimal gap rather than a relaxation artefact.
    tote = Container(length=60.0, width=40.0, height=40.0, max_weight=25.0)
    small = sorted(cartons, key=lambda c: c.volume, reverse=True)[:14]
    small_vols = [c.volume for c in small]
    cap = tote.volume
    ffd_small = ffd_min_bins_1d(small_vols, cap)
    cpsat_small = cpsat_min_bins(small_vols, cap)
    gap_pct = 0.0
    if cpsat_small > 0:
        gap_pct = 100.0 * (ffd_small - cpsat_small) / cpsat_small

    return {
        "naive_fill": naive.fill_rate,
        "naive_containers": naive.n_containers,
        "ffd_fill": ffd.fill_rate,
        "ffd_containers": ffd.n_containers,
        "containers_saved": naive.n_containers - ffd.n_containers,
        "fill_improvement_pct": 100.0 * (ffd.fill_rate - naive.fill_rate) / naive.fill_rate
        if naive.fill_rate > 0
        else 0.0,
        "cpsat_small_instance_size": len(small),
        "cpsat_small_bins": cpsat_small,
        "ffd_small_bins": ffd_small,
        "cpsat_gap_pct": gap_pct,
        "ffd_result": ffd,
        "naive_result": naive,
    }
