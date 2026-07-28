"""Tests for the WarehouseTwin <-> engine layout interchange (logitwin/layout.py)."""

import pytest

from logitwin.layout import (
    SCHEMA,
    Element,
    Layout,
    LayoutError,
    analyze_layout,
    decode_share_payload,
    dump_layout,
    element_capacity,
    encode_share_payload,
    layout_to_warehouse,
    load_layout,
    load_share_link,
    slotting_demo,
)


def _wt_layout():
    """A hand-built layout in WarehouseTwin's exact serialize() shape."""
    return {
        "version": "wt-1",
        "gridW": 10,
        "gridH": 10,
        "cell": 1.0,
        "elements": [
            {"id": "el-1", "type": "dock-out", "x": 4, "y": 9, "w": 2, "d": 1},
            {"id": "el-2", "type": "selective-racking", "x": 0, "y": 0, "w": 4, "d": 1},
            {"id": "el-3", "type": "selective-racking", "x": 0, "y": 3, "w": 4, "d": 1},
        ],
        "config": {"minAisleMetres": 2.9, "seed": 42, "strategy": "abc", "flowMode": "pull"},
        "savedAt": "2026-07-28T00:00:00.000Z",
    }


def test_schema_constant():
    assert SCHEMA == "wt-1"


def test_round_trip_element_for_element():
    obj = _wt_layout()
    layout = load_layout(obj)
    # Element-for-element equality against the hand-built layout.
    assert layout.elements == (
        Element(id="el-1", type="dock-out", x=4, y=9, w=2, d=1),
        Element(id="el-2", type="selective-racking", x=0, y=0, w=4, d=1),
        Element(id="el-3", type="selective-racking", x=0, y=3, w=4, d=1),
    )
    assert layout.min_aisle_m == 2.9
    assert layout.saved_at == "2026-07-28T00:00:00.000Z"
    # A genuine WarehouseTwin export round-trips byte-for-byte through the engine.
    assert dump_layout(layout) == obj


def test_dump_reloads_equal():
    layout = load_layout(_wt_layout())
    assert load_layout(dump_layout(layout)) == layout


def test_reject_unknown_element_type():
    obj = _wt_layout()
    obj["elements"][1]["type"] = "teleporter"
    with pytest.raises(LayoutError, match="unknown element type 'teleporter' at index 1"):
        load_layout(obj)


def test_reject_missing_and_unknown_fields():
    with pytest.raises(LayoutError, match="missing required field 'cell'"):
        obj = _wt_layout()
        del obj["cell"]
        load_layout(obj)
    with pytest.raises(LayoutError, match="unknown top-level field"):
        obj = _wt_layout()
        obj["bogus"] = 1
        load_layout(obj)
    with pytest.raises(LayoutError, match="missing field.*w"):
        obj = _wt_layout()
        del obj["elements"][1]["w"]
        load_layout(obj)


def test_reject_bad_version():
    obj = _wt_layout()
    obj["version"] = "wt-99"
    with pytest.raises(LayoutError, match="unsupported layout schema 'wt-99'"):
        load_layout(obj)


def test_reject_out_of_bounds_and_bad_geometry():
    with pytest.raises(LayoutError, match="extends beyond the grid"):
        obj = _wt_layout()
        obj["elements"][1]["w"] = 20  # 0 + 20 > gridW=10
        load_layout(obj)
    with pytest.raises(LayoutError, match="must be an integer"):
        obj = _wt_layout()
        obj["elements"][1]["x"] = 1.5
        load_layout(obj)


def test_reject_duplicate_id():
    obj = _wt_layout()
    obj["elements"][2]["id"] = "el-2"
    with pytest.raises(LayoutError, match="duplicate element id 'el-2'"):
        load_layout(obj)


def test_reject_overlaps_flag():
    obj = _wt_layout()
    # Move the second rack on top of the first.
    obj["elements"][2]["y"] = 0
    load_layout(obj)  # tolerated by default
    with pytest.raises(LayoutError, match="overlap"):
        load_layout(obj, reject_overlaps=True)


