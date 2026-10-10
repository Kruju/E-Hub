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
# Headline as published in the Sustainability Report (Category 15 excluded).
TOTAL_GHG_DESC = "Greenhouse gas emissions in tCO2e (Market Based, excluding financed emissions)"

# Categorical palette (dataviz skill's validated default), used in fixed
# order per entity -- never reassigned based on filters/rank.
BLUE, ORANGE, AQUA, YELLOW, MAGENTA = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4",
)
GOOD, CRITICAL = "#0ca30c", "#d03b3b"
GRIDLINE, AXIS_LINE, MUTED_TEXT, PRIMARY_TEXT, SECONDARY_TEXT = (
    "#e1e0d9", "#c3c2b7", "#898781", "#0b0b0b", "#52514e",
)
CHART_SURFACE = "#fcfcfb"

# Scope colors double as the table's section tints, so the pivot and the
# charts read as one system.
SCOPE1_COLOR, SCOPE2_COLOR, SCOPE3_COLOR = BLUE, ORANGE, AQUA
ENERGY_COLOR, PWW_COLOR = YELLOW, MAGENTA

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
    "Include Category 15 (Investments) in charts",
    value=False,
    help="Category 15 is 40-50x larger than all other emissions combined, so it's left out "
         "of both emissions charts by default to keep Scopes 1-3 readable. When included, "
         "the category chart switches to a log scale.",
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


def trend(description: str) -> list[float]:
    """Oldest -> newest, 0 for missing, for sparklines."""
    return [val(description, yr) or 0 for yr in sorted(YEARS)]


def base_layout(**overrides) -> dict:
    layout = dict(
        plot_bgcolor=CHART_SURFACE,
        paper_bgcolor=CHART_SURFACE,
        font_color=PRIMARY_TEXT,
        font_family="system-ui, -apple-system, 'Segoe UI', sans-serif",
        hoverlabel=dict(bgcolor=CHART_SURFACE, font_color=PRIMARY_TEXT, bordercolor=AXIS_LINE),
        margin=dict(t=10, b=10, l=10, r=10),
    )
    layout.update(overrides)
    return layout


# --------------------------------------------------------------------- KPIs

latest, prior = YEARS[0], YEARS[1]
title = f"e-Hub ESG Pivot — {country_label}"
st.title(title)
st.caption(f"Figures shown for {latest} unless noted. See the Full table tab for {', '.join(str(y) for y in shown_years)}.")

scope2_total_desc = f"Scope 2 ({basis})"
scope1 = val("Scope 1", latest)
scope2 = val(scope2_total_desc, latest)
scope3_op = val("Scope 3 operational", latest)
cat15 = val("Category 15 - Investments", latest)
total_ghg_market = val(TOTAL_GHG_DESC, latest)
total_ghg_prior = val(TOTAL_GHG_DESC, prior)
total_energy = val("Total energy consumption in MWh", latest)
ghg_per_fte = val("Greenhouse gas emissions in kgCO2e per FTE (Market Based)", latest)


def sparkline(values: list[float], good_if_down: bool = True) -> go.Figure:
    """A 12px-tall trend line: muted except the final point, which takes the
    direction color. Pure decoration riding beside the stat tile's number."""
    x = list(range(len(values)))
    direction_color = MUTED_TEXT
    if len(values) >= 2 and values[0] != values[-1]:
        rising = values[-1] > values[0]
        good = (not rising) if good_if_down else rising
        direction_color = GOOD if good else CRITICAL
    fig = go.Figure(
        go.Scatter(
            x=x, y=values, mode="lines+markers", line=dict(color=MUTED_TEXT, width=2),
            marker=dict(
                size=[0] * (len(values) - 1) + [8],
                color=[direction_color] * len(values),
                line=dict(color=CHART_SURFACE, width=2),
            ),
            hoverinfo="skip",
        )
    )
    fig.update_layout(
        base_layout(
            height=40,
            margin=dict(t=2, b=2, l=2, r=2),
            xaxis=dict(visible=False),
            yaxis=dict(visible=False),
            showlegend=False,
        )
    )
    return fig


