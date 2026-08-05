"""Hand-drawn SVG renderers for the engine's slotting output.

Pure-string SVG (no plotting library, no network): the two figures below are drawn straight from the
slotting engine's assignment maps (:mod:`logitwin.slotting`) over the seeded rack geometry
(:mod:`logitwin.data`), so they are real engine output, not decoration. They complement the
frontier chart in :mod:`logitwin.sensitivity` (which plots the effort/saving trade-off); here we draw
the *floor* the engine produces:

* :func:`warehouse_layout_svg` - the velocity-slotted rack map, one cell per storage slot, coloured
  by the ABC velocity class of the SKU the optimizer placed there, with the near-dispatch "golden
  zone" ringed. Reading it: the fast (A) movers cluster into the gold-ringed slots nearest dispatch.
* :func:`slotting_comparison_svg` - the same rack drawn twice, *before* (legacy alphabetical) and
  *after* (velocity-optimized), with the two figures the engine computes between them: golden-zone
  A-occupancy and demand-weighted pick-travel reduction.

Everything is deterministic: the cells come from the seeded assignment and the coordinates are pure
integer grid arithmetic, so a given dataset yields byte-identical SVG across re-runs. All data is
synthetic and teaching-scale (a 60-slot facility); the slotting optimum is exact only for the
one-SKU-per-slot model. Numbers are rendered exactly as the engine computes them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .data import Carton, Warehouse, dataset
from .sensitivity import (
    GOLDEN_ZONE_SLOT_FRACTION,
    _golden_zone_slot_ids,
    golden_zone_occupancy_pct,
)
from .slotting import legacy_slotting, optimize_slotting

# Velocity-class fills match the app's warehouse map exactly (static/app.js VEL_COLOR), so the
# committed figures and the live dashboard read as one system.
_VEL_COLOR = {"A": "#2a9d8f", "B": "#e9a03b", "C": "#6b7a8d", "-": "#33414f"}
_NAVY = "#1f3a5f"
_GREY = "#9aa5b1"
_INK = "#333333"
_GOLD = "#b8860b"  # golden-zone ring; deliberately darker than the amber B fill so they never merge
_DISPATCH = "#2a9d8f"

_CELL = 30  # px per slot
_GAP = 6  # px between slots
_PAD_L = 74  # left margin for aisle labels + the dispatch rail
_PAD_R = 22


def _grid_positions(warehouse: Warehouse) -> tuple[dict[int, int], dict[int, int], int, int]:
    """Map each slot to a (row, col) cell: row = aisle (top-down), col = distance rank (near first).

    Left-most column is the slot nearest dispatch within its aisle - the same "nearest first"
    ordering the live map uses - so the golden zone sits against the dispatch rail on the left.
    Returns ``(row_by_slot, col_by_slot, n_rows, n_cols)``.
    """
    aisles = sorted({s.aisle for s in warehouse.slots})
    row_of = {a: i for i, a in enumerate(aisles)}
    col_of: dict[int, int] = {}
    for a in aisles:
        ranked = sorted((s for s in warehouse.slots if s.aisle == a), key=lambda s: (s.distance, s.id))
        for c, s in enumerate(ranked):
            col_of[s.id] = c
    n_cols = (max(col_of.values()) + 1) if col_of else 0
    return {s.id: row_of[s.aisle] for s in warehouse.slots}, col_of, len(aisles), n_cols


def _rack(
    assignment: dict[str, int],
    cartons: list[Carton],
    warehouse: Warehouse,
    golden: set[int],
    ox: int,
    oy: int,
) -> tuple[list[str], int, int]:
    """One rack grid as SVG fragments; returns (parts, grid_width, grid_height)."""
    velocity = {c.sku: c.velocity for c in cartons}
    occupant = {slot: sku for sku, slot in assignment.items()}
    row_by_slot, col_of, n_rows, n_cols = _grid_positions(warehouse)
    grid_w = n_cols * (_CELL + _GAP) - _GAP
    grid_h = n_rows * (_CELL + _GAP) - _GAP

    parts: list[str] = []
    # Dispatch rail down the left edge: nearer slots are closer to it.
    parts.append(
        f'<line x1="{ox - 12}" y1="{oy - 6}" x2="{ox - 12}" y2="{oy + grid_h + 6}" '
        f'stroke="{_DISPATCH}" stroke-width="2"/>'
    )
    parts.append(
        f'<text x="{ox - 18}" y="{oy + grid_h / 2:.0f}" font-size="10" fill="{_DISPATCH}" '
        f'text-anchor="middle" transform="rotate(-90 {ox - 18} {oy + grid_h / 2:.0f})">DISPATCH</text>'
    )
    # Aisle labels + cells, iterated in slot-id order for deterministic output.
    for s in sorted(warehouse.slots, key=lambda s: s.id):
        r, c = row_by_slot[s.id], col_of[s.id]
        x = ox + c * (_CELL + _GAP)
        y = oy + r * (_CELL + _GAP)
        sku = occupant.get(s.id)
        vel = velocity.get(sku, "-") if sku else "-"
        parts.append(
            f'<rect x="{x}" y="{y}" width="{_CELL}" height="{_CELL}" rx="4" '
            f'fill="{_VEL_COLOR.get(vel, _VEL_COLOR["-"])}"/>'
        )
        if s.id in golden:
            parts.append(
                f'<rect x="{x}" y="{y}" width="{_CELL}" height="{_CELL}" rx="4" fill="none" '
                f'stroke="{_GOLD}" stroke-width="3"/>'
            )
    for row, aisle in enumerate(sorted({s.aisle for s in warehouse.slots})):
        y = oy + row * (_CELL + _GAP) + _CELL / 2 + 4
        parts.append(
            f'<text x="{ox - 26}" y="{y:.0f}" font-size="10" fill="{_GREY}" text-anchor="end">'
            f"A{aisle + 1}</text>"
        )
    return parts, grid_w, grid_h


_LEGEND_H = 40  # two rows: A/B/C fills, then the golden-zone ring


def _legend(x: int, y: int) -> list[str]:
    """Colour key (two rows): A/B/C velocity fills, then the golden-zone ring."""
    parts: list[str] = []
    items = [("A (fast)", _VEL_COLOR["A"]), ("B (medium)", _VEL_COLOR["B"]), ("C (slow)", _VEL_COLOR["C"])]
    cx = x
    for label, color in items:
        parts.append(f'<rect x="{cx}" y="{y}" width="14" height="14" rx="3" fill="{color}"/>')
        parts.append(f'<text x="{cx + 20}" y="{y + 11}" font-size="10" fill="{_INK}">{label}</text>')
        cx += 20 + 8 * len(label) + 16
    y2 = y + 22
    parts.append(f'<rect x="{x}" y="{y2}" width="14" height="14" rx="3" fill="none" stroke="{_GOLD}" stroke-width="3"/>')
    parts.append(
        f'<text x="{x + 20}" y="{y2 + 11}" font-size="10" fill="{_INK}">'
        f"golden zone - the nearest 20% of slots to dispatch</text>"
    )
    return parts


def _svg(width: int, height: int, body: list[str]) -> str:
    head = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif">'
    )
    return "\n".join([head, f'<rect width="{width}" height="{height}" fill="#ffffff"/>', *body, "</svg>"]) + "\n"


def warehouse_layout_svg(
    assignment: dict[str, int],
    cartons: list[Carton],
    warehouse: Warehouse,
    *,
    title: str = "Velocity-slotted warehouse layout (engine output)",
    subtitle: str = "",
) -> str:
    """Render one slotted rack map: cells coloured by ABC class, golden zone ringed."""
    golden = _golden_zone_slot_ids(warehouse, GOLDEN_ZONE_SLOT_FRACTION)
    grid_top = 82
    rack, grid_w, grid_h = _rack(assignment, cartons, warehouse, golden, _PAD_L, grid_top)
    width = _PAD_L + grid_w + _PAD_R
    legend_y = grid_top + grid_h + 26
    height = legend_y + _LEGEND_H

    body: list[str] = []
    body.append(f'<text x="{_PAD_L - 40}" y="30" font-size="16" font-weight="bold" fill="{_NAVY}">{title}</text>')
    sub = subtitle or "Synthetic, seeded 60-slot facility - columns ordered near-dispatch first (left)"
    body.append(f'<text x="{_PAD_L - 40}" y="48" font-size="11" fill="{_GREY}">{sub}</text>')
    body.append(
        f'<text x="{_PAD_L - 40}" y="66" font-size="11" fill="{_INK}">'
        f"A-movers pulled into the gold-ringed slots nearest dispatch</text>"
    )
    body.extend(rack)
    body.extend(_legend(_PAD_L - 40, legend_y))
    return _svg(width, height, body)


def slotting_comparison_svg(cartons: list[Carton], warehouse: Warehouse) -> str:
    """Render the before/after rack maps (legacy vs velocity-optimized) with the engine's deltas."""
    legacy = legacy_slotting(cartons, warehouse)
    optimized = optimize_slotting(cartons, warehouse, prefer=legacy.assignment)
    reduction = (
        100.0 * (legacy.total_travel - optimized.total_travel) / legacy.total_travel
        if legacy.total_travel > 0
        else 0.0
    )
    occ_before = golden_zone_occupancy_pct(legacy.assignment, cartons, warehouse)
    occ_after = golden_zone_occupancy_pct(optimized.assignment, cartons, warehouse)
    golden = _golden_zone_slot_ids(warehouse, GOLDEN_ZONE_SLOT_FRACTION)

    left = _PAD_L
    tx = left - 40  # text left edge, aligned under the title
    top_a = 92
    rack_a, grid_w, grid_h = _rack(legacy.assignment, cartons, warehouse, golden, left, top_a)
    strip_y = top_a + grid_h + 24  # first delta line
    top_b = strip_y + 52  # room for the two delta lines + the AFTER label
    rack_b, _, _ = _rack(optimized.assignment, cartons, warehouse, golden, left, top_b)
    width = left + grid_w + _PAD_R
    legend_y = top_b + grid_h + 26
    height = legend_y + _LEGEND_H

    body: list[str] = []
    body.append(
        f'<text x="{tx}" y="30" font-size="16" font-weight="bold" fill="{_NAVY}">'
        f"Slotting: before vs after (engine output)</text>"
    )
    body.append(
        f'<text x="{tx}" y="48" font-size="11" fill="{_GREY}">'
        f"Synthetic, seeded 60-slot facility - one-SKU-per-slot model, deterministic</text>"
    )
    # Before rack.
    body.append(f'<text x="{tx}" y="{top_a - 12}" font-size="12" font-weight="bold" fill="{_INK}">BEFORE - legacy (alphabetical) slotting</text>')
    body.extend(rack_a)
    # Delta strip between the two racks (two lines so nothing clips the canvas edge).
    body.append(
        f'<text x="{tx}" y="{strip_y}" font-size="12" fill="{_NAVY}">'
        f"Golden-zone A-occupancy: {occ_before:.0f}% &#8594; {occ_after:.0f}%</text>"
    )
    body.append(
        f'<text x="{tx}" y="{strip_y + 18}" font-size="12" fill="{_NAVY}">'
        f"Demand-weighted pick travel: &#8722;{reduction:.1f}%</text>"
    )
    # After rack.
    body.append(f'<text x="{tx}" y="{top_b - 12}" font-size="12" font-weight="bold" fill="{_INK}">AFTER - velocity-optimized slotting</text>')
    body.extend(rack_b)
    body.extend(_legend(tx, legend_y))
    return _svg(width, height, body)


