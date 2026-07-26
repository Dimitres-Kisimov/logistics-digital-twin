import pytest

from logitwin.analysis import report_for_dataset
from logitwin.importer import (
    MAX_SKUS,
    MIN_SKUS,
    CsvImportError,
    build_cartons,
    dataset_from_cartons,
    derive_velocities,
    parse_sku_csv,
)

HEADER = "sku,length_cm,width_cm,height_cm,weight_kg,velocity"


def _sku_csv(n=12, velocity=True):
    header = HEADER if velocity else HEADER.rsplit(",", 1)[0]
    lines = [header]
    for i in range(n):
        vel = ("A", "B", "C")[i % 3]
        row = f"WID-{i:03d},{20 + i},{15 + i},{10 + i},{1.5 + i}"
        lines.append(row + f",{vel}" if velocity else row)
    return "\n".join(lines) + "\n"


def _orders_csv(rows):
    return "sku,qty\n" + "\n".join(f"{sku},{qty}" for sku, qty in rows) + "\n"


def test_parse_sku_csv_happy_path():
    rows = parse_sku_csv(_sku_csv(10))
    assert len(rows) == 10
    assert rows[0]["sku"] == "WID-000"
    assert rows[0]["length"] == 20.0
    assert rows[0]["velocity"] == "A"
    assert rows[1]["velocity"] == "B"


def test_parse_sku_csv_rejects_missing_columns_and_bad_rows():
    with pytest.raises(CsvImportError, match="missing column"):
        parse_sku_csv("sku,length_cm\nX,10\n")
    with pytest.raises(CsvImportError, match="row 2.*non-numeric"):
        parse_sku_csv(HEADER + "\nX,abc,10,10,1,A\n")
    with pytest.raises(CsvImportError, match="row 3.*duplicate"):
        parse_sku_csv(HEADER + "\nX,10,10,10,1,A\nx,10,10,10,1,B\n")
    with pytest.raises(CsvImportError, match="row 2.*positive"):
        parse_sku_csv(HEADER + "\nX,-5,10,10,1,A\n")
    with pytest.raises(CsvImportError, match="row 2.*negative"):
        parse_sku_csv(HEADER + "\nX,10,10,10,-1,A\n")
    with pytest.raises(CsvImportError, match="row 2.*velocity"):
        parse_sku_csv(HEADER + "\nX,10,10,10,1,Q\n")


def test_build_cartons_enforces_catalog_size():
    with pytest.raises(CsvImportError, match=f"at least {MIN_SKUS}"):
        build_cartons(_sku_csv(MIN_SKUS - 1))
    with pytest.raises(CsvImportError, match=f"at most {MAX_SKUS}"):
        build_cartons(_sku_csv(MAX_SKUS + 1))


def test_build_cartons_requires_velocity_or_orders():
    with pytest.raises(CsvImportError, match="velocity"):
        build_cartons(_sku_csv(10, velocity=False))


def test_build_cartons_keeps_declared_velocities_without_orders():
    cartons, assumptions = build_cartons(_sku_csv(9))
    assert [c.sku for c in cartons] == [f"WID-{i:03d}" for i in range(9)]
    assert [c.velocity for c in cartons] == ["A", "B", "C"] * 3
    # The synthetic-geometry assumptions are always disclosed.
    assert any("rack geometry" in a for a in assumptions)
    assert any("order stream" in a for a in assumptions)


def test_derive_velocities_uses_synthetic_abc_split():
    skus = [f"S{i}" for i in range(10)]
    picks = {sku: float(100 - i * 10) for i, sku in enumerate(skus)}
    vel = derive_velocities(picks, skus)
    # 10 SKUs: top 20% (2) A, next 30% (3) B, rest (5) C, ranked by picks.
    assert [vel[s] for s in skus] == ["A", "A", "B", "B", "B", "C", "C", "C", "C", "C"]


def test_orders_csv_overrides_velocity_column_and_discloses_it():
    # Reverse the declared classes: the LAST SKUs get the most picks.
    orders = _orders_csv([(f"WID-{i:03d}", (i + 1) * 5) for i in range(10)])
    cartons, assumptions = build_cartons(_sku_csv(10), orders)
    by_sku = {c.sku: c.velocity for c in cartons}
    assert by_sku["WID-009"] == "A" and by_sku["WID-008"] == "A"
    assert by_sku["WID-000"] == "C"
    assert any("derived from your order lines" in a for a in assumptions)
    assert any("velocity column was ignored" in a for a in assumptions)


def test_orders_csv_ignores_unknown_skus_but_discloses_count():
    orders = _orders_csv([("WID-000", 5), ("NOT-IN-MASTER", 3), ("ALSO-MISSING", 1)])
    _, assumptions = build_cartons(_sku_csv(10), orders)
    assert any("2 order line(s)" in a for a in assumptions)


def test_orders_csv_with_no_matching_rows_is_rejected():
    with pytest.raises(CsvImportError, match="no rows matching"):
        build_cartons(_sku_csv(10), _orders_csv([("NOPE", 1)]))


def test_imported_report_is_valid_and_deterministic():
    cartons, _ = build_cartons(_sku_csv(15))
    r1 = report_for_dataset(dataset_from_cartons(cartons))
    assert r1["n_slots"] == 15
    s = r1["slotting"]
    assert s["plan_valid"] and s["sequence_valid"]
    assert s["optimized_travel"] <= s["legacy_travel"]
    # Same CSV in, identical figures out (seeded warehouse + order stream).
    cartons2, _ = build_cartons(_sku_csv(15))
    r2 = report_for_dataset(dataset_from_cartons(cartons2))
    assert r1["slotting"]["reduction_pct"] == r2["slotting"]["reduction_pct"]
    assert r1["packing"]["ffd_containers"] == r2["packing"]["ffd_containers"]
    assert r1["simulation"]["cycle_time_reduction_pct"] == r2["simulation"]["cycle_time_reduction_pct"]
