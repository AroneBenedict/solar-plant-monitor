"""Monthly Excel report for asset managers: KPIs, alerts and data quality, one sheet each."""

from __future__ import annotations

import io
import sqlite3

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER = PatternFill("solid", fgColor="1F3A5F")


def _sheet(wb: Workbook, title: str, df: pd.DataFrame, formats: dict[str, str] | None = None):
    ws = wb.create_sheet(title)
    ws.append(list(df.columns))
    for cell in ws[1]:
        cell.font, cell.fill = Font(bold=True, color="FFFFFF"), HEADER
        cell.alignment = Alignment(horizontal="center")
    for row in df.itertuples(index=False):
        ws.append([None if pd.isna(v) else v for v in row])
    for i, column in enumerate(df.columns, 1):
        width = max([len(str(column))] + [len(str(v)) for v in df[column].head(200)]) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(width, 50)
        if formats and column in formats:
            for cell in ws[get_column_letter(i)][1:]:
                cell.number_format = formats[column]
    ws.freeze_panes = "A2"
    return ws


def monthly_report(con: sqlite3.Connection, month: str) -> bytes:
    """Workbook bytes for one month, e.g. '2025-03'."""
    kpi = pd.read_sql_query(
        "SELECT p.id AS plant, p.name, p.city, m.energy_mwh, m.specific_yield, m.avg_performance_ratio AS performance_ratio, "
        "m.avg_availability AS availability FROM plant_monthly m JOIN plants p ON p.id = m.plant_id "
        "WHERE m.month = ? ORDER BY p.id", con, params=(month,))
    if kpi.empty:
        raise KeyError(f"no data for {month}")
    alerts = pd.read_sql_query(
        "SELECT id, plant_id AS plant, device, kind, start_ts AS start, end_ts AS end, lost_kwh, status, detail "
        "FROM alerts WHERE substr(start_ts, 1, 7) = ? ORDER BY lost_kwh DESC", con, params=(month,))
    daily = pd.read_sql_query("SELECT plant_id AS plant, day, energy_kwh, performance_ratio, availability "
                              "FROM plant_daily WHERE substr(day, 1, 7) = ? ORDER BY plant_id, day", con, params=(month,))
    quality = pd.read_sql_query("SELECT reason, COUNT(*) AS rows FROM rejected_readings "
                                "WHERE substr(ts, 1, 7) = ? GROUP BY reason", con, params=(month,))

    wb = Workbook()
    wb.remove(wb.active)
    ws = _sheet(wb, "Summary", kpi, {"performance_ratio": "0.0%", "availability": "0.0%", "energy_mwh": "#,##0.0"})
    chart = BarChart()
    chart.title, chart.y_axis.title = f"Energy by plant, {month}", "MWh"
    chart.add_data(Reference(ws, min_col=4, min_row=1, max_row=len(kpi) + 1), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=len(kpi) + 1))
    ws.add_chart(chart, "I2")
    _sheet(wb, "Alerts", alerts, {"lost_kwh": "#,##0.0"})
    _sheet(wb, "Daily", daily, {"performance_ratio": "0.0%", "availability": "0.0%"})
    _sheet(wb, "Data quality", quality)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
