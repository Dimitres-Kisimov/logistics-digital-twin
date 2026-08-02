"""WarehouseTwin <-> engine layout interchange ("one format, two tools").

The interchange format mirrors WarehouseTwin, (c) Dimitres Kisimov. WarehouseTwin
(the browser app in the sibling repo ``logistics-flow-studio``) serializes a floor
layout with ``serialize()`` / share links (``share.js``); this module loads that exact
JSON, validates it, maps it into this engine's spatial model, and runs an analysis on
it - so a layout DESIGNED IN THE APP can be ANALYZED BY THE ENGINE, and written back.

WarehouseTwin's serialized shape (``app.js`` ``serialize()``)::

    {
      "version": "wt-1",
      "gridW": 40, "gridH": 24, "cell": 1.0,        # cells across/down; metres per cell
      "elements": [{"id": "el-1", "type": "selective-racking",
                    "x": 2, "y": 6, "w": 12, "d": 1}],  # footprint in grid cells
      "config": {"minAisleMetres": 2.9, ...},
      "savedAt": "2026-07-28T..."
    }

``x, y`` are the top-left corner in grid cells; ``w`` runs along +x, ``d`` along +y;
one cell is ``cell`` metres (WarehouseTwin uses ``METRES_PER_CELL = 1.0``). The element
``type`` vocabulary, storage densities and the aisle guard are ported verbatim from the
app's ``domain.js`` (see ``_STORAGE_DENSITY`` / ``_facing_aisle_pairs`` below).

HONEST MAPPING (what maps, what is approximated, what is dropped)
----------------------------------------------------------------
MAPS FAITHFULLY (exact ports of WarehouseTwin / ``domain.js``):
  * element footprint geometry (x, y, w, d in cells; ``cell`` metres) - carried exactly;
  * the element ``type`` vocabulary and storage-vs-flow category;
  * pallet-position CAPACITY per storage element - identical formula and rounding as
    ``domain.js`` ``elementCapacity`` (``round(area_m2 * density)``, half-up like JS
    ``Math.round``, same density table);
  * the aisle-width guard - identical to ``domain.js`` ``facingAislePairs`` /
    ``aisleViolations`` against ``config.minAisleMetres``.

APPROXIMATED (documented proxies, not measurements):
  * pick-travel distance = rectilinear (Manhattan) distance from a storage element's
    centroid to the nearest I/O dock centroid, doubled for a round trip. Real aisle
    routing, rack entry points, one-way flow and cross-aisles are NOT modeled. This
    matches the ENGINE's own abstraction - ``Slot.distance`` is likewise a "round-trip
    proxy" (see ``data.py``) - but it is a proxy, not a walked path;
  * mapping storage positions to engine ``Slot`` objects: each pallet position becomes
    one ``Slot`` sharing its element's round-trip distance; ``aisle``/``bay``/``level``
    are synthetic positional indices (the 2D floor plan has no rack-internal bay/level
    structure) and slot cavity dims are the engine defaults - the layout carries none.

DROPPED (not represented by the engine's abstract model):
  * SKU / carton data - a WarehouseTwin layout carries NO products, demand or velocity
    classes, so a true slotting OPTIMIZATION cannot be computed from the layout alone.
    :func:`slotting_demo` therefore pairs the layout GEOMETRY with SEEDED SYNTHETIC
    cartons and discloses it: its reduction % is a property of the synthetic demand, not
    of the layout;
  * material-flow chain semantics (conveyor / staging / push-pull connectivity, the
    dock->storage->pack->ship chain from ``domain.js`` ``analyzeChains``) - flow elements
    are counted, but their connectivity is not analyzed here;
  * per-element attributes beyond geometry + capacity (selectivity, FIFO/LIFO rotation,
    handling deltas, goods-to-person cycle times, heightM, cost index, colour, levels);
  * ``config`` keys other than ``minAisleMetres`` (seed, strategy, orders, skuCount,
    flowMode, demandSkew, palletType, boxType, wagePerHour, weeklyOrders) and ``savedAt``
    are PRESERVED verbatim for a loss-free round trip, but are not interpreted here.

Determinism: every function here is a pure function of its input. There is no sampling in
the geometry analysis; the optional slotting demo is seeded (default :data:`SEED`), so the
same layout always yields identical output.

CLI (matches the package ``__main__`` style)::

    python -m logitwin.layout --analyze layout.json
    python -m logitwin.layout --analyze layout.json --slotting   # + seeded synthetic demo
    python -m logitwin.layout --analyze layout.json --json       # machine-readable
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import SEED
from .data import Slot, Warehouse, make_cartons
from .slotting import slotting_report

# The ``version`` token WarehouseTwin's ``serialize()`` stamps on every export/share link.
SCHEMA = "wt-1"

# Storage-element pallet DENSITY (positions per m^2), ported verbatim from WarehouseTwin
# ``domain.js`` ELEMENTS. Only "storage"-category elements contribute pallet positions.
_STORAGE_DENSITY: dict[str, float] = {
    "selective-racking": 2.4,
    "block-stack": 3.2,
    "drive-in": 3.0,
    "double-deep": 2.9,
    "push-back": 3.0,
    "pallet-flow": 3.4,
    "carton-flow": 1.6,
    "mobile-racking": 3.8,
    "cantilever": 0.8,
    "asrs": 5.0,
    "shuttle": 4.5,
    "mezzanine": 2.0,
}

# "flow"-category element types: docks, staging, control/pack stations, and TRANSPORT lanes.
# Transport lanes are ``conveyor`` and ``rgv`` (a rail/rack-guided-vehicle lane): they MOVE goods
# between zones, they do not STORE them, so - like every non-storage type - they contribute 0 pallet
# positions (see :func:`element_capacity`). ``rgv`` mirrors the WarehouseTwin ``rgv`` transport
# element so a generated plant with RGV lanes (see ``generate.py``) round-trips through both tools.
_FLOW_TYPES: frozenset[str] = frozenset(
    {"dock-in", "dock-out", "staging", "conveyor", "rgv",
     "push-station", "pull-station", "pack-station"}
)

# Transport lanes among the flow types (non-storage, move-not-store). Documented for honesty; the
# capacity/aisle logic already treats them like any other non-storage type.
_TRANSPORT_TYPES: frozenset[str] = frozenset({"conveyor", "rgv"})

# The full placeable-element vocabulary (``domain.js`` ``paletteOrder``). Anything outside
# this set is rejected by :func:`load_layout` (WarehouseTwin's own importer silently DROPS
# unknown types; the interchange is stricter and refuses them).
KNOWN_TYPES: frozenset[str] = frozenset(_STORAGE_DENSITY) | _FLOW_TYPES

# WarehouseTwin ``domain.js`` AISLE.defaultMinMetres - the fallback minimum working aisle
# when a layout's ``config`` omits ``minAisleMetres``.
DEFAULT_MIN_AISLE_M = 2.9

# Engine-default slot cavity dims (cm) used when materializing ``Slot`` objects; the layout
# carries no per-pallet cavity, so these are NOT derived from it (see ``data.make_warehouse``).
_SLOT_L, _SLOT_W, _SLOT_H = 60.0, 45.0, 40.0

# Cap for the optional slotting demo so the Hungarian solve (O(n^3)) stays fast.
_DEMO_MAX_POSITIONS = 400

_ALLOWED_TOP = frozenset({"version", "gridW", "gridH", "cell", "elements", "config", "savedAt"})
_ELEM_KEYS = frozenset({"id", "type", "x", "y", "w", "d"})
_MAX_SHARE_CHARS = 200_000  # mirrors share.js MAX_PAYLOAD_CHARS


class LayoutError(ValueError):
    """A user-facing problem with a WarehouseTwin layout (bad schema, unknown type, bad geometry)."""


@dataclass(frozen=True)
class Element:
    """One placed element: a footprint on the metric grid (``domain.js`` element model)."""

    id: str
    type: str
    x: int  # top-left corner, grid cells
    y: int
    w: int  # width along +x, grid cells
    d: int  # depth along +y, grid cells

    @property
    def is_storage(self) -> bool:
        return self.type in _STORAGE_DENSITY


@dataclass
class Layout:
    """A parsed WarehouseTwin layout: grid, config and placed elements.

    ``config`` is preserved verbatim so a WarehouseTwin export round-trips loss-free; only
    ``minAisleMetres`` is interpreted by the engine analysis (via :attr:`min_aisle_m`).
    """

    grid_w: int
    grid_h: int
    cell: float
    elements: tuple[Element, ...]
    config: dict[str, Any] = field(default_factory=dict)
    version: str = SCHEMA
    saved_at: str | None = None

    @property
    def min_aisle_m(self) -> float:
        v = self.config.get("minAisleMetres")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return DEFAULT_MIN_AISLE_M
        return float(v)

    @property
    def storage_elements(self) -> list[Element]:
        return [e for e in self.elements if e.is_storage]

    @property
    def floor_area_m2(self) -> float:
        return self.grid_w * self.grid_h * self.cell * self.cell


# --------------------------------------------------------------------------- geometry helpers


def _round_half_up(v: float) -> int:
    """Round half away from zero for positive values - matches JS ``Math.round``."""
    return math.floor(v + 0.5)


def element_capacity(el: Element, cell: float) -> int:
    """Pallet positions contributed by one element - exact port of ``domain.js`` ``elementCapacity``."""
    density = _STORAGE_DENSITY.get(el.type)
    if density is None:  # flow/transport element (dock, staging, conveyor, rgv, ...) -> no positions
        return 0
    area_m2 = el.w * el.d * cell * cell
    return max(0, _round_half_up(area_m2 * density))


def _centroid_m(el: Element, cell: float) -> tuple[float, float]:
    """Element footprint centroid in metres (matches app.js centre-of-mass maths)."""
    return ((el.x + el.w / 2.0) * cell, (el.y + el.d / 2.0) * cell)


def _io_elements(layout: Layout) -> tuple[list[Element], str | None]:
    """The I/O reference for pick travel: outbound docks if any, else inbound, else none.

    ``domain.js`` makes ``dock-out`` "the default I/O point for pick travel"; we fall back to
    ``dock-in`` when a layout has only inbound docks, and report which was used.
    """
    outs = [e for e in layout.elements if e.type == "dock-out"]
    if outs:
        return outs, "dock-out"
    ins = [e for e in layout.elements if e.type == "dock-in"]
    if ins:
        return ins, "dock-in"
    return [], None


def _round_trip_m(el: Element, io_elems: list[Element], cell: float) -> float:
    """Rectilinear round-trip distance (metres) from an element centroid to the nearest I/O dock."""
    ecx, ecy = _centroid_m(el, cell)
    one_way = min(
        abs(ecx - icx) + abs(ecy - icy)
        for icx, icy in (_centroid_m(io, cell) for io in io_elems)
    )
    return 2.0 * one_way


def _facing_aisle_pairs(layout: Layout) -> list[dict[str, Any]]:
    """Every facing storage-row pair forming a working aisle - port of ``domain.js`` ``facingAislePairs``.

    Overlapping racks and back-to-back racks (gap 0) are excluded; only gap > 0 is an aisle.
    """
    st = layout.storage_elements
    cell = layout.cell
    out: list[dict[str, Any]] = []
    for i in range(len(st)):
        for j in range(i + 1, len(st)):
            a, b = st[i], st[j]
            o_x = a.x < b.x + b.w and b.x < a.x + a.w
            o_y = a.y < b.y + b.d and b.y < a.y + a.d
            if o_x and o_y:
                continue  # overlap, not an aisle
            if o_x and not o_y:
                gap = max(a.y, b.y) - min(a.y + a.d, b.y + b.d)
                if gap > 0:
                    out.append({"a": a.id, "b": b.id, "gap_m": gap * cell, "axis": "y"})
            elif o_y and not o_x:
                gap = max(a.x, b.x) - min(a.x + a.w, b.x + b.w)
                if gap > 0:
                    out.append({"a": a.id, "b": b.id, "gap_m": gap * cell, "axis": "x"})
    return out


def _overlaps(a: Element, b: Element) -> bool:
    """True if two footprints overlap (same test as app.js ``overlapsAny``)."""
    return a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.d and b.y < a.y + a.d


# --------------------------------------------------------------------------- load / dump


def _require_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LayoutError(f"{where} must be an integer, got {type(value).__name__}")
    return value


def _validate_element(raw: Any, idx: int, grid_w: int, grid_h: int) -> Element:
    if not isinstance(raw, dict):
        raise LayoutError(f"element {idx} is not an object")
    keys = set(raw)
    unknown = keys - _ELEM_KEYS
    if unknown:
        raise LayoutError(f"element {idx} has unknown field(s): {', '.join(sorted(unknown))}")
    missing = _ELEM_KEYS - keys
    if missing:
        raise LayoutError(f"element {idx} is missing field(s): {', '.join(sorted(missing))}")
    el_type = raw["type"]
    if el_type not in KNOWN_TYPES:
        raise LayoutError(f"unknown element type {el_type!r} at index {idx}")
    if not isinstance(raw["id"], str) or not raw["id"]:
        raise LayoutError(f"element {idx} has an invalid id (expected a non-empty string)")
    x = _require_int(raw["x"], f"element {idx} x")
    y = _require_int(raw["y"], f"element {idx} y")
    w = _require_int(raw["w"], f"element {idx} w")
    d = _require_int(raw["d"], f"element {idx} d")
    if w < 1 or d < 1:
        raise LayoutError(f"element {idx} footprint must be >= 1x1 (got {w}x{d})")
    if x < 0 or y < 0:
        raise LayoutError(f"element {idx} position must be non-negative (got {x},{y})")
    if x + w > grid_w or y + d > grid_h:
        raise LayoutError(
            f"element {raw['id']!r} extends beyond the grid "
            f"(x+w={x + w} > gridW={grid_w} or y+d={y + d} > gridH={grid_h})"
        )
    return Element(id=raw["id"], type=el_type, x=x, y=y, w=w, d=d)


def load_layout(obj: dict | str | bytes, *, reject_overlaps: bool = False) -> Layout:
    """Parse and VALIDATE a WarehouseTwin layout (dict or JSON string) into a :class:`Layout`.

    Tolerant of the WarehouseTwin JSON exactly as ``share.js`` / ``serialize()`` emit it, but
    strict about correctness: unknown top-level fields, an unsupported ``version``, unknown
    element types, missing element fields, non-integer or out-of-bounds geometry, and duplicate
    element ids all raise :class:`LayoutError` with a clear message. Pass ``reject_overlaps=True``
    to additionally reject any two overlapping footprints.
    """
    if isinstance(obj, (str, bytes)):
        try:
            obj = json.loads(obj)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LayoutError(f"layout is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise LayoutError(f"layout must be a JSON object, got {type(obj).__name__}")

    unknown = set(obj) - _ALLOWED_TOP
    if unknown:
        raise LayoutError(f"unknown top-level field(s): {', '.join(sorted(unknown))}")
    for req in ("version", "gridW", "gridH", "cell", "elements"):
        if req not in obj:
            raise LayoutError(f"missing required field {req!r}")

    if obj["version"] != SCHEMA:
        raise LayoutError(f"unsupported layout schema {obj['version']!r}; expected {SCHEMA!r}")

    grid_w = _require_int(obj["gridW"], "gridW")
    grid_h = _require_int(obj["gridH"], "gridH")
    if grid_w < 1 or grid_h < 1:
        raise LayoutError(f"grid must be at least 1x1 (got {grid_w}x{grid_h})")
    cell = obj["cell"]
    if isinstance(cell, bool) or not isinstance(cell, (int, float)) or cell <= 0:
        raise LayoutError(f"cell (metres per cell) must be a positive number, got {cell!r}")

    if not isinstance(obj["elements"], list):
        raise LayoutError("elements must be a list")
    elements: list[Element] = []
    seen_ids: set[str] = set()
    for idx, raw in enumerate(obj["elements"]):
        el = _validate_element(raw, idx, grid_w, grid_h)
        if el.id in seen_ids:
            raise LayoutError(f"duplicate element id {el.id!r}")
        seen_ids.add(el.id)
        elements.append(el)

    config = obj.get("config", {})
    if not isinstance(config, dict):
        raise LayoutError("config must be an object")
    aisle = config.get("minAisleMetres")
    if aisle is not None and (isinstance(aisle, bool) or not isinstance(aisle, (int, float)) or aisle <= 0):
        raise LayoutError(f"config.minAisleMetres must be a positive number, got {aisle!r}")

    saved_at = obj.get("savedAt")
    if saved_at is not None and not isinstance(saved_at, str):
        raise LayoutError("savedAt must be a string timestamp")

    if reject_overlaps:
        for i in range(len(elements)):
            for j in range(i + 1, len(elements)):
                if _overlaps(elements[i], elements[j]):
                    raise LayoutError(
                        f"elements {elements[i].id!r} and {elements[j].id!r} overlap"
                    )

    return Layout(
        grid_w=grid_w,
        grid_h=grid_h,
        cell=float(cell),
        elements=tuple(elements),
        config=dict(config),
        version=obj["version"],
        saved_at=saved_at,
    )


def dump_layout(layout: Layout) -> dict[str, Any]:
    """Serialize a :class:`Layout` back to a JSON-serializable dict in WarehouseTwin's shape.

    Round-trips: ``load_layout(dump_layout(x)) == x`` and, for a genuine WarehouseTwin export,
    ``dump_layout(load_layout(export)) == export``.
    """
    out: dict[str, Any] = {
        "version": layout.version,
        "gridW": layout.grid_w,
        "gridH": layout.grid_h,
        "cell": layout.cell,
        "elements": [
            {"id": e.id, "type": e.type, "x": e.x, "y": e.y, "w": e.w, "d": e.d}
            for e in layout.elements
        ],
        "config": dict(layout.config),
    }
    if layout.saved_at is not None:
        out["savedAt"] = layout.saved_at
    return out


# --------------------------------------------------------------------------- engine mapping


def layout_to_warehouse(layout: Layout) -> Warehouse:
    """Map a layout into this engine's :class:`~logitwin.data.Warehouse` of :class:`~logitwin.data.Slot`.

    Each storage element yields ``element_capacity`` slots, all sharing the element's
    round-trip pick-travel distance to the nearest I/O dock. ``aisle``/``bay``/``level`` are
    synthetic positional indices and slot cavity dims are engine defaults (see the module
    docstring - the 2D floor plan carries neither). Raises :class:`LayoutError` if the layout
    has no dock, since pick travel is then undefined.
    """
    io_elems, _ = _io_elements(layout)
    if not io_elems:
        raise LayoutError("layout has no dock element - pick travel to an I/O point is undefined")
    slots: list[Slot] = []
    sid = 0
    for aisle_idx, el in enumerate(layout.storage_elements):
        distance = round(_round_trip_m(el, io_elems, layout.cell), 2)
        for bay in range(element_capacity(el, layout.cell)):
            slots.append(
                Slot(
                    id=sid,
                    aisle=aisle_idx,
                    bay=bay,
                    level=0,
                    slot_l=_SLOT_L,
                    slot_w=_SLOT_W,
                    slot_h=_SLOT_H,
                    distance=distance,
                )
            )
            sid += 1
    return Warehouse(slots=slots)


def analyze_layout(layout: Layout) -> dict[str, Any]:
    """Run the geometry-honest engine analysis on a WarehouseTwin layout.

    Pure function of the layout (no sampling). Reports capacity, floor utilisation, the aisle
    guard, and - when the layout has an I/O dock - a pick-travel summary computed over the
    engine's mapped ``Slot`` distances (capacity-weighted, since each pallet position is one
    slot). No SKU/demand-dependent number is produced here; see :func:`slotting_demo`.
    """
    cell = layout.cell
    storage = layout.storage_elements
    flow = [e for e in layout.elements if e.type in _FLOW_TYPES]
    total_positions = sum(element_capacity(e, cell) for e in storage)
    storage_area = sum(e.w * e.d * cell * cell for e in storage)
    floor_area = layout.floor_area_m2

    pairs = _facing_aisle_pairs(layout)
    violations = [p for p in pairs if p["gap_m"] < layout.min_aisle_m - 1e-6]
    narrowest = round(min((p["gap_m"] for p in pairs), default=0.0), 3) if pairs else None

    io_elems, io_kind = _io_elements(layout)
    assumptions = [
        "pick-travel distance is a rectilinear centroid-to-nearest-dock round-trip proxy; "
        "aisle routing and rack entry points are not modeled",
        "pallet capacity uses WarehouseTwin's exact elementCapacity formula (area x density, "
        "half-up rounding)",
        "SKU/demand data is absent from a layout, so no slotting optimization is computed here",
    ]
    if io_kind is None:
        travel: dict[str, Any] = {
            "io_point": None,
            "n_positions": total_positions,
            "mean_round_trip_m": None,
            "min_round_trip_m": None,
            "max_round_trip_m": None,
            "total_position_m": None,
        }
        assumptions.append("no dock element present - pick travel to an I/O point is undefined")
    else:
        distances = [s.distance for s in layout_to_warehouse(layout).slots]
        if distances:
            travel = {
                "io_point": io_kind,
                "n_positions": len(distances),
                "mean_round_trip_m": round(sum(distances) / len(distances), 2),
                "min_round_trip_m": round(min(distances), 2),
                "max_round_trip_m": round(max(distances), 2),
                "total_position_m": round(sum(distances), 2),
            }
        else:
            travel = {
                "io_point": io_kind,
                "n_positions": 0,
                "mean_round_trip_m": None,
                "min_round_trip_m": None,
                "max_round_trip_m": None,
                "total_position_m": None,
            }

    return {
        "schema": layout.version,
        "grid": {
            "w": layout.grid_w,
            "h": layout.grid_h,
            "cell_m": cell,
            "floor_area_m2": round(floor_area, 2),
        },
        "counts": {
            "elements": len(layout.elements),
            "storage": len(storage),
            "flow": len(flow),
            "dock_in": sum(1 for e in layout.elements if e.type == "dock-in"),
            "dock_out": sum(1 for e in layout.elements if e.type == "dock-out"),
        },
        "capacity": {
            "pallet_positions": total_positions,
            "storage_area_m2": round(storage_area, 2),
            "floor_area_m2": round(floor_area, 2),
            "storage_area_pct": round(100.0 * storage_area / floor_area, 1) if floor_area > 0 else 0.0,
        },
        "aisle": {
            "min_aisle_m": layout.min_aisle_m,
            "facing_pairs": len(pairs),
            "violations": len(violations),
            "narrowest_gap_m": narrowest,
        },
        "travel": travel,
        "assumptions": assumptions,
    }


def slotting_demo(layout: Layout, seed: int = SEED) -> dict[str, Any]:
    """OPT-IN demonstration: run the engine's real slotting optimizer on the layout's geometry.

    The layout supplies real SLOT GEOMETRY (position count + round-trip I/O distances); the
    SKUs, velocity classes and demand are SEEDED SYNTHETIC (a layout has none). The reduction %
    is therefore a property of the synthetic demand model, not of the layout - this is disclosed
    in ``assumptions``. Deterministic for a given ``seed``. Mirrors the disclosed-assumption
    pattern of ``importer.py`` (which does the symmetric thing: real cartons, synthetic racks).
    """
    warehouse = layout_to_warehouse(layout)
    n = warehouse.n_slots
    assumptions = [
        "SLOT geometry (position count and round-trip I/O distances) comes from your layout",
        "SKUs, velocity classes and daily demand are SEEDED SYNTHETIC (a layout carries none) - "
        "the reduction % reflects the synthetic demand model, not your layout",
    ]
    if n < 2:
        return {
            "skipped": True,
            "reason": f"need >= 2 pallet positions to slot (layout has {n})",
            "n_positions": n,
            "assumptions": assumptions,
        }
    if n > _DEMO_MAX_POSITIONS:
        n = _DEMO_MAX_POSITIONS
        assumptions.append(
            f"capacity exceeds the demo cap - slotting the first {_DEMO_MAX_POSITIONS} positions "
            "(nearest the front of the element list) to keep the Hungarian solve fast"
        )
    cartons = make_cartons(n=n, seed=seed)
    warehouse = Warehouse(slots=warehouse.slots[:n])
    report = slotting_report(cartons, warehouse)
    return {
        "skipped": False,
        "seed": seed,
        "n_positions": n,
        "legacy_travel": round(report["legacy_travel"], 2),
        "optimized_travel": round(report["optimized_travel"], 2),
        "reduction_pct": round(report["reduction_pct"], 1),
        "n_moves": report["n_moves"],
        "plan_valid": report["plan_valid"],
        "sequence_valid": report["sequence_valid"],
        "assumptions": assumptions,
    }


# --------------------------------------------------------------------------- share-link codec
# Mirrors WarehouseTwin ``share.js``: JSON -> UTF-8 -> base64url (url-safe, '=' stripped).


def encode_share_payload(layout_or_dict: Layout | dict) -> str:
    """Encode a layout as a WarehouseTwin ``#layout=`` share payload (base64url, no padding)."""
    obj = dump_layout(layout_or_dict) if isinstance(layout_or_dict, Layout) else layout_or_dict
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_share_payload(payload: str) -> dict[str, Any]:
    """Decode a WarehouseTwin share payload back to a layout dict (mirrors ``share.js`` decode)."""
    if not isinstance(payload, str) or not payload:
        raise LayoutError("empty share payload")
    if len(payload) > _MAX_SHARE_CHARS:
        raise LayoutError("share payload too large")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", payload):
        raise LayoutError("not base64url")
    pad = "=" * (-len(payload) % 4)
    try:
        text = base64.urlsafe_b64decode(payload + pad).decode("utf-8")
        obj = json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise LayoutError("corrupted share payload") from exc
    if not isinstance(obj, dict):
        raise LayoutError("share payload is not a layout object")
    return obj


