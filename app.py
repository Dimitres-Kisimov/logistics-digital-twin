"""Flask 'warehouse command' UI.

Serves a single-page dashboard with a hand-drawn warehouse map (inline SVG), KPI tiles, and a
'scan a carton' panel. All graphics are built client-side with no external libraries or CDNs, so the
page works fully offline. JSON endpoints below feed the page.
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from flask import Flask, jsonify, render_template, request  # noqa: E402

from logitwin import __version__  # noqa: E402
from logitwin.analysis import full_report, headline_numbers  # noqa: E402
from logitwin.data import Container  # noqa: E402
from logitwin.packing import ffd_pack  # noqa: E402

app = Flask(__name__)

# Build the (deterministic) report once at startup and reuse it.
_REPORT = full_report()
_HEADLINE = headline_numbers(_REPORT)

# Slot id -> human-readable location code (warehouse staff navigate by aisle/bay/level, not ids).
_SLOT_BY_ID = {sl.id: sl for sl in _REPORT["dataset"]["warehouse"].slots}
# Canonical catalog SKUs (uppercase key) so /api/scan can tell "unknown SKU" from "no move needed".
_SKU_CANON = {c.sku.upper(): c.sku for c in _REPORT["dataset"]["cartons"]}


def _slot_code(slot_id: int) -> str:
    """Human-readable Aisle-Bay-Level code for a slot id, e.g. ``A1-B2-L0``."""
    sl = _SLOT_BY_ID.get(slot_id)
    if sl is None:
        return f"slot {slot_id}"
    return f"A{sl.aisle + 1}-B{sl.bay + 1}-L{sl.level}"


def _slots_payload() -> list[dict]:
    """Slot geometry + velocity class of the SKU assigned under legacy and optimized layouts."""
    ds = _REPORT["dataset"]
    s = _REPORT["slotting"]
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
                "code": _slot_code(sl.id),
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
    return render_template("index.html", version=__version__, headline=_HEADLINE)


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "version": __version__, "synthetic": True})


@app.route("/api/kpis")
def kpis():
    return jsonify(_HEADLINE)


@app.route("/api/slots")
def slots():
    return jsonify({"slots": _slots_payload()})


@app.route("/api/reshuffle")
def reshuffle():
    s = _REPORT["slotting"]
    moves = [
        {
            "sku": m.sku,
            "from_slot": m.from_slot,
            "from_code": _slot_code(m.from_slot),
            "to_slot": m.to_slot,
            "to_code": _slot_code(m.to_slot),
        }
        for m in s["moves"]
    ]
    return jsonify(
        {
            "n_moves": s["n_moves"],
            "break_even_days": round(s["break_even_days"], 2),
            "reduction_pct": round(s["reduction_pct"], 2),
            "moves": moves,
        }
    )


@app.route("/api/scan", methods=["POST"])
def scan():
    """Given a carton (dims + weight + SKU), recommend a container placement and, if the SKU is due
    to be re-slotted, the shift instruction to move it."""
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
    canonical = _SKU_CANON.get(sku.upper())
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
        for m in _REPORT["slotting"]["moves"]:
            if m.sku == sku:
                move = {
                    "from_slot": m.from_slot,
                    "from_code": _slot_code(m.from_slot),
                    "to_slot": m.to_slot,
                    "to_code": _slot_code(m.to_slot),
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
