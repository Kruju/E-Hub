# e-Hub ESG Pivot — PostgreSQL + Python

Rebuilds the "Output 2025" pivot table (Total energy, Scope 1/2/3 GHG
emissions, per-FTE ratios, YoY delta) from the raw "e-Hub Data Structure"
export, in PostgreSQL + Python, with a country toggle.

This README is the record of *every* decision made while reverse-engineering
the pivot — what's confirmed exact, what's a judgment call you made, and
what's still an open gap in the source data. Read it before trusting a
number, and update it if you change a rule below.

## Quick start

```bash
pip install -r requirements.txt
psql "$DSN" -f schema.sql
python load_seed_data.py --dsn "$DSN"
python calculations.py --dsn "$DSN"                # all countries combined
python calculations.py --dsn "$DSN" --country CH   # Switzerland only
```

**After pulling new changes, re-run the `schema.sql` and `load_seed_data.py`
lines.** `schema.sql` drops and recreates every table, so this is always a
clean rebuild from `seed/` (and the only way an existing database picks up
schema or data changes). Skipping it gives errors like `relation
"historical_overrides" does not exist`.

`calculations.py` also exposes `build_output_table(conn, country_code=None,
years=(2025, 2024, 2023))` for use from other Python code (e.g. a future
FastAPI endpoint) — see the bottom of the file for the CLI wrapper.

### Tests

`tests/test_pivot_against_sample.py` is a regression test that locks in
every line against the figures published in the **Sustainability Report
2025** (Environmental Matters, p.273), for all three years:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/test_pivot_against_sample.py --dsn "$DSN"
```

If this fails after changing `calculations.py`, treat it as a real
regression — see "Reconciliation with the published report" below.

### Web dashboard

`app.py` is a Streamlit dashboard over the same `build_output_table()` — no
separate backend, it queries Postgres directly.

```bash
pip install -r requirements-web.txt
streamlit run app.py
```

It opens at `http://localhost:8501`. Toggles, all in the sidebar:

- **Country** — same `country_code` values as the CLI's `--country` flag.
- **Years shown in table** — pick any subset of 2023/2024/2025 for the table columns.
- **Scope 2 basis** (Location vs Market Based) — controls which Scope 2 figure
  feeds the KPI tiles and the scope-by-year chart; the full table always shows
  both bases side by side regardless of this toggle.
- **Show line-item detail** — expands the sub-categories under Electricity,
  Heat, Fuels, the Scope 1/2 breakdowns and the Scope 3 categories (collapsed by default to keep the
  table scannable).
- **Include Category 15 (Investments) in charts** — Category 15 is 40-50x
  larger than all other emissions combined, so both the scope chart and the
  category breakdown leave it out by default; switching this on adds it back
  (the category chart then uses a log-scaled axis so the smaller categories
  stay readable).

Company-wide-only lines (Category 1 & 2, Category 15, and everything that
depends on them) show as "—" whenever a specific country is selected — see
"Country toggle" below for why.

## Data model

Five tables mirror the source export 1:1 (`offices`, `office_annual_metrics`
[was `offices_information`], `activity_types`, `emission_factors`,
`activities`), plus two small tables needed to make the pivot reproducible:

- **`category_1_2_overrides`** — static per-year totals for "Category 1 & 2 –
  Purchased Goods & Services and Capital Goods". Per your decision, this is
  hardcoded because there is **no source activity data for it at all** — see
  "Still open" below.
- **`historical_overrides`** — company-wide 2023/2024 Employee Commuting
  figures, which are *missing* from the export. Only used when the
  live data for that metric/year is empty, so they switch off automatically
  once real data is loaded — see "Historical overrides" below.

## Reconciliation with the published report

The target is the **Sustainability Report 2025, Environmental Matters
(p.273)**. Note that the `Output 2025` sheet in the two workbooks (marked
"PAS DONNER PWC AVANT REVUE") is an **earlier draft** and does *not* match the
published report in several places: 2024 Heat / Natural gas, 2024 Scope 1,
2024 energy per FTE, 2025 location-based Scope 2, and its headline total
includes financed emissions. An earlier version of this app was calibrated
against that draft, which is why its numbers disagreed with the PDF.

