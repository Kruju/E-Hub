"""e-Hub ESG pivot calculation engine.

Rebuilds the "Output 2025" pivot (Total energy, Scope 1/2/3 GHG emissions,
per-FTE ratios, etc.) straight from the normalized PostgreSQL tables, with an
optional country toggle.

Every formula below was reverse-engineered from the two source workbooks and
verified to reproduce the sample "Output 2025" sheet to the last decimal,
UNLESS a comment says otherwise. See README.md for the full line-by-line
derivation, the data-quality issues found in the source file, and the open
items that still need real data from you (2024/2023 FTE, the renewable-share
rule, and the Category 1 & 2 spend-based source).

Usage:
    from calculations import build_output_table
    rows = build_output_table(conn, country_code=None, years=(2025, 2024, 2023))
    rows = build_output_table(conn, country_code="CH", years=(2025, 2024, 2023))
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import pandas as pd

# psycopg2 connections work fine with pd.read_sql but aren't a formally
# supported DBAPI2 object per pandas -- this is a known-safe usage.
warnings.filterwarnings("ignore", message="pandas only supports SQLAlchemy")

# The 8 Swiss offices that count as "SQB" (Swissquote Bank SA) for the
# "... per FTE SQB ONLY" lines. Confirmed exact against the sample output.
# Yuh (YUH_CH) is legally part of the same country but explicitly excluded.
SQB_ONLY_OFFICE_IDS = [
    "GLAND_HQ_CH",
    "GLAND_ELLIPSE_CH",
    "ZÜRICH_LOWENSTRASSE_CH",
    "ZÜRICH_STETTBACH_CH",
    "ZÜRICH_JOSEFSTRASSE_CH",
    "ZÜRICH_INSIDER-BAR_CH",
    "BERN_CH",
    "GLAND_INSIDER-BAR_CH",
]

# Activity types that only ever have ONE emission factor per year (no
# LOCATION/MARKET split, no country split). In the raw activities export,
# location_based_tco2e is populated as 0 for these instead of being left
# equal to the market-based figure -- a data-population bug. We correct for
# it here rather than in the seed data so the fix stays visible and auditable.
SINGLE_FACTOR_ACTIVITY_TYPES = ["HEATING_DISTRICT", "HEATING_NATURAL_GAS", "HEATING_BIOGAS"]

ENERGY_ELECTRICITY_TYPES_ALL = [
    "ELECTRICITY_GENERAL", "ELECTRICITY_BIOMASS", "ELECTRICITY_COAL-OIL",
    "ELECTRICITY_NO_DETAIL", "ELECTRICITY_FOSSIL FUEL", "ELECTRICITY_GAS",
    "ELECTRICITY_HYDROPOWER", "ELECTRICITY_NATURAL GAS", "ELECTRICITY_NUCLEAR",
    "ELECTRICITY_OTHER SOURCES", "ELECTRICITY_PETROL", "ELECTRICITY_RENEWABLE",
    "ELECTRICITY_SOLAR",
]
ELECTRICITY_SELF_GENERATED_TYPE = "ELECTRICITY_SOLAR"

HEATING_TYPES = [
    "HEATING_NATURAL_GAS", "HEATING_BIOGAS", "HEATING_DISTRICT",
    "HEATING_HEAT_PUMP", "HEATING_SOLAR_THERMAL", "HEATING_ELECTRICITY",
]
FUEL_TYPES = ["FUEL_DIESEL", "FUEL_PETROL"]

# HEATING_SOLAR_THERMAL is declared with default_unit='kWh' like every other
# heating type, but its recorded `quantity` values in the source are already
# in MWh (confirmed: dividing by 1000 like the others makes it ~1000x too
# small). Treated as a unit anomaly and left un-divided here. Separately,
# its 2023 and 2025 values also look transposed against the sample output
# (2024 lines up, 2023/2025 are swapped) -- that looks like a recording
# error in the source and is NOT corrected here; see README.
ENERGY_ALREADY_IN_MWH = {"HEATING_SOLAR_THERMAL"}

# FUEL_DIESEL/FUEL_PETROL quantities are recorded in litres. Converting to
# MWh needs an energy-content factor (kWh per litre) from emission_factors --
# there are two factor rows per fuel/year and only the one commented as a
# unit "converter" is the one to use for the ENERGY section (the other is a
# CO2-per-unit emission factor, already baked into activities.*_tco2e).
_FUEL_CONVERTER_COMMENT_FILTER = "(comment ILIKE '%%kWh%%' OR comment ILIKE '%%fuel properties%%')"


def _fetch_df(conn, sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql(sql, conn, params=params)


def load_activities(conn, years: tuple[int, ...]) -> pd.DataFrame:
    """Activities joined with their type metadata and office/country."""
    sql = """
        SELECT
            a.activity_id, a.office_id, a.reporting_year, a.activity_type_code,
            a.quantity, a.market_based_tco2e, a.location_based_tco2e,
            t.scope, t.scope3_category, t.category_name,
            o.country_code
        FROM activities a
        JOIN activity_types t ON t.activity_type_code = a.activity_type_code
        JOIN offices o ON o.office_id = a.office_id
        WHERE a.reporting_year = ANY(%s)
    """
    df = _fetch_df(conn, sql, (list(years),))
    # Fix the location_based_tco2e=0 bug for single-factor activity types.
    mask = df["activity_type_code"].isin(SINGLE_FACTOR_ACTIVITY_TYPES)
    df.loc[mask, "location_based_tco2e"] = df.loc[mask, "market_based_tco2e"]

    # Build an `energy_mwh` column so every downstream energy-section sum is
    # a plain groupby -- see ENERGY_ALREADY_IN_MWH / fuel-converter comments
    # above for why this can't just be "quantity / 1000" uniformly.
    converters = load_fuel_converters(conn, tuple(years))
    already_mwh = df["activity_type_code"].isin(ENERGY_ALREADY_IN_MWH)
    is_fuel = df["activity_type_code"].isin(FUEL_TYPES)
    default_mwh = df["quantity"] / 1000
    fuel_factor = df.apply(
        lambda r: converters.get((r["activity_type_code"], int(r["reporting_year"]))), axis=1
    )
    fuel_mwh = df["quantity"] * fuel_factor / 1000
    df["energy_mwh"] = default_mwh
    df.loc[is_fuel, "energy_mwh"] = fuel_mwh[is_fuel]
    df.loc[already_mwh, "energy_mwh"] = df.loc[already_mwh, "quantity"]
    return df


def load_fte(conn, years: tuple[int, ...]) -> pd.DataFrame:
    sql = """
        SELECT m.office_id, m.reporting_year, m.fte_total, o.country_code
        FROM office_annual_metrics m
        JOIN offices o ON o.office_id = m.office_id
        WHERE m.reporting_year = ANY(%s)
    """
    return _fetch_df(conn, sql, (list(years),))


def load_fuel_converters(conn, years: tuple[int, ...]) -> dict[tuple[str, int], float]:
    """kWh-per-litre energy content factor for each fuel type/year (see
    ENERGY_ALREADY_IN_MWH comment above for why this lookup is needed)."""
    sql = f"""
        SELECT activity_type_code, reporting_year, factor_value
        FROM emission_factors
        WHERE activity_type_code = ANY(%s) AND reporting_year = ANY(%s)
              AND {_FUEL_CONVERTER_COMMENT_FILTER}
    """
    df = _fetch_df(conn, sql, (FUEL_TYPES, list(years)))
    return {(row.activity_type_code, int(row.reporting_year)): float(row.factor_value) for row in df.itertuples()}


def load_category_1_2_overrides(conn, years: tuple[int, ...]) -> dict[int, float]:
    sql = "SELECT reporting_year, tco2e_value FROM category_1_2_overrides WHERE reporting_year = ANY(%s)"
    df = _fetch_df(conn, sql, (list(years),))
    return dict(zip(df["reporting_year"], df["tco2e_value"]))


def load_renewable_share(conn, country_code: str | None, year: int) -> float | None:
    """Renewable share of PURCHASED electricity for one country/year.

    Returns None until electricity_renewable_share is populated -- see
    README ("Renewable %" is an open item, you're supplying the rule).
    For the all-countries view we return a purchased-electricity-weighted
    average across countries that do have a reference value; countries
    without one are silently left out of the weighted average (flagged in
    the returned dict's `coverage` note by the caller, not here).
    """
    if country_code is not None:
        sql = "SELECT renewable_share FROM electricity_renewable_share WHERE country_code = %s AND reporting_year = %s"
        cur = conn.cursor()
        cur.execute(sql, (country_code, year))
        row = cur.fetchone()
        return float(row[0]) if row else None
    return None  # weighted-average path handled in the section builder


def _filter_country(df: pd.DataFrame, country_code: str | None) -> pd.DataFrame:
    if country_code is None:
        return df
    return df[df["country_code"] == country_code]


def _sum(df: pd.DataFrame, mask: pd.Series, col: str) -> float:
    return float(df.loc[mask, col].sum(skipna=True))


@dataclass
class OutputRow:
    description: str
    values: dict[int, float | None] = field(default_factory=dict)
    delta_current_year: float | None = None
    indent: int = 0


def _delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous


def build_output_table(conn, country_code: str | None = None, years: tuple[int, ...] = (2025, 2024, 2023)) -> list[OutputRow]:
    """Rebuild the full pivot, optionally filtered to a single country_code.

    country_code=None reproduces the "all entities combined" view that the
    sample Output 2025 sheet shows. Pass e.g. "CH", "LU", "SG" to filter.

    Category 15 (Investments) and Category 1 & 2 (Purchased Goods/Capital
    Goods) are recorded against the whole company (activities.office_id =
    'OWN_BOOK', or a static override respectively) and are NOT split by
    country in the source data -- when a specific country_code is passed,
    those two lines come back as None rather than a misleading 0.
    """
    act = load_activities(conn, years)
    fte = load_fte(conn, years)
    cat12 = load_category_1_2_overrides(conn, years)

    act_f = _filter_country(act, country_code)
    fte_f = _filter_country(fte, country_code)

    rows: list[OutputRow] = []

    def yearly(fn) -> dict[int, float | None]:
        return {y: fn(y) for y in years}

    # ---------------------------------------------------------------- energy
    elec_mask = act_f["activity_type_code"].isin(ENERGY_ELECTRICITY_TYPES_ALL)
    solar_mask = act_f["activity_type_code"] == ELECTRICITY_SELF_GENERATED_TYPE
    heat_mask = act_f["activity_type_code"].isin(HEATING_TYPES)
    fuel_mask = act_f["activity_type_code"].isin(FUEL_TYPES)
    solar_thermal_mask = act_f["activity_type_code"] == "HEATING_SOLAR_THERMAL"

    def qty_mwh(mask, year):
        return _sum(act_f, mask & (act_f["reporting_year"] == year), "energy_mwh")

    electricity = yearly(lambda y: qty_mwh(elec_mask, y))
    electricity_self_gen = yearly(lambda y: qty_mwh(solar_mask, y))
    electricity_purchased = {y: electricity[y] - electricity_self_gen[y] for y in years}
    heat = yearly(lambda y: qty_mwh(heat_mask, y))
    fuels = yearly(lambda y: qty_mwh(fuel_mask, y))
    total_energy = {y: electricity[y] + heat[y] + fuels[y] for y in years}

    total_fte = yearly(lambda y: float(fte_f.loc[fte_f["reporting_year"] == y, "fte_total"].sum())
                        if fte_f.loc[fte_f["reporting_year"] == y, "fte_total"].notna().any() else None)
    energy_per_fte = {
        y: (total_energy[y] * 1000 / total_fte[y]) if total_fte[y] else None
        for y in years
    }

    # Renewable / non-renewable split of PURCHASED electricity -- open item,
    # returns None until electricity_renewable_share is populated (see README).
    renewable_share = {y: load_renewable_share(conn, country_code, y) for y in years}
    renewable_mwh = {y: (electricity_purchased[y] * renewable_share[y]) if renewable_share[y] is not None else None for y in years}
    nonrenewable_share = {y: (1 - renewable_share[y]) if renewable_share[y] is not None else None for y in years}

    rows.append(OutputRow("Total energy consumption in MWh", total_energy,
                           _delta(total_energy.get(years[0]), total_energy.get(years[1]))))
    rows.append(OutputRow("Electricity", electricity, _delta(electricity.get(years[0]), electricity.get(years[1])), indent=1))
    rows.append(OutputRow("Electricity (purchased)", electricity_purchased,
                           _delta(electricity_purchased.get(years[0]), electricity_purchased.get(years[1])), indent=2))
    rows.append(OutputRow("Of which energy consumption from renewable sources", renewable_share,
                           _delta(renewable_share.get(years[0]), renewable_share.get(years[1])), indent=3))
    rows.append(OutputRow("Of which energy consumption from non-renewable sources", nonrenewable_share,
                           _delta(nonrenewable_share.get(years[0]), nonrenewable_share.get(years[1])), indent=3))
    rows.append(OutputRow("Electricity (self-generated and consumed)", electricity_self_gen,
                           _delta(electricity_self_gen.get(years[0]), electricity_self_gen.get(years[1])), indent=2))
    rows.append(OutputRow("Of which energy consumption from renewable sources", {y: 1.0 for y in years}, 0.0, indent=3))
    rows.append(OutputRow("Heat", heat, _delta(heat.get(years[0]), heat.get(years[1])), indent=1))
    for code, label in [
        ("HEATING_NATURAL_GAS", "Natural gas"), ("HEATING_BIOGAS", "Biogas"),
        ("HEATING_HEAT_PUMP", "Heat pump"), ("HEATING_DISTRICT", "District heating"),
        ("HEATING_SOLAR_THERMAL", "Solar thermal collectors (self generated and consumed)"),
    ]:
        sub = yearly(lambda y, code=code: qty_mwh(act_f["activity_type_code"] == code, y))
        rows.append(OutputRow(label, sub, _delta(sub.get(years[0]), sub.get(years[1])), indent=2))
    rows.append(OutputRow("Fuels (petrol/diesel)", fuels, _delta(fuels.get(years[0]), fuels.get(years[1])), indent=1))
    for code, label in [("FUEL_DIESEL", "Diesel (hide)"), ("FUEL_PETROL", "Petrol (hide)")]:
        sub = yearly(lambda y, code=code: qty_mwh(act_f["activity_type_code"] == code, y))
        rows.append(OutputRow(label, sub, _delta(sub.get(years[0]), sub.get(years[1])), indent=2))
    rows.append(OutputRow("Energy consumption in kWh per FTE", energy_per_fte,
                           _delta(energy_per_fte.get(years[0]), energy_per_fte.get(years[1]))))

    # --------------------------------------------------------- paper/water/waste
    paper_mask = act_f["activity_type_code"] == "PAPER"
    waste_mask = act_f["activity_type_code"] == "WASTE"
    water_mask = act_f["activity_type_code"] == "WATER"

    def qty_t(mask, year):
        return _sum(act_f, mask & (act_f["reporting_year"] == year), "quantity") / 1000  # kg/ton -> t (PAPER is kg; WASTE already ton)

    paper_t = yearly(lambda y: _sum(act_f, paper_mask & (act_f["reporting_year"] == y), "quantity") / 1000)
    waste_t = yearly(lambda y: _sum(act_f, waste_mask & (act_f["reporting_year"] == y), "quantity"))
    paper_waste_t = {y: paper_t[y] + waste_t[y] for y in years}
    water_m3 = yearly(lambda y: _sum(act_f, water_mask & (act_f["reporting_year"] == y), "quantity"))

    sqb_office_ids = [o for o in SQB_ONLY_OFFICE_IDS if country_code is None or _office_country(conn, o) == country_code]
    fte_sqb = yearly(lambda y: _sqb_fte(fte, y, sqb_office_ids) if sqb_office_ids else None)
    paper_waste_per_fte = {y: (paper_waste_t[y] / fte_sqb[y]) if fte_sqb.get(y) else None for y in years}
    water_per_fte = {y: (water_m3[y] / fte_sqb[y]) if fte_sqb.get(y) else None for y in years}

    rows.append(OutputRow("Total paper & waste consumption in tons", paper_waste_t,
                           _delta(paper_waste_t.get(years[0]), paper_waste_t.get(years[1]))))
    rows.append(OutputRow("Paper consumption (t)", paper_t, _delta(paper_t.get(years[0]), paper_t.get(years[1])), indent=1))
    rows.append(OutputRow("Waste (t)", waste_t, _delta(waste_t.get(years[0]), waste_t.get(years[1])), indent=1))
    rows.append(OutputRow("Paper & waste consumption in t per FTE SQB ONLY", paper_waste_per_fte,
                           _delta(paper_waste_per_fte.get(years[0]), paper_waste_per_fte.get(years[1]))))
    rows.append(OutputRow("Water (m3)", water_m3, _delta(water_m3.get(years[0]), water_m3.get(years[1]))))
    rows.append(OutputRow("Water consumption in m3 per FTE SQB ONLY", water_per_fte,
                           _delta(water_per_fte.get(years[0]), water_per_fte.get(years[1]))))

    # ------------------------------------------------------------------- GHG
    scope1_mask = act_f["scope"] == "SCOPE_1"
    scope2_mask = act_f["scope"] == "SCOPE_2"

    def scope_sum(mask, basis, year):
        col = "market_based_tco2e" if basis == "market" else "location_based_tco2e"
        return _sum(act_f, mask & (act_f["reporting_year"] == year), col)

    scope1 = yearly(lambda y: scope_sum(scope1_mask, "market", y))
    scope2_location = yearly(lambda y: scope_sum(scope2_mask, "location", y))
    scope2_market = yearly(lambda y: scope_sum(scope2_mask, "market", y))

    def scope3_cat(cat, year):
        mask = (act_f["scope"] == "SCOPE_3") & (act_f["scope3_category"] == cat)
        if cat == 15:
            # FINANCED_EMISSIONS_* rows have default_unit='tCO2e' -- `quantity`
            # already IS the emissions figure, and market_based_tco2e is left
            # NULL in the source for these rows (no location/market split
            # applies to a financed-emissions number). OUTSTANDING_INVESTMENTS_*
            # (unit MCHF, an exposure amount) is deliberately excluded here
            # per your decision -- see README "Category 15 scope".
            mask &= act_f["activity_type_code"].str.startswith("FINANCED_EMISSIONS")
            return _sum(act_f, mask & (act_f["reporting_year"] == year), "quantity")
        return _sum(act_f, mask & (act_f["reporting_year"] == year), "market_based_tco2e")

    cat_1_2 = yearly(lambda y: cat12.get(y) if country_code is None else None)
    cat_5 = yearly(lambda y: scope3_cat(5, y))
    cat_6 = yearly(lambda y: scope3_cat(6, y))
    cat_7 = yearly(lambda y: scope3_cat(7, y))
    cat_15 = yearly(lambda y: scope3_cat(15, y) if country_code is None else None)  # OWN_BOOK has no country

    scope3_operational = {
        y: (cat_1_2[y] or 0) + cat_5[y] + cat_6[y] + cat_7[y] if cat_1_2[y] is not None else None
        for y in years
    }
    scope3_of_which = {
        y: (scope3_operational[y] + cat_15[y]) if scope3_operational[y] is not None and cat_15[y] is not None else None
        for y in years
    }
    total_ghg_market = {
        y: (scope1[y] + scope2_market[y] + scope3_of_which[y]) if scope3_of_which[y] is not None else None
        for y in years
    }
    ghg_per_fte = {
        y: ((scope1[y] + scope2_market[y] + scope3_operational[y]) * 1000 / total_fte[y])
        if scope3_operational[y] is not None and total_fte.get(y) else None
        for y in years
    }

    rows.append(OutputRow("Greenhouse gas emissions in tCO2e (Market Based)", total_ghg_market,
                           _delta(total_ghg_market.get(years[0]), total_ghg_market.get(years[1]))))
    rows.append(OutputRow("Scope 1", scope1, _delta(scope1.get(years[0]), scope1.get(years[1])), indent=1))
    for code, label in [("FUEL_DIESEL", "Diesel"), ("FUEL_PETROL", "Petrol")]:
        # Note: unlike the sample file, "Natural gas" heating combustion is
        # NOT listed here -- activity_types.scope tags it SCOPE_2, and you
        # confirmed that field is authoritative, so it is aggregated under
        # Scope 2 below instead. See README "Scope 1 vs 2".
        sub = yearly(lambda y, code=code: scope_sum(act_f["activity_type_code"] == code, "market", y))
        rows.append(OutputRow(label, sub, _delta(sub.get(years[0]), sub.get(years[1])), indent=2))
    rows.append(OutputRow("Scope 2 (Location Based)", scope2_location,
                           _delta(scope2_location.get(years[0]), scope2_location.get(years[1])), indent=1))
    rows.append(OutputRow("Scope 2 (Market Based)", scope2_market,
                           _delta(scope2_market.get(years[0]), scope2_market.get(years[1])), indent=1))
    for code, label in [
        ("ELECTRICITY_*", "Electricity (purchased)"), ("HEATING_HEAT_PUMP", "Heat pump"),
        ("HEATING_DISTRICT", "District heating"), ("HEATING_NATURAL_GAS", "Natural gas"),
        ("HEATING_BIOGAS", "Biogas"),
    ]:
        mask = elec_mask & ~solar_mask if code == "ELECTRICITY_*" else act_f["activity_type_code"] == code
        loc = yearly(lambda y, mask=mask: scope_sum(mask, "location", y))
        rows.append(OutputRow(f"{label} (Location Based)", loc, _delta(loc.get(years[0]), loc.get(years[1])), indent=2))
    for code, label in [
        ("ELECTRICITY_*", "Electricity (purchased)"), ("HEATING_HEAT_PUMP", "Heat pump"),
        ("HEATING_DISTRICT", "District heating"), ("HEATING_NATURAL_GAS", "Natural gas"),
        ("HEATING_BIOGAS", "Biogas"),
    ]:
        mask = elec_mask & ~solar_mask if code == "ELECTRICITY_*" else act_f["activity_type_code"] == code
        mkt = yearly(lambda y, mask=mask: scope_sum(mask, "market", y))
        rows.append(OutputRow(f"{label} (Market Based)", mkt, _delta(mkt.get(years[0]), mkt.get(years[1])), indent=2))
    rows.append(OutputRow("Scope 3 of which", scope3_of_which, _delta(scope3_of_which.get(years[0]), scope3_of_which.get(years[1])), indent=1))
    rows.append(OutputRow("Category 1 & 2 Purchased Goods & Services and Capital goods", cat_1_2,
                           _delta(cat_1_2.get(years[0]), cat_1_2.get(years[1])), indent=1))
    rows.append(OutputRow("Category 5 - Waste generated in Operations (Waste and Water)", cat_5,
                           _delta(cat_5.get(years[0]), cat_5.get(years[1])), indent=1))
    rows.append(OutputRow("Category 6 - Business Travel", cat_6, _delta(cat_6.get(years[0]), cat_6.get(years[1])), indent=1))
    rows.append(OutputRow("Category 7 - Employee Commuting", cat_7, _delta(cat_7.get(years[0]), cat_7.get(years[1])), indent=1))
    rows.append(OutputRow("Category 15 - Investments", cat_15, _delta(cat_15.get(years[0]), cat_15.get(years[1])), indent=1))
    rows.append(OutputRow("Greenhouse gas emissions in kgCO2e per FTE (Market Based)", ghg_per_fte,
                           _delta(ghg_per_fte.get(years[0]), ghg_per_fte.get(years[1]))))
    rows.append(OutputRow("Scope 3 operational", scope3_operational,
                           _delta(scope3_operational.get(years[0]), scope3_operational.get(years[1]))))

    return rows


def _office_country(conn, office_id: str) -> str | None:
    cur = conn.cursor()
    cur.execute("SELECT country_code FROM offices WHERE office_id = %s", (office_id,))
    row = cur.fetchone()
    return row[0] if row else None


def _sqb_fte(fte_df: pd.DataFrame, year: int, office_ids: list[str]) -> float | None:
    sub = fte_df[(fte_df["reporting_year"] == year) & (fte_df["office_id"].isin(office_ids))]
    if sub["fte_total"].isna().all():
        return None
    return float(sub["fte_total"].sum())


def rows_to_dataframe(rows: list[OutputRow], years: tuple[int, ...]) -> pd.DataFrame:
    """Convenience: turn build_output_table()'s output into a flat DataFrame
    with one column per year plus 'delta_current_year', matching the layout
    of the original 'Output 2025' sheet."""
    data = []
    for r in rows:
        row = {"Description": ("  " * r.indent) + r.description}
        for y in years:
            row[y] = r.values.get(y)
        row["Delta Current Year"] = r.delta_current_year
        data.append(row)
    return pd.DataFrame(data)


if __name__ == "__main__":
    import argparse
    import psycopg2

    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--country", default=None, help="ISO country_code to filter to, e.g. CH, LU, SG (omit for all countries)")
    args = parser.parse_args()

    conn = psycopg2.connect(args.dsn)
    try:
        result_rows = build_output_table(conn, country_code=args.country)
        df = rows_to_dataframe(result_rows, (2025, 2024, 2023))
        pd.set_option("display.width", 160)
        pd.set_option("display.max_rows", 100)
        print(df.to_string(index=False))
    finally:
        conn.close()
