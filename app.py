"""Flask 'warehouse command' UI.

Serves a single-page dashboard with a hand-drawn warehouse map (inline SVG), KPI tiles, and a
'scan a carton' panel. All graphics are built client-side with no external libraries or CDNs, so the
page works fully offline. JSON endpoints below feed the page.

The app starts on the seeded synthetic dataset. POST /api/import swaps in the user's own SKU master
(and optional order history) - the same analyses re-run on their data, every payload flips its
``synthetic`` flag, and the disclosed modelling assumptions ride along. POST /api/reset restores the
synthetic dataset.
"""

from __future__ import annotations

import math
import sys
import threading

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from flask import Flask, jsonify, render_template, request  # noqa: E402

from logitwin import __version__  # noqa: E402
from logitwin.analysis import full_report, headline_numbers, report_for_dataset  # noqa: E402
from logitwin.data import Container  # noqa: E402
from logitwin.importer import CsvImportError, build_cartons, dataset_from_cartons  # noqa: E402
from logitwin.packing import ffd_pack  # noqa: E402

app = Flask(__name__)

_MAX_CSV_BYTES = 2_000_000  # per uploaded file


def _round_or_none(value: float | None, ndigits: int = 2) -> float | None:
    """Round a number for a JSON payload; None/NaN/inf become None (JSON null).

    Flask's jsonify would happily emit the bare tokens ``Infinity``/``NaN``, which are not valid
    JSON and make strict clients (browser ``fetch().json()`` included) throw. Every numeric field
    that can be undefined goes through this guard.
    """
    if value is None or not math.isfinite(value):
        return None
    return round(value, ndigits)


def _json_safe(payload: dict) -> dict:
    """Replace any non-finite float values in a flat dict with None so the JSON stays valid."""
    return {
        k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in payload.items()
    }


def _make_state(report: dict, source: dict) -> dict:
    """Bundle a report with everything the endpoints derive from it, swapped atomically."""
    return {
        "report": report,
        "headline": headline_numbers(report),
        # Slot id -> slot (warehouse staff navigate by aisle/bay/level, not ids).
        "slot_by_id": {sl.id: sl for sl in report["dataset"]["warehouse"].slots},
        # Canonical catalog SKUs (uppercase key) so /api/scan can tell "unknown SKU" from "no move".
        "sku_canon": {c.sku.upper(): c.sku for c in report["dataset"]["cartons"]},
        "source": source,
    }


def _synthetic_state() -> dict:
    report = full_report()
    return _make_state(
        report,
        {
            "synthetic": True,
            "label": "seeded synthetic dataset",
            "filename": None,
            "n_skus": report["n_cartons"],
            "assumptions": [],
        },
    )


# Build the (deterministic) synthetic report once at startup; imports replace _STATE wholesale.
_LOCK = threading.Lock()
_STATE = _synthetic_state()


def _slot_code(state: dict, slot_id: int) -> str:
    """Human-readable Aisle-Bay-Level code for a slot id, e.g. ``A1-B2-L0``."""
    sl = state["slot_by_id"].get(slot_id)
    if sl is None:
        return f"slot {slot_id}"
    return f"A{sl.aisle + 1}-B{sl.bay + 1}-L{sl.level}"


def _slots_payload(state: dict) -> list[dict]:
    """Slot geometry + velocity class of the SKU assigned under legacy and optimized layouts."""
    ds = state["report"]["dataset"]
    s = state["report"]["slotting"]
    vel_of = {c.sku: c.velocity for c in ds["cartons"]}
    legacy_by_slot = {v: k for k, v in s["legacy_result"].assignment.items()}
    opt_by_slot = {v: k for k, v in s["optimized_result"].assignment.items()}
    out = []
    for sl in ds["warehouse"].slots:
        lg_sku = legacy_by_slot.get(sl.id)
        op_sku = opt_by_slot.get(sl.id)
        out.append(
            {
                "id": sl.id,
                "code": _slot_code(state, sl.id),
                "aisle": sl.aisle,
                "bay": sl.bay,
                "level": sl.level,
                "distance": sl.distance,
                "legacy_velocity": vel_of.get(lg_sku, "-") if lg_sku else "-",
                "optimized_velocity": vel_of.get(op_sku, "-") if op_sku else "-",
                "legacy_sku": lg_sku or "",
                "optimized_sku": op_sku or "",
            }
        )
    return out