def test_element_capacity_matches_domain():
    # Exact ports of WarehouseTwin domain.js elementCapacity (area * density, half-up round).
    assert element_capacity(Element("a", "selective-racking", 0, 0, 4, 1), 1.0) == 10  # 9.6 -> 10
    assert element_capacity(Element("b", "asrs", 0, 0, 8, 2), 1.0) == 80  # 16 * 5.0
    assert element_capacity(Element("c", "carton-flow", 0, 0, 3, 1), 1.0) == 5  # 4.8 -> 5
    assert element_capacity(Element("d", "dock-out", 0, 0, 2, 1), 1.0) == 0  # flow -> no positions


def test_analyze_known_values():
    result = analyze_layout(load_layout(_wt_layout()))
    assert result["schema"] == "wt-1"
    assert result["counts"] == {"elements": 3, "storage": 2, "flow": 1, "dock_in": 0, "dock_out": 1}
    assert result["capacity"]["pallet_positions"] == 20
    assert result["capacity"]["storage_area_m2"] == 8.0
    assert result["capacity"]["floor_area_m2"] == 100.0
    assert result["capacity"]["storage_area_pct"] == 8.0
    assert result["aisle"] == {
        "min_aisle_m": 2.9,
        "facing_pairs": 1,
        "violations": 1,
        "narrowest_gap_m": 2.0,
    }
    # Rack centroids (2.0,0.5) and (2.0,3.5); dock-out centroid (5.0,9.5); Manhattan *2:
    #   rack near dock: (3+6)*2 = 18 m round trip; far rack: (3+9)*2 = 24 m. 10 slots each.
    assert result["travel"] == {
        "io_point": "dock-out",
        "n_positions": 20,
        "mean_round_trip_m": 21.0,
        "min_round_trip_m": 18.0,
        "max_round_trip_m": 24.0,
        "total_position_m": 420.0,
    }


def test_analyze_is_deterministic():
    layout = load_layout(_wt_layout())
    assert analyze_layout(layout) == analyze_layout(layout)


def test_layout_to_warehouse_maps_positions_and_distances():
    warehouse = layout_to_warehouse(load_layout(_wt_layout()))
    assert warehouse.n_slots == 20
    distances = sorted({s.distance for s in warehouse.slots})
    assert distances == [18.0, 24.0]
    assert sum(1 for s in warehouse.slots if s.distance == 18.0) == 10


def test_no_dock_makes_travel_undefined_but_keeps_capacity():
    obj = _wt_layout()
    obj["elements"] = obj["elements"][1:]  # drop the dock-out
    layout = load_layout(obj)
    result = analyze_layout(layout)
    assert result["capacity"]["pallet_positions"] == 20  # capacity still computed
    assert result["travel"]["io_point"] is None
    assert result["travel"]["mean_round_trip_m"] is None
    assert any("no dock" in note for note in result["assumptions"])
    with pytest.raises(LayoutError, match="no dock element"):
        layout_to_warehouse(layout)


def test_slotting_demo_is_deterministic_and_discloses_synthetic_demand():
    layout = load_layout(_wt_layout())
    d1 = slotting_demo(layout)
    d2 = slotting_demo(layout)
    assert not d1["skipped"]
    assert d1["reduction_pct"] == d2["reduction_pct"]
    assert d1["optimized_travel"] == d2["optimized_travel"]
    assert d1["plan_valid"] and d1["sequence_valid"]
    assert d1["optimized_travel"] <= d1["legacy_travel"]
    assert any("SEEDED SYNTHETIC" in note for note in d1["assumptions"])


def test_share_codec_round_trip():
    layout = load_layout(_wt_layout())
    payload = encode_share_payload(layout)
    assert decode_share_payload(payload) == dump_layout(layout)
    # A full share link ("#layout=...") loads straight into the engine.
    assert load_share_link("#layout=" + payload) == layout
    assert load_share_link(payload) == layout


def test_share_codec_rejects_garbage():
    with pytest.raises(LayoutError, match="empty share payload"):
        decode_share_payload("")
    with pytest.raises(LayoutError, match="not base64url"):
        decode_share_payload("has spaces!")


def test_load_accepts_json_string_and_rejects_non_object():
    obj = _wt_layout()
    assert load_layout('{"version":"wt-1","gridW":10,"gridH":10,"cell":1.0,"elements":[]}').grid_w == 10
    with pytest.raises(LayoutError, match="must be a JSON object"):
        load_layout("[1, 2, 3]")
    with pytest.raises(LayoutError, match="not valid JSON"):
        load_layout("{not json")
    # Sanity: the fixture itself is well-formed.
    assert isinstance(load_layout(obj), Layout)
