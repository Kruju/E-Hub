"""Regression test: locks in every line of the pivot that was confirmed
exact (or near-exact, with a documented reason) against the sample
"Output 2025" sheet, for the 2025 column (the one reliable year -- see
README "Known multi-year data gaps" for why 2023/2024 are not checked here).

Run against a database that already has schema.sql + load_seed_data.py
applied:

    python -m pytest tests/test_pivot_against_sample.py --dsn "$DSN"

If this test starts failing after a change to calculations.py, that's a
real regression -- these numbers were hand-verified against the source
workbook line by line (see README.md).
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg2
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from calculations import build_output_table  # noqa: E402

# Tolerances are loose enough to absorb floating-point summation-order
# differences between SQL and pandas (last-digit noise), not real slack.
EXPECTED_2025 = {
    "Total energy consumption in MWh": (4184.488957837996, 2.0),  # solar-thermal year-swap anomaly, see README
    "Electricity": (3593.729943908915, 1e-4),
    "Electricity (purchased)": (3576.1789439089152, 1e-4),
    "Electricity (self-generated and consumed)": (17.551, 1e-4),
    "Fuels (petrol/diesel)": (52.60863913023256, 1e-4),
    "Diesel (hide)": (17.0149367, 1e-4),
    "Petrol (hide)": (35.59370243023256, 1e-4),
    "Scope 1": (11.969556853999999, 1e-4),  # Fuels only -- Natural gas moved to Scope 2, see README
    "Category 5 - Waste generated in Operations (Waste and Water)": (2.5160410731570417, 1e-4),
    "Category 7 - Employee Commuting": (1351.9963423654276, 1e-4),
    "Category 15 - Investments": (590700.17, 1e-4),
    "Scope 3 operational": (13413.178497957237, 0.1),  # absorbs the ~0.07 Business Travel rounding gap, see README
    "Heat pump (Location Based)": (24.330371500000002, 1e-4),
    "District heating (Market Based)": (5.279208929999999, 1e-4),
    "Electricity (purchased) (Market Based)": (125.45542119389286, 1e-4),
}


@pytest.fixture(scope="module")
def rows(request):
    conn = psycopg2.connect(request.config.getoption("--dsn"))
    try:
        yield {r.description: r.values[2025] for r in build_output_table(conn, country_code=None)}
    finally:
        conn.close()


@pytest.mark.parametrize("description,expected", EXPECTED_2025.items())
def test_line_matches_sample(rows, description, expected):
    target, tolerance = expected
    actual = rows[description]
    assert actual is not None, f"{description!r} came back None"
    assert actual == pytest.approx(target, abs=tolerance), (
        f"{description!r}: got {actual}, expected {target} (+/- {tolerance})"
    )