def _payload_from_hash(fragment: str) -> str | None:
    m = re.match(r"^#?layout=(.+)$", fragment)
    return m.group(1) if m else None


def load_share_link(fragment: str, *, reject_overlaps: bool = False) -> Layout:
    """Load a layout directly from a WarehouseTwin share link (``#layout=...`` or a raw payload)."""
    payload = _payload_from_hash(fragment) or fragment
    return load_layout(decode_share_payload(payload), reject_overlaps=reject_overlaps)


# --------------------------------------------------------------------------- CLI


def _print_analysis(result: dict[str, Any]) -> None:
    grid = result["grid"]
    counts = result["counts"]
    cap = result["capacity"]
    aisle = result["aisle"]
    travel = result["travel"]
    print(f"[analyze] WarehouseTwin layout (schema {result['schema']})")
    print(f"  grid: {grid['w']} x {grid['h']} cells @ {grid['cell_m']} m/cell  "
          f"({grid['floor_area_m2']} m^2 floor)")
    print(f"  elements: {counts['elements']}  (storage {counts['storage']}, flow {counts['flow']}; "
          f"docks in={counts['dock_in']} out={counts['dock_out']})")
    print(f"  capacity: {cap['pallet_positions']} pallet positions across "
          f"{cap['storage_area_m2']} m^2 storage ({cap['storage_area_pct']}% of floor)")
    print(f"  aisle guard (min {aisle['min_aisle_m']} m): {aisle['facing_pairs']} facing pair(s), "
          f"{aisle['violations']} violation(s)"
          + (f", narrowest {aisle['narrowest_gap_m']} m" if aisle["narrowest_gap_m"] is not None else ""))
    if travel["io_point"] is None:
        print("  pick travel: undefined (no dock element in the layout)")
    else:
        print(f"  pick travel to {travel['io_point']} (rectilinear round-trip proxy): "
              f"mean {travel['mean_round_trip_m']} m, min {travel['min_round_trip_m']} m, "
              f"max {travel['max_round_trip_m']} m over {travel['n_positions']} positions")
    if "slotting_demo" in result:
        demo = result["slotting_demo"]
        if demo.get("skipped"):
            print(f"  slotting demo: skipped ({demo['reason']})")
        else:
            print(f"  slotting demo (SEEDED SYNTHETIC demand, seed {demo['seed']}): "
                  f"{demo['reduction_pct']}% travel reduction over {demo['n_positions']} positions, "
                  f"{demo['n_moves']} moves")
    print("  notes:")
    for note in result["assumptions"]:
        print(f"    - {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="logitwin.layout",
        description="WarehouseTwin <-> engine layout interchange (load, validate, analyze).",
    )
    parser.add_argument("--analyze", metavar="FILE", help="load a WarehouseTwin layout JSON and analyze it")
    parser.add_argument("--slotting", action="store_true",
                        help="also run the seeded-synthetic slotting demo (discloses assumptions)")
    parser.add_argument("--reject-overlaps", action="store_true",
                        help="fail if any two elements overlap")
    parser.add_argument("--json", action="store_true", help="emit the analysis as JSON")
    parser.add_argument("--seed", type=int, default=SEED, help="seed for the slotting demo")
    args = parser.parse_args(argv)

    if args.analyze:
        try:
            text = Path(args.analyze).read_text(encoding="utf-8")
            layout = load_layout(text, reject_overlaps=args.reject_overlaps)
        except (OSError, LayoutError) as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 2
        result = analyze_layout(layout)
        if args.slotting:
            result["slotting_demo"] = slotting_demo(layout, seed=args.seed)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            _print_analysis(result)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    # Windows console safety: force UTF-8 so ASCII markers and any stray unicode never crash stdout.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
