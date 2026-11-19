"""Fault detection on validated telemetry.

Main idea: compare each inverter with its peers in the same plant, not with the irradiance
sensor. Peers see the same sun, clouds, temperature and soiling, so the plant median is a
reference that does not break when the sensor does. The sensor is still checked, separately.

Detectors
    inverter_trip    near-zero output for 30+ minutes of daylight while peers produce
    string_failure   an inverter stuck several percent below its usual share of peer output, day after day
    sensor_stuck     the irradiance reading frozen for 2+ hours of daylight
    comm_loss        no data for 45+ minutes from a whole plant or from one inverter

Baseline (for comparison): the usual "actual vs. expected from the irradiance sensor" check.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .plants import Plant
from .simulate import INTERVAL_H, ac_power_kw


@dataclass(frozen=True)
class Thresholds:
    daylight_share: float = 0.05     # peers above 5% of rating = daylight
    trip_level: float = 0.02         # below 2% of rating = not producing
    trip_min_intervals: int = 2      # 30 minutes
    string_ratio: float = 0.94       # daily share of peer output below this = underperforming
    string_min_points: int = 8       # usable intervals needed to judge a day
    stuck_min_intervals: int = 8     # 2 hours
    gap_min_intervals: int = 3       # 45 minutes


@dataclass
class Detection:
    plant_id: str
    device: str
    kind: str
    start: pd.Timestamp
    end: pd.Timestamp
    lost_kwh: float
    detail: str
    raised: pd.Timestamp | None = None  # earliest moment the rule could fire on live data

    def __post_init__(self) -> None:
        if self.raised is None:
            self.raised = self.start


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Start and end (inclusive) positions of True runs."""
    if not mask.any():
        return []
    padded = np.concatenate([[False], mask, [False]])
    change = np.flatnonzero(np.diff(padded.astype(int)))
    return list(zip(change[::2], change[1::2] - 1, strict=True))


