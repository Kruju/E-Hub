-- e-Hub ESG pivot database schema
--
-- Models the 5 tables found in the "e-Hub Data Structure" export
-- (offices, offices_information, activity_types, emission_factors, activities)
-- plus two small tables needed to make the "Output 2025" pivot reproducible:
--   category_1_2_overrides       -- static historical values for a line with no source data (see README)
--   electricity_renewable_share  -- placeholder reference table (see README, rule still pending)
--
-- See README.md for the full mapping between these tables and every line of
-- the target pivot output, plus every data-quality issue found in the source
-- file and the decisions made about how to handle them.

BEGIN;

CREATE TABLE countries (
    country_code CHAR(2) PRIMARY KEY,
    country_name TEXT NOT NULL
);

CREATE TABLE offices (
    office_id           TEXT PRIMARY KEY,
    office_city_name    TEXT,  -- NULL for virtual entries (OWN_BOOK)
    office_detail       TEXT,
    country_code        CHAR(2) REFERENCES countries(country_code),
    entity_legal_name   TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    -- OWN_BOOK (investment book) and SWITZERLAND_CH (country-level rollup used
    -- by a handful of legacy activity rows) are not physical sites.
    is_virtual          BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE office_annual_metrics (
    id                      SERIAL PRIMARY KEY,
    office_id               TEXT NOT NULL REFERENCES offices(office_id),
    reporting_year          SMALLINT NOT NULL,
    fte_female              NUMERIC(10, 4),
    fte_male                NUMERIC(10, 4),
    fte_total               NUMERIC(10, 4),
    working_days            SMALLINT,
    percentage_average_ho   NUMERIC(6, 5),
    UNIQUE (office_id, reporting_year)
);

CREATE TABLE activity_types (
    activity_type_code  TEXT PRIMARY KEY,
    activity_name       TEXT NOT NULL,
    scope               TEXT NOT NULL CHECK (scope IN ('SCOPE_1', 'SCOPE_2', 'SCOPE_3')),
    scope3_category     SMALLINT,
    category_name       TEXT NOT NULL,
    default_unit        TEXT NOT NULL
);

CREATE TABLE emission_factors (
    id                  SERIAL PRIMARY KEY,
    factor_id           TEXT NOT NULL,   -- original id from the source export, kept for traceability
    activity_type_code  TEXT NOT NULL REFERENCES activity_types(activity_type_code),
    reporting_year       SMALLINT NOT NULL,
    location_vs_market   TEXT CHECK (location_vs_market IN ('LOCATION', 'MARKET')),
    -- Not a FK to countries: this table's country_code also covers ~60
    -- worldwide hotel-destination countries (BUSINESS_TRAVEL_HOTEL) that
    -- Swissquote doesn't have an office in, so it isn't a subset of offices.
    country_code         CHAR(2),
    factor_value         NUMERIC(14, 6),
    co2e_weight          TEXT,
    source_name          TEXT,
    comment               TEXT
    -- NOTE: for several activity types (Employee Commuting, Business Travel,
    -- Fuel, Water) more than one row applies to the same
    -- (activity_type_code, reporting_year[, country_code]) and the rows must
    -- be SUMMED together (components such as a "WTT" well-to-tank factor on
    -- top of a direct combustion factor, or a unit-conversion factor
    -- alongside the real CO2 factor). This is preserved as-is from the
    -- source; see README for which categories this affects.
);

CREATE TABLE activities (
    id                      SERIAL PRIMARY KEY,
    -- Not unique: a few source rows (e.g. multiple hotel bookings in the
    -- same office/year) share the same generated activity_id -- both are
    -- real, distinct records and must both be kept/summed. See README.
    activity_id             TEXT NOT NULL,
    office_id               TEXT NOT NULL REFERENCES offices(office_id),
    reporting_year          SMALLINT NOT NULL,
    period_start            DATE NOT NULL,
    period_end              DATE NOT NULL,
    activity_type_code      TEXT NOT NULL REFERENCES activity_types(activity_type_code),
    -- Nullable: a handful of source rows only ever recorded the final
    -- tCO2e figure with no underlying quantity (e.g. season-ticket rail
    -- travel, some hotel nights) -- see README.
    quantity                NUMERIC(18, 6),
    market_based_tco2e      NUMERIC(18, 6),
    location_based_tco2e    NUMERIC(18, 6),
    is_estimated            BOOLEAN NOT NULL DEFAULT FALSE,
    source_type             TEXT
);

CREATE INDEX idx_activities_type_year ON activities (activity_type_code, reporting_year);
CREATE INDEX idx_activities_office_year ON activities (office_id, reporting_year);

-- Category "1 & 2 - Purchased Goods & Services and Capital Goods" (~tCO2e)
-- has NO source records anywhere in the provided export (activity types
-- PURCHASED_GOODS_SERVICES / CAPITAL_GOODS exist but zero activity rows were
-- ever populated for them). Per your decision, we store the known historical
-- totals as a static override until the real spend-based data/factors exist.
-- This is necessarily a global (all-countries) figure -- it cannot be split
-- per country from the data available.
CREATE TABLE category_1_2_overrides (
    reporting_year  SMALLINT PRIMARY KEY,
    tco2e_value     NUMERIC(18, 6) NOT NULL,
    note            TEXT
);

-- "Of which energy consumption from renewable / non-renewable sources"
-- cannot be derived from the activity-level electricity subtype quantities
-- (every combination was checked against the known 2023-2025 ratios -- none
-- matched). Left empty until the real rule/reference data is supplied.
CREATE TABLE electricity_renewable_share (
    country_code     CHAR(2) NOT NULL REFERENCES countries(country_code),
    reporting_year   SMALLINT NOT NULL,
    renewable_share  NUMERIC(6, 5) NOT NULL CHECK (renewable_share BETWEEN 0 AND 1),
    source           TEXT,
    PRIMARY KEY (country_code, reporting_year)
);

COMMIT;
