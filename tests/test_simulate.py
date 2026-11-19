import numpy as np
import pandas as pd

from solarmon.plants import PLANTS
from solarmon.simulate import ac_power_kw, simulate


def test_feed_shape_and_ranges(sim):
    r = sim.readings
    assert list(r.columns) == ["ts", "inverter_id", "ac_kw", "poa_wm2", "module_temp_c"]
    assert set(r["inverter_id"].str[:3]) == {"BLR", "JDH", "MAA"}
    clean = r[(r["ac_kw"] >= 0) & (r["ac_kw"] < 1000)]
    assert clean["ac_kw"].max() <= 250.0 * 1.03 and r["poa_wm2"].between(0, 1500).all()  # meter noise above rating


def test_no_output_at_night(sim):
    r = sim.readings
    night = r[r["ts"].dt.hour.isin([0, 1, 2, 3, 22, 23]) & (r["ac_kw"] >= 0) & (r["ac_kw"] < 1000)]
    assert night["ac_kw"].max() == 0 and night["poa_wm2"].max() < 5


def test_reproducible_and_seed_dependent():
    a, b, c = simulate(5, days=3), simulate(5, days=3), simulate(6, days=3)
    assert a.readings.equals(b.readings) and not a.readings.equals(c.readings)


def test_yearly_yield_is_realistic_for_india():
    sim = simulate(2, days=365, plants=PLANTS[:1])
    plant = PLANTS[0]
    kwh_per_kwp = sim.true_energy["fault_free_kwh"].sum() / plant.dc_capacity_kw
    assert 1350 < kwh_per_kwp < 1750  # typical Indian utility-scale range


def test_faults_are_recorded_and_visible(sim):
    kinds = {e.kind for e in sim.truth}
    assert kinds == {"inverter_trip", "string_failure", "sensor_stuck", "comm_loss"}
    trip = next(e for e in sim.truth if e.kind == "inverter_trip" and (e.end - e.start) > pd.Timedelta(hours=6))
    rows = sim.readings[(sim.readings["inverter_id"] == trip.device) & sim.readings["ts"].between(trip.start, trip.end)]
    valid = rows[(rows["ac_kw"] >= 0) & (rows["ac_kw"] < 1000)]
    assert (valid["ac_kw"] == 0).all()
    assert trip.to_dict()["start"].startswith(str(trip.start.year))


def test_feed_has_defects_to_clean(sim):
    r = sim.readings
    assert r.duplicated(["inverter_id", "ts"]).sum() > 0
    assert ((r["ac_kw"] < 0) | (r["ac_kw"] > 1000)).sum() > 0


def test_pvwatts_model_clips_at_rating():
    plant = PLANTS[1]
    out = ac_power_kw(np.array([0, 500, 1000, 1400]), np.array([25, 40, 50, 55]), plant)
    assert out[0] == 0 and 0 < out[1] < out[2] <= plant.inverter_ac_kw and out[3] == plant.inverter_ac_kw
