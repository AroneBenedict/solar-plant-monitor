# Evaluation report

Held-out seeds 11, 12, 13: one simulated year each for 3 plants and 24 inverters, 15-minute data. Thresholds were tuned on seed 1 only. Faults that never overlap daylight are excluded, since they have no visible effect. Delay runs from the fault's start to the moment the rule could first fire on live data, so a trip that starts at night counts the hours until sunrise, and string failures (a daily check) are known at the end of the first day.

## Detection by fault type

| Fault | Events | Precision | Recall | Median delay |
|---|---|---|---|---|
| inverter_trip | 525 | 100.0% | 100.0% | 1.13 h |
| string_failure | 203 | 93.5% | 100.0% | 21.21 h |
| sensor_stuck | 61 | 100.0% | 100.0% | 3.92 h |
| comm_loss | 349 | 100.0% | 99.4% | 0.75 h |

## Production faults: peer comparison vs. the usual sensor-based check

| Method | Alerts | Precision | Recall |
|---|---|---|---|
| Actual below 80% of irradiance-sensor expectation | 3034 | 73.8% | 96.4% |
| Peer comparison (this project) | 733 | 98.6% | 100.0% |

Energy lost to faults, estimated from alerts vs. true: mean error -13.6%.

Data validation: 2,513,437 rows received, 6,272 rejected (duplicates and out-of-range values), each with its reason recorded.
