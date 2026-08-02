"""Procedural plant-layout generator (WarehouseTwin-compatible).

Given a *plant profile* (the kind of distribution operation) and a floor size, this module
lays out a compliant warehouse floor - docks, staging, racking rows and automation lanes - and
emits it in the SHARED ``wt-1`` interchange (see :mod:`logitwin.layout`), so a plant generated
here loads straight into the WarehouseTwin browser app, and a layout drawn in the app can be
analysed by the engine. One format, two tools.

WHAT THIS IS (and is not)
-------------------------
This is a RULE / HEURISTIC generator, informed by warehouse-design best practice - **not** a
trained model and not an optimiser. It places elements by deterministic rules (band layout,
proportional zone mix, aisle-width arithmetic) seeded by ``seed``, so a given
``(profile, grid, seed)`` always yields a byte-identical layout, and different seeds vary the
zone arrangement and row insets. Every generated layout is passed back through
:func:`~logitwin.layout.load_layout` with ``reject_overlaps=True`` before it is returned, so the
output is guaranteed to be schema-valid, in-bounds, overlap-free and aisle-compliant, or the call
raises rather than emitting a bad layout.

HONEST ASSUMPTIONS / WHAT IT DOES **NOT** MODEL
-----------------------------------------------
The generator reasons only about 2D footprint geometry on a fixed 1 m grid. It does NOT model:
  * real aisle routing, traffic, one-way flow or cross-aisles beyond the single transport spine;
  * rack-internal bay/level structure, true rack depths (a single ``rack_depth`` is assumed per
    profile) or the gravity/FIFO semantics of flow racks;
  * SKUs, demand, velocity classes or throughput - so it computes NO slotting or labour numbers
    (pair the result with :func:`logitwin.layout.slotting_demo` for a seeded-synthetic demo);
  * building constraints (structural column grid, fire egress, sprinkler/ESFR, refrigeration
    zoning, seismic/floor-load ratings, dock levellers or the yard);
  * multi-level mezzanines (the floor plan is a single storey) or automation control logic.
The per-profile numbers in :data:`PLANT_PROFILES` (aisle widths, dock counts, zone mix, rack
depth, automation) are documented best-practice ASSUMPTIONS, not measurements from a real site.

CLI (matches the ``python -m logitwin.layout`` style)::

    python -m logitwin.generate --profile spare-parts-distribution --seed 42
    python -m logitwin.generate --profile cold-chain --grid-w 48 --grid-h 28 --json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from . import SEED
from .layout import SCHEMA, Layout, analyze_layout, dump_layout, load_layout

# Fixed floor geometry (WarehouseTwin uses METRES_PER_CELL = 1.0; one cell is one metre).
CELL_M = 1.0
DEFAULT_GRID_W = 40
DEFAULT_GRID_H = 24

# Band depths / margins in grid cells (see the band layout in :func:`generate_layout`).
SIDE_MARGIN = 1  # cells kept clear of the left/right walls
DOCK_DEPTH = 1
DOCK_W = 2
STAGE_DEPTH = 2
INSET_MAX = 2  # max seed-chosen horizontal indent applied to a rack row (organic variation)
MIN_ROW_W = 4  # a rack row is never narrower than this many cells


@dataclass(frozen=True)
class PlantProfile:
    """A documented plant archetype: the design ASSUMPTIONS the generator places to.

    Every field is a best-practice assumption for the archetype, not a measurement. ``zone_mix``
    is an ordered list of ``(racking_type, share)`` pairs (shares are normalised); each becomes a
    contiguous block of rack rows sized in proportion, so the floor reads as zones. ``automation``
    is the transport-lane types woven between rows (``conveyor`` and/or ``rgv``).
    """

    key: str
    label: str
    description: str
    zone_mix: tuple[tuple[str, float], ...]
    min_aisle_m: float
    dock_count: int
    rack_depth: int  # cells; a single fixed double-sided rack-block depth per profile (assumption)
    automation: tuple[str, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)


# The FOUR shared plant-profile keys - pinned EXACTLY to interoperate with the WarehouseTwin app.
PLANT_PROFILES: dict[str, PlantProfile] = {
    "ecommerce-fulfilment": PlantProfile(
        key="ecommerce-fulfilment",
        label="E-commerce fulfilment centre",
        description="High-SKU, small-item pick operation: dense pick faces feeding a conveyor "
        "sortation spine, with RGV shuttles to pack-out.",
        zone_mix=(("carton-flow", 0.40), ("shuttle", 0.25), ("selective-racking", 0.35)),
        min_aisle_m=2.4,  # narrow-aisle order-pickers / goods-to-person shuttles
        dock_count=6,
        rack_depth=2,
        automation=("conveyor", "rgv"),
        notes=(
            "aisle 2.4 m assumes narrow-aisle order-picker / shuttle access, not counterbalance",
            "carton-flow + shuttle zones assume goods-to-person pick faces; reserve is selective",
        ),
    ),
    "spare-parts-distribution": PlantProfile(
        key="spare-parts-distribution",
        label="Spare-parts distribution centre",
        description="Very high SKU count, low units per line: mobile and carton-flow racking for "
        "small parts with a pick-to-conveyor line.",
        zone_mix=(("carton-flow", 0.35), ("mobile-racking", 0.30), ("selective-racking", 0.35)),
        min_aisle_m=2.2,  # small-parts pick trucks; mobile racking closes aisles when idle
        dock_count=4,
        rack_depth=2,
        automation=("conveyor",),
        notes=(
            "aisle 2.2 m assumes small-parts picking trucks; mobile-racking aisle is nominal",
            "mobile-racking density is treated as static here (no open/closed aisle dynamics)",
        ),
    ),
    "automotive-supply": PlantProfile(
        key="automotive-supply",
        label="Automotive supply / JIT sequencing",
        description="Line-feed and sequencing operation: selective and cantilever racking with "
        "FIFO pallet-flow lanes served by RGV/AGV sequencing.",
        zone_mix=(("selective-racking", 0.40), ("pallet-flow", 0.30), ("cantilever", 0.30)),
        min_aisle_m=3.5,  # counterbalance trucks handling long / heavy loads and cantilever stock
        dock_count=4,
        rack_depth=3,
        automation=("rgv",),
        notes=(
            "aisle 3.5 m assumes counterbalance trucks for long/heavy loads and cantilever access",
            "RGV lane models a sequencing/line-feed transport corridor, not the AGV control logic",
        ),
    ),
    "cold-chain": PlantProfile(
        key="cold-chain",
        label="Cold-chain / freezer distribution",
        description="Cube-maximising freezer store: deep drive-in, mobile and double-deep blocks "
        "with an RGV lane so travel through the cold zone is unmanned.",
        zone_mix=(("drive-in", 0.40), ("mobile-racking", 0.30), ("double-deep", 0.30)),
        min_aisle_m=2.9,  # reach trucks in a freezer; deep blocks minimise open aisle length
        dock_count=3,
        rack_depth=4,
        automation=("rgv",),
        notes=(
            "aisle 2.9 m assumes reach trucks; deep drive-in blocks trade selectivity for cube",
            "refrigeration/insulation zoning and airlocks are NOT modelled - geometry only",
        ),
    ),
}

# Public tuple of the shared profile keys (CLI choices, tests).
PROFILE_KEYS: tuple[str, ...] = tuple(PLANT_PROFILES)


class GenerationError(ValueError):
    """A user-facing problem generating a plant (unknown profile, or a grid too small to lay out)."""


def get_profile(profile_key: str) -> PlantProfile:
    """Look up a :class:`PlantProfile` by key, raising :class:`GenerationError` for an unknown key."""
    try:
        return PLANT_PROFILES[profile_key]
    except KeyError:
        raise GenerationError(
            f"unknown plant profile {profile_key!r}; expected one of {', '.join(PROFILE_KEYS)}"
        ) from None


# --------------------------------------------------------------------------- geometry helpers


def _aisle_cells(min_aisle_m: float, cell: float) -> int:
    """Smallest whole-cell aisle width whose metric width satisfies ``min_aisle_m`` (gap > 0)."""
    n = max(1, math.ceil(min_aisle_m / cell - 1e-9))
    while n * cell < min_aisle_m - 1e-9:
        n += 1
    return n


def _largest_remainder(weights: list[float], total: int) -> list[int]:
    """Apportion ``total`` integer rows across ``weights`` (largest-remainder / Hamilton method)."""
    s = sum(weights)
    if s <= 0 or total <= 0:
        return [0 for _ in weights]
    raw = [w / s * total for w in weights]
    base = [int(math.floor(r)) for r in raw]
    rem = total - sum(base)
    order = sorted(range(len(weights)), key=lambda i: (raw[i] - base[i], -i), reverse=True)
    for k in range(rem):
        base[order[k % len(order)]] += 1
    return base


def _spread(count: int, x0: int, x1: int, item_w: int) -> list[int]:
    """Evenly spread ``count`` items of width ``item_w`` in ``[x0, x1)``; guaranteed non-overlapping."""
    usable = x1 - x0
    count = max(1, min(count, usable // item_w))
    free = usable - count * item_w
    gap = free // (count + 1)
    xs: list[int] = []
    x = x0 + gap
    for _ in range(count):
        xs.append(x)
        x += item_w + gap
    return xs


def _transport_gap_slots(interior: int, n_lanes: int) -> list[int]:
    """Pick ``n_lanes`` distinct interior-gap indices to host transport lanes, spread evenly."""
    slots: list[int] = []
    for t in range(n_lanes):
        idx = int(round((t + 1) * interior / (n_lanes + 1))) - 1
        idx = max(0, min(interior - 1, idx))
        while idx in slots:
            idx = (idx + 1) % interior
        slots.append(idx)
    return slots


# --------------------------------------------------------------------------- generator


def generate_layout(
    profile_key: str,
    *,
    grid_w: int = DEFAULT_GRID_W,
    grid_h: int = DEFAULT_GRID_H,
    seed: int = SEED,
) -> Layout:
    """Deterministically generate a compliant plant :class:`~logitwin.layout.Layout`.

    Lays out, top to bottom in ``y``: a row of inbound docks, an inbound staging band, a storage
    region of full-width rack rows separated by aisles (and the profile's transport lanes), an
    outbound staging band, and a row of outbound docks. Rack rows are grouped into contiguous zones
    per :attr:`PlantProfile.zone_mix`; adjacent rack rows are always separated by at least the
    profile's minimum aisle width, so the layout passes the aisle guard. ``seed`` varies the zone
    arrangement and per-row horizontal insets; the same arguments always yield the same layout.

    The result is validated (schema, bounds, no overlaps) before return. Raises
    :class:`GenerationError` for an unknown profile or a grid too small to lay out.
    """
    profile = get_profile(profile_key)
    if grid_w < 1 or grid_h < 1:
        raise GenerationError(f"grid must be at least 1x1 (got {grid_w}x{grid_h})")

    cell = CELL_M
    usable_w = grid_w - 2 * SIDE_MARGIN
    if usable_w < max(MIN_ROW_W, DOCK_W):
        raise GenerationError(
            f"grid width {grid_w} is too small; need at least {2 * SIDE_MARGIN + max(MIN_ROW_W, DOCK_W)} cells"
        )

    aisle = _aisle_cells(profile.min_aisle_m, cell)
    transport_depth = max(aisle, 2)
    rack_depth = profile.rack_depth

    storage_top = DOCK_DEPTH + STAGE_DEPTH
    storage_bottom = grid_h - DOCK_DEPTH - STAGE_DEPTH
    avail = storage_bottom - storage_top
    if avail < rack_depth:
        need = 2 * (DOCK_DEPTH + STAGE_DEPTH) + rack_depth
        raise GenerationError(
            f"grid height {grid_h} is too small for profile {profile.key!r}; "
            f"need at least {need} cells (docks + staging + one {rack_depth}-cell rack row)"
        )

    # Largest rack-row count R that fits, given each of ``automation`` becomes one transport lane
    # (in place of an aisle) and the rest of the between-row gaps are plain aisles.
    n_auto = len(profile.automation)
    best_rows, n_transport = 1, 0
    max_rows = avail // rack_depth + 1
    for rows in range(max_rows, 0, -1):
        t = min(n_auto, max(0, rows - 1))
        used = rows * rack_depth + (rows - 1 - t) * aisle + t * transport_depth
        if used <= avail:
            best_rows, n_transport = rows, t
            break

    rng = np.random.default_rng(seed)

    # Assign a racking type to each rack row: contiguous zone blocks sized by share, block order
    # shuffled by the seed so different seeds arrange the zones differently.
    shares = [s for _, s in profile.zone_mix]
    counts = _largest_remainder(shares, best_rows)
    zone_order = rng.permutation(len(profile.zone_mix))
    row_types: list[str] = []
    for zi in zone_order:
        row_types.extend([profile.zone_mix[zi][0]] * counts[zi])
    # Guard against rounding leaving a short/long list (shouldn't happen, but stay exact).
    row_types = (row_types + [profile.zone_mix[0][0]] * best_rows)[:best_rows]

    interior = best_rows - 1
    transport_at = {}
    if interior > 0 and n_transport > 0:
        for slot, lane in zip(
            _transport_gap_slots(interior, n_transport), profile.automation, strict=False
        ):
            transport_at[slot] = lane

    inset_max = max(0, min(INSET_MAX, (usable_w - MIN_ROW_W) // 2))

    elements: list[dict[str, Any]] = []
    counter = 0

    def _add(el_type: str, x: int, y: int, w: int, d: int) -> None:
        nonlocal counter
        counter += 1
        elements.append({"id": f"el-{counter}", "type": el_type, "x": x, "y": y, "w": w, "d": d})

    # Inbound docks (top edge) and outbound docks (bottom edge); at least one of each.
    dock_out_n = max(1, profile.dock_count // 2)
    dock_in_n = max(1, profile.dock_count - dock_out_n)
    for x in _spread(dock_in_n, SIDE_MARGIN, grid_w - SIDE_MARGIN, DOCK_W):
        _add("dock-in", x, 0, DOCK_W, DOCK_DEPTH)
    # Inbound staging band (full usable width).
    _add("staging", SIDE_MARGIN, DOCK_DEPTH, usable_w, STAGE_DEPTH)

    # Storage region: rack rows separated by aisles / transport lanes.
    y = storage_top
    for i in range(best_rows):
        li = int(rng.integers(0, inset_max + 1))
        ri = int(rng.integers(0, inset_max + 1))
        _add(row_types[i], SIDE_MARGIN + li, y, usable_w - li - ri, rack_depth)
        y += rack_depth
        if i < best_rows - 1:
            if i in transport_at:
                _add(transport_at[i], SIDE_MARGIN, y, usable_w, transport_depth)
                y += transport_depth
            else:
                y += aisle  # plain aisle: empty vertical clearance

    # Outbound staging band + outbound docks (bottom edge).
    _add("staging", SIDE_MARGIN, grid_h - DOCK_DEPTH - STAGE_DEPTH, usable_w, STAGE_DEPTH)
    for x in _spread(dock_out_n, SIDE_MARGIN, grid_w - SIDE_MARGIN, DOCK_W):
        _add("dock-out", x, grid_h - DOCK_DEPTH, DOCK_W, DOCK_DEPTH)

    raw: dict[str, Any] = {
        "version": SCHEMA,
        "gridW": grid_w,
        "gridH": grid_h,
        "cell": cell,
        "elements": elements,
        "config": {
            "minAisleMetres": profile.min_aisle_m,
            "generator": "logitwin.generate",
            "profile": profile.key,
            "seed": int(seed),
        },
    }
    # Validate on the way out: schema, bounds, unique ids, and (crucially) overlap-free. If our
    # construction ever violates the contract this raises here instead of emitting a bad layout.
    return load_layout(raw, reject_overlaps=True)


def generation_summary(layout: Layout, profile: PlantProfile) -> dict[str, Any]:
    """Structured summary of a generated plant (profile provenance + engine analysis + lane counts)."""
    type_counts: dict[str, int] = {}
    for el in layout.elements:
        type_counts[el.type] = type_counts.get(el.type, 0) + 1
    analysis = analyze_layout(layout)
    transport = {t: n for t, n in sorted(type_counts.items()) if t in ("conveyor", "rgv")}
    return {
        "profile": profile.key,
        "label": profile.label,
        "description": profile.description,
        "seed": layout.config.get("seed"),
        "type_counts": type_counts,
        "transport_lanes": transport,
        "analysis": analysis,
        "assumptions": list(profile.notes),
    }


# --------------------------------------------------------------------------- CLI


def _print_summary(summary: dict[str, Any]) -> None:
    a = summary["analysis"]
    grid, counts, cap, aisle, travel = (a["grid"], a["counts"], a["capacity"], a["aisle"], a["travel"])
    tc = summary["type_counts"]
    print(f"[generate] plant layout {summary['profile']!r} (schema {a['schema']}, seed {summary['seed']})")
    print(f"  {summary['label']}: {summary['description']}")
    print(f"  grid: {grid['w']} x {grid['h']} cells @ {grid['cell_m']} m/cell  "
          f"({grid['floor_area_m2']} m^2 floor)")
    print(f"  docks: in={counts['dock_in']} out={counts['dock_out']}  |  "
          f"staging bands: {tc.get('staging', 0)}")
    zones = ", ".join(f"{t} x{n}" for t, n in sorted(tc.items())
                      if t not in ("dock-in", "dock-out", "staging", "conveyor", "rgv"))
    print(f"  zones (rack rows): {zones or 'none'}")
    lanes = summary["transport_lanes"]
    print("  transport lanes: " + (", ".join(f"{t} x{n}" for t, n in lanes.items()) or "none")
          + " (transport, not storage -> 0 pallet positions)")
    print(f"  capacity: {cap['pallet_positions']} pallet positions across "
          f"{cap['storage_area_m2']} m^2 storage ({cap['storage_area_pct']}% of floor)")
    print(f"  aisle guard (min {aisle['min_aisle_m']} m): {aisle['facing_pairs']} facing pair(s), "
          f"{aisle['violations']} violation(s)"
          + (f", narrowest {aisle['narrowest_gap_m']} m" if aisle["narrowest_gap_m"] is not None else ""))
    if travel["io_point"] is not None:
        print(f"  pick travel to {travel['io_point']} (rectilinear round-trip proxy): "
              f"mean {travel['mean_round_trip_m']} m over {travel['n_positions']} positions")
    print("  generator: deterministic rule/heuristic (best-practice-informed), NOT a trained model")
    print("  assumptions:")
    for note in summary["assumptions"]:
        print(f"    - {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="logitwin.generate",
        description="Procedurally generate a WarehouseTwin-compatible plant layout (wt-1).",
    )
    parser.add_argument("--profile", choices=PROFILE_KEYS, help="plant profile to generate")
    parser.add_argument("--grid-w", type=int, default=DEFAULT_GRID_W, help="grid width in cells")
    parser.add_argument("--grid-h", type=int, default=DEFAULT_GRID_H, help="grid height in cells")
    parser.add_argument("--seed", type=int, default=SEED, help="deterministic generation seed")
    parser.add_argument("--json", action="store_true", help="emit the generated wt-1 layout as JSON")
    parser.add_argument("--list-profiles", action="store_true", help="list the plant profiles and exit")
    args = parser.parse_args(argv)

    if args.list_profiles:
        print("[profiles] WarehouseTwin-compatible plant profiles:")
        for key in PROFILE_KEYS:
            p = PLANT_PROFILES[key]
            print(f"  {key}: {p.label}")
        return 0

    if not args.profile:
        parser.print_help()
        return 0

    try:
        layout = generate_layout(args.profile, grid_w=args.grid_w, grid_h=args.grid_h, seed=args.seed)
    except GenerationError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(dump_layout(layout), indent=2))
    else:
        _print_summary(generation_summary(layout, get_profile(args.profile)))
    return 0


if __name__ == "__main__":
    # Windows console safety: force UTF-8 so ASCII markers and any stray unicode never crash stdout.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