def canonical_figures(seed: int = 42, n: int = 60) -> dict[str, str]:
    """The committed figures, built from one seeded dataset. Keys are the file stems under docs/img."""
    ds = dataset(seed=seed, n=n)
    cartons, warehouse = ds["cartons"], ds["warehouse"]
    legacy = legacy_slotting(cartons, warehouse)
    optimized = optimize_slotting(cartons, warehouse, prefer=legacy.assignment)
    return {
        "warehouse_layout": warehouse_layout_svg(optimized.assignment, cartons, warehouse),
        "slotting_before_after": slotting_comparison_svg(cartons, warehouse),
    }


def write_figures(out_dir: Path, seed: int = 42, n: int = 60) -> list[Path]:
    """Write the canonical figures as SVG files into ``out_dir`` and return the paths written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for stem, svg in canonical_figures(seed=seed, n=n).items():
        path = out_dir / f"{stem}.svg"
        path.write_text(svg, encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="logitwin.render",
        description="Render the engine's slotting output as deterministic, committed SVG figures.",
    )
    parser.add_argument(
        "--out-dir",
        default="docs/img",
        help="directory to write the SVG figures into (default docs/img)",
    )
    parser.add_argument("--seed", type=int, default=42, help="dataset seed (default 42)")
    parser.add_argument("--n", type=int, default=60, help="number of slots/SKUs (default 60)")
    args = parser.parse_args(argv)
    for path in write_figures(Path(args.out_dir), seed=args.seed, n=args.n):
        print(f"[ok] wrote {path} ({path.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
