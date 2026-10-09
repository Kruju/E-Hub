"""Regression test: locks in every line of the pivot against the figures
published in the Sustainability Report 2025 (Environmental Matters, p.273),
for all three years.

Run against a database that already has schema.sql + load_seed_data.py
applied:

    python -m pytest tests/test_pivot_against_sample.py --dsn "$DSN"

If this test starts failing after a change to calculations.py, that's a
real regression -- see README.md "Reconciliation with the published report"
for how each line was matched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from calculations import build_output_table  # noqa: E402

YEARS = (2025, 2024, 2023)

# Published values (2025, 2024, 2023), as rounded in the report. Absolute
# tolerance is 1 unit of the last published digit: the report rounds each
# line independently, so a total can sit one unit away from the sum of its
# rounded parts (e.g. 2024 Heat: 614.05 computed vs 615 published).
PUBLISHED = {
    "Total energy consumption in MWh": ((4185, 3920, 3948), 1),
    "Electricity": ((3594, 3244, 3148), 1),
    "Electricity (purchased)": ((3576, 3226, 3129), 1),
    "Of which energy consumption from renewable sources": ((0.94, 0.93, 0.93), 0.01),
    "Electricity (self-generated and consumed)": ((18, 18, 19), 1),
    "Heat": ((538, 615, 746), 1),
    "Biogas": ((73, 45, 41), 1),
    "Heat pump": ((206, 141, 157), 1),
    "District heating": ((30, 39, 32), 1),
    "Solar thermal collectors (self generated and consumed)": ((18, 16, 16), 1),
    "Fuels (petrol/diesel)": ((53, 61, 54), 1),
    "Total paper & waste consumption in tons": ((280, 290, 281), 1),
    "Paper consumption (t)": ((184, 182, 162), 1),
    "Waste (t)": ((96, 108, 119), 1),
    "Paper & waste consumption in t per FTE SQB ONLY": ((0.246, 0.288, 0.293), 0.001),
    "Water (m3)": ((4333, 4150, 3958), 1),
    "Water consumption in m3 per FTE SQB ONLY": ((3.80, 4.13, 4.13), 0.01),
    "Greenhouse gas emissions in tCO2e (Market Based, excluding financed emissions)": ((13595, 10937, 9928), 1.5),
    "Scope 1": ((51, 83, 105), 1),
    "Fuels": ((12, 15, 13), 1),
    "Scope 2 (Market Based)": ((130, 132, 124), 1),
    "Heat pump (Market Based)": ((0, 0, 0), 0.5),
    "District heating (Market Based)": ((5, 7, 6), 1),
    "Electricity (purchased) (Market Based)": ((125, 125, 118), 1),
    "District heating (Location Based)": ((5, 7, 6), 1),
    "Electricity (purchased) (Location Based)": ((535, 451, 432), 1),
    "Scope 3 operational": ((13414, 10722, 9699), 1),
    "Category 1 & 2 Purchased Goods & Services and Capital goods": ((11569, 8898, 7973), 1),
    "Category 5 - Waste generated in Operations (Waste and Water)": ((3, 3, 5), 1),
    "Category 6 - Business Travel": ((490, 655, 563), 1),
    "Category 7 - Employee Commuting": ((1352, 1166, 1158), 1),
    "FTE in locations covered by environmental indicators": ((1448, 1217, 1134), 1),
    "Category 15 - Investments": ((590700, 477714, 341918), 1),
}

# Per-FTE lines: the report divides by FTE rounded to a whole number, so
# these sit up to ~0.07% from the exact ratio -- checked relatively.
PUBLISHED_PER_FTE = {
    "Energy consumption in kWh per FTE": (2890, 3221, 3481),
    "Greenhouse gas emissions in kgCO2e per FTE (Market Based)": (9389, 8987, 8754),
}

# Known, documented gap (README "Still open"): the report's 2025 location-based
# heat pump figure (17 t) can't be reproduced from the export (206,015 kWh x
# the CH 2025 factor 118.1 g = 24.3 t). 2024/2023 match.
PUBLISHED_LOCATION_HEAT_PUMP = {
    "Heat pump (Location Based)": ((None, 14, 16), 1),
    "Scope 2 (Location Based)": ((None, 472, 454), 1),
}


@pytest.fixture(scope="module")
def rows(request):
    conn = psycopg2.connect(request.config.getoption("--dsn"))
    try:
        result = {}
        for r in build_output_table(conn, country_code=None, years=YEARS):
            # Some labels repeat under different parents (e.g. "Of which ...
            # renewable sources"); the first occurrence is the one checked.
            result.setdefault(r.description, r.values)
        yield result
    finally:
        conn.close()


def _cases(table):
    for description, (values, tolerance) in table.items():
        for year, value in zip(YEARS, values):
            if value is not None:
                yield pytest.param(description, year, value, tolerance, id=f"{description} [{year}]")


@pytest.mark.parametrize("description,year,expected,tolerance",
                         list(_cases(PUBLISHED)) + list(_cases(PUBLISHED_LOCATION_HEAT_PUMP)))
def test_line_matches_report(rows, description, year, expected, tolerance):
    actual = rows[description][year]
    assert actual is not None, f"{description!r} {year} came back None"
    assert actual == pytest.approx(expected, abs=tolerance), (
        f"{description!r} {year}: got {actual}, published {expected} (+/- {tolerance})"
    )


@pytest.mark.parametrize("description,year,expected", [
    pytest.param(d, y, v, id=f"{d} [{y}]")
    for d, values in PUBLISHED_PER_FTE.items() for y, v in zip(YEARS, values)
])
def test_per_fte_line_matches_report(rows, description, year, expected):
    actual = rows[description][year]
    assert actual is not None, f"{description!r} {year} came back None"
    assert actual == pytest.approx(expected, rel=1e-3)


def test_renewable_share_never_exceeds_100_percent(request):
    conn = psycopg2.connect(request.config.getoption("--dsn"))
    try:
        for r in build_output_table(conn, country_code="CH", years=YEARS):
            if "renewable sources" in r.description:
                assert all(v is None or v <= 1.0 for v in r.values.values())
    finally:
        conn.close()
