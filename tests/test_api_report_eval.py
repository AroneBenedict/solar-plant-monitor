import io
import json

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from solarmon.api import create_app
from solarmon.evaluate import main
from solarmon.report import monthly_report


@pytest.fixture
def client(built):
    with TestClient(create_app(built)) as c:
        yield c


def test_plants_and_kpis(client):
    plants = client.get("/api/plants").json()
    assert [p["id"] for p in plants] == ["BLR", "JDH", "MAA"] and plants[0]["inverters"] == 8
    daily = client.get("/api/plants/JDH/daily", params={"start": "2025-01-05", "end": "2025-01-10"}).json()
    assert [d["day"] for d in daily] == [f"2025-01-{d:02d}" for d in range(5, 11)]
    assert client.get("/api/plants/JDH/monthly").json()[0]["month"] == "2025-01"
    inverters = client.get("/api/plants/BLR/inverters").json()
    assert len(inverters) == 8 and inverters[0]["energy_mwh"] <= inverters[-1]["energy_mwh"]
    assert client.get("/api/plants/XYZ/daily").status_code == 404


def test_alert_workflow(client):
    open_alerts = client.get("/api/alerts", params={"status": "open"}).json()
    assert open_alerts and open_alerts == sorted(open_alerts, key=lambda a: -a["lost_kwh"])
    aid = open_alerts[0]["id"]
    assert client.patch(f"/api/alerts/{aid}", json={"status": "acknowledged"}).json()["status"] == "acknowledged"
    assert client.patch(f"/api/alerts/{aid}", json={"status": "resolved"}).status_code == 422  # note required
    done = client.patch(f"/api/alerts/{aid}", json={"status": "resolved", "note": "Inverter reset on site"}).json()
    assert done["status"] == "resolved" and done["note"] == "Inverter reset on site"
    assert client.patch(f"/api/alerts/{aid}", json={"status": "acknowledged"}).status_code == 409
    assert client.patch(f"/api/alerts/{aid}", json={"status": "closed"}).status_code == 422
    assert client.get("/api/alerts/999999").status_code == 404
    kinds = {a["kind"] for a in client.get("/api/alerts", params={"plant": "BLR", "limit": 1000}).json()}
    assert kinds <= {"inverter_trip", "string_failure", "sensor_stuck", "comm_loss"}


def test_data_quality_and_dashboard(client):
    dq = client.get("/api/data-quality").json()
    assert dq["loaded"] > 0 and dq["rejected"] == sum(r["rows"] for r in dq["by_reason"])
    page = client.get("/")
    assert page.status_code == 200 and "Solar Plant Monitor" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/health").json()["status"] == "ok"


def test_excel_report(client, built):
    r = client.get("/api/reports/monthly", params={"month": "2025-01"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Summary", "Alerts", "Daily", "Data quality"]
    summary = wb["Summary"]
    assert [c.value for c in summary[1]][:3] == ["plant", "name", "city"] and summary.max_row == 4
    assert len(summary._charts) == 1
    assert client.get("/api/reports/monthly", params={"month": "2030-01"}).status_code == 404
    assert client.get("/api/reports/monthly", params={"month": "jan"}).status_code == 422
    with pytest.raises(KeyError):
        monthly_report(built, "1999-01")


def test_evaluation_cli(tmp_path, monkeypatch):
    import solarmon.evaluate as ev
    from solarmon.simulate import simulate as real

    monkeypatch.setattr(ev, "simulate", lambda seed: real(seed, days=20))
    result = main(["--seeds", "4", "--out", str(tmp_path)])
    saved = json.loads((tmp_path / "metrics.json").read_text())
    assert saved["summary"] == result["summary"]
    assert (tmp_path / "REPORT.md").read_text().startswith("# Evaluation report")


def test_parallel_requests_do_not_break_the_shared_connection(client):
    """The dashboard fires five requests at once; sqlite3 connections are not safe for that without a lock."""
    from concurrent.futures import ThreadPoolExecutor

    urls = ["/api/plants/BLR/daily", "/api/plants/BLR/monthly", "/api/plants/BLR/inverters", "/api/alerts",
            "/api/data-quality"] * 8
    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(lambda u: client.get(u).status_code, urls))
    assert codes == [200] * len(urls)
