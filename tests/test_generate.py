"""Tests for the procedural plant-layout generator (logitwin/generate.py).

The generator emits layouts in the shared ``wt-1`` interchange, so these tests lean on the same
load/dump/analyze contract as test_layout.py: every generated plant must round-trip, be
overlap-free, and satisfy the aisle guard.
"""

import json

import pytest

from logitwin.generate import (
    DEFAULT_GRID_H,
    DEFAULT_GRID_W,
    PLANT_PROFILES,
    PROFILE_KEYS,
    GenerationError,
    generate_layout,
    generation_summary,
    get_profile,
    main,
)
from logitwin.layout import (
    SCHEMA,
    Element,
    Layout,
    analyze_layout,
    dump_layout,
    element_capacity,
    load_layout,
)

# A comfortably-sized grid used by most tests (defaults are also exercised).
_GW, _GH = DEFAULT_GRID_W, DEFAULT_GRID_H


def test_profile_keys_are_the_shared_four():
    # Pinned EXACTLY to interoperate with the WarehouseTwin app - order and spelling matter.
    assert PROFILE_KEYS == (
        "ecommerce-fulfilment",
        "spare-parts-distribution",
        "automotive-supply",
        "cold-chain",
    )
    assert set(PLANT_PROFILES) == set(PROFILE_KEYS)


def test_generation_is_deterministic():
    for key in PROFILE_KEYS:
        a = generate_layout(key, grid_w=_GW, grid_h=_GH, seed=42)
        b = generate_layout(key, grid_w=_GW, grid_h=_GH, seed=42)
        assert dump_layout(a) == dump_layout(b)


def test_seed_changes_the_layout():
    d1 = dump_layout(generate_layout("ecommerce-fulfilment", grid_w=_GW, grid_h=_GH, seed=1))
    d2 = dump_layout(generate_layout("ecommerce-fulfilment", grid_w=_GW, grid_h=_GH, seed=2))
    assert d1 != d2


@pytest.mark.parametrize("key", PROFILE_KEYS)
def test_each_profile_round_trips_and_analyzes(key):
    layout = generate_layout(key, grid_w=_GW, grid_h=_GH, seed=42)
    assert isinstance(layout, Layout)
    assert layout.version == SCHEMA
    # Round-trips loss-free through the shared interchange.
    assert load_layout(dump_layout(layout)) == layout
    # Accepted by the engine analysis, with real storage capacity.
    result = analyze_layout(layout)
    assert result["schema"] == SCHEMA
    assert result["capacity"]["pallet_positions"] > 0


@pytest.mark.parametrize("key", PROFILE_KEYS)
def test_generated_layouts_are_overlap_free(key):
    obj = dump_layout(generate_layout(key, grid_w=_GW, grid_h=_GH, seed=42))
    # reject_overlaps=True raises LayoutError on any overlapping footprints - it must NOT here.
    assert load_layout(obj, reject_overlaps=True).version == SCHEMA


@pytest.mark.parametrize("key", PROFILE_KEYS)
def test_respects_min_aisle_width(key):
    layout = generate_layout(key, grid_w=_GW, grid_h=_GH, seed=42)
    result = analyze_layout(layout)
    assert result["aisle"]["violations"] == 0
    if result["aisle"]["facing_pairs"] > 0:
        assert result["aisle"]["narrowest_gap_m"] >= layout.min_aisle_m - 1e-9


def test_config_carries_min_aisle_and_provenance():
    for key in PROFILE_KEYS:
        layout = generate_layout(key, grid_w=_GW, grid_h=_GH, seed=42)
        assert layout.config["minAisleMetres"] == PLANT_PROFILES[key].min_aisle_m
        assert layout.min_aisle_m == PLANT_PROFILES[key].min_aisle_m
        assert layout.config["profile"] == key
        assert layout.config["seed"] == 42