kpi_cols = st.columns(4)
kpi_defs = [
    (
        f"Total GHG, {latest} (tCO2e, Market Based, excl. financed)",
        total_ghg_market,
        (total_ghg_market - total_ghg_prior) / total_ghg_prior if total_ghg_market is not None and total_ghg_prior else None,
        trend(TOTAL_GHG_DESC),
        True,
    ),
    (f"Total energy, {latest} (MWh)", total_energy, None, trend("Total energy consumption in MWh"), True),
    (
        f"GHG per FTE, {latest} (kgCO2e)",
        ghg_per_fte,
        None,
        trend("Greenhouse gas emissions in kgCO2e per FTE (Market Based)"),
        True,
    ),
    (f"Scope 3 operational, {latest} (tCO2e)", scope3_op, None, trend("Scope 3 operational"), True),
]
for col, (label, value, delta, series, good_if_down) in zip(kpi_cols, kpi_defs):
    with col:
        st.metric(
            label,
            f"{value:,.0f}" if value is not None else "—",
            f"{delta*100:+.1f}% vs {prior}" if delta is not None else None,
            delta_color="inverse" if delta is not None else "off",
        )
        if any(series):
            st.plotly_chart(sparkline(series, good_if_down), width="stretch", config={"displayModeBar": False})

st.divider()


# ------------------------------------------------------------- biggest movers

movers = [
    r for r in rows
    if r["indent"] <= 1 and r["delta"] is not None and not pd.isna(r["delta"]) and r["description"] != TOTAL_GHG_DESC
]
increases = sorted(movers, key=lambda r: r["delta"], reverse=True)[:3]
decreases = sorted(movers, key=lambda r: r["delta"])[:3]
increases = [r for r in increases if r["delta"] > 0]
decreases = [r for r in decreases if r["delta"] < 0]

if increases or decreases:
    with st.expander(f"Biggest movers, {latest} vs {prior}", expanded=False):
        move_cols = st.columns(2)
        with move_cols[0]:
            st.markdown(f"**Largest increases**")
            if increases:
                for r in increases:
                    st.markdown(f"- {r['description']} &nbsp; :red[+{r['delta']*100:.1f}%]")
            else:
                st.caption("No increases among top-level lines.")
        with move_cols[1]:
            st.markdown(f"**Largest decreases**")
            if decreases:
                for r in decreases:
                    st.markdown(f"- {r['description']} &nbsp; :green[{r['delta']*100:.1f}%]")
            else:
                st.caption("No decreases among top-level lines.")

st.divider()


# --------------------------------------------------------------------- tabs

tab_overview, tab_scopes, tab_compare, tab_table = st.tabs(
    ["Overview", "Scopes & categories", "Country comparison", "Full table"]
)


# ----------------------------------------------------------------- overview

with tab_overview:
    overview_cols = st.columns([1, 1])

    with overview_cols[0]:
        st.subheader(f"Share of total GHG by scope, {latest}")
        donut_series = [
            ("Scope 1", "Scope 1", SCOPE1_COLOR),
            ("Scope 2", scope2_total_desc, SCOPE2_COLOR),
            ("Scope 3 operational", "Scope 3 operational", SCOPE3_COLOR),
        ]
        if include_investments:
            donut_series.append(("Category 15 (Investments)", "Category 15 - Investments", YELLOW))
        labels, values, colors = [], [], []
        for label, desc, color in donut_series:
            v = val(desc, latest)
            if v:
                labels.append(label)
                values.append(v)
                colors.append(color)
        if values:
            total = sum(values)
            fig = go.Figure(
                go.Pie(
                    labels=labels, values=values, hole=0.58,
                    marker=dict(colors=colors, line=dict(color=CHART_SURFACE, width=2)),
                    textinfo="label+percent", textposition="outside",
                    hovertemplate="%{label}: %{value:,.0f} tCO2e (%{percent})<extra></extra>",
                )
            )
            fig.add_annotation(
                text=f"<b>{total:,.0f}</b><br><span style='font-size:11px;color:{SECONDARY_TEXT}'>tCO2e</span>",
                x=0.5, y=0.5, showarrow=False, font=dict(size=22, color=PRIMARY_TEXT),
            )
            fig.update_layout(
                base_layout(
                    showlegend=True,
                    legend=dict(orientation="h", yanchor="top", y=-0.05, xanchor="center", x=0.5),
                    height=340,
                )
            )
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        else:
            st.info("No scope data for this selection.")

    with overview_cols[1]:
        st.subheader(f"Total energy, {latest} — by source")
        energy_series = [
            ("Electricity (purchased)", "Electricity (purchased)", BLUE),
            ("Electricity (self-generated)", "Electricity (self-generated and consumed)", AQUA),
            ("Heat", "Heat", ORANGE),
            ("Fuels (petrol/diesel)", "Fuels (petrol/diesel)", YELLOW),
        ]
        e_labels, e_values, e_colors = [], [], []
        for label, desc, color in energy_series:
            v = val(desc, latest)
            if v:
                e_labels.append(label)
                e_values.append(v)
                e_colors.append(color)
        if e_values:
            fig = go.Figure(
                go.Bar(
                    x=e_values, y=e_labels, orientation="h",
                    marker=dict(color=e_colors, cornerradius=4),
                    text=[f"{v:,.0f}" for v in e_values], textposition="outside",
                    hovertemplate="%{y}: %{x:,.0f} MWh<extra></extra>",
                )
            )
            fig.update_layout(
                base_layout(
                    xaxis=dict(title="MWh", gridcolor=GRIDLINE, zerolinecolor=AXIS_LINE),
                    yaxis=dict(autorange="reversed"),
                    showlegend=False,
                    height=340,
                    margin=dict(t=10, b=10, l=10, r=40),
                )
            )
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        else:
            st.info("No energy data for this selection.")

    st.caption(
        "The donut always excludes financed emissions (Category 15) unless switched on in the "
        "sidebar — matching the published headline total."
    )


