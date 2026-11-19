"""Build a monitoring database: simulate the feed, validate and load it, detect faults, store alerts.

    python -m solarmon.pipeline --db solar.db --seed 11 --days 365
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .db import connect, ingest, load_plants, read_readings
from .detect import Detection, detect_plant
from .plants import PLANTS
from .simulate import simulate


def store_alerts(con: sqlite3.Connection, detections: list[Detection]) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = [(d.plant_id, d.device, d.kind, d.start.isoformat(), d.end.isoformat(), d.lost_kwh, d.detail, now)
            for d in sorted(detections, key=lambda d: (d.start, d.device))]
    con.executemany("INSERT INTO alerts (plant_id, device, kind, start_ts, end_ts, lost_kwh, detail, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    return len(rows)


def build(db: str | Path = ":memory:", seed: int = 11, days: int = 365) -> sqlite3.Connection:
    sim = simulate(seed, days=days)
    con = connect(db)
    load_plants(con)
    ingest(con, sim.readings)
    detections: list[Detection] = []
    for plant in PLANTS:
        detections += detect_plant(read_readings(con, plant.id), plant, sim.start, sim.end)
    store_alerts(con, detections)
    return con


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="solar.db")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args(argv)
    if Path(args.db).exists():
        Path(args.db).unlink()
    began = time.perf_counter()
    con = build(args.db, args.seed, args.days)
    counts = {row[0]: row[1] for row in con.execute("SELECT kind, COUNT(*) FROM alerts GROUP BY kind")}
    readings = con.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    print(f"built {args.db}: {readings:,} readings, alerts {counts} in {time.perf_counter() - began:.1f}s")


if __name__ == "__main__":
    main()
