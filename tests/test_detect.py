import numpy as np
import pandas as pd

from solarmon.db import read_readings
from solarmon.detect import Thresholds, _runs, baseline_plant, detect_plant
from solarmon.evaluate import PRODUCTION, match, observable
from solarmon.plants import PLANT_BY_ID, PLANTS


def test_runs():
    assert _runs(np.array([0, 1, 1, 0, 1], dtype=bool)) == [(1, 2), (4, 4)]
    assert _runs(np.zeros(3, dtype=bool)) == []


def _all(con, sim, fn):
    out = []
    for plant in PLANTS:
        out += fn(read_readings(con, plant.id), plant, sim.start, sim.end)
    return out


def _visible(con, sim):
    from solarmon.detect import daylight_mask
    vis = []
    for plant in PLANTS:
        day = daylight_mask(read_readings(con, plant.id), plant, sim.start, sim.end)
        vis += [t for t in sim.truth if t.plant_id == plant.id and observable(t, day)]
    return vis


def test_detector_finds_injected_faults(con, sim):
    detections = _all(con, sim, detect_plant)
    visible = _visible(con, sim)
    for kind in ("inverter_trip", "string_failure", "sensor_stuck", "comm_loss"):
        m = match(detections, visible, (kind,))
        if m["truth"]:
            assert m["recall"] >= 0.9, (kind, m)
            assert m["precision"] >= 0.85, (kind, m)


def test_peer_comparison_raises_fewer_false_alarms_than_sensor_check(con, sim):
    visible = _visible(con, sim)
    peer = match(_all(con, sim, detect_plant), visible, PRODUCTION, typed=False)
    base = match(_all(con, sim, baseline_plant), visible, PRODUCTION, typed=False)
    assert peer["precision"] > base["precision"] and peer["detected"] < base["detected"]


def test_trip_detection_on_a_hand_made_feed():
    plant = PLANT_BY_ID["MAA"]
    times = pd.date_range("2025-03-01 06:00", "2025-03-01 18:00", freq="15min", tz="Asia/Kolkata")
    bell = 200 * np.sin(np.linspace(0, np.pi, len(times)))
    rows = []
    for n, inv in enumerate(plant.inverter_ids()):
        ac = bell.copy()
        if n == 2:
            ac[20:28] = 0  # two-hour trip at midday
        rows.append(pd.DataFrame({"ts": times, "inverter_id": inv, "ac_kw": ac, "poa_wm2": bell * 4 + np.arange(len(times)),
                                  "module_temp_c": 40.0}))
    found = detect_plant(pd.concat(rows), plant, times[0], times[-1])
    trips = [d for d in found if d.kind == "inverter_trip"]
    assert len(trips) == 1 and trips[0].device == "MAA-INV03"
    assert trips[0].start == times[20] and trips[0].end == times[27]
    assert trips[0].lost_kwh > 300 and trips[0].raised == times[21] + pd.Timedelta(minutes=15)


def test_stuck_sensor_on_a_hand_made_feed():
    plant = PLANT_BY_ID["MAA"]
    times = pd.date_range("2025-03-01 06:00", "2025-03-01 18:00", freq="15min", tz="Asia/Kolkata")
    bell = 200 * np.sin(np.linspace(0, np.pi, len(times)))
    poa = bell * 4 + np.arange(len(times))
    poa[10:30] = poa[10]
    rows = [pd.DataFrame({"ts": times, "inverter_id": inv, "ac_kw": bell, "poa_wm2": poa, "module_temp_c": 40.0})
            for inv in plant.inverter_ids()]
    found = detect_plant(pd.concat(rows), plant, times[0], times[-1], Thresholds())
    stuck = [d for d in found if d.kind == "sensor_stuck"]
    assert len(stuck) == 1 and stuck[0].start == times[10] and stuck[0].end == times[29]
    assert not [d for d in found if d.kind in ("inverter_trip", "string_failure")]  # production was fine
