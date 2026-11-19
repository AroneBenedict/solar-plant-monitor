import pandas as pd

from solarmon.db import connect, ingest, load_plants, read_readings, validate


def test_validation_rules_and_reasons():
    raw = pd.DataFrame({
        "ts": pd.to_datetime(["2025-01-01 10:00"] * 6 + ["2025-01-01 10:15"], utc=True),
        "inverter_id": ["BLR-INV01", "BLR-INV01", "BLR-INV02", "XYZ-INV01", "BLR-INV03", "BLR-INV04", "BLR-INV05"],
        "ac_kw": [120.0, 120.0, -3.0, 50.0, 100.0, 9999.0, 80.0],
        "poa_wm2": [800, 800, 800, 800, 2000, 800, None],
        "module_temp_c": [40, 40, 40, 40, 40, 40, 40],
    })
    clean, rejected = validate(raw)
    assert len(clean) == 1
    assert sorted(rejected["reason"]) == ["ac_out_of_range", "ac_out_of_range", "duplicate", "irradiance_out_of_range",
                                          "missing_value", "unknown_device"]


def test_ingest_accounts_for_every_row(con, sim):
    counts = con.execute("SELECT (SELECT COUNT(*) FROM readings), (SELECT COUNT(*) FROM rejected_readings)").fetchone()
    assert counts[0] + counts[1] == len(sim.readings)
    assert {r["reason"] for r in con.execute("SELECT reason FROM data_quality")} == {"duplicate", "ac_out_of_range"}


def test_ingest_is_idempotent_for_resent_rows(sim):
    con = connect()
    load_plants(con)
    first = ingest(con, sim.readings.head(500))
    ingest(con, sim.readings.head(500))
    assert con.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == first["loaded"]


def test_kpi_views(built):
    con = built
    daily = pd.read_sql_query("SELECT * FROM plant_daily", con)
    assert set(daily["plant_id"]) == {"BLR", "JDH", "MAA"}
    pr = daily["performance_ratio"].dropna()
    assert pr.between(0.4, 1.0).all() and 0.7 < pr.median() < 0.9
    stuck = con.execute("SELECT substr(start_ts, 1, 10) FROM alerts WHERE kind = 'sensor_stuck'").fetchall()
    for (day,) in stuck:
        assert daily.loc[daily["day"] == day, "performance_ratio"].isna().all()
    assert daily["availability"].between(0, 1).all()
    monthly = pd.read_sql_query("SELECT * FROM plant_monthly", con)
    assert (monthly["energy_mwh"] > 0).all()
    total_daily = daily.groupby("plant_id")["energy_kwh"].sum() / 1000
    total_monthly = monthly.groupby("plant_id")["energy_mwh"].sum()
    assert ((total_daily - total_monthly).abs() < 1).all()


def test_read_readings_filters_by_plant(con):
    df = read_readings(con, "MAA")
    assert set(df["inverter_id"].str[:3]) == {"MAA"} and df["ts"].dt.tz is not None


def test_alert_status_is_constrained(con):
    import sqlite3

    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO alerts (plant_id, device, kind, start_ts, end_ts, status) "
                    "VALUES ('BLR', 'BLR', 'x', 'a', 'b', 'closed')")
