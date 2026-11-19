"""SQLite storage and the validated ingestion pipeline.

Every raw row is either loaded into ``readings`` or kept in ``rejected_readings`` with the
rule it broke, so nothing disappears silently and the data-quality report adds up.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from .plants import PLANTS, Plant

SCHEMA = """
CREATE TABLE IF NOT EXISTS plants (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL, latitude REAL, longitude REAL,
    dc_capacity_kw REAL NOT NULL, inverter_ac_kw REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS inverters (
    id TEXT PRIMARY KEY, plant_id TEXT NOT NULL REFERENCES plants(id), dc_kw REAL NOT NULL, ac_kw REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS readings (
    ts TEXT NOT NULL, inverter_id TEXT NOT NULL REFERENCES inverters(id),
    ac_kw REAL NOT NULL, poa_wm2 REAL NOT NULL, module_temp_c REAL NOT NULL,
    PRIMARY KEY (inverter_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rejected_readings (
    ts TEXT, inverter_id TEXT, ac_kw REAL, poa_wm2 REAL, module_temp_c REAL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id TEXT NOT NULL, device TEXT NOT NULL, kind TEXT NOT NULL,
    start_ts TEXT NOT NULL, end_ts TEXT NOT NULL, lost_kwh REAL NOT NULL DEFAULT 0, detail TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved')),
    note TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, plant_id);
"""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    con = sqlite3.connect(str(path), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    views = (Path(__file__).parent / "sql" / "views.sql").read_text(encoding="utf-8")
    con.executescript(views)
    return con


def load_plants(con: sqlite3.Connection, plants: tuple[Plant, ...] = PLANTS) -> None:
    con.executemany("INSERT OR REPLACE INTO plants VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(p.id, p.name, p.city, p.latitude, p.longitude, p.dc_capacity_kw, p.inverter_ac_kw) for p in plants])
    con.executemany("INSERT OR REPLACE INTO inverters VALUES (?, ?, ?, ?)",
                    [(i, p.id, p.dc_kw_per_inverter, p.inverter_ac_kw) for p in plants for i in p.inverter_ids()])
    con.commit()


def validate(raw: pd.DataFrame, plants: tuple[Plant, ...] = PLANTS) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a raw feed into (clean, rejected). Rules are applied in order; the first failure is recorded."""
    rating = {i: p.inverter_ac_kw for p in plants for i in p.inverter_ids()}
    df = raw.copy()
    df["reason"] = None

    def reject(mask: pd.Series, reason: str) -> None:
        df.loc[mask & df["reason"].isna(), "reason"] = reason

    reject(~df["inverter_id"].isin(rating), "unknown_device")
    reject(df[["ac_kw", "poa_wm2", "module_temp_c"]].isna().any(axis=1), "missing_value")
    limit = df["inverter_id"].map(rating).fillna(0) * 1.05
    reject((df["ac_kw"] < 0) | (df["ac_kw"] > limit), "ac_out_of_range")
    reject((df["poa_wm2"] < 0) | (df["poa_wm2"] > 1600), "irradiance_out_of_range")
    reject((df["module_temp_c"] < -20) | (df["module_temp_c"] > 95), "temperature_out_of_range")
    reject(df.duplicated(["inverter_id", "ts"], keep="first"), "duplicate")

    rejected = df[df["reason"].notna()].copy()
    clean = df[df["reason"].isna()].drop(columns="reason")
    return clean, rejected


def ingest(con: sqlite3.Connection, raw: pd.DataFrame) -> dict:
    clean, rejected = validate(raw)
    for frame in (clean, rejected):
        frame["ts"] = pd.to_datetime(frame["ts"]).dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    con.executemany("INSERT OR IGNORE INTO readings VALUES (?, ?, ?, ?, ?)",
                    clean[["ts", "inverter_id", "ac_kw", "poa_wm2", "module_temp_c"]].itertuples(index=False, name=None))
    con.executemany("INSERT INTO rejected_readings VALUES (?, ?, ?, ?, ?, ?)",
                    rejected[["ts", "inverter_id", "ac_kw", "poa_wm2", "module_temp_c", "reason"]]
                    .astype(object).where(rejected.notna(), None).itertuples(index=False, name=None))
    con.commit()
    return {"received": int(len(raw)), "loaded": int(len(clean)), "rejected": int(len(rejected)),
            "by_reason": {k: int(v) for k, v in rejected["reason"].value_counts().sort_index().items()}}


def read_readings(con: sqlite3.Connection, plant_id: str | None = None) -> pd.DataFrame:
    query = "SELECT r.* FROM readings r JOIN inverters i ON i.id = r.inverter_id"
    params: tuple = ()
    if plant_id:
        query += " WHERE i.plant_id = ?"
        params = (plant_id,)
    df = pd.read_sql_query(query, con, params=params)
    df["ts"] = pd.to_datetime(df["ts"], format="%Y-%m-%dT%H:%M:%S%z")
    return df
