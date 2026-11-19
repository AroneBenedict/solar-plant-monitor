"""Realistic inverter telemetry with known faults, built from real weather and physical PV models.

How a reading is made:

1. Sun position for the plant (pvlib, NREL SPA algorithm) and a clear-sky irradiance.
2. Clouds: the hour-by-hour clearness pattern of a real NREL TMY weather file (bundled with
   pvlib) scales the clear-sky irradiance, so the plant sees real day-to-day weather swings.
3. Irradiance split into direct and diffuse (Erbs) and transposed onto the tilted panels.
4. Cell temperature (Faiman model), DC power (PVWatts) and inverter AC output with clipping.
5. Soiling builds up between cleanings; every inverter has a small, fixed mismatch.
6. Inverters do not see identical conditions: passing clouds cover different blocks of a large
   plant at different moments, and some rows are shaded when the sun is low.
7. The grid operator sometimes curtails the whole plant. That lowers output but is not a fault.
8. Faults are injected and recorded as ground truth: inverter trips, failed strings, a stuck
   irradiance sensor and communication outages; the feed also carries duplicate rows and
   out-of-range spikes, as real SCADA exports do.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
import pvlib

from .plants import PLANTS, Plant

INTERVAL_H = 0.25
GAMMA_PDC = -0.0037        # power temperature coefficient, per degree C
INVERTER_EFFICIENCY = 0.96
STRINGS_PER_INVERTER = 10


@dataclass(frozen=True)
class FaultEvent:
    id: int
    plant_id: str
    device: str        # inverter id, or the plant id for plant-level sensor and comms faults
    kind: str          # inverter_trip, string_failure, sensor_stuck, comm_loss
    start: pd.Timestamp
    end: pd.Timestamp  # inclusive
    severity: float    # fraction of output lost (trips: 1.0; strings: failed share)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"], d["end"] = self.start.isoformat(), self.end.isoformat()
        return d


@dataclass
class Simulation:
    readings: pd.DataFrame     # raw feed as it would arrive: ts, inverter_id, ac_kw, poa_wm2, module_temp_c
    truth: list[FaultEvent]
    true_energy: pd.DataFrame  # per inverter-day: actual and fault-free energy (for loss accuracy)
    start: pd.Timestamp
    end: pd.Timestamp


def _clearness_pattern(weather_file: str) -> np.ndarray:
    """Hourly clearness index (measured GHI / clear-sky GHI) from a real TMY file, 8760 values."""
    folder = os.path.join(os.path.dirname(pvlib.__file__), "data")
    path = os.path.join(folder, weather_file)
    if weather_file.endswith(".tm2"):
        data, meta = pvlib.iotools.read_tmy2(path)
        ghi = data["GHI"].to_numpy(dtype=float)
    else:
        data, meta = pvlib.iotools.read_tmy3(path, map_variables=True)
        ghi = data["ghi"].to_numpy(dtype=float)
    times = pd.date_range("2021-01-01", periods=len(ghi), freq="h", tz=f"Etc/GMT{int(-meta['TZ']):+d}")
    times = times + pd.Timedelta(minutes=30)  # TMY values describe the hour ending at the stamp
    zenith = pvlib.solarposition.get_solarposition(times, meta["latitude"], meta["longitude"])["apparent_zenith"]
    clear = pvlib.clearsky.haurwitz(zenith)["ghi"].to_numpy()
    kt = np.where(clear > 60, ghi / np.maximum(clear, 1), np.nan)
    kt = pd.Series(np.clip(kt, 0.03, 1.15)).ffill().bfill().to_numpy()
    return kt[:8760]


def weather(plant: Plant, times: pd.DatetimeIndex, rng: np.random.Generator) -> pd.DataFrame:
    solpos = pvlib.solarposition.get_solarposition(times, plant.latitude, plant.longitude)
    zenith, azimuth = solpos["apparent_zenith"], solpos["azimuth"]
    clear = pvlib.clearsky.haurwitz(zenith)["ghi"]

    kt_hourly = _clearness_pattern(plant.weather_source)
    offset = int(rng.integers(0, 365))  # each plant gets its own stretch of the weather year
    hour_index = (((times.dayofyear - 1 + offset) % 365) * 24 + times.hour).to_numpy()
    kt = kt_hourly[np.clip(hour_index, 0, 8759)]
    kt = kt * np.exp(rng.normal(0, 0.03, len(times)))       # passing clouds within the hour
    ghi = (clear.to_numpy() * kt).clip(0, None)

    decomposed = pvlib.irradiance.erbs(ghi, zenith, times.dayofyear)
    poa = pvlib.irradiance.get_total_irradiance(plant.tilt, 180, zenith, azimuth, decomposed["dni"], ghi,
                                                decomposed["dhi"], model="isotropic")["poa_global"].fillna(0)
    hours = times.hour + times.minute / 60
    seasonal = 3.0 * np.sin(2 * np.pi * (times.dayofyear - 80) / 365)
    temp_air = plant.climate_temp_c - 4 + seasonal + 6 * np.sin(2 * np.pi * (hours - 9) / 24)
    temp_cell = pvlib.temperature.faiman(poa, temp_air, wind_speed=2.0)
    return pd.DataFrame({"poa": poa.to_numpy(), "temp_cell": np.asarray(temp_cell), "zenith": zenith.to_numpy(),
                         "kt": kt}, index=times)


def _block_clouds(kt: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Multiplicative factor for one inverter block: clouds over this block, not the whole plant.

    Correlated in time (AR(1)), and larger on broken-cloud days than on clear or overcast ones.
    """
    variability = np.clip(4 * kt * (1 - kt), 0, 1)       # peaks at kt = 0.5 (broken clouds)
    noise = rng.normal(0, 1, len(kt))
    ar = np.empty_like(noise)
    ar[0] = noise[0]
    for i in range(1, len(noise)):
        ar[i] = 0.7 * ar[i - 1] + 0.71 * noise[i]
    return np.exp(0.07 * variability * ar)