@app.route("/")
def index():
    st = _STATE
    return render_template("index.html", version=__version__, headline=st["headline"], source=st["source"])


@app.route("/api/health")
def health():
    st = _STATE
    return jsonify(
        {
            "status": "ok",
            "version": __version__,
            "synthetic": st["source"]["synthetic"],
            "source": st["source"],
        }
    )


@app.route("/api/kpis")
def kpis():
    # _json_safe is a belt-and-braces guard: headline_numbers already emits None for an undefined
    # break-even, but no non-finite float must ever reach the wire as Infinity/NaN.
    return jsonify(_json_safe(_STATE["headline"]))


@app.route("/api/slots")
def slots():
    st = _STATE
    return jsonify({"slots": _slots_payload(st), "synthetic": st["source"]["synthetic"]})


@app.route("/api/reshuffle")
def reshuffle():
    st = _STATE
    s = st["report"]["slotting"]
    moves = [
        {
            "sku": m.sku,
            "from_slot": m.from_slot,
            "from_code": _slot_code(st, m.from_slot),
            "to_slot": m.to_slot,
            "to_code": _slot_code(st, m.to_slot),
        }
        for m in s["moves"]
    ]
    # Executable sequence: STAGE marks the off-rack staging position each cycle uses once.
    sequence = [
        {
            "seq": step.seq,
            "sku": step.sku,
            "from_slot": step.from_slot,
            "from_code": _slot_code(st, step.from_slot) if step.from_slot is not None else "STAGE",
            "to_slot": step.to_slot,
            "to_code": _slot_code(st, step.to_slot) if step.to_slot is not None else "STAGE",
            "cycle": step.cycle,
            "staging": step.from_slot is None or step.to_slot is None,
            "saving_m_day": round(step.saving, 2),
        }
        for step in s["sequence"]
    ]
    return jsonify(
        {
            "n_moves": s["n_moves"],
            "n_steps": s["n_steps"],
            "n_cycles": s["n_cycles"],
            # No moves -> no travel saved -> no break-even day count exists; the flag lets the UI
            # say so honestly instead of receiving invalid JSON (bare Infinity) and going blank.
            "no_moves": s["n_moves"] == 0,
            "break_even_days": _round_or_none(s["break_even_days"]),
            "reduction_pct": round(s["reduction_pct"], 2),
            "travel_saved_m_day": round(s["travel_saved"], 2),
            "synthetic": st["source"]["synthetic"],
            "moves": moves,
            "sequence": sequence,
        }
    )


def _read_csv_upload(file_storage) -> str:
    """Decode an uploaded CSV as UTF-8 (BOM tolerated), enforcing the size cap."""
    raw = file_storage.read(_MAX_CSV_BYTES + 1)
    if len(raw) > _MAX_CSV_BYTES:
        raise CsvImportError(f"{file_storage.filename}: file exceeds the {_MAX_CSV_BYTES // 1_000_000} MB limit")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvImportError(f"{file_storage.filename}: not UTF-8 text ({exc.reason})") from exc


