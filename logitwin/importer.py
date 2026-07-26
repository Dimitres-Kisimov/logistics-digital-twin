"""Import a user's own SKU master (and optional order history) from CSV.

This turns the demo into a decision tool for the user's *own* catalog while keeping the methods
identical to the synthetic run. Honesty contract (surfaced as ``assumptions`` on every import):

* Only carton data (dims, weight, velocity) and pick counts are imported. The rack geometry is
  still the app's seeded synthetic 6-aisle layout, sized to the imported SKU count - slot
  coordinates are NOT read from the CSV.
* The discrete-event simulation replays a seeded synthetic order stream weighted by the velocity
  classes; imported order lines set those classes but their timestamps are not replayed.
* Slotting demand uses the same A/B/C class weights as the synthetic run (see
  :data:`~logitwin.data.VELOCITY_DEMAND`), not per-SKU pick counts.

Velocity classes come either from a ``velocity`` column (A/B/C) or, when an order-lines CSV is
attached, are derived from pick share with the same split the synthetic generator uses: the top
20% of SKUs by picks are A, the next 30% B, the rest C.
"""

from __future__ import annotations

import csv
import io

from . import SEED
from .data import (
    VELOCITY_CLASSES,
    VELOCITY_DEMAND,
    Carton,
    Container,
    make_order_stream,
    make_warehouse,
)

# The order simulator draws up to 6 distinct SKUs per order without replacement, and the Hungarian
# solve is O(n^3) - both bounds below keep every analysis fast and well-defined.
MIN_SKUS = 8
MAX_SKUS = 500

_SKU_COLUMNS = ("sku", "length_cm", "width_cm", "height_cm", "weight_kg")
_MAX_ERRORS_SHOWN = 5


class CsvImportError(ValueError):
    """A user-facing problem with an uploaded CSV (bad header, bad rows, wrong size)."""


def _fail_rows(kind: str, errors: list[str]) -> CsvImportError:
    shown = "; ".join(errors[:_MAX_ERRORS_SHOWN])
    more = f" (+{len(errors) - _MAX_ERRORS_SHOWN} more)" if len(errors) > _MAX_ERRORS_SHOWN else ""
    return CsvImportError(f"{kind} has {len(errors)} bad row(s): {shown}{more}")


def parse_sku_csv(text: str) -> list[dict]:
    """Parse a SKU master CSV into row dicts.

    Required header: ``sku,length_cm,width_cm,height_cm,weight_kg``; optional ``velocity`` (A/B/C).
    Extra columns are ignored. Raises :class:`CsvImportError` with row numbers on any bad data.
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvImportError("SKU master CSV is empty")
    fields = {(f or "").strip().lower() for f in reader.fieldnames}
    missing = [c for c in _SKU_COLUMNS if c not in fields]
    if missing:
        raise CsvImportError(
            f"SKU master CSV is missing column(s): {', '.join(missing)} "
            "(expected header sku,length_cm,width_cm,height_cm,weight_kg[,velocity])"
        )
    has_velocity = "velocity" in fields

    rows: list[dict] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None}
        sku = row.get("sku", "")
        if not sku:
            errors.append(f"row {line_no}: empty sku")
            continue
        if sku.upper() in seen:
            errors.append(f"row {line_no}: duplicate sku {sku}")
            continue
        seen.add(sku.upper())
        try:
            length = float(row.get("length_cm", ""))
            width = float(row.get("width_cm", ""))
            height = float(row.get("height_cm", ""))
            weight = float(row.get("weight_kg", ""))
        except ValueError:
            errors.append(f"row {line_no}: non-numeric dimension/weight")
            continue
        if length <= 0 or width <= 0 or height <= 0:
            errors.append(f"row {line_no}: dimensions must be positive (cm)")
            continue
        if weight < 0:
            errors.append(f"row {line_no}: weight cannot be negative")
            continue
        velocity = row.get("velocity", "").upper() if has_velocity else ""
        if velocity and velocity not in VELOCITY_CLASSES:
            errors.append(f"row {line_no}: velocity must be A, B or C (got {velocity})")
            continue
        rows.append(
            {
                "sku": sku,
                "length": length,
                "width": width,
                "height": height,
                "weight": weight,
                "velocity": velocity or None,
            }
        )
    if errors:
        raise _fail_rows("SKU master CSV", errors)
    return rows


def parse_order_lines_csv(text: str, known_skus: set[str]) -> tuple[dict[str, float], int]:
    """Parse an order-lines CSV (``sku`` plus optional ``qty``, default 1) into picks per SKU.

    Lines referencing SKUs outside ``known_skus`` are ignored (the count is returned so the caller
    can disclose it). Matching is case-insensitive against the master's SKUs.
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvImportError("order-lines CSV is empty")
    fields = {(f or "").strip().lower() for f in reader.fieldnames}
    if "sku" not in fields:
        raise CsvImportError("order-lines CSV is missing the sku column (expected header sku[,qty])")
    canon = {s.upper(): s for s in known_skus}

    picks: dict[str, float] = {}
    ignored = 0
    errors: list[str] = []
    for line_no, raw in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None}
        sku = canon.get(row.get("sku", "").upper())
        if sku is None:
            ignored += 1
            continue
        qty_raw = row.get("qty", "")
        try:
            qty = float(qty_raw) if qty_raw else 1.0
        except ValueError:
            errors.append(f"row {line_no}: non-numeric qty")
            continue
        if qty <= 0:
            errors.append(f"row {line_no}: qty must be positive")
            continue
        picks[sku] = picks.get(sku, 0.0) + qty
    if errors:
        raise _fail_rows("order-lines CSV", errors)
    return picks, ignored