def test_rgv_type_validates_and_has_zero_storage_capacity():
    # The shared RGV element: transport, not storage -> 0 pallet positions, but a valid known type.
    assert element_capacity(Element("r", "rgv", 0, 0, 10, 2), 1.0) == 0
    obj = {
        "version": "wt-1",
        "gridW": 12,
        "gridH": 6,
        "cell": 1.0,
        "elements": [{"id": "lane", "type": "rgv", "x": 0, "y": 0, "w": 10, "d": 2}],
    }
    layout = load_layout(obj)  # must not raise - rgv is a recognised type
    assert layout.elements[0].type == "rgv"
    assert analyze_layout(layout)["capacity"]["pallet_positions"] == 0


def test_rgv_present_for_rgv_profiles():
    # RGV support is a headline feature for the automation-heavy profiles.
    for key in ("ecommerce-fulfilment", "automotive-supply", "cold-chain"):
        layout = generate_layout(key, grid_w=_GW, grid_h=_GH, seed=42)
        assert any(e.type == "rgv" for e in layout.elements), f"{key} should place an rgv lane"


def test_conveyor_present_for_conveyor_profiles():
    for key in ("ecommerce-fulfilment", "spare-parts-distribution"):
        layout = generate_layout(key, grid_w=_GW, grid_h=_GH, seed=42)
        assert any(e.type == "conveyor" for e in layout.elements)


@pytest.mark.parametrize("key", PROFILE_KEYS)
def test_transport_lanes_contribute_no_capacity(key):
    layout = generate_layout(key, grid_w=_GW, grid_h=_GH, seed=42)
    lanes = [e for e in layout.elements if e.type in ("rgv", "conveyor")]
    assert lanes, f"{key} should have at least one transport lane"
    assert all(element_capacity(e, layout.cell) == 0 for e in lanes)


@pytest.mark.parametrize("key", PROFILE_KEYS)
def test_has_docks_and_storage(key):
    layout = generate_layout(key, grid_w=_GW, grid_h=_GH, seed=42)
    kinds = [e.type for e in layout.elements]
    assert kinds.count("dock-in") >= 1
    assert kinds.count("dock-out") >= 1
    assert len(layout.storage_elements) >= 1


def test_unknown_profile_raises():
    with pytest.raises(GenerationError, match="unknown plant profile"):
        get_profile("nope")
    with pytest.raises(GenerationError, match="unknown plant profile"):
        generate_layout("nope")


def test_grid_too_small_raises():
    with pytest.raises(GenerationError, match="too small"):
        generate_layout("automotive-supply", grid_w=_GW, grid_h=6)
    with pytest.raises(GenerationError, match="too small"):
        generate_layout("cold-chain", grid_w=5, grid_h=_GH)


def test_generation_summary_reports_lanes_and_zones():
    layout = generate_layout("ecommerce-fulfilment", grid_w=_GW, grid_h=_GH, seed=42)
    summary = generation_summary(layout, get_profile("ecommerce-fulfilment"))
    assert summary["profile"] == "ecommerce-fulfilment"
    assert summary["transport_lanes"].get("rgv", 0) >= 1
    assert summary["analysis"]["aisle"]["violations"] == 0


def test_cli_summary_and_json(capsys):
    assert main(["--profile", "spare-parts-distribution", "--seed", "42"]) == 0
    out = capsys.readouterr().out
    assert "[generate] plant layout 'spare-parts-distribution'" in out

    assert main(["--profile", "cold-chain", "--seed", "7", "--json"]) == 0
    payload = capsys.readouterr().out
    obj = json.loads(payload)
    # The emitted JSON is a genuine wt-1 layout that loads straight back into the engine.
    assert obj["version"] == SCHEMA
    assert load_layout(obj, reject_overlaps=True).version == SCHEMA


def test_cli_list_profiles_and_bad_input(capsys):
    assert main(["--list-profiles"]) == 0
    listed = capsys.readouterr().out
    for key in PROFILE_KEYS:
        assert key in listed
    # A grid too small exits non-zero with a message on stderr.
    assert main(["--profile", "automotive-supply", "--grid-h", "6"]) == 2
    assert "too small" in capsys.readouterr().err
    # An invalid --profile choice is rejected by argparse (SystemExit, non-zero).
    with pytest.raises(SystemExit) as exc:
        main(["--profile", "nope"])
    assert exc.value.code == 2