@app.route("/api/import", methods=["POST"])
def import_data():
    """Swap the dashboard onto the user's own SKU master (multipart field ``sku_csv``, required)
    and optional order-lines CSV (``orders_csv``). Same analyses, disclosed assumptions."""
    global _STATE
    sku_file = request.files.get("sku_csv")
    if sku_file is None or not sku_file.filename:
        return jsonify({"error": "attach a SKU master CSV (multipart field sku_csv)"}), 400
    orders_file = request.files.get("orders_csv")
    try:
        sku_text = _read_csv_upload(sku_file)
        orders_text = _read_csv_upload(orders_file) if orders_file is not None and orders_file.filename else None
        cartons, assumptions = build_cartons(sku_text, orders_text)
        report = report_for_dataset(dataset_from_cartons(cartons))
    except CsvImportError as exc:
        return jsonify({"error": str(exc)}), 400
    filename = sku_file.filename + (f" + {orders_file.filename}" if orders_text is not None else "")
    source = {
        "synthetic": False,
        "label": f"imported SKU master ({filename})",
        "filename": filename,
        "n_skus": len(cartons),
        "assumptions": assumptions,
    }
    with _LOCK:
        _STATE = _make_state(report, source)
    return jsonify({"ok": True, "synthetic": False, "n_skus": len(cartons), "source": source})


@app.route("/api/reset", methods=["POST"])
def reset_data():
    """Restore the seeded synthetic dataset."""
    global _STATE
    with _LOCK:
        _STATE = _synthetic_state()
    return jsonify({"ok": True, "synthetic": True, "source": _STATE["source"]})


@app.route("/api/scan", methods=["POST"])
def scan():
    """Given a carton (dims + weight + SKU), recommend a container placement and, if the SKU is due
    to be re-slotted, the shift instruction to move it."""
    st = _STATE
    data = request.get_json(silent=True) or {}
    try:
        length = float(data.get("length", 0))
        width = float(data.get("width", 0))
        height = float(data.get("height", 0))
        weight = float(data.get("weight", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid numeric input"}), 400
    if length <= 0 or width <= 0 or height <= 0:
        return jsonify({"error": "carton dimensions must be positive numbers (cm)"}), 400
    if weight < 0:
        return jsonify({"error": "carton weight cannot be negative"}), 400
    sku = str(data.get("sku", "")).strip()
    # Case-insensitive catalog lookup: 'sku-0000' matches 'SKU-0000'.
    canonical = st["sku_canon"].get(sku.upper())
    sku_known = canonical is not None
    if sku_known:
        sku = canonical

    container = Container()
    fits_dims = length <= container.length and width <= container.width and height <= container.height
    over_weight = weight > container.max_weight
    # A carton is only shippable in a standard container if BOTH dims and weight are within limits;
    # never recommend a container for an overweight carton.
    shippable = fits_dims and not over_weight
    vol = length * width * height
    fill_if_alone = 100.0 * vol / container.volume if container.volume else 0.0

    # A tiny live pack for shippable cartons only, to show a concrete placement.
    placement = None
    if shippable:
        from logitwin.data import Carton

        probe = Carton(id=999999, sku=sku or "PROBE", length=length, width=width, height=height, weight=weight, velocity="B")
        packed = ffd_pack([probe], container)
        if packed.containers and packed.containers[0].placements:
            pl = packed.containers[0].placements[0]
            placement = {"x": pl.x, "y": pl.y, "z": pl.z, "container": 1}

    # Re-slot instruction if the SKU appears in the reshuffle plan.
    move = None
    if sku_known:
        for m in st["report"]["slotting"]["moves"]:
            if m.sku == sku:
                move = {
                    "from_slot": m.from_slot,
                    "from_code": _slot_code(st, m.from_slot),
                    "to_slot": m.to_slot,
                    "to_code": _slot_code(st, m.to_slot),
                }
                break

    if not fits_dims:
        note = "Carton exceeds container dimensions - use oversize handling."
    elif over_weight:
        note = (
            f"Carton exceeds the {container.max_weight:.0f} kg container weight limit - "
            "split the load or use heavy-goods handling."
        )
    else:
        note = "Consolidate with same-route picks into container 1 (FFD)."

    return jsonify(
        {
            "sku": sku,
            "sku_known": sku_known,
            "fits_container": shippable,
            "fits_dimensions": fits_dims,
            "carton_volume_cm3": round(vol, 1),
            "fill_if_alone_pct": round(fill_if_alone, 2),
            "recommended_container": 1 if shippable else None,
            "placement": placement,
            "over_weight": over_weight,
            "reslot_instruction": move,
            "note": note,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