def derive_velocities(picks: dict[str, float], skus: list[str]) -> dict[str, str]:
    """ABC classes from pick counts: top 20% of SKUs by picks are A, next 30% B, rest C.

    This mirrors the synthetic generator's split. SKUs with no order lines rank last (class C).
    Ties break alphabetically so the same CSVs always yield the same classes.
    """
    ranked = sorted(skus, key=lambda s: (-picks.get(s, 0.0), s))
    n = len(ranked)
    n_a = max(1, round(0.20 * n))
    n_b = max(1, round(0.30 * n))
    if n_a + n_b >= n:  # tiny catalogs: keep at least one C-mover
        n_b = max(0, n - n_a - 1)
    out: dict[str, str] = {}
    for i, sku in enumerate(ranked):
        out[sku] = "A" if i < n_a else ("B" if i < n_a + n_b else "C")
    return out


def build_cartons(sku_text: str, orders_text: str | None = None) -> tuple[list[Carton], list[str]]:
    """Turn uploaded CSV text into :class:`~logitwin.data.Carton` objects plus disclosed assumptions.

    When ``orders_text`` is given, velocity classes are derived from pick share and any velocity
    column in the master is ignored; otherwise every row must carry a velocity class.
    """
    rows = parse_sku_csv(sku_text)
    if len(rows) < MIN_SKUS:
        raise CsvImportError(f"need at least {MIN_SKUS} SKUs to build meaningful layouts (got {len(rows)})")
    if len(rows) > MAX_SKUS:
        raise CsvImportError(f"at most {MAX_SKUS} SKUs are supported (got {len(rows)})")

    assumptions: list[str] = []
    if orders_text is not None:
        picks, ignored = parse_order_lines_csv(orders_text, {r["sku"] for r in rows})
        if not picks:
            raise CsvImportError("order-lines CSV has no rows matching the SKU master")
        velocities = derive_velocities(picks, [r["sku"] for r in rows])
        note = "velocity classes derived from your order lines by pick share: top 20% A, next 30% B, rest C"
        if any(r["velocity"] for r in rows):
            note += " (the master's velocity column was ignored in favour of measured picks)"
        assumptions.append(note)
        if ignored:
            assumptions.append(f"{ignored} order line(s) referenced SKUs not in the master and were ignored")
    else:
        missing = sum(1 for r in rows if not r["velocity"])
        if missing:
            raise CsvImportError(
                f"{missing} SKU(s) have no velocity class - fill the velocity column (A/B/C) "
                "or attach an order-lines CSV to derive it from picks"
            )
        velocities = {r["sku"]: r["velocity"] for r in rows}

    demand = "/".join(f"{k}={VELOCITY_DEMAND[k]:.0f}" for k in VELOCITY_CLASSES)
    assumptions.append(
        "rack geometry is the app's seeded synthetic 6-aisle layout sized to your SKU count "
        "(slot coordinates are not imported)"
    )
    assumptions.append(
        "the simulation replays a seeded synthetic order stream weighted by the velocity classes "
        "(your order timestamps are not replayed)"
    )
    assumptions.append(
        f"slotting demand uses the class weights {demand} picks/day - the same model as the synthetic run"
    )

    cartons = [
        Carton(
            id=i,
            sku=r["sku"],
            length=r["length"],
            width=r["width"],
            height=r["height"],
            weight=r["weight"],
            velocity=velocities[r["sku"]],
        )
        for i, r in enumerate(rows)
    ]
    return cartons, assumptions


def dataset_from_cartons(cartons: list[Carton], seed: int = SEED) -> dict:
    """A full dataset around imported cartons: seeded warehouse + order stream, same as synthetic."""
    return {
        "cartons": cartons,
        "warehouse": make_warehouse(n_slots=len(cartons), seed=seed),
        "orders": make_order_stream(cartons, seed=seed),
        "container": Container(),
    }