# ------------------------------------------------------------ scopes & cats

with tab_scopes:
    chart_cols = st.columns(2)
    years_sorted = sorted(shown_years)
    x = [str(y) for y in years_sorted]

    with chart_cols[0]:
        st.subheader("Emissions by scope, by year")
        fig = go.Figure()
        scope_series = [
            ("Scope 1", "Scope 1", SCOPE1_COLOR),
            ("Scope 2", scope2_total_desc, SCOPE2_COLOR),
            ("Scope 3 operational", "Scope 3 operational", SCOPE3_COLOR),
        ]
        if include_investments:
            scope_series.append(("Category 15 (Investments)", "Category 15 - Investments", YELLOW))
        totals_by_year = [0.0] * len(years_sorted)
        for label, desc, color in scope_series:
            y = [val(desc, yr) or 0 for yr in years_sorted]
            totals_by_year = [t + v for t, v in zip(totals_by_year, y)]
            fig.add_bar(
                name=label, x=x, y=y, marker=dict(color=color, cornerradius=4),
                hovertemplate=f"{label}: " + "%{y:,.0f} tCO2e<extra></extra>",
            )
        fig.add_trace(
            go.Scatter(
                x=x, y=totals_by_year, mode="text",
                text=[f"{t:,.0f}" for t in totals_by_year], textposition="top center",
                textfont=dict(color=PRIMARY_TEXT, size=12), hoverinfo="skip", showlegend=False,
            )
        )
        fig.update_layout(
            base_layout(
                barmode="stack",
                bargap=0.35,
                hovermode="x unified",
                yaxis=dict(title="tCO2e", gridcolor=GRIDLINE, zerolinecolor=AXIS_LINE, rangemode="tozero"),
                # "category" so years render as labels, not a numeric axis with 2,022.5 ticks.
                xaxis=dict(type="category", gridcolor=GRIDLINE, linecolor=AXIS_LINE),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
            )
        )
        st.plotly_chart(fig, width="stretch")
        st.caption(
            "Scope 2 uses the basis selected in the sidebar. Category 15 (financed "
            "emissions) is excluded unless switched on in the sidebar. Labels above each "
            "bar are the year's total."
        )

    with chart_cols[1]:
        st.subheader("Total energy consumption, by year")
        y = [val("Total energy consumption in MWh", yr) or 0 for yr in years_sorted]
        fig = go.Figure()
        fig.add_bar(
            x=x, y=y, marker=dict(color=BLUE, cornerradius=4), name="Total energy",
            text=[f"{v:,.0f}" for v in y], textposition="outside",
            hovertemplate="Total energy: %{y:,.0f} MWh<extra></extra>",
        )
        fig.update_layout(
            base_layout(
                bargap=0.5,
                yaxis=dict(title="MWh", gridcolor=GRIDLINE, zerolinecolor=AXIS_LINE, rangemode="tozero"),
                xaxis=dict(type="category", gridcolor=GRIDLINE, linecolor=AXIS_LINE),
                showlegend=False,
            )
        )
        st.plotly_chart(fig, width="stretch")
        st.caption("Electricity + Heat + Fuels, converted to MWh.")

    st.subheader(f"Scope 3 category breakdown, {latest}")
    view_mode = st.radio(
        "View as", ["Absolute (tCO2e)", "Share of Scope 3 (%)"], horizontal=True, key="cat_view_mode"
    )
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
        if view_mode.startswith("Share"):
            cat_total = sum(cat_values) or 1
            plot_values = [v / cat_total * 100 for v in cat_values]
            x_title = "% of Scope 3 shown"
            text_fmt = [f"{v:.1f}%" for v in plot_values]
            hover_fmt = "%{y}: %{x:.1f}%<extra></extra>"
            axis_kwargs = dict(title=x_title, gridcolor=GRIDLINE, zerolinecolor=AXIS_LINE)
        else:
            plot_values = cat_values
            text_fmt = [f"{v:,.0f}" for v in cat_values]
            hover_fmt = "%{y}: %{x:,.0f} tCO2e<extra></extra>"
            axis_kwargs = dict(
                title="tCO2e" + (" (log scale)" if include_investments else ""),
                type="log" if include_investments else "linear",
                gridcolor=GRIDLINE, zerolinecolor=AXIS_LINE,
            )
        fig = go.Figure(
            go.Bar(
                x=plot_values, y=cat_labels, orientation="h",
                marker=dict(color=cat_colors, cornerradius=4),
                text=text_fmt, textposition="outside",
                hovertemplate=hover_fmt,
            )
        )
        fig.update_layout(
            base_layout(
                xaxis=axis_kwargs,
                yaxis=dict(autorange="reversed"),
                showlegend=False,
                height=80 + 50 * len(cat_labels),
                margin=dict(t=10, b=10, l=10, r=50),
            )
        )
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No Scope 3 category data for this country/year selection.")


