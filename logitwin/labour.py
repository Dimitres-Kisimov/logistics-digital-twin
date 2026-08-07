"""Labour / crew-size sensitivity: how many pickers does the floor actually need?

This is an honest "what-if" layer over the *existing* discrete-event simulation
(:mod:`logitwin.simulate`). Where the slotting sensitivity (:mod:`logitwin.sensitivity`) holds the
crew fixed and sweeps the *layout*, this one does the complement: it holds the layout fixed and
sweeps the **crew size** - the number of parallel pickers drawing from one shared order backlog -
to answer the staffing question ("right-size the crew"). It introduces no new heuristic: the same
:func:`logitwin.simulate.simulate_assignment` runs each crew size, only ``n_pickers`` changes.

Both operating regimes are swept, so the two levers of the whole project - *how you slot* and *how
many you staff* - can be read on one picture:

* **legacy** - arbitrary (alphabetical) slotting, one-carton-per-container, a round trip to dispatch
  for every pick line (``batch_size = 1``);
* **modern** - velocity-optimized slotting and batched picking (``batch_size = 4``), the same
  canonical optimized layout the slotting report and the simulation's "modern" regime use.

What responds, and what does not (measured, not asserted)
--------------------------------------------------------
On the seeded shift the arrival stream is fixed, so **order completion count and makespan are
arrival-bound** - every crew size finishes the same 101 orders inside the same horizon. Adding
pickers therefore shows up purely as **responsiveness** (mean and p95 order cycle time) and as
**lower per-picker utilization**, with a clear diminishing-returns knee - not as "more orders/hour".
This is the empirical confirmation of the queueing note the slotting-sensitivity module already
flags. **Total picker travel is essentially flat across crew size**: more pickers parallelize the
walking, they do not shorten it (legacy is exactly flat; modern drifts by <3% because a busier
backlog packs marginally fuller pick batches).

Honest scope
------------
The crew model has **no aisle congestion or pick-face contention** - pickers never block one
another - so more crew is pure parallelism with diminishing queueing returns, never a physical
slow-down (a real floor eventually congests; this model does not, and says so). Everything is
synthetic, seeded and deterministic: a given dataset always yields byte-identical tables, CSV and
SVG. Numbers are teaching-scale (a 60-SKU, 101-order synthetic shift), reported exactly as the code
computes them.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .data import Carton, Warehouse, dataset
from .simulate import MODERN_BATCH_SIZE, simulate_assignment
from .slotting import legacy_slotting, optimize_slotting

# Default crew grid: 1 .. 8 pickers. Eight is comfortably past the knee on the seeded shift, so the
# flat tail (where extra crew only lowers utilization) is visible.
DEFAULT_CREW_SIZES: tuple[int, ...] = tuple(range(1, 9))

# Share of the full-crew cycle-time reduction the recommended crew must bank (the staffing knee).
# Mirrors the slotting-sensitivity module's capture rule so the two frontiers speak the same
# language: the smallest lever that captures >= this fraction of the available benefit.
DEFAULT_CAPTURE = 0.90

# The two regimes swept, each as (label, batch_size). Batch size is the only picking-policy lever;
# the slotting for each is built in :func:`_assignment_for`.
REGIMES: tuple[tuple[str, int], ...] = (("legacy", 1), ("modern", MODERN_BATCH_SIZE))


@dataclass(frozen=True)
class CrewRow:
    """One crew size for one regime (all figures deterministic for a given dataset)."""

    regime: str
    n_pickers: int
    orders_completed: int
    mean_cycle_s: float  # mean order cycle time (responsiveness)
    p95_cycle_s: float  # 95th-percentile order cycle time (the tail)
    makespan_s: float  # time the last order is completed (arrival-bound here)
    sim_travel_m: float  # total picker travel over the shift (flat across crew size)
    utilization_pct: float  # mean per-picker utilization over the active horizon
    cycle_reduction_pct: float  # mean cycle time vs a single picker of the same regime


def _assignment_for(regime: str, cartons: list[Carton], warehouse: Warehouse) -> dict[str, int]:
    """The slotting each regime picks from: legacy alphabetical, or the canonical optimized layout.

    The modern layout is the *same* tie-broken optimum the slotting report and the simulation's
    "modern" regime produce, so a crew size of 1 here reproduces those modules' numbers exactly.
    """
    legacy = legacy_slotting(cartons, warehouse).assignment
    if regime == "legacy":
        return legacy
    if regime == "modern":
        return optimize_slotting(cartons, warehouse, prefer=legacy).assignment
    raise ValueError(f"unknown regime: {regime!r}")


def sweep_labour(
    cartons: list[Carton],
    warehouse: Warehouse,
    orders: list,
    regime: str,
    batch_size: int,
    crew_sizes: tuple[int, ...] = DEFAULT_CREW_SIZES,
) -> list[CrewRow]:
    """Evaluate every crew size for one regime and return the rows (deterministic)."""
    assignment = _assignment_for(regime, cartons, warehouse)
    rows: list[CrewRow] = []
    base_mean: float | None = None
    for c in crew_sizes:
        sim = simulate_assignment(
            assignment, cartons, warehouse, orders,
            batch_size=batch_size, n_pickers=c, label=regime,
        )
        if base_mean is None:
            base_mean = sim.mean_cycle_time_s
        reduction = (
            100.0 * (base_mean - sim.mean_cycle_time_s) / base_mean if base_mean > 0 else 0.0
        )
        util = (
            100.0 * sim.picker_busy_s / (c * sim.makespan_s) if sim.makespan_s > 0 else 0.0
        )
        rows.append(
            CrewRow(
                regime=regime,
                n_pickers=c,
                orders_completed=sim.orders_completed,
                mean_cycle_s=round(sim.mean_cycle_time_s, 2),
                p95_cycle_s=round(sim.p95_cycle_time_s, 2),
                makespan_s=round(sim.makespan_s, 2),
                sim_travel_m=round(sim.total_travel_m, 1),
                utilization_pct=round(util, 2),
                cycle_reduction_pct=round(reduction, 2),
            )
        )
    return rows


def recommended_crew(rows: list[CrewRow], capture: float = DEFAULT_CAPTURE) -> CrewRow:
    """The staffing knee: the smallest crew that banks ``capture`` of the full cycle-time saving.

    The full saving is the mean-cycle-time reduction at the largest crew evaluated. We return the
    first (smallest) crew whose reduction reaches ``capture`` of it; if none does (e.g. the curve is
    already flat), the single-picker row is returned.
    """
    if not rows:
        raise ValueError("no rows to choose from")
    best_reduction = max(r.cycle_reduction_pct for r in rows)
    target = capture * best_reduction
    for r in sorted(rows, key=lambda r: r.n_pickers):
        if r.cycle_reduction_pct >= target:
            return r
    return min(rows, key=lambda r: r.n_pickers)


def labour_report(
    cartons: list[Carton] | None = None,
    warehouse: Warehouse | None = None,
    orders: list | None = None,
    *,
    seed: int = 42,
    n: int = 60,
    crew_sizes: tuple[int, ...] = DEFAULT_CREW_SIZES,
    capture: float = DEFAULT_CAPTURE,
) -> dict:
    """Run the crew sweep for both regimes on a (synthetic, seeded) dataset and bundle it."""
    if cartons is None or warehouse is None or orders is None:
        ds = dataset(seed=seed, n=n)
        cartons, warehouse, orders = ds["cartons"], ds["warehouse"], ds["orders"]

    by_regime: dict[str, list[CrewRow]] = {}
    rec: dict[str, CrewRow] = {}
    for label, batch_size in REGIMES:
        rows = sweep_labour(cartons, warehouse, orders, label, batch_size, crew_sizes)
        by_regime[label] = rows
        rec[label] = recommended_crew(rows, capture=capture)

    return {
        "seed": seed,
        "n_skus": len(cartons),
        "n_orders": len(orders),
        "capture": capture,
        "crew_sizes": list(crew_sizes),
        "by_regime": by_regime,
        "recommended": rec,
        "read": trade_off_read(by_regime, rec, capture),
    }


def trade_off_read(
    by_regime: dict[str, list[CrewRow]],
    rec: dict[str, CrewRow],
    capture: float,
) -> str:
    """One-paragraph plain-language read of the staffing trade-off, numbers straight from the sweep."""
    lg, md = by_regime["legacy"], by_regime["modern"]
    lg1, md1 = lg[0], md[0]
    lg2 = next((r for r in lg if r.n_pickers == 2), lg[-1])
    md2 = next((r for r in md if r.n_pickers == 2), md[-1])
    lg_rec, md_rec = rec["legacy"], rec["modern"]
    return (
        f"On the seeded {md1.orders_completed}-order shift, a single picker on the modern "
        f"(velocity-slotted, batched) floor holds a {md1.mean_cycle_s:.0f}s mean cycle time at "
        f"{md1.utilization_pct:.0f}% utilization, while the same lone picker on the legacy floor sits "
        f"at {lg1.mean_cycle_s:.0f}s and {lg1.utilization_pct:.0f}% utilization. A second picker banks "
        f"most of the available responsiveness: legacy {lg1.mean_cycle_s:.0f}s -> {lg2.mean_cycle_s:.0f}s "
        f"({lg2.cycle_reduction_pct:.0f}% of the single-picker cycle time cut) and modern "
        f"{md1.mean_cycle_s:.0f}s -> {md2.mean_cycle_s:.0f}s. Beyond the knee both curves flatten - "
        f"the shift is arrival-bound, not labour-bound, so extra crew mainly lowers utilization while "
        f"orders completed and makespan hold. Recommended crew (>= {capture * 100:.0f}% of the "
        f"responsiveness gain): {lg_rec.n_pickers} pickers legacy, {md_rec.n_pickers} modern. Total "
        f"picker travel is essentially unchanged by crew size - more pickers parallelize the walking, "
        f"they do not shorten it. (Synthetic, seeded {md1.orders_completed}-order shift; no aisle "
        f"congestion modelled.)"
    )


# --- serialisation ------------------------------------------------------------------------------

_CSV_HEADER = (
    "regime",
    "n_pickers",
    "orders_completed",
    "mean_cycle_s",
    "p95_cycle_s",
    "makespan_s",
    "sim_travel_m",
    "utilization_pct",
    "cycle_reduction_pct",
)


def _all_rows(report: dict) -> list[CrewRow]:
    """Every row, legacy then modern, in crew order (the CSV / table order)."""
    rows: list[CrewRow] = []
    for label, _ in REGIMES:
        rows.extend(report["by_regime"][label])
    return rows


def to_csv(report: dict) -> str:
    """Deterministic CSV of the crew sweep (fixed formatting, trailing newline)."""
    lines = [",".join(_CSV_HEADER)]
    for r in _all_rows(report):
        lines.append(
            ",".join(
                (
                    r.regime,
                    str(r.n_pickers),
                    str(r.orders_completed),
                    f"{r.mean_cycle_s:.2f}",
                    f"{r.p95_cycle_s:.2f}",
                    f"{r.makespan_s:.2f}",
                    f"{r.sim_travel_m:.1f}",
                    f"{r.utilization_pct:.2f}",
                    f"{r.cycle_reduction_pct:.2f}",
                )
            )
        )
    return "\n".join(lines) + "\n"


def to_svg(report: dict) -> str:
    """Hand-drawn (pure-string, no plotting library) SVG of the staffing curves.

    X = crew size (pickers); Y = mean order cycle time (s), both regimes plotted, each regime's
    recommended crew ringed. Deterministic: every coordinate derives from the seeded sweep.
    """
    crew_sizes: list[int] = report["crew_sizes"]
    lg: list[CrewRow] = report["by_regime"]["legacy"]
    md: list[CrewRow] = report["by_regime"]["modern"]

    w, h = 720, 440
    ml, mr, mt, mb = 70, 30, 60, 70
    pw, ph = w - ml - mr, h - mt - mb

    max_crew = max(crew_sizes) if crew_sizes else 1
    min_crew = min(crew_sizes) if crew_sizes else 1
    span_crew = (max_crew - min_crew) or 1
    max_cycle = max((r.mean_cycle_s for r in lg + md), default=1.0) or 1.0

    def px(crew: float) -> float:
        return ml + pw * ((crew - min_crew) / span_crew)

    def py(cycle: float) -> float:
        return mt + ph * (1.0 - cycle / max_cycle)

    def polyline_for(rows: list[CrewRow], colour: str) -> list[str]:
        pts = [(px(r.n_pickers), py(r.mean_cycle_s)) for r in rows]
        poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        out = [f'<polyline points="{poly}" fill="none" stroke="{colour}" stroke-width="2.5"/>']
        out.extend(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{colour}"/>' for x, y in pts)
        return out

    legacy_colour, modern_colour, ring_colour = "#9aa5b1", "#2a9d8f", "#e9a03b"

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" font-family="sans-serif">'
    )
    parts.append(f'<rect width="{w}" height="{h}" fill="#ffffff"/>')
    parts.append(
        f'<text x="{ml}" y="30" font-size="17" font-weight="bold" fill="#1f3a5f">'
        f"Labour sensitivity - order cycle time vs crew size</text>"
    )
    parts.append(
        f'<text x="{ml}" y="48" font-size="11" fill="#9aa5b1">'
        f"Synthetic, seeded {report['n_skus']}-SKU / {report['n_orders']}-order shift - deterministic</text>"
    )
    # Axes.
    parts.append(
        f'<line x1="{ml}" y1="{mt + ph}" x2="{ml + pw}" y2="{mt + ph}" stroke="#333" stroke-width="1"/>'
    )
    parts.append(f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt + ph}" stroke="#333" stroke-width="1"/>')
    # Y gridlines / ticks (0 .. max_cycle).
    for i in range(5):
        cyc = max_cycle * i / 4
        y = py(cyc)
        parts.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{ml + pw}" y2="{y:.1f}" stroke="#eee" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{ml - 8}" y="{y + 4:.1f}" font-size="10" fill="#555" text-anchor="end">'
            f"{cyc:.0f}s</text>"
        )
    # X ticks (one per crew size).
    for c in crew_sizes:
        x = px(c)
        parts.append(
            f'<text x="{x:.1f}" y="{mt + ph + 18:.1f}" font-size="10" fill="#555" text-anchor="middle">'
            f"{c}</text>"
        )
    parts.append(
        f'<text x="{ml + pw / 2:.0f}" y="{h - 24}" font-size="12" fill="#333" text-anchor="middle">'
        f"pickers (crew size)</text>"
    )
    parts.append(
        f'<text x="18" y="{mt + ph / 2:.0f}" font-size="12" fill="#333" text-anchor="middle" '
        f'transform="rotate(-90 18 {mt + ph / 2:.0f})">mean order cycle time (s)</text>'
    )
    # The two curves.
    parts.extend(polyline_for(lg, legacy_colour))
    parts.extend(polyline_for(md, modern_colour))
    # Recommended crew per regime (ringed on each curve).
    for rec in (report["recommended"]["legacy"], report["recommended"]["modern"]):
        rx, ry = px(rec.n_pickers), py(rec.mean_cycle_s)
        parts.append(
            f'<circle cx="{rx:.1f}" cy="{ry:.1f}" r="7" fill="none" stroke="{ring_colour}" stroke-width="3"/>'
        )
    md_rec = report["recommended"]["modern"]
    rx, ry = px(md_rec.n_pickers), py(md_rec.mean_cycle_s)
    parts.append(
        f'<text x="{rx + 10:.1f}" y="{ry - 8:.1f}" font-size="11" fill="{ring_colour}" font-weight="bold">'
        f"recommended: {md_rec.n_pickers} pickers</text>"
    )
    # Legend.
    lx, ly = ml + pw - 150, mt + 6
    parts.append(f'<line x1="{lx}" y1="{ly}" x2="{lx + 22}" y2="{ly}" stroke="{legacy_colour}" stroke-width="2.5"/>')
    parts.append(f'<text x="{lx + 28}" y="{ly + 4}" font-size="11" fill="#555">legacy</text>')
    parts.append(f'<line x1="{lx}" y1="{ly + 18}" x2="{lx + 22}" y2="{ly + 18}" stroke="{modern_colour}" stroke-width="2.5"/>')
    parts.append(f'<text x="{lx + 28}" y="{ly + 22}" font-size="11" fill="#555">modern</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def render_table(report: dict) -> str:
    """Fixed-width text table of the crew sweep plus the trade-off read."""
    cols = ("regime", "pickers", "orders", "mean s", "p95 s", "makespan s", "travel m", "util %", "cyc -%")
    body = [
        (
            r.regime,
            str(r.n_pickers),
            str(r.orders_completed),
            f"{r.mean_cycle_s:.1f}",
            f"{r.p95_cycle_s:.1f}",
            f"{r.makespan_s:.0f}",
            f"{r.sim_travel_m:.0f}",
            f"{r.utilization_pct:.1f}",
            f"{r.cycle_reduction_pct:.1f}",
        )
        for r in _all_rows(report)
    ]
    widths = [len(c) for c in cols]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(cell.rjust(widths[i]) for i, cell in enumerate(row))

    line = "-" * (sum(widths) + 2 * (len(widths) - 1))
    lg_rec, md_rec = report["recommended"]["legacy"], report["recommended"]["modern"]
    out = [
        "Labour / crew-size sensitivity (synthetic, seeded)",
        line,
        fmt(cols),
        line,
    ]
    out.extend(fmt(row) for row in body)
    out.append(line)
    out.append(
        f"Recommended crew: legacy {lg_rec.n_pickers} pickers "
        f"(-{lg_rec.cycle_reduction_pct:.1f}% cycle time), "
        f"modern {md_rec.n_pickers} pickers (-{md_rec.cycle_reduction_pct:.1f}%)."
    )
    out.append("")
    out.append(report["read"])
    return "\n".join(out)


def _report_to_jsonable(report: dict) -> dict:
    return {
        "seed": report["seed"],
        "n_skus": report["n_skus"],
        "n_orders": report["n_orders"],
        "capture": report["capture"],
        "crew_sizes": report["crew_sizes"],
        "recommended": {k: asdict(v) for k, v in report["recommended"].items()},
        "read": report["read"],
        "by_regime": {k: [asdict(r) for r in v] for k, v in report["by_regime"].items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="logitwin.labour",
        description="Labour / crew-size sensitivity sweep over the number of pickers.",
    )
    parser.add_argument("--json", action="store_true", help="emit the sweep as JSON")
    parser.add_argument("--csv", metavar="FILE", help="write the sweep CSV to FILE")
    parser.add_argument("--svg", metavar="FILE", help="write the staffing-curve SVG to FILE")
    parser.add_argument("--seed", type=int, default=42, help="dataset seed (default 42)")
    parser.add_argument("--n", type=int, default=60, help="number of SKUs (default 60)")
    parser.add_argument("--max-crew", type=int, default=8, help="largest crew size to sweep (default 8)")
    args = parser.parse_args(argv)

    crew_sizes = tuple(range(1, max(1, args.max_crew) + 1))
    report = labour_report(seed=args.seed, n=args.n, crew_sizes=crew_sizes)

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
