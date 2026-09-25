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

`calculations.py` also exposes `build_output_table(conn, country_code=None,
years=(2025, 2024, 2023))` for use from other Python code (e.g. a future
FastAPI endpoint) — see the bottom of the file for the CLI wrapper.

### Tests

`tests/test_pivot_against_sample.py` is a regression test that locks in
every line confirmed exact (or near-exact with a documented reason) against
the sample `Output 2025` sheet, for the reliable 2025 column:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/test_pivot_against_sample.py --dsn "$DSN"
```

If this fails after changing `calculations.py`, treat it as a real
regression — every value in it was hand-verified against the source
workbook (see the table below).

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
  Heat, Fuels, and the Scope 1/2 breakdowns (collapsed by default to keep the
  table scannable).
- **Include Category 15 (Investments) in category chart** — Category 15 is
  50-100x larger than every other Scope 3 category, so the breakdown chart
  splits it out by default; switching this on adds it back with a log-scaled
  axis so the smaller categories stay readable.

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
  "Open gaps" below.
- **`electricity_renewable_share`** — empty placeholder table for the
  renewable/non-renewable electricity split. Per your decision, you're
  supplying the rule/reference data — see "Open gaps" below.

## The pivot, line by line

Every line was checked against the sample `Output 2025` sheet (both source
workbooks contain an identical copy of it) down to the last decimal, using
the 2025 column as the reliable year (see "Known multi-year data gaps" for
why 2023/2024 don't always reconcile with that sample).

### Energy section

| Line | Formula | Status |
|---|---|---|
| Total energy consumption (MWh) | Electricity + Heat + Fuels | exact |
| Electricity | Σ `quantity` of all `ELECTRICITY_*` types ÷ 1000 | exact |
| Electricity (self-generated) | `ELECTRICITY_SOLAR` quantity ÷ 1000 | exact |
| Electricity (purchased) | Electricity − self-generated | exact |
| Of which renewable / non-renewable | — | **not computable, see Open gaps** |
| Heat | Σ `quantity` of all `HEATING_*` types (see unit note below) | exact for 2025 |
| Fuels (petrol/diesel) | Σ `quantity` (litres) × the "energy converter" factor from `emission_factors` (kWh/litre), ÷ 1000 | exact, all 3 years |
| Energy consumption per FTE | Total energy × 1,000,000 ÷ total company FTE (kWh/FTE) | exact for years with FTE data |

**Unit anomaly — `HEATING_SOLAR_THERMAL`:** every other heating/electricity
type stores `quantity` in kWh (per its declared `default_unit`), but this one
type's recorded values are already in MWh. `calculations.py` special-cases it
(`ENERGY_ALREADY_IN_MWH`) rather than dividing by 1000. Confirmed by the fact
that dividing by 1000 like the others makes this line ~1000x too small.

**Fuel unit conversion:** `FUEL_DIESEL`/`FUEL_PETROL` are recorded in litres.
`emission_factors` has *two* rows per fuel/year — one commented "Converter in
kWh" / "…fuel properties" (litres → kWh, used for this section) and one that
is the CO2-per-unit factor (already folded into `activities.market_based_tco2e`,
not needed here). `load_fuel_converters()` picks the right one by comment text.

### Paper / Water / Waste section

| Line | Formula | Status |
|---|---|---|
| Paper (t) | Σ `PAPER` quantity (kg) ÷ 1000 | matches Category 5&7 checks; quantity vs. sample has a small gap in 2025 — see Open gaps |
| Waste (t) | Σ `WASTE` quantity (already tons) | has a real gap vs. the sample even in 2025 — see Open gaps |
| Water (m3) | Σ `WATER` quantity | same caveat as Waste |
| … per FTE **SQB ONLY** | divided by FTE of the 8 Swiss offices below | exact (confirmed independently via both the Water and Paper&Waste lines) |

**SQB ONLY office list** (your decision — Swiss offices excluding Yuh):
`GLAND_HQ_CH, GLAND_ELLIPSE_CH, ZÜRICH_LOWENSTRASSE_CH, ZÜRICH_STETTBACH_CH,
ZÜRICH_JOSEFSTRASSE_CH, ZÜRICH_INSIDER-BAR_CH, BERN_CH, GLAND_INSIDER-BAR_CH`.

### GHG section

| Line | Formula | Status |
|---|---|---|
| Scope 1 | Σ `market_based_tco2e` where `activity_types.scope = 'SCOPE_1'` | exact, all 3 years |
| Scope 2 (Location/Market) | Σ `location_based_tco2e` / `market_based_tco2e` where `scope = 'SCOPE_2'` | see "Scope 1 vs 2" below |
| Category 5 (Water/Waste) | Σ `market_based_tco2e` where `scope3_category = 5` | exact |
| Category 6 (Business Travel) | Σ `market_based_tco2e` where `scope3_category = 6` | ~0.015% off the sample — pass-through value, see Business Travel note |
| Category 7 (Employee Commuting) | Σ `market_based_tco2e` where `scope3_category = 7` | exact for 2025 (no 2023/2024 data exists at all — see Open gaps) |
| Category 15 (Investments) | Σ `quantity` (**not** `market_based_tco2e`, which is NULL for these rows) where `activity_type_code LIKE 'FINANCED_EMISSIONS%'` | **exact, all 3 years** — your decision was to exclude `OUTSTANDING_INVESTMENTS_*` (MCHF exposure, not an emission) |
| Category 1 & 2 | Static override, see Open gaps | your decision |
| Scope 3 operational | Cat 1&2 + Cat 5 + Cat 6 + Cat 7 | exact — your decision (excludes Cat 15) |
| Scope 3 of which | Scope 3 operational + Cat 15 | exact |
| GHG (tCO2e, Market Based) | Scope 1 + Scope 2 (Market) + Scope 3 of which | exact (to the Business Travel rounding) |
| GHG per FTE (kgCO2e) | (Scope 1 + Scope 2 Market + Scope 3 **operational**, i.e. excluding Cat 15) × 1000 ÷ total FTE | exact — this was reverse-engineered from the numbers, not given anywhere explicitly |

**Location-based bug fix:** `HEATING_DISTRICT`, `HEATING_NATURAL_GAS`, and
`HEATING_BIOGAS` each have only one emission factor per year (no
LOCATION/MARKET split in `emission_factors`) but the raw `activities` export
still has `location_based_tco2e = 0` for them, instead of equal to
`market_based_tco2e`. `calculations.py` corrects this (`SINGLE_FACTOR_ACTIVITY_TYPES`)
since a single blended factor applies to both bases.

**Scope 1 vs 2 — a deliberate difference from the sample file:**
`activity_types.scope` tags `HEATING_NATURAL_GAS` as `SCOPE_2`. The sample
`Output 2025` sheet instead shows a "Natural gas" line inside the **Scope 1**
section with a similar-magnitude number. You confirmed the `scope` column is
authoritative, so this rebuild puts natural gas heating combustion under
**Scope 2** (alongside Electricity, Heat pump, District heating, Biogas) and
Scope 1 only contains Fuels (diesel/petrol). All totals (Scope1+Scope2+Scope3)
still reconcile — only the presentation grouping changed. If your ESG team
actually wants natural-gas combustion reported as Scope 1 (common under the
GHG Protocol for on-site combustion), say so and this is a one-line change in
`build_output_table()`.

**Business Travel / Employee Commuting — not recomputed from factors:**
Both `BUSINESS_TRAVEL_*` and `EMPLOYEE_COMMUTING_*` emission factors in
`emission_factors` need attributes the `activities` export doesn't carry
(trip destination for `BUSINESS_TRAVEL_HOTEL`, which has ~60 different
per-destination-country factor rows and no destination field on the
activity; travel mode/haul-class for flights and commuting, several
additive "WTT" component rows per mode). Rather than guess a wrong
composition rule, this pipeline treats `activities.market_based_tco2e` as
authoritative for these two categories (pure aggregation, no recompute) —
that's why Category 6/7 reconcile almost perfectly with the sample even
though we never re-derive them from `emission_factors`. If you want these
genuinely recalculated from quantity × factor, we need the missing trip
attributes added to the activities export first.

## Data-quality fixes applied silently during seeding

These are typos/format issues confirmed by comparing `offices` against every
`office_id` actually referenced elsewhere — fixed because the correct
mapping was unambiguous:

- `GLAND_GLAND_HQ_CH` (offices) → `GLAND_HQ_CH` (everywhere else) — duplicated prefix.
- `ZÜRICH_LÖWENSTRASSE_CH` (offices) / `ZÜRICH_LÖWENSTRASSE_CH` (some activities rows)
  → `ZÜRICH_LOWENSTRASSE_CH` — umlaut vs. no-umlaut spelling used inconsistently,
  including *within the same table*.
- `FINANCED_EMISSIONS_GREEN BONDS` (activities) → `FINANCED_EMISSIONS_GREEN_BONDS`
  (activity_types) — space vs. underscore.
- 3 junk rows with `activity_type_code` blank (`NULL_YUH_CH_2025/2024/2023`) dropped.
- `fte_female` / `fte_male` / `fte_total` in `offices_information` are stored
  ×100 in the source (confirmed: dividing the sum by 100 reproduces the
  "per FTE" lines exactly). Stored as real headcounts in `office_annual_metrics`.

**One typo NOT confidently resolved** — flagged rather than guessed:
`GLAND_ELLIPSE_CH` is referenced by `offices_information`/`activities`, but
`offices` only has a `ZÜRICH_ELLIPSE_CH`. It's unclear whether this office is
actually in Gland or Zürich. **We mapped it to Gland** (kept the
`offices_information`/`activities` city name, renamed `offices`'s row) —
please confirm or correct with your facilities team.

## Known multi-year data gaps (not fixed — need real data from you)

The 2025 column reconciles with the sample almost perfectly everywhere. 2023
and 2024 frequently do not, and in some cases 2025 has small gaps too, for
reasons that live in the source data, not the calculation logic:

- **`offices_information` FTE for 2023/2024 is unrecoverable.** Checking the
  actual formulas (not cached values) in the source workbook shows every
  2023/2024 row is `=VLOOKUP(office_id, #REF!, ...)` — a dead reference. Real
  headcounts for those years don't exist anywhere in this file.
  **You said you'd supply real historical FTE — please provide it and load
  it into `office_annual_metrics`.**
- **`HEATING_NATURAL_GAS` and `HEATING_DISTRICT` quantities for 2023/2024**
  don't match what the sample output implies (2024 heating-natural-gas: raw
  data sums to 373,863 kWh across the same 5 offices vs. an implied 290,815
  kWh; `HEATING_DISTRICT` has no 2024 rows at all). 2025 matches perfectly.
  This looks like the historical activity records have been restated since
  the sample was produced. Per your decision, this system always computes
  live from current data, so these years will show whatever `activities`
  currently holds until the historical rows are corrected/backfilled.