def _row_shading(zenith: np.ndarray, strength: float) -> np.ndarray:
    """Loss on rows shaded by the row in front when the sun is low (morning and evening)."""
    return 1 - strength * np.clip((zenith - 65) / 20, 0, 1)


def ac_power_kw(poa: np.ndarray, temp_cell: np.ndarray, plant: Plant) -> np.ndarray:
    """PVWatts DC model and inverter, per inverter, in kW. Also used by the detector as 'expected'."""
    dc_w = pvlib.pvsystem.pvwatts_dc(poa, temp_cell, plant.dc_kw_per_inverter * 1000, GAMMA_PDC)
    dc_w = np.asarray(dc_w, dtype=float) * 0.97  # wiring and mismatch losses
    ac_w = pvlib.inverter.pvwatts(dc_w, plant.inverter_ac_kw * 1000 / INVERTER_EFFICIENCY, INVERTER_EFFICIENCY)
    return np.clip(np.asarray(ac_w, dtype=float), 0, plant.inverter_ac_kw * 1000) / 1000


def _soiling(plant: Plant, times: pd.DatetimeIndex, rng: np.random.Generator) -> np.ndarray:
    days = (times.normalize() - times[0].normalize()).days.to_numpy()
    phase = int(rng.integers(0, plant.cleaning_every_days))
    since_clean = (days + phase) % plant.cleaning_every_days
    return np.clip(1 - plant.soiling_per_day * since_clean, 0.85, 1)


def _events(kind: str, rate_per_day: float, n_days: int, rng: np.random.Generator,
            duration_h: tuple[float, float]) -> list[tuple[int, int]]:
    """(start interval, length in intervals) for a Poisson process of events."""
    out = []
    for _ in range(rng.poisson(rate_per_day * n_days)):
        start = int(rng.integers(0, n_days * 96))
        length = max(1, int(rng.uniform(*duration_h) / INTERVAL_H))
        out.append((start, length))
    return sorted(out)


