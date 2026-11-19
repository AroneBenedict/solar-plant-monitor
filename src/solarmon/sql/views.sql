-- Reporting views. Readings are 15-minute averages, so energy (kWh) = kW x 0.25.

-- Energy and irradiation per inverter per day.
CREATE VIEW IF NOT EXISTS inverter_daily AS
SELECT
    i.plant_id,
    r.inverter_id,
    substr(r.ts, 1, 10)                         AS day,
    ROUND(SUM(r.ac_kw) * 0.25, 2)               AS energy_kwh,
    ROUND(SUM(r.poa_wm2) * 0.25 / 1000.0, 3)    AS insolation_kwh_m2,
    SUM(r.poa_wm2 > 50)                         AS daylight_intervals,
    SUM(r.poa_wm2 > 50 AND r.ac_kw > 0.5)       AS producing_intervals,
    i.dc_kw
FROM readings r
JOIN inverters i ON i.id = r.inverter_id
GROUP BY r.inverter_id, day;

-- Plant KPIs per day.
--   specific yield      = energy / DC capacity                      (kWh per kWp)
--   performance ratio   = energy / (insolation x DC capacity)       (IEC 61724 style)
--   availability        = intervals producing / daylight intervals
CREATE VIEW IF NOT EXISTS plant_daily AS
SELECT
    d.plant_id,
    d.day,
    ROUND(SUM(d.energy_kwh), 1)                                                     AS energy_kwh,
    ROUND(SUM(d.energy_kwh) / p.dc_capacity_kw, 3)                                  AS specific_yield,
    -- PR needs a trustworthy irradiance reading. It is left empty when the sensor reads implausibly low,
    -- when a stuck-sensor alert covers the day, or when the result is physically impossible (above 1).
    CASE WHEN AVG(d.insolation_kwh_m2) >= 1.0
          AND SUM(d.energy_kwh) <= 1.0 * SUM(d.insolation_kwh_m2 * d.dc_kw)
          AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.plant_id = d.plant_id AND a.kind = 'sensor_stuck'
                          AND substr(a.start_ts, 1, 10) <= d.day AND substr(a.end_ts, 1, 10) >= d.day)
         THEN ROUND(SUM(d.energy_kwh) / SUM(d.insolation_kwh_m2 * d.dc_kw), 3) END AS performance_ratio,
    ROUND(AVG(d.insolation_kwh_m2), 3)                                              AS insolation_kwh_m2,
    ROUND(1.0 * SUM(d.producing_intervals) / NULLIF(SUM(d.daylight_intervals), 0), 4) AS availability,
    COUNT(*)                                                                         AS inverters_reporting
FROM inverter_daily d
JOIN plants p ON p.id = d.plant_id
GROUP BY d.plant_id, d.day;

-- Plant KPIs per month.
CREATE VIEW IF NOT EXISTS plant_monthly AS
SELECT
    plant_id,
    substr(day, 1, 7)                    AS month,
    ROUND(SUM(energy_kwh) / 1000.0, 2)   AS energy_mwh,
    ROUND(SUM(specific_yield), 1)        AS specific_yield,
    ROUND(SUM(CASE WHEN performance_ratio IS NOT NULL THEN energy_kwh END)
          / SUM(CASE WHEN performance_ratio IS NOT NULL THEN energy_kwh / performance_ratio END), 3)
                                         AS avg_performance_ratio,
    ROUND(AVG(availability), 4)          AS avg_availability
FROM plant_daily
GROUP BY plant_id, month;

-- Rejected rows by reason, for the data-quality page.
CREATE VIEW IF NOT EXISTS data_quality AS
SELECT reason, COUNT(*) AS rows FROM rejected_readings GROUP BY reason;