After the fixes below, **every line in the report reproduces for all three
years** (within the report's own rounding), except one location-based line
(see "Still open"). The fixes, by root cause:

| # | Root cause | Lines affected | Fix |
|---|---|---|---|
| 1 | **Mislabelled source row**: the 2024 Luxembourg district-heating record (39,192 kWh, period 2024-01-01→2024-12-31) was tagged `reporting_year = 2023` / `…_2023` in the original export | Heat, District heating, Total energy, Scope 2 (2024 showed 0, 2023 was doubled) | Corrected to 2024 — now also fixed in the source workbook |
| 2 | **Transposed source values**: solar thermal 2023 and 2025 quantities were swapped in the original export | Solar thermal, Heat, Total energy (2025, 2023) | Swapped back (report: 18 / 16 / 16) — now also fixed in the source workbook |
| 3 | **Wrong scope grouping**: natural-gas (and biogas) heating combustion was put in Scope 2 because `activity_types.scope` says so; the report (and GHG Protocol) puts it in Scope 1 | Scope 1 (was 12 vs 51), Scope 2 market (was 169 vs 130) and location | `SCOPE_1_HEATING_TYPES` in `calculations.py`; biogas folds into the "Natural gas" line as in the report |
| 4 | **Wrong office scope for paper/waste/water**: summed over *all* offices; the report says these are "based solely on Swissquote Bank Ltd" | Paper, Waste (was 112 vs 96), Water (was 5,492 vs 4,333), per-FTE lines | Quantities restricted to the SQB offices (+ `SWITZERLAND_CH`, where paper is booked) |
| 5 | **Wrong headline total**: included Category 15 financed emissions (604,295) — the report's headline is "market-based *without* financed emissions" | Total tCO2e (13,595), kgCO2e per FTE | Headline = Scope 1 + Scope 2 (market) + Scope 3 operational; the old figure is kept as a separate "including financed emissions" line |
| 6 | **Renewable share "not computable"**: the rule is (Hydropower + Renewable + self-generated Solar) ÷ purchased electricity | Renewable / non-renewable % | Implemented; reproduces 93.525% / 93.108% / 93.396% to floating-point precision. Capped at 100% for single-country views |
| 7 | **Self-generated solar left out of location-based electricity**: the report's line includes the grid-factor emissions recorded on the solar rows | Electricity (purchased) location-based (was 533 vs 535) | Included |
| 8 | **Missing 2023/2024 data**: FTE was `#REF!` in the original export, and commuting rows don't exist | Energy/GHG per FTE, FTE, Commuting, Scope 3 operational, totals (2023/2024) | Per-office FTE now loaded from the corrected workbook (totals 1,217.27 / 1,133.52; SQB 1,005.92 / 957.42 — exactly what the report implies); commuting via `historical_overrides` (see below) |

### Historical overrides

| Metric | 2024 | 2023 | Source |
|---|---|---|---|
| `CATEGORY_7` (tCO2e) | 1,166 | 1,158.14 | 2024 from the published report (the draft's 1,167.13 doesn't reconcile with the report's Scope 3 total of 10,722); 2023 from the draft sheet, matches report |

These are company-wide only (country views show "—" for those years).
**Replace them with real per-office commuting records when available** — the overrides are ignored as soon as live data exists.

## The pivot, line by line

### Energy section

| Line | Formula |
|---|---|
| Total energy consumption (MWh) | Electricity + Heat + Fuels |
| Electricity | Σ `quantity` of all `ELECTRICITY_*` types ÷ 1000 |
| Electricity (self-generated) | `ELECTRICITY_SOLAR` quantity ÷ 1000 |
| Electricity (purchased) | Electricity − self-generated |
| Of which renewable | (`ELECTRICITY_HYDROPOWER` + `ELECTRICITY_RENEWABLE` + `ELECTRICITY_SOLAR`) ÷ purchased, capped at 100% |
| Heat | Σ `quantity` of all `HEATING_*` types (see unit note below) |
| Fuels (petrol/diesel) | Σ `quantity` (litres) × the "energy converter" factor from `emission_factors` (kWh/litre), ÷ 1000 |
| Energy consumption per FTE | Total energy × 1,000 ÷ total company FTE (kWh/FTE) |

**Unit anomaly — `HEATING_SOLAR_THERMAL`:** every other heating/electricity
type stores `quantity` in kWh (per its declared `default_unit`), but this one
type's recorded values are already in MWh. `calculations.py` special-cases it
(`ENERGY_ALREADY_IN_MWH`) rather than dividing by 1000.

**Fuel unit conversion:** `FUEL_DIESEL`/`FUEL_PETROL` are recorded in litres.
`emission_factors` has *two* rows per fuel/year — one commented "Converter in
kWh" / "…fuel properties" (litres → kWh, used for this section) and one that
is the CO2-per-unit factor (already folded into `activities.market_based_tco2e`,
not needed here). `load_fuel_converters()` picks the right one by comment text.

### Paper / Water / Waste section (SQB only)

| Line | Formula |
|---|---|
| Paper (t) | Σ `PAPER` quantity (kg) ÷ 1000, SQB offices |
| Waste (t) | Σ `WASTE` quantity (already tons), SQB offices |
| Water (m3) | Σ `WATER` quantity, SQB offices |
| … per FTE SQB ONLY | divided by FTE of the SQB offices |

**SQB office list** (Swiss offices excluding Yuh):
`GLAND_HQ_CH, GLAND_ELLIPSE_CH, ZÜRICH_LOWENSTRASSE_CH, ZÜRICH_STETTBACH_CH,
ZÜRICH_JOSEFSTRASSE_CH, ZÜRICH_INSIDER-BAR_CH, BERN_CH, GLAND_INSIDER-BAR_CH`,
plus `SWITZERLAND_CH` (the bank-level record paper is booked against; it has
no FTE). For a country with no SQB office these lines are "—".

### GHG section

| Line | Formula |
|---|---|
| Total tCO2e (market-based, excluding financed emissions) | Scope 1 + Scope 2 (Market) + Scope 3 operational |
| Scope 1 | Σ `market_based_tco2e` for `scope = 'SCOPE_1'` (fuels) **plus** `HEATING_NATURAL_GAS` and `HEATING_BIOGAS` |
| Scope 2 (Location/Market) | Σ `location_based_tco2e` / `market_based_tco2e` for `scope = 'SCOPE_2'`, excluding the two heating types above |
| Electricity (purchased), location-based | includes the location-based tCO2e recorded on `ELECTRICITY_SOLAR` rows, as the report does |
| Category 5 / 6 / 7 | Σ `market_based_tco2e` where `scope3_category` = 5 / 6 / 7 |
| Category 1 & 2 | Static override, see "Still open" |
| Scope 3 operational | Cat 1&2 + Cat 5 + Cat 6 + Cat 7 |
| GHG per FTE (kgCO2e) | Total (excluding financed) × 1000 ÷ total FTE |
| Category 15 (Investments) | Σ `quantity` (**not** `market_based_tco2e`, which is NULL for these rows) where `activity_type_code LIKE 'FINANCED_EMISSIONS%'` — `OUTSTANDING_INVESTMENTS_*` (MCHF exposure) excluded |
| Scope 3 of which | Scope 3 operational + Cat 15 |
| Total including financed emissions | Total (excluding financed) + Cat 15 |

**Location-based bug fix:** `HEATING_DISTRICT`, `HEATING_NATURAL_GAS`, and
`HEATING_BIOGAS` each have only one emission factor per year (no
LOCATION/MARKET split in `emission_factors`) but the raw `activities` export
leaves `location_based_tco2e` empty for them. `calculations.py` copies the
market-based value (`SINGLE_FACTOR_ACTIVITY_TYPES`).

**Business Travel / Employee Commuting — not recomputed from factors:**
Both `BUSINESS_TRAVEL_*` and `EMPLOYEE_COMMUTING_*` emission factors in
`emission_factors` need attributes the `activities` export doesn't carry
(trip destination for `BUSINESS_TRAVEL_HOTEL`, travel mode/haul-class for
flights and commuting, several additive "WTT" component rows per mode), so
`activities.market_based_tco2e` is treated as authoritative for these two
categories (pure aggregation, no recompute).

## Data-quality fixes applied during seeding

These are typos/format issues confirmed by comparing `offices` against every
`office_id` actually referenced elsewhere, or against the published report:

- `GLAND_GLAND_HQ_CH` (offices) → `GLAND_HQ_CH` (everywhere else) — duplicated prefix.
- `ZÜRICH_LÖWENSTRASSE_CH` → `ZÜRICH_LOWENSTRASSE_CH` — umlaut vs. no-umlaut
  spelling used inconsistently, including *within the same table*.
- `FINANCED_EMISSIONS_GREEN BONDS` (activities) → `FINANCED_EMISSIONS_GREEN_BONDS`
  (activity_types) — space vs. underscore.
- 3 junk rows with `activity_type_code` blank (`NULL_YUH_CH_2025/2024/2023`) dropped.
- `fte_female` / `fte_male` / `fte_total` in `offices_information` are stored
  ×100 in the source (all three years). Stored as real headcounts in
  `office_annual_metrics`.

The district-heating year and solar-thermal swap described above were fixed
in the source workbook (2026-10 update), so `seed/` now matches it directly.

**One mapping NOT confidently resolved** — flagged rather than guessed:
`GLAND_ELLIPSE_CH` is referenced by `offices_information`/`activities`, but
`offices` only has a `ZÜRICH_ELLIPSE_CH`. **We mapped it to Gland** — please
confirm with your facilities team. (It's in Switzerland either way, so no
reported figure depends on it.)

## Still open

1. **2025 location-based heat pump: 24.3 t computed vs 17 t published.**
   The export has 206,015 kWh × the CH 2025 location factor of 118.1 gCO2e/kWh
   (IEA 2024) = 24.3 t, and the draft `Output 2025` sheet also shows 24.33.
   The published 17 t implies ~82 g/kWh, which isn't in `emission_factors`.
   This also makes Scope 2 (location-based) 565 vs 557 published. Scope 2
   market-based and every headline total are unaffected. Please check which
   factor was used for the final report.
2. **Per-FTE lines are ~0.03–0.07% off** because the report divides by FTE
   rounded to whole numbers (e.g. 1,448 instead of 1,448.4325): 9,386 vs 9,389
   kgCO2e/FTE in 2025. Immaterial; deliberately not replicated.
3. **Business Travel (Category 6)** is ~0.015% off the draft sheet (489.78 vs.
   489.71 tCO2e for 2025) — both round to the published 490.
4. **Real 2023/2024 commuting data** — see "Historical overrides".
5. **Category 1 & 2 real source data** — **zero** activity rows exist in the
   export (`PURCHASED_GOODS_SERVICES`/`CAPITAL_GOODS` are defined but never
   populated). `category_1_2_overrides` hardcodes the three published totals
   as a stopgap; it cannot be split by country. Replace with spend-based data
   (CHF spend × EEIO-style factor) when available.

## Country toggle

`build_output_table(conn, country_code=None, years=(2025, 2024, 2023))`.
`country_code=None` reproduces the all-entities view (`CH`, `AE`, `LU`, `ZA`,
`CY`, `CN`, `SG`, `MT`, `GB`, `RO` are the valid codes, matching `offices.country_code`
— this was your choice over toggling by the 10 named business entities or by
individual office, since Switzerland's several offices, including Yuh,
collapse to one `CH` figure). Category 1 & 2 and Category 15 (and everything
that depends on them: Scope 3 operational, Scope 3 of which, Total GHG, GHG per FTE) come back as
`None` for a specific country, rather than a misleadingly-small number,
because those two lines are recorded against the whole company
(`OWN_BOOK`, or the static override) and cannot be split by country from the
data available.