- **`EMPLOYEE_COMMUTING_*` has zero rows for 2023/2024** — commuting data was
  apparently never captured for prior years in this export.
- **`WASTE`/`WATER`/`PAPER` physical quantities don't fully reconcile even for
  2025** (e.g. Waste sums to 112.25 t here vs. 95.88 t implied by the sample),
  even though the derived Category 5 tCO2e figure matches exactly. This means
  the `quantity` column and the pre-computed `market_based_tco2e` column for
  these rows come from different vintages of the underlying data. Needs
  reconciliation on your side; nothing in the export explains which is current.
- **Category 1 & 2 (Purchased Goods & Capital Goods)** has **zero** activity
  rows in the entire export (`PURCHASED_GOODS_SERVICES`/`CAPITAL_GOODS` are
  defined in `activity_types` but never populated) even though the sample
  shows ~11,569 tCO2e for 2025. Per your decision, `category_1_2_overrides`
  hardcodes the three known historical totals (2023/2024/2025) as a stopgap.
  **This cannot be split by country** (no source data exists to split), so
  it's `None` whenever a specific `country_code` is passed. Replace this
  table with real spend-based data (CHF spend × EEIO-style factor) when
  available.
- **Solar thermal 2023/2025 values look transposed** against the sample
  (2024 lines up; 2023 and 2025 are swapped between what's recorded and what
  the sample shows). Not corrected — flagging for your ESG team to check the
  source record, since silently swapping a recorded year is a data decision,
  not a units fix.