def _grid(readings: pd.DataFrame, column: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    index = pd.date_range(start, end, freq="15min")
    return readings.pivot(index="ts", columns="inverter_id", values=column).reindex(index)


def detect_plant(readings: pd.DataFrame, plant: Plant, start: pd.Timestamp, end: pd.Timestamp,
                 th: Thresholds | None = None) -> list[Detection]:
    th = th or Thresholds()
    ac = _grid(readings, "ac_kw", start, end).reindex(columns=plant.inverter_ids())
    rating = plant.inverter_ac_kw
    peer = ac.median(axis=1, skipna=True)
    daylight = (peer > th.daylight_share * rating).to_numpy()
    times = ac.index
    found: list[Detection] = []

    # Each inverter's usual share of the peer median (absorbs small, permanent mismatch).
    usable = daylight & (peer < 0.85 * rating).to_numpy()   # skip clipping, where everyone looks equal
    share = ac.div(peer, axis=0).where(np.repeat(usable[:, None], ac.shape[1], axis=1))
    usual = share.where(share > 0.5).median()

    for inverter in plant.inverter_ids():
        series = ac[inverter].to_numpy()
        expected = peer.to_numpy() * usual[inverter]

        # Trips, judged on daylight readings only, so a trip that runs overnight stays one event.
        idx = np.flatnonzero(daylight & ~np.isnan(series))
        low = series[idx] < th.trip_level * rating
        for a, b in _runs(low):
            if b - a + 1 < th.trip_min_intervals:
                continue
            i0, i1 = idx[a], idx[b]
            window = slice(i0, i1 + 1)
            lost = np.nansum((expected[window] - np.nan_to_num(series[window])).clip(0, None)) * INTERVAL_H
            found.append(Detection(plant.id, inverter, "inverter_trip", times[i0], times[i1], round(lost, 1),
                                   f"{b - a + 1} daylight intervals at zero output",
                                   raised=times[idx[a + th.trip_min_intervals - 1]] + pd.Timedelta(minutes=15)))

        # String failures: daily median of the inverter's share of peer output, relative to its usual share.
        tripped = np.zeros(len(series), dtype=bool)
        tripped[idx[low]] = True
        ok = usable & ~np.isnan(series) & ~tripped
        rel = pd.Series(np.where(ok, series / np.where(peer > 0, peer, np.nan) / usual[inverter], np.nan), index=times)
        daily = rel.groupby(times.normalize()).agg(["median", "count"])
        judged = daily["count"] >= th.string_min_points
        flagged = (judged & (daily["median"] < th.string_ratio)).to_numpy()
        # A day that cannot be judged does not split an ongoing event.
        bridged = flagged.copy()
        for k in range(1, len(flagged) - 1):
            if not judged.iloc[k] and flagged[k - 1] and flagged[k + 1]:
                bridged[k] = True
        for a, b in _runs(bridged):
            days = daily.index[a:b + 1]
            first, last = days[0], days[-1] + pd.Timedelta(hours=23, minutes=45)
            window = (times >= first) & (times <= last) & ok
            lost = np.nansum((expected[window] - series[window]).clip(0, None)) * INTERVAL_H
            level = float(daily["median"].iloc[a:b + 1].mean())
            found.append(Detection(plant.id, inverter, "string_failure", first, last, round(lost, 1),
                                   f"running at {level:.0%} of its usual share for {b - a + 1} day(s)",
                                   raised=first + pd.Timedelta(days=1)))  # a daily check: known at day end

        # Inverter-level communication gaps (the whole plant reporting is handled below).
        plant_up = ac.notna().any(axis=1).to_numpy()
        for a, b in _runs(np.isnan(series) & plant_up):
            if b - a + 1 >= th.gap_min_intervals:
                found.append(Detection(plant.id, inverter, "comm_loss", times[a], times[b], 0.0,
                                       f"no data for {(b - a + 1) * 15} minutes",
                                       raised=times[a] + pd.Timedelta(minutes=15 * th.gap_min_intervals)))

    # Plant-level checks.
    for a, b in _runs(ac.isna().all(axis=1).to_numpy()):
        if b - a + 1 >= th.gap_min_intervals:
            found.append(Detection(plant.id, plant.id, "comm_loss", times[a], times[b], 0.0,
                                   f"whole plant silent for {(b - a + 1) * 15} minutes",
                                   raised=times[a] + pd.Timedelta(minutes=15 * th.gap_min_intervals)))
    poa = readings.groupby("ts")["poa_wm2"].first().reindex(times).to_numpy()
    frozen = np.zeros(len(poa), dtype=bool)
    frozen[1:] = (poa[1:] == poa[:-1]) & ~np.isnan(poa[1:])
    day_idx = np.flatnonzero(daylight)
    for a, b in _runs(frozen[day_idx]):  # on daylight readings only, so one frozen spell is one alert
        if b - a + 1 >= th.stuck_min_intervals:
            i0, i1 = day_idx[a], day_idx[b]
            found.append(Detection(plant.id, plant.id, "sensor_stuck", times[max(i0 - 1, 0)], times[i1], 0.0,
                                   f"irradiance reading frozen at {poa[i0]:.0f} W/m2",
                                   raised=times[day_idx[a + th.stuck_min_intervals - 1]] + pd.Timedelta(minutes=15)))
    return found


def baseline_plant(readings: pd.DataFrame, plant: Plant, start: pd.Timestamp, end: pd.Timestamp,
                   ratio: float = 0.8, min_intervals: int = 2) -> list[Detection]:
    """The common approach: flag an inverter producing under 80% of what the irradiance sensor predicts."""
    ac = _grid(readings, "ac_kw", start, end).reindex(columns=plant.inverter_ids())
    poa = readings.groupby("ts")["poa_wm2"].first().reindex(ac.index).to_numpy()
    temp = readings.groupby("ts")["module_temp_c"].first().reindex(ac.index).to_numpy()
    expected = ac_power_kw(np.nan_to_num(poa), np.nan_to_num(temp, nan=25), plant)
    sunny = np.nan_to_num(poa) > 200
    found = []
    for inverter in plant.inverter_ids():
        series = ac[inverter].to_numpy()
        idx = np.flatnonzero(sunny & ~np.isnan(series))
        under = series[idx] < ratio * expected[idx]
        for a, b in _runs(under):
            if b - a + 1 >= min_intervals:
                window = idx[a:b + 1]
                lost = float(np.sum(expected[window] - series[window]) * INTERVAL_H)
                found.append(Detection(plant.id, inverter, "underperformance", ac.index[idx[a]], ac.index[idx[b]],
                                       round(lost, 1), "below 80% of sensor-based expectation",
                                       raised=ac.index[idx[a + min_intervals - 1]] + pd.Timedelta(minutes=15)))
    return found


def daylight_mask(readings: pd.DataFrame, plant: Plant, start: pd.Timestamp, end: pd.Timestamp,
                  th: Thresholds | None = None) -> pd.Series:
    th = th or Thresholds()
    ac = _grid(readings, "ac_kw", start, end).reindex(columns=plant.inverter_ids())
    return ac.median(axis=1, skipna=True) > th.daylight_share * plant.inverter_ac_kw