# --------------------------------------------------------- country compare

with tab_compare:
    st.subheader("Compare countries")
    compare_metric = st.radio(
        "Metric", [f"Total GHG ({basis}, tCO2e)", "Total energy (MWh)"], horizontal=True, key="compare_metric"
    )
    compare_year = st.selectbox("Year", list(YEARS), index=0, key="compare_year")

    is_ghg = compare_metric.startswith("Total GHG")

    compare_rows = []
    for code, name in countries:
        country_rows = get_rows(dsn, code)
        country_by_desc = {r["description"]: r for r in country_rows}
        if is_ghg:
            scope1_v = country_by_desc.get("Scope 1", {}).get("values", {}).get(compare_year) or 0
            scope2_v = country_by_desc.get(f"Scope 2 ({basis})", {}).get("values", {}).get(compare_year) or 0
            scope3_v = country_by_desc.get("Scope 3 operational", {}).get("values", {}).get(compare_year)
            value = scope1_v + scope2_v + (scope3_v or 0)
        else:
            row = country_by_desc.get("Total energy consumption in MWh")
            value = row["values"].get(compare_year) if row else None
        if value:
            compare_rows.append((name, code, value))

    compare_rows.sort(key=lambda r: r[2], reverse=True)
    if compare_rows:
        names = [f"{name} ({code})" for name, code, _ in compare_rows]
        vals = [v for _, _, v in compare_rows]
        colors = [ORANGE if code == selected_country else BLUE for _, code, _ in compare_rows]
        unit = "tCO2e" if is_ghg else "MWh"
        fig = go.Figure(
            go.Bar(
                x=vals, y=names, orientation="h",
                marker=dict(color=colors, cornerradius=4),
                text=[f"{v:,.0f}" for v in vals], textposition="outside",
                hovertemplate=f"%{{y}}: %{{x:,.0f}} {unit}<extra></extra>",
            )
        )
        fig.update_layout(
            base_layout(
                xaxis=dict(title=unit, gridcolor=GRIDLINE, zerolinecolor=AXIS_LINE),
                yaxis=dict(autorange="reversed"),
                showlegend=False,
                height=80 + 36 * len(names),
                margin=dict(t=10, b=10, l=10, r=60),
            )
        )
        st.plotly_chart(fig, width="stretch")
        if selected_country:
            st.caption(f"The country selected in the sidebar ({country_label}) is highlighted in orange.")
        else:
            st.caption("Select a specific country in the sidebar to highlight it here.")
    else:
        st.info("No comparable data across countries for this metric/year.")


# --------------------------------------------------------------------- table

