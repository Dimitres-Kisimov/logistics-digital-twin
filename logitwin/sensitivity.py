"""Slotting / pick-travel sensitivity: how far to push a velocity re-slot.

This is an honest "what-if" layer over the *existing* engine. It sweeps ONE operational lever - the
fraction of SKUs (highest daily demand first) you commit to re-slotting toward the near-dispatch
"golden zone" - and reuses the slotting optimizer (:mod:`logitwin.slotting`) and the discrete-event
simulation (:mod:`logitwin.simulate`) unchanged to measure how three things respond:

* **average pick travel** - demand-weighted metres per pick, the same measure the slotting report
  minimises;
* **golden-zone occupancy** - the share of the nearest slots that end up holding A-class (fast)
  movers;
* **throughput** - mean order cycle time and simulated picker travel read off the real simulation
  under a fixed batched-pick regime (only the slotting varies across the sweep). Note: with batched
  picking the single picker is under-loaded here, so the *completion rate* (orders/hour) is
  arrival-bound and barely moves; slotting shows up as responsiveness (cycle time) and picker
  travel, not as a higher order-completion rate - which is itself an honest, reported finding.

The lever, precisely
---------------------
For a fraction ``f`` we take the top ``k = round(f * N)`` SKUs by daily demand and re-slot *only*
those, optimally, among the slots those SKUs currently occupy (the Hungarian solver on that
sub-instance, tie-broken toward staying put exactly as the main report does). The remaining
``N - k`` SKUs are left in their legacy (arbitrary/alphabetical) positions. So:

* ``f = 0`` is the untouched legacy layout;
* ``f = 1`` lets every SKU participate, which is the full one-SKU-per-slot optimum;
* in between, committing more SKUs can only lower (or hold) the travel, so travel-vs-effort is a
  genuine efficient frontier and the "recommended" point is its knee.

This is deliberately a *policy-depth* lever ("how many SKUs do we bother to re-slot?"), distinct
from the dashboard's what-if slider, which executes a growing prefix of the *full* plan's move
sequence. Both bottom out at the same optimum; this one is indexed by an ABC cutoff and also reads
throughput and golden-zone occupancy off each layout.

Everything is synthetic, seeded and deterministic: a given dataset always yields byte-identical
tables, CSV and SVG. Numbers are teaching-scale (a 60-SKU synthetic facility), reported exactly as
the code computes them. Slotting here is the exact optimum only for the one-SKU-per-slot model; real
slotting adds capacity, family and congestion constraints this omits.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .data import VELOCITY_DEMAND, Carton, Warehouse, dataset
from .simulate import simulate_assignment
from .slotting import legacy_slotting, optimize_slotting

# The golden zone is the nearest slots to dispatch. We size it at the classic A-class share (~20%),
# so "golden-zone occupancy" reads as: of the prime near-dispatch slots, what share hold A-movers.
GOLDEN_ZONE_SLOT_FRACTION = 0.20

# Default lever grid: 0%, 10%, ... 100% of SKUs committed to the re-slot.
DEFAULT_FRACTIONS: tuple[float, ...] = tuple(round(0.1 * i, 1) for i in range(11))

# Fraction of the full re-slot's travel saving the recommended operating point must capture.
DEFAULT_CAPTURE = 0.90


@dataclass(frozen=True)
class SensitivityRow:
    """One operating point on the sweep (all figures deterministic for a given dataset)."""

    promote_fraction: float
    n_promoted: int
    n_moves: int
    total_travel: float  # demand-weighted unit-m/day
    avg_travel_per_pick: float  # metres per pick (demand-weighted)
    travel_reduction_pct: float  # vs the legacy layout
    golden_zone_occupancy_pct: float  # share of nearest slots holding A-movers
    mean_cycle_s: float  # simulated mean order cycle time (responsiveness)
    sim_travel_m: float  # simulated picker travel over the shift (batched-pick regime)


def _demand(cartons: list[Carton]) -> dict[str, float]:
    """Daily pick demand per SKU, from its ABC velocity class (same model as the slotting report)."""
    return {c.sku: VELOCITY_DEMAND[c.velocity] for c in cartons}


def _travel(assignment: dict[str, int], demand: dict[str, float], dist: dict[int, float]) -> float:
    """Demand-weighted daily pick travel for a slotting (the slotting objective's own measure)."""
    return sum(demand[sku] * dist[slot] for sku, slot in assignment.items())


def _rank_by_demand(cartons: list[Carton], skus: list[str]) -> list[str]:
    """SKUs ordered fastest-mover first; ties broken by SKU id so the order is deterministic."""
    demand = _demand(cartons)
    return sorted(skus, key=lambda s: (-demand[s], s))


def partial_reslot(
    cartons: list[Carton],
    warehouse: Warehouse,
    promote_fraction: float,
) -> dict[str, int]:
    """Re-slot only the top ``promote_fraction`` of SKUs (by demand); leave the rest in place.

    The promoted SKUs are optimally re-assigned among the slots they already occupy, via the same
    Hungarian solver (:func:`logitwin.slotting.optimize_slotting`) the main report uses, tie-broken
    toward keeping each SKU where it is. Reusing the engine's own sub-optimum keeps the layout
    honest - no bespoke heuristic is introduced here.
    """
    legacy = legacy_slotting(cartons, warehouse).assignment
    ranked = _rank_by_demand(cartons, list(legacy.keys()))
    k = max(0, min(len(ranked), round(promote_fraction * len(ranked))))
    promoted = set(ranked[:k])

    result = dict(legacy)
    if k == 0:
        return result

    pool_slots = {legacy[sku] for sku in promoted}
    sub_cartons = [c for c in cartons if c.sku in promoted]
    sub_warehouse = Warehouse(slots=[s for s in warehouse.slots if s.id in pool_slots])
    prefer = {sku: legacy[sku] for sku in promoted}
    sub = optimize_slotting(sub_cartons, sub_warehouse, prefer=prefer).assignment
    result.update(sub)
    return result


def _golden_zone_slot_ids(warehouse: Warehouse, zone_fraction: float) -> set[int]:
    """The ids of the nearest ``zone_fraction`` of slots to dispatch (the golden zone)."""
    ordered = sorted(warehouse.slots, key=lambda s: (s.distance, s.id))
    g = max(1, round(zone_fraction * len(ordered)))
    return {s.id for s in ordered[:g]}


def golden_zone_occupancy_pct(
    assignment: dict[str, int],
    cartons: list[Carton],
    warehouse: Warehouse,
    zone_fraction: float = GOLDEN_ZONE_SLOT_FRACTION,
) -> float:
    """Percentage of golden-zone slots occupied by A-class (fast-mover) SKUs under ``assignment``."""
    zone = _golden_zone_slot_ids(warehouse, zone_fraction)
    if not zone:
        return 0.0
    velocity = {c.sku: c.velocity for c in cartons}
    slot_to_sku = {slot: sku for sku, slot in assignment.items()}
    a_in_zone = sum(1 for slot in zone if velocity.get(slot_to_sku.get(slot, ""), "") == "A")
    return 100.0 * a_in_zone / len(zone)


def sweep(
    cartons: list[Carton],
    warehouse: Warehouse,
    orders: list,
    fractions: tuple[float, ...] = DEFAULT_FRACTIONS,
    zone_fraction: float = GOLDEN_ZONE_SLOT_FRACTION,
) -> list[SensitivityRow]:
    """Evaluate every lever setting and return the sensitivity rows (deterministic)."""
    demand = _demand(cartons)
    dist = {s.id: s.distance for s in warehouse.slots}
    total_picks = sum(demand.values())
    legacy = legacy_slotting(cartons, warehouse).assignment
    legacy_travel = _travel(legacy, demand, dist)

    rows: list[SensitivityRow] = []
    for f in fractions:
        assignment = partial_reslot(cartons, warehouse, f)
        travel = _travel(assignment, demand, dist)
        n_moves = sum(1 for sku, slot in assignment.items() if legacy[sku] != slot)
        reduction = 100.0 * (legacy_travel - travel) / legacy_travel if legacy_travel > 0 else 0.0
        occ = golden_zone_occupancy_pct(assignment, cartons, warehouse, zone_fraction)
        sim = simulate_assignment(assignment, cartons, warehouse, orders)
        rows.append(
            SensitivityRow(
                promote_fraction=round(f, 4),
                n_promoted=max(0, min(len(legacy), round(f * len(legacy)))),
                n_moves=n_moves,
                total_travel=round(travel, 2),
                avg_travel_per_pick=round(travel / total_picks, 4) if total_picks > 0 else 0.0,
                travel_reduction_pct=round(reduction, 2),
                golden_zone_occupancy_pct=round(occ, 2),
                mean_cycle_s=round(sim.mean_cycle_time_s, 2),
                sim_travel_m=round(sim.total_travel_m, 1),
            )
        )
    return rows


def recommended_operating_point(
    rows: list[SensitivityRow],
    capture: float = DEFAULT_CAPTURE,
) -> SensitivityRow:
    """The knee of the frontier: the smallest lever that banks ``capture`` of the full saving.

    The full saving is the travel reduction at the most aggressive lever evaluated. We return the
    first (smallest-effort) row whose reduction reaches ``capture`` of it; if none does (e.g. the
    saving is zero), the least-aggressive row is returned.
    """
    if not rows:
        raise ValueError("no rows to choose from")
    best_reduction = max(r.travel_reduction_pct for r in rows)
    target = capture * best_reduction
    for r in sorted(rows, key=lambda r: r.promote_fraction):
        if r.travel_reduction_pct >= target:
            return r
    return min(rows, key=lambda r: r.promote_fraction)


def frontier_report(
    cartons: list[Carton] | None = None,
    warehouse: Warehouse | None = None,
    orders: list | None = None,
    *,
    seed: int = 42,
    n: int = 60,
    fractions: tuple[float, ...] = DEFAULT_FRACTIONS,
    capture: float = DEFAULT_CAPTURE,
    zone_fraction: float = GOLDEN_ZONE_SLOT_FRACTION,
) -> dict:
    """Run the full sweep on a (synthetic, seeded) dataset and bundle the frontier + read."""
    if cartons is None or warehouse is None or orders is None:
        ds = dataset(seed=seed, n=n)
        cartons, warehouse, orders = ds["cartons"], ds["warehouse"], ds["orders"]

    rows = sweep(cartons, warehouse, orders, fractions=fractions, zone_fraction=zone_fraction)
    rec = recommended_operating_point(rows, capture=capture)
    first, last = rows[0], rows[-1]
    return {
        "seed": seed,
        "n_skus": len(cartons),
        "zone_fraction": zone_fraction,
        "capture": capture,
        "rows": rows,
        "legacy": first,
        "full_reslot": last,
        "recommended": rec,
        "read": trade_off_read(first, last, rec, capture),
    }


def trade_off_read(
    legacy: SensitivityRow,
    full: SensitivityRow,
    rec: SensitivityRow,
    capture: float,
) -> str:
    """One-paragraph plain-language read of the trade-off, with numbers straight from the sweep."""
    extra_moves = full.n_moves - rec.n_moves
    extra_pct = round(full.travel_reduction_pct - rec.travel_reduction_pct, 2)
    return (
        f"Committing the top {rec.n_promoted} of {legacy.n_promoted or full.n_promoted} SKUs "
        f"({rec.n_moves} moves) captures {rec.travel_reduction_pct:.1f}% pick-travel reduction - "
        f"{capture * 100:.0f}%+ of the full re-slot's {full.travel_reduction_pct:.1f}%. "
        f"Going all the way to 100% adds {extra_moves} more moves for only {extra_pct:.1f} more "
        f"points. Across that same push, golden-zone A-mover occupancy rises "
        f"{legacy.golden_zone_occupancy_pct:.0f}% -> {full.golden_zone_occupancy_pct:.0f}% and mean "
        f"order cycle time falls {legacy.mean_cycle_s:.0f}s -> {full.mean_cycle_s:.0f}s. "
        f"(Synthetic, seeded 60-SKU facility; one-SKU-per-slot model.)"
    )


# --- serialisation ------------------------------------------------------------------------------

_CSV_HEADER = (
    "promote_fraction",
    "n_promoted",
    "n_moves",
    "total_travel_unit_m_day",
    "avg_travel_m_per_pick",
    "travel_reduction_pct",
    "golden_zone_occupancy_pct",
    "mean_cycle_s",
    "sim_travel_m",
)


def to_csv(rows: list[SensitivityRow]) -> str:
    """Deterministic CSV of the sweep (fixed formatting, trailing newline)."""
    lines = [",".join(_CSV_HEADER)]
    for r in rows:
        lines.append(
            ",".join(
                (
                    f"{r.promote_fraction:.1f}",
                    str(r.n_promoted),
                    str(r.n_moves),
                    f"{r.total_travel:.2f}",
                    f"{r.avg_travel_per_pick:.4f}",
                    f"{r.travel_reduction_pct:.2f}",
                    f"{r.golden_zone_occupancy_pct:.2f}",
                    f"{r.mean_cycle_s:.2f}",
                    f"{r.sim_travel_m:.1f}",
                )
            )
        )
    return "\n".join(lines) + "\n"


def to_svg(report: dict) -> str:
    """Hand-drawn (pure-string, no plotting library) SVG of the efficient frontier.

    X = SKUs committed to the re-slot (moves/effort); Y = pick-travel reduction (%). The recommended
    knee is ringed. Deterministic: every coordinate is derived from the seeded sweep.
    """
    rows: list[SensitivityRow] = report["rows"]
    rec: SensitivityRow = report["recommended"]

    w, h = 720, 440
    ml, mr, mt, mb = 70, 30, 60, 70
    pw, ph = w - ml - mr, h - mt - mb

    max_moves = max((r.n_moves for r in rows), default=1) or 1
    max_red = max((r.travel_reduction_pct for r in rows), default=1.0) or 1.0

    def px(moves: float) -> float:
        return ml + pw * (moves / max_moves)

    def py(red: float) -> float:
        return mt + ph * (1.0 - red / max_red)

    pts = [(px(r.n_moves), py(r.travel_reduction_pct)) for r in rows]
    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" font-family="sans-serif">'
    )
    parts.append(f'<rect width="{w}" height="{h}" fill="#ffffff"/>')
    parts.append(
        f'<text x="{ml}" y="30" font-size="17" font-weight="bold" fill="#1f3a5f">'
        f"Slotting sensitivity - pick-travel reduction vs re-slot effort</text>"
    )
    parts.append(
        f'<text x="{ml}" y="48" font-size="11" fill="#9aa5b1">'
        f"Synthetic, seeded {report['n_skus']}-SKU facility - deterministic</text>"
    )
    # Axes.
    parts.append(
        f'<line x1="{ml}" y1="{mt + ph}" x2="{ml + pw}" y2="{mt + ph}" stroke="#333" stroke-width="1"/>'
    )
    parts.append(f'<line x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt + ph}" stroke="#333" stroke-width="1"/>')
    # Y gridlines / ticks (0 .. max_red).
    for i in range(5):
        red = max_red * i / 4
        y = py(red)
        parts.append(
            f'<line x1="{ml}" y1="{y:.1f}" x2="{ml + pw}" y2="{y:.1f}" stroke="#eee" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{ml - 8}" y="{y + 4:.1f}" font-size="10" fill="#555" text-anchor="end">'
            f"{red:.0f}%</text>"
        )
    # X ticks (0 .. max_moves).
    for i in range(5):
        moves = max_moves * i / 4
        x = px(moves)
        parts.append(
            f'<text x="{x:.1f}" y="{mt + ph + 18:.1f}" font-size="10" fill="#555" text-anchor="middle">'
            f"{moves:.0f}</text>"
        )
    parts.append(
        f'<text x="{ml + pw / 2:.0f}" y="{h - 24}" font-size="12" fill="#333" text-anchor="middle">'
        f"SKUs re-slotted (moves / effort)</text>"
    )
    parts.append(
        f'<text x="18" y="{mt + ph / 2:.0f}" font-size="12" fill="#333" text-anchor="middle" '
        f'transform="rotate(-90 18 {mt + ph / 2:.0f})">pick-travel reduction (%)</text>'
    )
    # The frontier.
    parts.append(f'<polyline points="{polyline}" fill="none" stroke="#2a9d8f" stroke-width="2.5"/>')
    for x, y in pts:
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="#1f3a5f"/>')
    # Recommended knee.
    rx, ry = px(rec.n_moves), py(rec.travel_reduction_pct)
    parts.append(f'<circle cx="{rx:.1f}" cy="{ry:.1f}" r="7" fill="none" stroke="#e9a03b" stroke-width="3"/>')
    parts.append(
        f'<text x="{rx + 10:.1f}" y="{ry - 8:.1f}" font-size="11" fill="#e9a03b" font-weight="bold">'
        f"recommended: {rec.n_moves} moves, -{rec.travel_reduction_pct:.1f}%</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def render_table(report: dict) -> str:
    """Fixed-width text table of the sweep plus the trade-off read."""
    cols = ("f", "SKUs", "moves", "travel(u-m/d)", "m/pick", "travel -%", "goldZ%A", "cycle s", "sim m")
    rows: list[SensitivityRow] = report["rows"]
    body = [
        (
            f"{r.promote_fraction:.1f}",
            str(r.n_promoted),
            str(r.n_moves),
            f"{r.total_travel:.1f}",
            f"{r.avg_travel_per_pick:.3f}",
            f"{r.travel_reduction_pct:.1f}",
            f"{r.golden_zone_occupancy_pct:.0f}",
            f"{r.mean_cycle_s:.1f}",
            f"{r.sim_travel_m:.0f}",
        )
        for r in rows
    ]
    widths = [len(c) for c in cols]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(cell.rjust(widths[i]) for i, cell in enumerate(row))

    line = "-" * (sum(widths) + 2 * (len(widths) - 1))
    rec: SensitivityRow = report["recommended"]
    out = [
        "Slotting / pick-travel sensitivity (synthetic, seeded)",
        line,
        fmt(cols),
        line,
    ]
    out.extend(fmt(row) for row in body)
    out.append(line)
    out.append(f"Recommended operating point: f={rec.promote_fraction:.1f} "
               f"({rec.n_promoted} SKUs, {rec.n_moves} moves) -> -{rec.travel_reduction_pct:.1f}% travel.")
    out.append("")
    out.append(report["read"])
    return "\n".join(out)


def _report_to_jsonable(report: dict) -> dict:
    from dataclasses import asdict

    return {
        "seed": report["seed"],
        "n_skus": report["n_skus"],
        "zone_fraction": report["zone_fraction"],
        "capture": report["capture"],
        "recommended": asdict(report["recommended"]),
        "read": report["read"],
        "rows": [asdict(r) for r in report["rows"]],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="logitwin.sensitivity",
        description="Slotting / pick-travel sensitivity sweep over the re-slot depth lever.",
    )
    parser.add_argument("--json", action="store_true", help="emit the sweep as JSON")
    parser.add_argument("--csv", metavar="FILE", help="write the sweep CSV to FILE")
    parser.add_argument("--svg", metavar="FILE", help="write the frontier SVG to FILE")
    parser.add_argument("--seed", type=int, default=42, help="dataset seed (default 42)")
    parser.add_argument("--n", type=int, default=60, help="number of SKUs (default 60)")
    args = parser.parse_args(argv)

    report = frontier_report(seed=args.seed, n=args.n)

    if args.csv:
        Path(args.csv).write_text(to_csv(report["rows"]), encoding="utf-8")
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
