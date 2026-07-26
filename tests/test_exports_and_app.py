import io

from logitwin.analysis import full_report, headline_numbers
from logitwin.exports import build_excel, build_pdf


def _sku_master_bytes(n=10):
    lines = ["sku,length_cm,width_cm,height_cm,weight_kg,velocity"]
    for i in range(n):
        lines.append(f"WID-{i:03d},{20 + i},{15 + i},{10 + i},{1.5 + i},{('A', 'B', 'C')[i % 3]}")
    return ("\n".join(lines) + "\n").encode()


def test_full_report_is_deterministic():
    a = headline_numbers(full_report())
    b = headline_numbers(full_report())
    assert a == b


def test_pdf_export_non_empty_bytes():
    data = build_pdf()
    assert isinstance(data, bytes)
    assert len(data) > 10_000
    assert data[:4] == b"%PDF"


def test_excel_export_non_empty_bytes():
    data = build_excel()
    assert isinstance(data, bytes)
    assert len(data) > 10_000
    assert data[:2] == b"PK"  # xlsx is a zip container


def test_flask_health_ok():
    from app import app

    client = app.test_client()
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "ok"
    assert body["synthetic"] is True


def test_flask_kpis_and_slots_and_scan():
    from app import app

    client = app.test_client()
    assert client.get("/api/kpis").status_code == 200

    slots = client.get("/api/slots").get_json()
    assert len(slots["slots"]) == 60
    # Every slot carries a human-readable Aisle-Bay-Level code.
    assert all(s["code"].startswith("A") and "-B" in s["code"] and "-L" in s["code"] for s in slots["slots"])

    res = client.post(
        "/api/scan",
        json={"sku": "SKU-0000", "length": 40, "width": 30, "height": 25, "weight": 6},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["fits_container"] is True
    assert body["sku_known"] is True
    assert body["recommended_container"] == 1


def test_flask_scan_overweight_never_recommends_a_container():
    from app import app

    client = app.test_client()
    res = client.post(
        "/api/scan",
        json={"sku": "SKU-0001", "length": 40, "width": 30, "height": 25, "weight": 350},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["over_weight"] is True
    assert body["fits_dimensions"] is True  # dims alone are fine
    assert body["fits_container"] is False  # but the carton is NOT shippable
    assert body["recommended_container"] is None
    assert body["placement"] is None
    assert "weight limit" in body["note"]


def test_flask_scan_rejects_non_positive_dimensions():
    from app import app

    client = app.test_client()
    # Negative dims.
    res = client.post("/api/scan", json={"length": -5, "width": -3, "height": -2, "weight": 1})
    assert res.status_code == 400
    # Zero dims.
    res = client.post("/api/scan", json={"length": 0, "width": 10, "height": 10, "weight": 1})
    assert res.status_code == 400
    # Empty body.
    res = client.post("/api/scan", json={})
    assert res.status_code == 400
    # Negative weight.
    res = client.post("/api/scan", json={"length": 10, "width": 10, "height": 10, "weight": -1})
    assert res.status_code == 400


def test_flask_scan_distinguishes_unknown_sku_and_matches_case_insensitively():
    from app import app

    client = app.test_client()
    dims = {"length": 40, "width": 30, "height": 25, "weight": 6}
    # Unknown SKU: flagged, not silently 'already optimal'.
    body = client.post("/api/scan", json={"sku": "HVY-1", **dims}).get_json()
    assert body["sku_known"] is False
    assert body["reslot_instruction"] is None
    # Lower-case typo of a real SKU still matches the catalog and the move plan.
    body = client.post("/api/scan", json={"sku": "sku-0000", **dims}).get_json()
    assert body["sku_known"] is True
    assert body["sku"] == "SKU-0000"
    assert body["reslot_instruction"] is not None
    assert body["reslot_instruction"]["from_code"].startswith("A")


def test_flask_reshuffle_moves_have_location_codes():
    from app import app

    client = app.test_client()
    body = client.get("/api/reshuffle").get_json()
    assert body["n_moves"] == len(body["moves"])
    for m in body["moves"][:5]:
        assert m["from_code"].startswith("A") and "-L" in m["from_code"]
        assert m["to_code"].startswith("A") and "-L" in m["to_code"]


def test_flask_reshuffle_serves_executable_sequence():
    from app import app

    client = app.test_client()
    body = client.get("/api/reshuffle").get_json()
    seq = body["sequence"]
    assert body["n_steps"] == len(seq)
    assert body["n_steps"] == body["n_moves"] + body["n_cycles"]  # one staging park per cycle
    assert [s["seq"] for s in seq] == list(range(1, len(seq) + 1))
    # Each cycle opens by parking a carton in STAGE and closes by retrieving it.
    stage_out = [s for s in seq if s["to_slot"] is None]
    stage_in = [s for s in seq if s["from_slot"] is None]
    assert len(stage_out) == len(stage_in) == body["n_cycles"]
    assert all(s["to_code"] == "STAGE" and s["staging"] for s in stage_out)
    assert all(s["from_code"] == "STAGE" and s["staging"] for s in stage_in)
    # Per-step savings add up to the total daily travel saved (what-if slider relies on this).
    assert abs(sum(s["saving_m_day"] for s in seq) - body["travel_saved_m_day"]) < 0.5
    # The tie-broken plan is no longer a full re-slot: some SKUs stay put.
    assert body["n_moves"] < 60


def test_flask_import_roundtrip_and_reset():
    from app import app

    client = app.test_client()
    try:
        res = client.post(
            "/api/import",
            data={"sku_csv": (io.BytesIO(_sku_master_bytes(10)), "my-skus.csv")},
            content_type="multipart/form-data",
        )
        assert res.status_code == 200
        body = res.get_json()
        assert body["ok"] is True
        assert body["synthetic"] is False
        assert body["n_skus"] == 10
        # The synthetic-geometry assumptions are always disclosed on an import.
        assert any("rack geometry" in a for a in body["source"]["assumptions"])

        health = client.get("/api/health").get_json()
        assert health["synthetic"] is False
        assert health["source"]["filename"] == "my-skus.csv"

        # The whole dashboard now serves the imported catalog.
        slots = client.get("/api/slots").get_json()
        assert slots["synthetic"] is False
        assert len(slots["slots"]) == 10
        skus = {s["legacy_sku"] for s in slots["slots"]}
        assert "WID-000" in skus and "SKU-0000" not in skus

        reshuffle = client.get("/api/reshuffle").get_json()
        assert reshuffle["synthetic"] is False
        assert all(m["sku"].startswith("WID-") for m in reshuffle["moves"])

        scan = client.post(
            "/api/scan", json={"sku": "wid-000", "length": 20, "width": 15, "height": 10, "weight": 2}
        ).get_json()
        assert scan["sku_known"] is True
        assert scan["sku"] == "WID-000"
        # The synthetic catalog's SKUs are now unknown.
        scan = client.post(
            "/api/scan", json={"sku": "SKU-0000", "length": 20, "width": 15, "height": 10, "weight": 2}
        ).get_json()
        assert scan["sku_known"] is False

        # The page banner reflects the imported source honestly.
        html = client.get("/").get_data(as_text=True)
        assert "my-skus.csv" in html
        assert "Synthetic, seeded data" not in html
    finally:
        res = client.post("/api/reset")
        assert res.status_code == 200

    health = client.get("/api/health").get_json()
    assert health["synthetic"] is True
    assert len(client.get("/api/slots").get_json()["slots"]) == 60


def test_flask_import_rejects_bad_csv_and_keeps_state():
    from app import app

    client = app.test_client()
    # No file at all.
    assert client.post("/api/import", data={}, content_type="multipart/form-data").status_code == 400
    # Missing columns.
    res = client.post(
        "/api/import",
        data={"sku_csv": (io.BytesIO(b"sku,length_cm\nX,10\n"), "bad.csv")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 400
    assert "missing column" in res.get_json()["error"]
    # Too few SKUs.
    res = client.post(
        "/api/import",
        data={"sku_csv": (io.BytesIO(_sku_master_bytes(3)), "tiny.csv")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 400
    # A failed import never leaves the app in a half-imported state.
    health = client.get("/api/health").get_json()
    assert health["synthetic"] is True


def test_flask_index_serves_offline_page():
    from app import app

    client = app.test_client()
    html = client.get("/").get_data(as_text=True)
    assert "Warehouse Command" in html
    # No external resource hosts referenced (offline requirement).
    for needle in ("http://", "https://", "cdn", "googleapis", "unpkg"):
        assert needle not in html