with tab_table:
    st.subheader("Full pivot table")

    search = st.text_input("Filter rows", placeholder="e.g. heat, scope 2, waste...")

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
        if 0 < abs(value) < 1:
            return f"{value:,.3f}"  # small ratios, e.g. paper & waste t per FTE (0.246)
        return f"{value:,.2f}"


    # Section tags are assigned by position in the row stream (state machine),
    # not by matching each row's own label -- several labels (e.g. "Natural
    # gas", "Fuels") repeat once in the Energy section and once under Scope 1,
    # so only stream order disambiguates which section a row belongs to.
    SECTION_STARTS = {
        "Total energy consumption in MWh": "energy",
        "Total paper & waste consumption in tons": "pww",
        TOTAL_GHG_DESC: "headline",
        "Scope 1": "scope1",
        "Scope 2 (Market Based)": "scope2",
        "Scope 2 (Location Based)": "scope2",
        "Scope 3 operational": "scope3",
        "Greenhouse gas emissions in kgCO2e per FTE (Market Based)": "headline",
        "FTE in locations covered by environmental indicators": "headline",
        "Category 15 - Investments": "headline",
        "Scope 3 of which": "headline",
        "Greenhouse gas emissions in tCO2e (Market Based, including financed emissions)": "headline",
    }
    SECTION_TINT = {
        "energy": ENERGY_COLOR,
        "pww": PWW_COLOR,
        "scope1": SCOPE1_COLOR,
        "scope2": SCOPE2_COLOR,
        "scope3": SCOPE3_COLOR,
        "headline": None,  # bold, neutral -- no hue, just weight
    }


    def hex_to_rgba(hex_color: str, alpha: float) -> str:
        h = hex_color.lstrip("#")
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
        return f"rgba({r},{g},{b},{alpha})"


    hide_basis_suffix = " (Location Based)" if basis == "Market Based" else " (Market Based)"

    table_records = []
    row_sections = []
    current_section = "other"
    for r in rows:
        if r["description"] in SECTION_STARTS:
            current_section = SECTION_STARTS[r["description"]]
        if r["indent"] >= 2 and not show_detail:
            continue
        if r["indent"] >= 2 and r["description"].endswith(hide_basis_suffix):
            continue
        if search and search.lower() not in r["description"].lower():
            continue
        pct = is_percent_row(r["description"])
        label = ("    " * r["indent"]) + r["description"]
        record = {"Description": label}
        for y in shown_years:
            record[str(y)] = fmt(r["values"].get(y), pct)
        record[f"Δ {latest} vs {prior}"] = fmt(r["delta"], True)
        table_records.append(record)
        row_sections.append((current_section, r["indent"] == 0))

    df = pd.DataFrame(table_records)

    delta_col = f"Δ {latest} vs {prior}"


    def color_delta(series: pd.Series) -> list[str]:
        styles = []
        for v in series:
            if v in ("—",) or v is None:
                styles.append("")
                continue
            negative = v.strip().startswith("-")
            styles.append(f"color: {GOOD}; font-weight: 600" if negative else f"color: {CRITICAL}; font-weight: 600")
        return styles


    def section_row_styles(row: pd.Series) -> list[str]:
        section, is_headline = row_sections[df.index.get_loc(row.name)]
        tint = SECTION_TINT.get(section)
        style = f"background-color: {hex_to_rgba(tint, 0.14)}" if tint else ""
        if is_headline:
            style += "; font-weight: 700; background-color: rgba(11,11,11,0.05)" if not style else "; font-weight: 700"
        return [style] * len(row)


    if df.empty:
        st.info("No rows match that filter.")
    else:
        styled = (
            df.style.apply(section_row_styles, axis=1)
            .apply(lambda s: color_delta(s) if s.name == delta_col else [""] * len(s), axis=0)
        )
        st.dataframe(styled, width="stretch", height=min(45 * (len(df) + 1), 900))

        st.caption(
            f"Row tint by section — "
            f":blue-background[Scope 1] · :orange-background[Scope 2] · "
            f":green-background[Scope 3] · Energy and Paper/Water/Waste sections are tinted "
            f"to match, and **bold** rows are section totals / headline figures. "
            f"Delta column: green = decrease vs the previous year, red = increase — every line "
            f"here is a consumption/emissions metric, so a decrease reads as an improvement."
        )

        st.download_button(
            "Download table as CSV",
            df.to_csv(index=False).encode("utf-8"),
            file_name=f"e-hub-pivot-{country_label.replace(' ', '_')}.csv",
            mime="text/csv",
        )
