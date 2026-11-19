"""The plant portfolio: three fictional utility-scale solar plants in India."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plant:
    id: str
    name: str
    city: str
    latitude: float
    longitude: float
    inverters: int
    inverter_ac_kw: float      # rated AC output of one inverter
    dc_ac_ratio: float         # DC array size relative to inverter rating
    tilt: float
    weather_source: str        # which real weather file supplies the cloud pattern
    climate_temp_c: float      # mean daytime air temperature
    soiling_per_day: float     # fraction of output lost per day without cleaning
    cleaning_every_days: int

    @property
    def dc_kw_per_inverter(self) -> float:
        return self.inverter_ac_kw * self.dc_ac_ratio

    @property
    def dc_capacity_kw(self) -> float:
        return self.dc_kw_per_inverter * self.inverters

    def inverter_ids(self) -> list[str]:
        return [f"{self.id}-INV{n:02d}" for n in range(1, self.inverters + 1)]


PLANTS: tuple[Plant, ...] = (
    Plant("BLR", "Kolar Solar Park (fictional)", "Bengaluru", 13.14, 78.13, 8, 250.0, 1.25, 13.0,
          "723170TYA.CSV", 27.0, 0.0020, 21),
    Plant("JDH", "Thar Sun Farm (fictional)", "Jodhpur", 26.24, 73.02, 10, 250.0, 1.30, 26.0,
          "723170TYA.CSV", 33.0, 0.0045, 14),
    Plant("MAA", "Coromandel Solar (fictional)", "Chennai", 13.08, 80.27, 6, 250.0, 1.20, 13.0,
          "12839.tm2", 31.0, 0.0025, 21),
)
PLANT_BY_ID = {p.id: p for p in PLANTS}
