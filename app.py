"""e-Hub ESG pivot — web dashboard.

A Streamlit front end over calculations.build_output_table(). Run with:

    streamlit run app.py

See README.md ("Web dashboard" section) for setup. This file only renders —
every number comes straight from calculations.py, so if a figure looks wrong
here, check there (or the underlying data) first.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import psycopg2
import streamlit as st

from calculations import build_output_table

YEARS = (2025, 2024, 2023)

# Categorical palette (dataviz skill's validated default), used in fixed
# order per entity -- never reassigned based on filters/rank.
BLUE, ORANGE, AQUA, YELLOW, MAGENTA = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4",
)
GOOD, CRITICAL = "#0ca30c", "#d03b3b"
GRIDLINE, AXIS_LINE, MUTED_TEXT, PRIMARY_TEXT = "#e1e0d9", "#c3c2b7", "#898781", "#0b0b0b"
CHART_SURFACE = "#fcfcfb"

st.set_page_config(page_title="e-Hub ESG Pivot", layout="wide")


# --------------------------------------------------------------------- data

@st.cache_resource(show_spinner=False)
def get_connection(dsn: str):
    return psycopg2.connect(dsn)


@st.cache_data(show_spinner=False)
def get_countries(dsn: str) -> list[tuple[str, str]]:
    conn = get_connection(dsn)
    df = pd.read_sql("SELECT country_code, country_name FROM countries ORDER BY country_name", conn)
    return list(df.itertuples(index=False, name=None))


@st.cache_data(show_spinner="Computing pivot from the database...")
def get_rows(dsn: str, country_code: str | None) -> list[dict]:
    conn = get_connection(dsn)
    rows = build_output_table(conn, country_code=country_code, years=YEARS)
    # Plain dicts so Streamlit can hash/cache the result.
    return [
        {"description": r.description, "values": r.values, "delta": r.delta_current_year, "indent": r.indent}
        for r in rows
    ]


# ------------------------------------------------------------------ sidebar

st.sidebar.header("Connection")
dsn = st.sidebar.text_input(
    "PostgreSQL DSN",
    value=st.session_state.get("dsn", "postgresql://postgres:postgres@localhost:5432/ehub"),
    help="Same connection string you use with calculations.py --dsn",
)
st.session_state["dsn"] = dsn

try:
    get_connection(dsn)
except Exception as exc:  # noqa: BLE001 -- surfacing any connection failure to the user
    st.error(
        "Couldn't connect to the database with that DSN.\n\n"
        f"**Error:** {exc}\n\n"
        "Check that PostgreSQL is running, the database exists, and the "
        "DSN matches what you used for `load_seed_data.py`."
    )
    st.stop()

st.sidebar.header("Filters")
countries = get_countries(dsn)
country_choices = [("All countries", None)] + [(f"{name} ({code})", code) for code, name in countries]
country_label = st.sidebar.selectbox("Country", [c[0] for c in country_choices])
selected_country = dict(country_choices)[country_label]

shown_years = st.sidebar.multiselect("Years shown in table", options=list(YEARS), default=list(YEARS))
if not shown_years:
    st.sidebar.warning("Pick at least one year.")
    st.stop()
shown_years = sorted(shown_years, reverse=True)

basis = st.sidebar.radio(
    "Scope 2 basis",
    ["Market Based", "Location Based"],
    help="Which Scope 2 figure feeds the KPI tiles and the scope chart below. "
         "Both totals always show in the table regardless of this toggle.",
)

show_detail = st.sidebar.checkbox(
    "Show line-item detail",
    value=False,
    help="Expand the sub-categories under Electricity, Heat, Fuels, and the Scope breakdowns.",
)

include_investments = st.sidebar.checkbox(
    "Include Category 15 (Investments) in category chart",
    value=False,
    help="Category 15 is 50-100x larger than every other Scope 3 category, so it's split "
         "out by default (log scale when included) to keep the operational categories readable.",
)

st.sidebar.caption(
    "Category 1&2 and Category 15 are recorded company-wide and can't be split "
    "by country — they show as “—” whenever a specific country is selected."
)

rows = get_rows(dsn, selected_country)
by_desc = {r["description"]: r for r in rows}


def val(description: str, year: int) -> float | None:
    row = by_desc.get(description)
    return row["values"].get(year) if row else None


# --------------------------------------------------------------------- KPIs

latest, prior = YEARS[0], YEARS[1]
title = f"e-Hub ESG Pivot — {country_label}"
st.title(title)
st.caption(f"Figures shown for {latest} unless noted. See the table below for {', '.join(str(y) for y in shown_years)}.")

scope2_total_desc = f"Scope 2 ({basis})"
scope1 = val("Scope 1", latest)
scope2 = val(scope2_total_desc, latest)
scope3_op = val("Scope 3 operational", latest)
cat15 = val("Category 15 - Investments", latest)
total_ghg_market = val("Greenhouse gas emissions in tCO2e (Market Based)", latest)
total_ghg_prior = val("Greenhouse gas emissions in tCO2e (Market Based)", prior)
total_energy = val("Total energy consumption in MWh", latest)
ghg_per_fte = val("Greenhouse gas emissions in kgCO2e per FTE (Market Based)", latest)

kpi_cols = st.columns(4)
with kpi_cols[0]:
    delta = None
    if total_ghg_market is not None and total_ghg_prior:
        delta = (total_ghg_market - total_ghg_prior) / total_ghg_prior
    st.metric(
        f"Total GHG, {latest} (tCO2e, Market Based)",
        f"{total_ghg_market:,.0f}" if total_ghg_market is not None else "—",
        f"{delta*100:+.1f}% vs {prior}" if delta is not None else None,
        delta_color="inverse",  # a rise in emissions is bad, a fall is good
    )
with kpi_cols[1]:
    st.metric(
        f"Total energy, {latest} (MWh)",
        f"{total_energy:,.0f}" if total_energy is not None else "—",
    )
with kpi_cols[2]:
    st.metric(
        f"GHG per FTE, {latest} (kgCO2e)",
        f"{ghg_per_fte:,.0f}" if ghg_per_fte is not None else "—",
    )
with kpi_cols[3]:
    st.metric(
        f"Scope 3 operational, {latest} (tCO2e)",
        f"{scope3_op:,.0f}" if scope3_op is not None else "—",
    )

st.divider()


# ------------------------------------------------------------------- charts

chart_cols = st.columns(2)

with chart_cols[0]:
    st.subheader("Emissions by scope, by year")
    years_sorted = sorted(shown_years)
    x = [str(y) for y in years_sorted]
    fig = go.Figure()
    for label, desc, color in [
        ("Scope 1", "Scope 1", BLUE),
        ("Scope 2", scope2_total_desc, ORANGE),
        ("Scope 3 operational", "Scope 3 operational", AQUA),
        ("Category 15 (Investments)", "Category 15 - Investments", YELLOW),
    ]:
        y = [val(desc, yr) or 0 for yr in years_sorted]
        fig.add_bar(name=label, x=x, y=y, marker_color=color)
    fig.update_layout(
        barmode="stack",
        plot_bgcolor=CHART_SURFACE,
        paper_bgcolor=CHART_SURFACE,
        font_color=PRIMARY_TEXT,
        yaxis=dict(title="tCO2e", gridcolor=GRIDLINE, zerolinecolor=AXIS_LINE),
        xaxis=dict(gridcolor=GRIDLINE, linecolor=AXIS_LINE),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        margin=dict(t=10, b=10, l=10, r=10),
    )
    st.plotly_chart(fig, width='stretch')
    st.caption("Scope 2 uses the basis selected in the sidebar. Category 15 dwarfs the "
               "other categories -- see the breakdown chart for those on a readable scale.")

with chart_cols[1]:
    st.subheader("Total energy consumption, by year")
    y = [val("Total energy consumption in MWh", yr) or 0 for yr in years_sorted]
    fig = go.Figure()
    fig.add_bar(x=x, y=y, marker_color=BLUE, name="Total energy")
    fig.update_layout(
        plot_bgcolor=CHART_SURFACE,
        paper_bgcolor=CHART_SURFACE,
        font_color=PRIMARY_TEXT,
        yaxis=dict(title="MWh", gridcolor=GRIDLINE, zerolinecolor=AXIS_LINE),
        xaxis=dict(gridcolor=GRIDLINE, linecolor=AXIS_LINE),
        showlegend=False,
        margin=dict(t=10, b=10, l=10, r=10),
    )
    st.plotly_chart(fig, width='stretch')
    st.caption("Electricity + Heat + Fuels, converted to MWh.")

st.subheader(f"Scope 3 category breakdown, {latest}")
cat_series = [
    ("Cat 1&2 — Purchased Goods & Capital Goods", "Category 1 & 2 Purchased Goods & Services and Capital goods", BLUE),
    ("Cat 5 — Waste & Water", "Category 5 - Waste generated in Operations (Waste and Water)", ORANGE),
    ("Cat 6 — Business Travel", "Category 6 - Business Travel", AQUA),
    ("Cat 7 — Employee Commuting", "Category 7 - Employee Commuting", YELLOW),
]
if include_investments:
    cat_series.append(("Cat 15 — Investments", "Category 15 - Investments", MAGENTA))

cat_labels, cat_values, cat_colors = [], [], []
for label, desc, color in cat_series:
    v = val(desc, latest)
    if v is not None:
        cat_labels.append(label)
        cat_values.append(v)
        cat_colors.append(color)

if cat_labels:
    fig = go.Figure(go.Bar(x=cat_values, y=cat_labels, orientation="h", marker_color=cat_colors))
    fig.update_layout(
        plot_bgcolor=CHART_SURFACE,
        paper_bgcolor=CHART_SURFACE,
        font_color=PRIMARY_TEXT,
        xaxis=dict(
            title="tCO2e" + (" (log scale)" if include_investments else ""),
            type="log" if include_investments else "linear",
            gridcolor=GRIDLINE, zerolinecolor=AXIS_LINE,
        ),
        yaxis=dict(autorange="reversed"),
        showlegend=False,
        margin=dict(t=10, b=10, l=10, r=10),
        height=80 + 50 * len(cat_labels),
    )
    st.plotly_chart(fig, width='stretch')
else:
    st.info("No Scope 3 category data for this country/year selection.")

st.divider()


# --------------------------------------------------------------------- table

st.subheader("Full pivot table")

PERCENT_MARKERS = ("renewable sources",)


def is_percent_row(description: str) -> bool:
    return any(marker in description for marker in PERCENT_MARKERS)


def fmt(value: float | None, as_percent: bool) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    if as_percent:
        return f"{value * 100:,.1f}%"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:,.2f}"


hide_basis_suffix = " (Location Based)" if basis == "Market Based" else " (Market Based)"

table_records = []
for r in rows:
    if r["indent"] >= 2 and not show_detail:
        continue
    if r["indent"] >= 2 and r["description"].endswith(hide_basis_suffix):
        continue
    pct = is_percent_row(r["description"])
    label = (" " * 4 * r["indent"]) + r["description"]
    record = {"Description": label}
    for y in shown_years:
        record[str(y)] = fmt(r["values"].get(y), pct)
    record[f"Δ {latest} vs {prior}"] = fmt(r["delta"], True)
    table_records.append(record)

df = pd.DataFrame(table_records)


def color_delta(series: pd.Series) -> list[str]:
    styles = []
    for v in series:
        if v in ("—",) or v is None:
            styles.append("")
            continue
        negative = v.strip().startswith("-")
        styles.append(f"color: {GOOD}" if negative else f"color: {CRITICAL}")
    return styles


delta_col = f"Δ {latest} vs {prior}"
styled = df.style.apply(lambda s: color_delta(s) if s.name == delta_col else [""] * len(s), axis=0)
st.dataframe(styled, width="stretch", height=min(45 * (len(df) + 1), 900))
st.caption(
    "Green = decrease vs the previous year, red = increase — every line here is a "
    "consumption/emissions metric, so a decrease reads as an improvement throughout."
)
