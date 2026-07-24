from logitwin.analysis import full_report, headline_numbers
from logitwin.exports import build_excel, build_pdf


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

    res = client.post(
        "/api/scan",
        json={"sku": "SKU-0000", "length": 40, "width": 30, "height": 25, "weight": 6},
    )
    assert res.status_code == 200
    assert res.get_json()["fits_container"] is True


def test_flask_index_serves_offline_page():
    from app import app

    client = app.test_client()
    html = client.get("/").get_data(as_text=True)
    assert "Warehouse Command" in html
    # No external resource hosts referenced (offline requirement).
    for needle in ("http://", "https://", "cdn", "googleapis", "unpkg"):
        assert needle not in html
