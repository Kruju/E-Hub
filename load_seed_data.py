"""Load the cleaned e-Hub CSV extracts into PostgreSQL.

Usage:
    python load_seed_data.py --dsn "postgresql://user:pass@localhost:5432/ehub"

Run schema.sql first:
    psql "$DSN" -f schema.sql
    python load_seed_data.py --dsn "$DSN"

The CSVs in seed/ are cleaned versions of the raw "e-Hub Data Structure" export.
See README.md for exactly what was changed and why (office_id typos fixed,
FTE figures descaled, junk rows dropped, etc.) -- nothing here invents numbers,
it only repairs referential-integrity breaks that were confirmed against the
raw file.
"""
from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

SEED_DIR = Path(__file__).parent / "seed"

# office_city_name / detail aren't unique identifiers for countries, so we
# derive the countries table straight out of offices.csv.
COUNTRY_OVERRIDE_NAMES = {
    # offices.csv lists Hong Kong under country_code CN / country_name "China"
    # (Hong Kong doesn't have its own ISO code in this dataset) -- kept as-is,
    # just documented here so it isn't mistaken for an error later.
}


def _none_if_blank(value: str | None):
    if value is None:
        return None
    value = value.strip()
    if value == "" or value.lower() == "nan":
        return None
    return value


def _bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("yes", "true", "1")


def read_csv(name: str) -> list[dict]:
    with open(SEED_DIR / name, newline="", encoding="utf-8") as f:
        return [
            {k: _none_if_blank(v) for k, v in row.items()}
            for row in csv.DictReader(f)
        ]


def load_countries(cur):
    offices = read_csv("offices.csv")
    seen = {}
    for row in offices:
        code = row.get("country_code")
        name = row.get("country_name")
        if code and name and code not in seen:
            seen[code] = name
    execute_values(
        cur,
        "INSERT INTO countries (country_code, country_name) VALUES %s ON CONFLICT DO NOTHING",
        list(seen.items()),
    )


def load_offices(cur):
    offices = read_csv("offices.csv")
    rows = [
        (
            r["office_id"],
            r["office_city_name"],
            r.get("office_detail"),
            r.get("country_code"),
            r.get("Entity Legal Name"),
            _bool(r.get("is_active")),
            _bool(r.get("is_virtual")),
        )
        for r in offices
    ]
    execute_values(
        cur,
        """INSERT INTO offices
           (office_id, office_city_name, office_detail, country_code,
            entity_legal_name, is_active, is_virtual)
           VALUES %s ON CONFLICT (office_id) DO NOTHING""",
        rows,
    )


def load_office_annual_metrics(cur):
    rows_raw = read_csv("office_annual_metrics.csv")
    rows = [
        (
            r["office_id"],
            int(float(r["reporting_year"])),
            float(r["fte_female"]) if r.get("fte_female") else None,
            float(r["fte_male"]) if r.get("fte_male") else None,
            float(r["fte_total"]) if r.get("fte_total") else None,
            int(float(r["working_days"])) if r.get("working_days") else None,
            float(r["percentage_average_ho"]) if r.get("percentage_average_ho") else None,
        )
        for r in rows_raw
    ]
    execute_values(
        cur,
        """INSERT INTO office_annual_metrics
           (office_id, reporting_year, fte_female, fte_male, fte_total,
            working_days, percentage_average_ho)
           VALUES %s ON CONFLICT (office_id, reporting_year) DO NOTHING""",
        rows,
    )


def load_activity_types(cur):
    rows_raw = read_csv("activity_types.csv")
    rows = [
        (
            r["activity_type_code"],
            r["activity_name"],
            r["scope"],
            int(float(r["scope3_category"])) if r.get("scope3_category") else None,
            r["category_name"],
            r["default_unit"],
        )
        for r in rows_raw
    ]
    execute_values(
        cur,
        """INSERT INTO activity_types
           (activity_type_code, activity_name, scope, scope3_category, category_name, default_unit)
           VALUES %s ON CONFLICT (activity_type_code) DO NOTHING""",
        rows,
    )


def load_emission_factors(cur):
    rows_raw = read_csv("emission_factors.csv")
    rows = [
        (
            r["factor_id"],
            r["activity_type_code"],
            int(float(r["reporting_year"])),
            r.get("location_vs_market"),
            r.get("country_code"),
            float(r["factor_value"]) if r.get("factor_value") else None,
            r.get("co2e_weight"),
            r.get("source_name"),
            r.get("comment"),
        )
        for r in rows_raw
        # EMPLOYEE_COMMUTING has no matching activity_types row (the factor
        # table keys it generically while activity_types splits it by mode
        # -- FOOT/BICYCLE/PUBLIC TRANSPORT/MOTOR VEHICLE). Skipped here since
        # the FK would reject it; see README for how commuting emissions are
        # handled (pass-through of the pre-computed activities.*_tco2e).
        if r["activity_type_code"] != "EMPLOYEE_COMMUTING"
    ]
    execute_values(
        cur,
        """INSERT INTO emission_factors
           (factor_id, activity_type_code, reporting_year, location_vs_market,
            country_code, factor_value, co2e_weight, source_name, comment)
           VALUES %s""",
        rows,
    )


def load_activities(cur):
    rows_raw = read_csv("activities.csv")
    rows = [
        (
            r["activity_id"],
            r["office_id"],
            int(float(r["reporting_year"])),
            r["period_start"][:10],
            r["period_end"][:10],
            r["activity_type_code"],
            float(r["quantity"]) if r.get("quantity") else None,
            float(r["market_based_tco2e"]) if r.get("market_based_tco2e") else None,
            float(r["location_based_tco2e"]) if r.get("location_based_tco2e") else None,
            _bool(r.get("is_estimated")),
            r.get("source_type"),
        )
        for r in rows_raw
    ]
    # No ON CONFLICT here: activity_id is not guaranteed unique in the
    # source (see schema.sql comment) -- every row is a distinct record.
    execute_values(
        cur,
        """INSERT INTO activities
           (activity_id, office_id, reporting_year, period_start, period_end,
            activity_type_code, quantity, market_based_tco2e, location_based_tco2e,
            is_estimated, source_type)
           VALUES %s""",
        rows,
    )


def load_category_1_2_overrides(cur):
    # Values taken directly from the one known-good "Output 2025" sample
    # (all-countries total). See README for why these are hardcoded.
    rows = [
        (2025, 11568.953877017651, "Static override -- no source activity data for Purchased Goods/Capital Goods (see README)"),
        (2024, 8898.48423153813, "Static override -- no source activity data for Purchased Goods/Capital Goods (see README)"),
        (2023, 7972.927446587648, "Static override -- no source activity data for Purchased Goods/Capital Goods (see README)"),
    ]
    execute_values(
        cur,
        """INSERT INTO category_1_2_overrides (reporting_year, tco2e_value, note)
           VALUES %s ON CONFLICT (reporting_year) DO NOTHING""",
        rows,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True, help="PostgreSQL connection string")
    args = parser.parse_args()

    conn = psycopg2.connect(args.dsn)
    try:
        with conn.cursor() as cur:
            load_countries(cur)
            load_offices(cur)
            load_office_annual_metrics(cur)
            load_activity_types(cur)
            load_emission_factors(cur)
            load_activities(cur)
            load_category_1_2_overrides(cur)
        conn.commit()
        print("Seed data loaded.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
