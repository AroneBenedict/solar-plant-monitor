"""Score the detectors against the injected ground truth.

    python -m solarmon.evaluate                 # held-out seeds 11, 12, 13, one year each
    python -m solarmon.evaluate --seeds 11

Thresholds were tuned on seed 1 only; every number in the report comes from other seeds.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .db import connect, ingest, load_plants, read_readings
from .detect import Detection, Thresholds, baseline_plant, daylight_mask, detect_plant
from .pipeline import store_alerts
from .plants import PLANTS
from .simulate import FaultEvent, simulate

KINDS = ("inverter_trip", "string_failure", "sensor_stuck", "comm_loss")
PRODUCTION = ("inverter_trip", "string_failure")
DEV_SEED = 1
TEST_SEEDS = (11, 12, 13)
SLACK = pd.Timedelta(hours=1)


def observable(event: FaultEvent, daylight: pd.Series) -> bool:
    """A fault that never overlaps daylight has no effect on production or irradiance and cannot be seen."""
    if event.kind == "comm_loss":
        return True
    window = daylight.loc[event.start:event.end]
    return int(window.sum()) >= (2 if event.kind == "inverter_trip" else 8)


def _overlaps(d: Detection, t: FaultEvent, slack: pd.Timedelta) -> bool:
    return d.device == t.device and d.start <= t.end + slack and d.end >= t.start - slack


def match(detections: list[Detection], truth: list[FaultEvent], kinds: tuple[str, ...], typed: bool = True,
          slack: pd.Timedelta = SLACK) -> dict:
    """Precision, recall and detection delay. ``typed`` requires the detected kind to equal the true kind."""
    truth = [t for t in truth if t.kind in kinds]
    detections = [d for d in detections if d.kind in kinds or (not typed and d.kind == "underperformance")]
    hit_truth, delays = set(), []
    hit_det = 0
    for d in detections:
        matched = [t for t in truth if _overlaps(d, t, slack) and (not typed or d.kind == t.kind)]
        if matched:
            hit_det += 1
            for t in matched:
                if t.id not in hit_truth:
                    hit_truth.add(t.id)
                    delays.append(max((d.raised - t.start).total_seconds() / 3600, 0.0))
    precision = hit_det / len(detections) if detections else None
    recall = len(hit_truth) / len(truth) if truth else None
    return {"truth": len(truth), "detected": len(detections), "precision": _r(precision), "recall": _r(recall),
            "median_delay_h": _r(float(np.median(delays)) if delays else None, 2)}


def _r(x, digits: int = 4):
    return None if x is None else round(x, digits)


def run_seed(seed: int, th: Thresholds | None = None) -> dict:
    th = th or Thresholds()
    sim = simulate(seed)
    con = connect()
    load_plants(con)
    quality = ingest(con, sim.readings)
    detections, baseline, visible = [], [], []
    for plant in PLANTS:
        readings = read_readings(con, plant.id)
        detections += detect_plant(readings, plant, sim.start, sim.end, th)
        baseline += baseline_plant(readings, plant, sim.start, sim.end)
        daylight = daylight_mask(readings, plant, sim.start, sim.end, th)
        visible += [t for t in sim.truth if t.plant_id == plant.id and observable(t, daylight)]

    store_alerts(con, detections)  # so KPI views can exclude sensor-fault days, as in production
    out = {"seed": seed, "data_quality": quality, "truth_events": len(sim.truth), "observable_events": len(visible)}
    out["by_kind"] = {kind: match(detections, visible, (kind,)) for kind in KINDS}
    out["production_faults"] = {
        "peer_comparison": match(detections, visible, PRODUCTION, typed=False),
        "sensor_baseline": match(baseline, visible, PRODUCTION, typed=False),
    }
    # Energy lost to faults: estimate from alerts vs. the simulator's fault-free counterfactual.
    true_loss = float((sim.true_energy["fault_free_kwh"] - sim.true_energy["actual_kwh"]).sum())
    est_loss = float(sum(d.lost_kwh for d in detections if d.kind in PRODUCTION))
    out["lost_energy"] = {"true_mwh": round(true_loss / 1000, 1), "estimated_mwh": round(est_loss / 1000, 1),
                          "error": round(est_loss / true_loss - 1, 4)}
    kpi = pd.read_sql_query("SELECT plant_id, ROUND(SUM(energy_mwh), 1) AS mwh, ROUND(AVG(avg_performance_ratio), 3) AS pr, "
                            "ROUND(AVG(avg_availability), 4) AS availability FROM plant_monthly GROUP BY plant_id", con)
    out["plant_kpis"] = kpi.to_dict(orient="records")
    return out


def aggregate(runs: list[dict]) -> dict:
    def mean(path):
        values = []
        for r in runs:
            v = r
            for key in path:
                v = v[key]
            if v is not None:
                values.append(v)
        return round(float(np.mean(values)), 4) if values else None

    summary = {"by_kind": {}, "production_faults": {}}
    for kind in KINDS:
        summary["by_kind"][kind] = {m: mean(("by_kind", kind, m)) for m in ("precision", "recall", "median_delay_h")}
        summary["by_kind"][kind]["events"] = sum(r["by_kind"][kind]["truth"] for r in runs)
    for name in ("peer_comparison", "sensor_baseline"):
        summary["production_faults"][name] = {m: mean(("production_faults", name, m))
                                              for m in ("precision", "recall", "median_delay_h")}
        summary["production_faults"][name]["alerts"] = sum(r["production_faults"][name]["detected"] for r in runs)
    summary["lost_energy_error"] = mean(("lost_energy", "error"))
    summary["rows_received"] = sum(r["data_quality"]["received"] for r in runs)
    summary["rows_rejected"] = sum(r["data_quality"]["rejected"] for r in runs)
    return summary


def render(result: dict) -> str:
    s = result["summary"]
    pct = lambda v: "n/a" if v is None else f"{v:.1%}"  # noqa: E731
    lines = ["# Evaluation report", "",
             f"Held-out seeds {', '.join(map(str, result['seeds']))}: one simulated year each for 3 plants and 24 "
             "inverters, 15-minute data. Thresholds were tuned on seed 1 only. Faults that never overlap daylight "
             "are excluded, since they have no visible effect. Delay runs from the fault's start to the moment "
             "the rule could first fire on live data, so a trip that starts at night counts the hours until "
             "sunrise, and string failures (a daily check) are known at the end of the first day.", "",
             "## Detection by fault type", "",
             "| Fault | Events | Precision | Recall | Median delay |", "|---|---|---|---|---|"]
    for kind, m in s["by_kind"].items():
        delay = "n/a" if m["median_delay_h"] is None else f"{m['median_delay_h']:.2f} h"
        lines.append(f"| {kind} | {m['events']} | {pct(m['precision'])} | {pct(m['recall'])} | {delay} |")
    lines += ["", "## Production faults: peer comparison vs. the usual sensor-based check", "",
              "| Method | Alerts | Precision | Recall |", "|---|---|---|---|"]
    for name, label in (("sensor_baseline", "Actual below 80% of irradiance-sensor expectation"),
                        ("peer_comparison", "Peer comparison (this project)")):
        m = s["production_faults"][name]
        lines.append(f"| {label} | {m['alerts']} | {pct(m['precision'])} | {pct(m['recall'])} |")
    lines += ["", f"Energy lost to faults, estimated from alerts vs. true: mean error {s['lost_energy_error']:+.1%}.", "",
              f"Data validation: {s['rows_received']:,} rows received, {s['rows_rejected']:,} rejected "
              "(duplicates and out-of-range values), each with its reason recorded."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> dict:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seeds", type=int, nargs="+", default=list(TEST_SEEDS))
    parser.add_argument("--out", type=Path, default=Path("results"))
    args = parser.parse_args(argv)
    began = time.perf_counter()
    runs = []
    for seed in args.seeds:
        runs.append(run_seed(seed))
        print(f"seed {seed} done")
    result = {"seeds": args.seeds, "summary": aggregate(runs), "runs": runs,
              "seconds": round(time.perf_counter() - began, 1)}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "metrics.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (args.out / "REPORT.md").write_text(render(result), encoding="utf-8")
    print(render(result))
    return result


if __name__ == "__main__":
    main()
