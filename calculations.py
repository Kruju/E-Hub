"""e-Hub ESG pivot calculation engine.

Rebuilds the "Output 2025" pivot (Total energy, Scope 1/2/3 GHG emissions,
per-FTE ratios, etc.) straight from the normalized PostgreSQL tables, with an
optional country toggle.

Every formula below reproduces the figures published in the Sustainability
Report 2025 (Environmental Matters) for 2025, 2024 and 2023, UNLESS a comment
says otherwise. See README.md for the full line-by-line derivation, the
data-quality issues found in the source file, and the items that still need
real data from you (2024/2023 FTE and commuting, Category 1 & 2 source data).

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

# The Swiss offices that count as "SQB" (Swissquote Bank SA). Paper, waste
# and water are reported for SQB ONLY (report footnote: "based solely on
# Swissquote Bank Ltd due to data unavailability in other locations"), so both
# the quantities AND the per-FTE denominators use this list. Confirmed exact
# against the report for all 3 years. Yuh (YUH_CH) is legally part of the same
# country but explicitly excluded. SWITZERLAND_CH is the bank-level record
# that paper consumption is booked against (it has no FTE rows of its own).
SQB_ONLY_OFFICE_IDS = [
    "GLAND_HQ_CH",
    "GLAND_ELLIPSE_CH",
    "ZÜRICH_LOWENSTRASSE_CH",
    "ZÜRICH_STETTBACH_CH",
    "ZÜRICH_JOSEFSTRASSE_CH",
    "ZÜRICH_INSIDER-BAR_CH",
    "BERN_CH",
    "GLAND_INSIDER-BAR_CH",
    "SWITZERLAND_CH",
]

# On-site combustion for heating is reported under Scope 1 (GHG Protocol and
# the published report's "Scope 1 > Natural gas" line), even though
# activity_types.scope tags these SCOPE_2. Biogas is folded into the
# "Natural gas" line, exactly as the report does (38.621 + 0.016 = 38.637).
SCOPE_1_HEATING_TYPES = ["HEATING_NATURAL_GAS", "HEATING_BIOGAS"]

# Renewable share of purchased electricity, as reported:
#   (HYDROPOWER + RENEWABLE + self-generated SOLAR) / purchased electricity.
# Reproduces the sample's 93.525% / 93.108% / 93.396% (2025/2024/2023) to
# floating-point precision. Note the self-generated solar sits in the
# numerator but not the denominator -- that is how the reported figure is
# built, kept as-is so the numbers reconcile.
RENEWABLE_ELECTRICITY_TYPES = ["ELECTRICITY_HYDROPOWER", "ELECTRICITY_RENEWABLE"]

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
# small). Treated as a unit anomaly and left un-divided here. (Its 2023 and
# 2025 values were transposed in the source export; corrected in
# seed/activities.csv -- see README.)
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


def load_historical_overrides(conn, years: tuple[int, ...]) -> dict[tuple[str, int], float]:
    """{(metric, year): value} for figures missing from the export -- see
    schema.sql / README "Historical overrides". Only used as a fallback when
    the live data for that metric/year is empty."""
    sql = "SELECT metric, reporting_year, value FROM historical_overrides WHERE reporting_year = ANY(%s)"
    df = _fetch_df(conn, sql, (list(years),))
    return {(row.metric, int(row.reporting_year)): float(row.value) for row in df.itertuples()}


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
    overrides = load_historical_overrides(conn, years)

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

    def group_fte(y):
        live = float(fte_f.loc[fte_f["reporting_year"] == y, "fte_total"].sum(skipna=True))
        if live:
            return live
        # 2023/2024 FTE are #REF! in the export -- company-wide fallback only.
        return overrides.get(("FTE_GROUP", y)) if country_code is None else None

    total_fte = yearly(group_fte)
    energy_per_fte = {
        y: (total_energy[y] * 1000 / total_fte[y]) if total_fte[y] else None
        for y in years
    }

    # Renewable / non-renewable split of PURCHASED electricity -- see
    # RENEWABLE_ELECTRICITY_TYPES for the rule.
    renewable_mask = act_f["activity_type_code"].isin(RENEWABLE_ELECTRICITY_TYPES) | solar_mask
    # Capped at 100%: a country whose purchased electricity is all renewable
    # (e.g. CH) would otherwise exceed it because of the solar quirk above.
    renewable_share = {
        y: min(qty_mwh(renewable_mask, y) / electricity_purchased[y], 1.0) if electricity_purchased[y] else None
        for y in years
    }
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

    # SQB-only scope (see SQB_ONLY_OFFICE_IDS). For a country with no SQB
    # office these lines are None rather than a misleading 0.
    sqb_office_ids = [o for o in SQB_ONLY_OFFICE_IDS if country_code is None or _office_country(conn, o) == country_code]
    sqb_mask = act_f["office_id"].isin(sqb_office_ids)

    def sqb_qty(mask, year, divisor=1.0):
        if not sqb_office_ids:
            return None
        return _sum(act_f, mask & sqb_mask & (act_f["reporting_year"] == year), "quantity") / divisor

    paper_t = yearly(lambda y: sqb_qty(paper_mask, y, 1000))  # PAPER is kg
    waste_t = yearly(lambda y: sqb_qty(waste_mask, y))  # WASTE already in tons
    paper_waste_t = {y: (paper_t[y] + waste_t[y]) if paper_t[y] is not None else None for y in years}
    water_m3 = yearly(lambda y: sqb_qty(water_mask, y))

    def sqb_fte(y):
        if not sqb_office_ids:
            return None
        live = _sqb_fte(fte, y, sqb_office_ids)
        return live if live else overrides.get(("FTE_SQB", y))

    fte_sqb = yearly(sqb_fte)
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
    # Natural gas / biogas heating moves from SCOPE_2 (activity_types) to
    # Scope 1 -- see SCOPE_1_HEATING_TYPES.
    scope1_heating_mask = act_f["activity_type_code"].isin(SCOPE_1_HEATING_TYPES)
    scope1_mask = (act_f["scope"] == "SCOPE_1") | scope1_heating_mask
    scope2_mask = (act_f["scope"] == "SCOPE_2") & ~scope1_heating_mask

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
        year_mask = mask & (act_f["reporting_year"] == year)
        if cat == 7 and not year_mask.any():
            # No commuting rows exist for 2023/2024 in the export: use the
            # company-wide historical figure, or "no data" for one country.
            return overrides.get(("CATEGORY_7", year), 0.0) if country_code is None else None
        return _sum(act_f, year_mask, "market_based_tco2e")

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
    # Headline figure as published: market-based WITHOUT financed emissions.
    total_ghg_market = {
        y: (scope1[y] + scope2_market[y] + scope3_operational[y]) if scope3_operational[y] is not None else None
        for y in years
    }
    total_ghg_market_with_financed = {
        y: (total_ghg_market[y] + cat_15[y]) if total_ghg_market[y] is not None and cat_15[y] is not None else None
        for y in years
    }
    ghg_per_fte = {
        y: (total_ghg_market[y] * 1000 / total_fte[y])
        if total_ghg_market[y] is not None and total_fte.get(y) else None
        for y in years
    }

    def add(label, values, indent=0):
        rows.append(OutputRow(label, values, _delta(values.get(years[0]), values.get(years[1])), indent=indent))

    def by_code(codes, basis, include_solar=False):
        mask = act_f["activity_type_code"].isin(codes)
        if include_solar:
            mask |= solar_mask
        return yearly(lambda y: scope_sum(mask, basis, y))

    purchased_elec_codes = [c for c in ENERGY_ELECTRICITY_TYPES_ALL if c != ELECTRICITY_SELF_GENERATED_TYPE]

    add("Greenhouse gas emissions in tCO2e (Market Based, excluding financed emissions)", total_ghg_market)
    add("Scope 1", scope1, indent=1)
    add("Natural gas", by_code(SCOPE_1_HEATING_TYPES, "market"), indent=2)
    add("Fuels", by_code(FUEL_TYPES, "market"), indent=2)
    add("Scope 2 (Market Based)", scope2_market, indent=1)
    add("Heat pump (Market Based)", by_code(["HEATING_HEAT_PUMP"], "market"), indent=2)
    add("District heating (Market Based)", by_code(["HEATING_DISTRICT"], "market"), indent=2)
    add("Electricity (purchased) (Market Based)", by_code(purchased_elec_codes, "market"), indent=2)
    add("Scope 2 (Location Based)", scope2_location, indent=1)
    add("Heat pump (Location Based)", by_code(["HEATING_HEAT_PUMP"], "location"), indent=2)
    add("District heating (Location Based)", by_code(["HEATING_DISTRICT"], "location"), indent=2)
    # The report's location-based "Electricity (purchased)" line also carries
    # the grid-factor emissions recorded on the self-generated solar rows
    # (e.g. 532.898 + 2.073 = 534.971 for 2025), so they are included here.
    add("Electricity (purchased) (Location Based)", by_code(purchased_elec_codes, "location", include_solar=True), indent=2)
    add("Scope 3 operational", scope3_operational, indent=1)
    add("Category 1 & 2 Purchased Goods & Services and Capital goods", cat_1_2, indent=2)
    add("Category 5 - Waste generated in Operations (Waste and Water)", cat_5, indent=2)
    add("Category 6 - Business Travel", cat_6, indent=2)
    add("Category 7 - Employee Commuting", cat_7, indent=2)
    add("Greenhouse gas emissions in kgCO2e per FTE (Market Based)", ghg_per_fte)
    add("FTE in locations covered by environmental indicators", total_fte)
    add("Category 15 - Investments", cat_15)
    add("Scope 3 of which", scope3_of_which)
    add("Greenhouse gas emissions in tCO2e (Market Based, including financed emissions)", total_ghg_market_with_financed)

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
