"""REST API and web dashboard.

    SOLARMON_DB=solar.db uvicorn solarmon.api:app --port 8000      # then open http://localhost:8000

Without SOLARMON_DB the app builds a 60-day demo database in memory at start-up.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .db import connect
from .report import monthly_report

STATIC = Path(__file__).parent / "static"
TRANSITIONS = {"open": {"acknowledged", "resolved"}, "acknowledged": {"resolved"}, "resolved": set()}


class AlertUpdate(BaseModel):
    status: Literal["acknowledged", "resolved"]
    note: str | None = Field(None, max_length=500)


class Database:
    """One SQLite connection shared by the API's worker threads.

    FastAPI runs plain ``def`` endpoints in a thread pool, so requests arrive concurrently.
    sqlite3 connections must not be used from two threads at once (that surfaced as
    "bad parameter or other API misuse" errors under parallel dashboard requests), so every
    statement runs, and is fully read, under one lock.
    """

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.lock = threading.Lock()

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.con.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        found = self.rows(sql, params)
        return found[0] if found else None

    def write(self, sql: str, params: tuple = ()) -> None:
        with self.lock:
            self.con.execute(sql, params)
            self.con.commit()

    def run(self, fn, *args):
        with self.lock:
            return fn(self.con, *args)


def create_app(con: sqlite3.Connection | None = None) -> FastAPI:
    state: dict[str, Database] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if con is not None:
            state["db"] = Database(con)
        elif os.environ.get("SOLARMON_DB"):
            state["db"] = Database(connect(os.environ["SOLARMON_DB"]))
        else:
            from .pipeline import build

            state["db"] = Database(build(":memory:", seed=11, days=60))
        yield

    app = FastAPI(title="Solar Plant Monitor", version=__version__, lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def db() -> Database:
        return state["db"]

    def require_plant(plant_id: str) -> None:
        if db().one("SELECT 1 FROM plants WHERE id = ?", (plant_id,)) is None:
            raise HTTPException(404, f"plant {plant_id} not found")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health() -> dict:
        readings = db().one("SELECT COUNT(*) AS n FROM readings")["n"]
        return {"status": "ok", "version": __version__, "readings": readings}

    @app.get("/api/plants")
    def plants() -> list[dict]:
        return db().rows(
            "SELECT p.*, COUNT(i.id) AS inverters, "
            "(SELECT COUNT(*) FROM alerts a WHERE a.plant_id = p.id AND a.status != 'resolved') AS open_alerts, "
            "(SELECT ROUND(SUM(energy_mwh), 1) FROM plant_monthly m WHERE m.plant_id = p.id) AS energy_mwh "
            "FROM plants p JOIN inverters i ON i.plant_id = p.id GROUP BY p.id ORDER BY p.id")

    @app.get("/api/plants/{plant_id}/daily")
    def plant_daily(plant_id: str, start: str | None = None, end: str | None = None) -> list[dict]:
        require_plant(plant_id)
        return db().rows(
            "SELECT * FROM plant_daily WHERE plant_id = ? AND day >= ? AND day <= ? ORDER BY day",
            (plant_id, start or "0000", end or "9999"))

    @app.get("/api/plants/{plant_id}/monthly")
    def plant_monthly(plant_id: str) -> list[dict]:
        require_plant(plant_id)
        return db().rows("SELECT * FROM plant_monthly WHERE plant_id = ? ORDER BY month", (plant_id,))

    @app.get("/api/plants/{plant_id}/inverters")
    def inverter_summary(plant_id: str) -> list[dict]:
        """Energy and availability per inverter, worst performers first."""
        require_plant(plant_id)
        return db().rows(
            "SELECT inverter_id, ROUND(SUM(energy_kwh) / 1000.0, 1) AS energy_mwh, "
            "ROUND(1.0 * SUM(producing_intervals) / SUM(daylight_intervals), 4) AS availability "
            "FROM inverter_daily WHERE plant_id = ? GROUP BY inverter_id ORDER BY energy_mwh", (plant_id,))

    @app.get("/api/alerts")
    def alerts(status: Literal["open", "acknowledged", "resolved"] | None = None, plant: str | None = None,
               kind: str | None = None, limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
        clauses, params = [], []
        for column, value in (("status", status), ("plant_id", plant), ("kind", kind)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return db().rows(f"SELECT * FROM alerts {where} ORDER BY lost_kwh DESC, start_ts DESC LIMIT ?",
                         (*params, limit))

    @app.get("/api/alerts/{alert_id}")
    def alert(alert_id: int) -> dict:
        row = db().one("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        if row is None:
            raise HTTPException(404, f"alert {alert_id} not found")
        return row

    @app.patch("/api/alerts/{alert_id}")
    def update_alert(alert_id: int, body: AlertUpdate) -> dict:
        current = alert(alert_id)
        if body.status not in TRANSITIONS[current["status"]]:
            raise HTTPException(409, f"cannot move an alert from {current['status']} to {body.status}")
        if body.status == "resolved" and not (body.note or "").strip():
            raise HTTPException(422, "a note is required to resolve an alert")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db().write("UPDATE alerts SET status = ?, note = COALESCE(?, note), updated_at = ? WHERE id = ?",
                   (body.status, body.note, now, alert_id))
        return alert(alert_id)

    @app.get("/api/data-quality")
    def data_quality() -> dict:
        loaded = db().one("SELECT COUNT(*) AS n FROM readings")["n"]
        rejected = db().rows("SELECT * FROM data_quality ORDER BY rows DESC")
        return {"loaded": loaded, "rejected": sum(r["rows"] for r in rejected), "by_reason": rejected}

    @app.get("/api/reports/monthly")
    def report(month: str = Query(..., pattern=r"^\d{4}-\d{2}$")) -> Response:
        try:
            content = db().run(monthly_report, month)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'")) from None
        return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="solar-report-{month}.xlsx"'})

    return app


app = create_app()