def simulate(seed: int = 1, start: str = "2025-01-01", days: int = 365, plants: tuple[Plant, ...] = PLANTS) -> Simulation:
    rng = np.random.default_rng(seed)
    times = pd.date_range(start, periods=days * 96, freq="15min", tz="Asia/Kolkata")
    frames, truth, energy = [], [], []

    def record(plant_id: str, device: str, kind: str, a: int, length: int, severity: float) -> None:
        b = min(a + length, len(times)) - 1
        truth.append(FaultEvent(len(truth), plant_id, device, kind, times[a], times[b], round(severity, 3)))

    for plant in plants:
        wx = weather(plant, times, rng)
        soil = _soiling(plant, times, rng)
        clean_ac = ac_power_kw(wx["poa"].to_numpy(), wx["temp_cell"].to_numpy(), plant)

        poa_meas = wx["poa"].to_numpy() * (1 + rng.normal(0, 0.015, len(times)))
        for a, length in _events("sensor_stuck", 1 / 45, days, rng, (4, 72)):
            b = min(a + length, len(times))
            poa_meas[a:b] = poa_meas[a]
            record(plant.id, plant.id, "sensor_stuck", a, length, 0.0)
        poa_meas = poa_meas.clip(0, None)
        temp_meas = wx["temp_cell"].to_numpy() + rng.normal(0, 0.8, len(times))

        curtail = np.ones(len(times))
        for a, length in _events("curtailment", 1 / 20, days, rng, (1, 4)):
            curtail[a:a + length] = rng.uniform(0.6, 0.85)  # cap as a share of rated output (not a fault)

        plant_present = np.ones(len(times), dtype=bool)
        for a, length in _events("comm_loss", 1 / 60, days, rng, (1, 10)):
            plant_present[a:a + length] = False
            record(plant.id, plant.id, "comm_loss", a, length, 0.0)

        for inverter in plant.inverter_ids():
            mismatch = rng.uniform(0.985, 1.01)
            shading = _row_shading(wx["zenith"].to_numpy(), rng.choice([0.0, 0.0, 0.1, 0.25]))
            healthy = clean_ac * soil * mismatch * shading * _block_clouds(wx["kt"].to_numpy(), rng)
            healthy = np.minimum(healthy, plant.inverter_ac_kw * curtail)
            factor = np.ones(len(times))
            for a, length in _events("string_failure", 1 / 120, days, rng, (36, 360)):
                failed = int(rng.integers(1, 4)) / STRINGS_PER_INVERTER
                factor[a:a + length] *= 1 - failed
                record(plant.id, inverter, "string_failure", a, length, failed)
            for a, length in _events("inverter_trip", 1 / 45, days, rng, (1, 48)):
                factor[a:a + length] = 0.0
                record(plant.id, inverter, "inverter_trip", a, length, 1.0)
            actual = healthy * factor * (1 + rng.normal(0, 0.006, len(times)))
            actual = np.where(clean_ac > 0, actual.clip(0, None), 0.0)

            present = plant_present.copy()
            for a, length in _events("comm_loss", 1 / 90, days, rng, (1, 8)):
                present[a:a + length] = False
                record(plant.id, inverter, "comm_loss", a, length, 0.0)

            frame = pd.DataFrame({"ts": times, "inverter_id": inverter, "ac_kw": actual.round(3),
                                  "poa_wm2": poa_meas.round(1), "module_temp_c": temp_meas.round(1)})[present]
            frames.append(frame)
            day = times.normalize()
            energy.append(pd.DataFrame({"day": day, "inverter_id": inverter, "actual_kwh": actual * INTERVAL_H,
                                        "fault_free_kwh": healthy * INTERVAL_H})
                          .groupby(["day", "inverter_id"], as_index=False).sum())

    readings = pd.concat(frames, ignore_index=True)
    # Feed defects that every SCADA export has: re-sent rows and corrupted values.
    dupes = readings.sample(frac=0.002, random_state=seed)
    spikes = readings.sample(frac=0.0005, random_state=seed + 1).copy()
    spikes["ac_kw"] = rng.choice([-50.0, 9999.0], len(spikes))
    readings = readings.drop(spikes.index)
    readings = pd.concat([readings, spikes, dupes]).sort_values(["ts", "inverter_id"], kind="stable")
    readings = readings.reset_index(drop=True)
    return Simulation(readings, truth, pd.concat(energy, ignore_index=True), times[0], times[-1])
