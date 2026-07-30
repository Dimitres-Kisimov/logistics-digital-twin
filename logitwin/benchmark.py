"""Honest benchmark of the packing engine against standard 1D bin-packing instances.

This runs the *real* engine entry points from :mod:`logitwin.packing` - the First-Fit-Decreasing
heuristic (:func:`ffd_min_bins_1d`) and the CP-SAT exact solver (:func:`cpsat_min_bins`) - on a
small set of well-documented 1D bin-packing instances whose optimum is *proven*, and reports the
gap to that optimum openly, including the instances where the heuristic loses.

Where the instances come from (see ``data/benchmark_instances.json`` for the full, per-instance
citations):

* **triplet-\\*** - constructed following Falkenauer's (1996) *triplet* instance scheme, the classic
  hard case for FFD: items are grouped into triples that each fill a bin exactly, so the optimum is
  ``n / 3``. The published files live in OR-Library (Beasley 1990) and BPPLIB (Delorme et al. 2018);
  we do not fetch them (this benchmark is offline and deterministic), we reproduce the scheme.
* **perfect-pairs / even-split** - hand-constructed instances FFD solves optimally.
* **ffd-suboptimal-\\*** - hand-constructed instances where FFD provably uses one bin too many
  (cf. Johnson 1973; Dosa 2007).

For every instance the *known optimum* is proven the same way: the volume lower bound
``ceil(sum(items) / bin_capacity)`` equals the number of bins in an exhibited optimal packing, so it
is at once a valid lower bound and an achievable upper bound. :func:`validate_instance` re-checks
that proof on load, so a mislabelled optimum cannot slip through.

Determinism: FFD is deterministic; on these small instances CP-SAT proves optimality (status
OPTIMAL) well within the time limit, so the returned bin count is the proven optimum and does not
depend on wall-clock timing. Runtimes are measured for information only and are excluded from the
deterministic result (:func:`evaluate_instance`).

CLI (matches the repo's ``python -m ...`` style)::

    python -m logitwin.benchmark            # print the benchmark table
    python -m logitwin.benchmark --json     # machine-readable report
    python -m logitwin.benchmark --instances path/to/instances.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .packing import cpsat_min_bins, ffd_min_bins_1d

SCHEMA = "logitwin-bpp-1"

# The instance file lives at the repo root under data/. benchmark.py is in logitwin/.
DEFAULT_INSTANCES_PATH = Path(__file__).resolve().parent.parent / "data" / "benchmark_instances.json"

# CP-SAT is given a fixed, generous time budget. Every embedded instance is small enough that CP-SAT
# proves optimality far inside this budget, so the returned bin count is the proven optimum (not a
# wall-clock-dependent feasible bound). This keeps the benchmark result deterministic.
CPSAT_TIME_LIMIT_S = 10.0


class BenchmarkError(ValueError):
    """Raised when the instance file is malformed or an optimum fails its proof check."""


@dataclass(frozen=True)
class BenchmarkInstance:
    """One 1D bin-packing instance with a proven optimum and its provenance."""

    name: str
    dimension: str
    bin_capacity: float
    items: tuple[float, ...]
    known_optimum: int
    optimum_source: str
    optimum_proof: str
    optimal_packing: tuple[tuple[float, ...], ...]
    citation: str
    note: str


@dataclass(frozen=True)
class InstanceResult:
    """Deterministic benchmark result for one instance (no runtime - see :func:`run_benchmark`)."""

    name: str
    dimension: str
    bin_capacity: float
    n_items: int
    known_optimum: int
    optimum_source: str
    ffd_bins: int
    cpsat_bins: int
    ffd_abs_gap: int
    ffd_pct_gap: float
    cpsat_abs_gap: int
    cpsat_pct_gap: float
    note: str


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BenchmarkError(message)


def validate_instance(raw: dict) -> BenchmarkInstance:
    """Validate one raw instance dict and re-prove its optimum, returning a typed instance.

    The optimum is accepted only if (a) an exhibited ``optimal_packing`` of exactly
    ``known_optimum`` bins covers every item with no bin over capacity, and (b) the volume lower
    bound ``ceil(sum(items) / bin_capacity)`` equals ``known_optimum``. Together these prove
    ``OPT == known_optimum`` (lower bound == achievable count).
    """
    name = raw.get("name")
    _require(isinstance(name, str) and name, "instance is missing a string 'name'")

    def field(key: str, types: type | tuple[type, ...]) -> object:
        _require(key in raw, f"instance {name!r} is missing '{key}'")
        value = raw[key]
        _require(isinstance(value, types), f"instance {name!r}: '{key}' has the wrong type")
        return value

    dimension = field("dimension", str)
    _require(dimension == "1D", f"instance {name!r}: only '1D' dimension is supported, got {dimension!r}")

    bin_capacity = field("bin_capacity", (int, float))
    _require(bin_capacity > 0, f"instance {name!r}: 'bin_capacity' must be positive")

    items_raw = field("items", list)
    _require(len(items_raw) > 0, f"instance {name!r}: 'items' must be non-empty")
    _require(
        all(isinstance(x, (int, float)) and x > 0 for x in items_raw),
        f"instance {name!r}: every item must be a positive number",
    )
    _require(
        all(x <= bin_capacity for x in items_raw),
        f"instance {name!r}: an item exceeds the bin capacity (cannot fit in any bin)",
    )
    items = tuple(items_raw)

    known_optimum = field("known_optimum", int)
    # bool is an int subclass; reject it explicitly.
    _require(not isinstance(raw["known_optimum"], bool), f"instance {name!r}: 'known_optimum' must be an int")
    _require(known_optimum >= 1, f"instance {name!r}: 'known_optimum' must be >= 1")

    for key in ("optimum_source", "optimum_proof", "citation", "note"):
        field(key, str)

    packing_raw = field("optimal_packing", list)
    _require(
        all(isinstance(b, list) and b for b in packing_raw),
        f"instance {name!r}: 'optimal_packing' must be a list of non-empty bins",
    )
    packing = tuple(tuple(b) for b in packing_raw)

    # --- Re-prove the optimum ---------------------------------------------------------------
    # (a) the exhibited packing is feasible and uses exactly known_optimum bins ...
    _require(
        len(packing) == known_optimum,
        f"instance {name!r}: optimal_packing has {len(packing)} bins but known_optimum is {known_optimum}",
    )
    for i, b in enumerate(packing):
        _require(
            sum(b) <= bin_capacity,
            f"instance {name!r}: optimal_packing bin {i} sums to {sum(b)} > capacity {bin_capacity}",
        )
    _require(
        sorted(x for b in packing for x in b) == sorted(items),
        f"instance {name!r}: optimal_packing does not cover exactly the instance items",
    )
    # (b) ... and the volume lower bound coincides with it, which proves optimality.
    volume_lb = math.ceil(sum(items) / bin_capacity)
    _require(
        volume_lb == known_optimum,
        f"instance {name!r}: volume lower bound ceil(sum/cap)={volume_lb} != known_optimum "
        f"{known_optimum}; the exhibited packing is not proven optimal",
    )

    return BenchmarkInstance(
        name=name,
        dimension=dimension,
        bin_capacity=bin_capacity,
        items=items,
        known_optimum=known_optimum,
        optimum_source=raw["optimum_source"],
        optimum_proof=raw["optimum_proof"],
        optimal_packing=packing,
        citation=raw["citation"],
        note=raw["note"],
    )


def load_instances(path: str | Path | None = None) -> list[BenchmarkInstance]:
    """Load and validate every instance from the JSON instance file."""
    path = Path(path) if path is not None else DEFAULT_INSTANCES_PATH
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise BenchmarkError(f"cannot read instance file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"instance file {path} is not valid JSON: {exc}") from exc

    _require(isinstance(obj, dict), "instance file must be a JSON object")
    _require(obj.get("schema") == SCHEMA, f"instance file schema must be {SCHEMA!r}")
    raw_instances = obj.get("instances")
    _require(isinstance(raw_instances, list) and raw_instances, "instance file needs a non-empty 'instances' list")

    instances = [validate_instance(raw) for raw in raw_instances]
    names = [inst.name for inst in instances]
    _require(len(names) == len(set(names)), "instance names must be unique")
    return instances


def _pct_gap(bins: int, optimum: int) -> float:
    """Percentage a bin count is above the optimum (0.0 when optimal)."""
    if optimum <= 0:
        return 0.0
    return round(100.0 * (bins - optimum) / optimum, 2)


def evaluate_instance(inst: BenchmarkInstance) -> InstanceResult:
    """Run the real engine on one instance and compute the (deterministic) gaps to the optimum."""
    sizes = list(inst.items)
    ffd_bins = ffd_min_bins_1d(sizes, inst.bin_capacity)
    cpsat_bins = cpsat_min_bins(sizes, inst.bin_capacity, time_limit_s=CPSAT_TIME_LIMIT_S)

    ffd_abs = ffd_bins - inst.known_optimum
    cpsat_abs = cpsat_bins - inst.known_optimum
    if ffd_abs == 0:
        note = "FFD optimal"
    else:
        note = f"FFD +{ffd_abs} bin{'s' if ffd_abs != 1 else ''} over optimum"
    if cpsat_abs != 0:
        # CP-SAT should reach the proven optimum on these small instances; surface it if it did not.
        note += f"; CP-SAT off by {cpsat_abs:+d}"

    return InstanceResult(
        name=inst.name,
        dimension=inst.dimension,
        bin_capacity=inst.bin_capacity,
        n_items=len(inst.items),
        known_optimum=inst.known_optimum,
        optimum_source=inst.optimum_source,
        ffd_bins=ffd_bins,
        cpsat_bins=cpsat_bins,
        ffd_abs_gap=ffd_abs,
        ffd_pct_gap=_pct_gap(ffd_bins, inst.known_optimum),
        cpsat_abs_gap=cpsat_abs,
        cpsat_pct_gap=_pct_gap(cpsat_bins, inst.known_optimum),
        note=note,
    )


def evaluate_all(instances: list[BenchmarkInstance] | None = None) -> list[InstanceResult]:
    """Deterministic core: evaluate every instance (no timing). Two calls return equal results."""
    instances = instances if instances is not None else load_instances()
    return [evaluate_instance(inst) for inst in instances]


def run_benchmark(
    instances: list[BenchmarkInstance] | None = None,
    *,
    with_runtime: bool = True,
) -> dict:
    """Evaluate every instance and bundle a report.

    With ``with_runtime=False`` the report is fully deterministic (no wall-clock fields) - this is
    the form the determinism test compares. With ``with_runtime=True`` (default) each row also
    carries a ``runtime_ms`` block timing the two engine calls, for the CLI/report only.
    """
    instances = instances if instances is not None else load_instances()
    rows: list[dict] = []
    for inst in instances:
        if with_runtime:
            t0 = time.perf_counter()
            ffd_bins = ffd_min_bins_1d(list(inst.items), inst.bin_capacity)
            t1 = time.perf_counter()
            cpsat_bins = cpsat_min_bins(list(inst.items), inst.bin_capacity, time_limit_s=CPSAT_TIME_LIMIT_S)
            t2 = time.perf_counter()
            result = evaluate_instance(inst)
            # Sanity: the timed calls must agree with the deterministic evaluation.
            assert result.ffd_bins == ffd_bins and result.cpsat_bins == cpsat_bins
            row = asdict(result)
            row["runtime_ms"] = {
                "ffd": round(1000.0 * (t1 - t0), 3),
                "cpsat": round(1000.0 * (t2 - t1), 3),
            }
        else:
            row = asdict(evaluate_instance(inst))
        rows.append(row)

    n_optimal = sum(1 for r in rows if r["ffd_abs_gap"] == 0)
    n_suboptimal = sum(1 for r in rows if r["ffd_abs_gap"] > 0)
    cpsat_all_optimal = all(r["cpsat_abs_gap"] == 0 for r in rows)
    max_pct = max((r["ffd_pct_gap"] for r in rows), default=0.0)
    return {
        "schema": SCHEMA,
        "n_instances": len(rows),
        "instances": rows,
        "summary": {
            "ffd_optimal_instances": n_optimal,
            "ffd_suboptimal_instances": n_suboptimal,
            "cpsat_all_optimal": cpsat_all_optimal,
            "worst_ffd_pct_gap": max_pct,
        },
    }


# --- rendering ----------------------------------------------------------------------------------

_COLUMNS = ("instance", "n", "FFD (heur.)", "CP-SAT", "known/opt", "FFD gap", "note")


def render_table(report: dict) -> str:
    """Render the benchmark report as a fixed-width text table."""
    rows: list[tuple[str, ...]] = []
    for r in report["instances"]:
        gap = "0 (optimal)" if r["ffd_abs_gap"] == 0 else f"+{r['ffd_abs_gap']} ({r['ffd_pct_gap']:.1f}%)"
        rows.append(
            (
                r["name"],
                str(r["n_items"]),
                str(r["ffd_bins"]),
                str(r["cpsat_bins"]),
                str(r["known_optimum"]),
                gap,
                r["note"],
            )
        )

    widths = [len(h) for h in _COLUMNS]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    line = "-" * (sum(widths) + 2 * (len(widths) - 1))
    out = [
        "1D bin-packing benchmark - logitwin engine vs proven optimum",
        line,
        fmt(_COLUMNS),
        line,
    ]
    out.extend(fmt(row) for row in rows)
    out.append(line)

    s = report["summary"]
    out.append(
        f"FFD optimal on {s['ffd_optimal_instances']}/{report['n_instances']} instances; "
        f"suboptimal on {s['ffd_suboptimal_instances']} "
        f"(worst gap {s['worst_ffd_pct_gap']:.1f}% above optimum)."
    )
    out.append(
        "CP-SAT reached the proven optimum on every instance."
        if s["cpsat_all_optimal"]
        else "CP-SAT did NOT reach the proven optimum on some instance (see the 'note' column)."
    )
    out.append(
        "FFD is a heuristic: its worst-case 1D bound is FFD <= (11/9)OPT + 6/9 (Dosa 2007), "
        "~22% above optimum. The triplet-* rows are Falkenauer's classic hard case for FFD."
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="logitwin.benchmark",
        description="Benchmark the packing engine (FFD + CP-SAT) against proven 1D bin-packing optima.",
    )
    parser.add_argument("--instances", metavar="FILE", help="path to a benchmark instance JSON file")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    try:
        instances = load_instances(args.instances)
    except BenchmarkError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    report = run_benchmark(instances)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_table(report))
    return 0


if __name__ == "__main__":
    # Windows console safety: force UTF-8 so ASCII markers and any stray unicode never crash stdout.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
