# Design notes

## Problem

A solar asset manager needs three things from monitoring: trustworthy numbers (energy, PR,
availability), fast notice when equipment fails, and few false alarms, because every alert
costs a technician's time. The raw feed is noisy: duplicates, corrupted values, gaps, and an
irradiance sensor that can itself fail.

## Data flow

```
raw 15-min feed -> validate (reject with reason) -> readings table -> SQL KPI views
                                                 -> detectors -> alerts table -> API / dashboard / Excel
```

## Decisions

**Reject, never drop.** Every invalid row goes to `rejected_readings` with the first rule it
broke, so loaded plus rejected always equals received, and data-quality questions have an
answer.

**Compare inverters with their peers, not only with the sensor.** Inverters in one plant see
the same sun, clouds, heat and dust. The plant median is a reference that stays correct when
the irradiance sensor fails or the grid curtails the plant. Each inverter's usual share of the
median is learned, which absorbs small permanent differences such as row shading. The usual
sensor-based check raised four times as many production alerts at much lower precision.

**Judge string failures daily.** A 10% loss is invisible in a single 15-minute reading under
passing clouds, but clear in a day's median. The cost is latency: such an alert is known at
the end of the first day.

**Judge trips and frozen sensors on daylight readings only,** so a fault that runs overnight
is one alert, not two.

**Report latency honestly.** Each alert records the earliest moment its rule could fire on
live data, and the delay is measured from the fault's true start to that moment.

**Blank the PR when the sensor is unreliable.** A frozen sensor makes PR meaningless (one test
day showed a PR of 2.06). PR is left empty on days with a stuck-sensor alert, implausibly low
insolation, or a physically impossible value.

**Alert workflow in the API.** Open, then acknowledged, then resolved. Moving backwards is
refused (409), and resolving needs a note, so the history says what was done.

**One locked SQLite connection.** The API's thread pool made concurrent use of one connection
fail intermittently. A lock around each statement and its fetch fixed it, and a regression
test with 40 parallel requests guards it.

## Evaluation

- Seed 1 for development and threshold tuning. Seeds 11, 12 and 13 for reported numbers.
- A fault that never overlaps daylight is excluded from recall, since it changes nothing.
- A detection matches a true fault on the same device, overlapping in time (one hour of
  slack), and of the same type for the per-type table.

## Next steps

1. Replace the simulator with a public plant dataset through the same `ingest` function.
2. PostgreSQL and a scheduled ingestion job instead of a one-off build.
3. Detect slow degradation and inverter derating, which the simulator does not inject yet.
4. Notifications (email or chat) when a high-loss alert opens.