- **Business Travel (Category 6)** is ~0.015% off the sample (489.78 vs.
  489.71 tCO2e for 2025) even using the pass-through `market_based_tco2e`
  values — likely last-mile rounding somewhere upstream. Immaterial, not
  investigated further.

## Open items — still need your input

1. **Renewable / non-renewable electricity split.** Every combination of the
   electricity activity subtypes (Hydropower, Renewable, Nuclear, Biomass,
   Coal/Oil, …) was tested against the known ratios (93.5%/6.5% for 2025,
   etc.) — none reproduce them. This isn't derivable from the activity-level
   data in this export. `electricity_renewable_share(country_code,
   reporting_year, renewable_share)` is ready to receive whatever reference
   table/rule you provide; `calculations.py` returns `None` for this line
   until it's populated.
2. **Real 2023/2024 FTE** per office (see above) — load into
   `office_annual_metrics`.
3. **Category 1 & 2 real source data** — either the CHF-spend figures plus
   the EEIO-style factors to compute it properly, or confirmation that the
   static override approach is fine long-term.

## Country toggle

`build_output_table(conn, country_code=None, years=(2025, 2024, 2023))`.
`country_code=None` reproduces the all-entities view (`CH`, `AE`, `LU`, `ZA`,
`CY`, `CN`, `SG`, `MT`, `GB`, `RO` are the valid codes, matching `offices.country_code`
— this was your choice over toggling by the 10 named business entities or by
individual office, since Switzerland's several offices, including Yuh,
collapse to one `CH` figure). Category 1 & 2 and Category 15 (and everything
that depends on them: Scope 3 of which, Total GHG, GHG per FTE) come back as
`None` for a specific country, rather than a misleadingly-small number,
because those two lines are recorded against the whole company
(`OWN_BOOK`, or the static override) and cannot be split by country from the
data available.
