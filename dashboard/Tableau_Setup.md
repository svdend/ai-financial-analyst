# Tableau Setup — PANW Financial Model

> **⚠️ WARNING: Tableau Public publishes data WORLD-READABLE and Google-INDEXABLE.**
> **The SEC data here is already public, but if you ever extend this project to**
> **non-public sources, do NOT publish to Tableau Public.**

---

## 1. File Overview

The `/dashboard/tableau_data/` folder contains five CSVs plus a Hyper extract:

| File | Description |
|---|---|
| `fact_financials.csv` | Long-format quarterly actuals (all line items) with provenance |
| `fact_forecasts.csv` | Prophet + AutoARIMA + Lasso forecasts with 80%/95% CIs |
| `dim_date.csv` | Date dimension: fiscal year/quarter + calendar year/quarter |
| `dim_metric.csv` | Metric metadata: label, category, unit |
| `dim_filing.csv` | One row per `accession_no` with `filing_url`, `form_type`, `filed_date` |
| `PANW_financials.hyper` | Pre-built Tableau extract over `fact_financials` only (convenience; see §3) |

---

## 2. Star Schema

Connect all tables in Tableau using these join keys:

```
fact_financials ──── dim_date    on  fact_financials.period_end = dim_date.date_key
fact_financials ──── dim_metric  on  fact_financials.line_item  = dim_metric.line_item
fact_financials ──── dim_filing  on  fact_financials.accession_no = dim_filing.accession_no
fact_forecasts  ──── dim_date    on  fact_forecasts.period_end  = dim_date.date_key
```

`dim_filing` is the **provenance dimension** — every mark on a Tableau viz can
carry a tooltip linking to the source SEC filing.

---

## 3. Connecting in Tableau Desktop / Tableau Public

1. Open Tableau Desktop or Tableau Public.
2. **Connect → Text File** → select `fact_financials.csv`.
3. Add remaining files via **Data Source** tab → drag each CSV to the canvas.
4. Create the joins as described above.

> The CSVs are the authoritative source — open all five via Tableau's Text
> File connector. The `.hyper` extract bundled in `tableau_data/` is a
> **`fact_financials`-only** convenience extract for faster scrolling on
> large quarter ranges; it is **not** a substitute for the `dim_*` and
> `fact_forecasts` CSVs. If you connect to the `.hyper` you will be missing
> the date/metric/filing dimensions and all forecast rows.

---

## 4. Recommended Worksheets

The three sheets below match the **currently published v1 dashboard**. Each
is built from `fact_financials` joined to the dimension tables.

### Sheet 1: Revenue Actuals
- Rows: `SUM([value])` filtered to `line_item = 'Revenue'`, scaled to $B
- Columns: `period_end` (continuous, quarterly)
- Marks: Line + circle for individual quarter marks
- Filters: `line_item = 'Revenue'`
- **"Source" tooltip** on every mark, joined via `dim_filing`:
  ```
  Accession: <accession_no>
  Filed: <filed_date>
  Form: <form_type>
  ATTR([filing_url])  ← wire up as a URL action (see §6)
  ```
- Click-through provenance is direct here — every Revenue mark is a single
  XBRL fact with a single accession.

### Sheet 2: PANW Margins %
- Rows: gross margin %, operating margin % (two measures on the same axis)
- Columns: `period_end` (continuous, quarterly)
- Marks: Line, one colour per margin type
- Calculated fields (see §5):
  ```
  Gross Margin %     = SUM([GrossProfit])  / SUM([Revenue])
  Operating Margin % = SUM([OperatingIncome]) / SUM([Revenue])
  ```
- Format axis as percentage; reference lines optional.
- Provenance: each margin computes from two source rows (numerator and
  denominator) — show both accession_no values in the mark's tooltip.

> **GAAP Net Margin excluded.** FY2024 Q2 (period_end 2024-01-31) contains
> a non-recurring $1.5B net tax benefit from a deferred tax asset
> valuation allowance release (per 10-Q `0001327567-24-000005`, filed
> 2024-02-21), which produces a ~88% net margin print that distorts trend
> comparisons. The Net Margin % calculation is preserved in the data
> source for reuse but is not surfaced on this sheet.

### Sheet 3: Revenue Growth
- Rows: YoY revenue growth %
- Columns: `period_end` (continuous, quarterly)
- Marks: Bar (positive/negative colour split) or Line
- Calculated field:
  ```
  YoY Revenue Growth =
    (SUM([Revenue]) - LOOKUP(SUM([Revenue]), -4))
    / ABS(LOOKUP(SUM([Revenue]), -4))
  ```
- Filter out the first four quarters (no prior-year comparable).
- Provenance: each growth value comes from two Revenue rows (current quarter
  + same quarter prior year) — both accession_no values are tooltip-able.

### Future work (v2 — not yet published)

The pipeline already produces `fact_forecasts.csv` and `v_variance_facts`,
but the following four sheets have not been built into the published
workbook. They are tracked as v2 dashboard work:

- **Actual vs Forecast** — dual-axis line: actuals from `fact_financials` +
  forecast bands (80% / 95% CIs) from `fact_forecasts`, three-model ensemble
  (Prophet / AutoARIMA / Lasso).
- **Variance Drivers** — bar chart of `revenue_variance_vs_forecast` per
  quarter, coloured by driver type (volume / margin / mix / one-time) from
  `v_variance_facts`.
- **Forecast Accuracy** — MAE and MAPE per expanding-window CV fold,
  grouped by model, with a 10% MAPE reference line.
- **Scenario Toggle** — parameter-driven Base / Bull / Bear filter on
  `fact_forecasts`, showing revenue forecast with CI bands.

---

## 5. Calculated Fields (margins, growth, variance)

Margins and growth rates are **not** materialized as fact rows in
`fact_financials.csv` — they have no single source accession and would break
the "every mark traces to a filing" claim. Compute them in Tableau as
calculated fields from the sourced rows. Provenance flows naturally: each
input row carries its own `accession_no`, so a margin or growth tooltip can
list the source filings used.

```
// Margins (use SUMIF-style expressions over the long-format fact_financials)
Gross Margin %     = SUM(IF [line_item]='GrossProfit'      THEN [value] END)
                   / SUM(IF [line_item]='Revenue'          THEN [value] END)

Operating Margin % = SUM(IF [line_item]='OperatingIncome'  THEN [value] END)
                   / SUM(IF [line_item]='Revenue'          THEN [value] END)

Net Margin %       = SUM(IF [line_item]='NetIncome'        THEN [value] END)
                   / SUM(IF [line_item]='Revenue'          THEN [value] END)

FCF Margin %       = SUM(IF [line_item]='FreeCashFlow'     THEN [value] END)
                   / SUM(IF [line_item]='Revenue'          THEN [value] END)

// Growth (period_end on Columns, sorted ascending, line_item filtered to Revenue)
YoY Revenue Growth = (SUM([value]) - LOOKUP(SUM([value]), -4))
                     / ABS(LOOKUP(SUM([value]), -4))

QoQ Revenue Growth = (SUM([value]) - LOOKUP(SUM([value]), -1))
                     / ABS(LOOKUP(SUM([value]), -1))

// Variance + accuracy (against fact_forecasts)
Revenue Variance % = ([Revenue Actual] - [Revenue Forecast]) / ABS([Revenue Forecast])
MAPE per fold      = ABS(([Revenue Actual] - [Revenue Forecast]) / [Revenue Actual])
```

---

## 6. Provenance Tooltip Setup

On any worksheet showing actuals:
1. In the **Tooltip** editor, add:
   ```
   Source filing: <accession_no>
   Filed: <filed_date>  |  Form: <form_type>
   Click to open: <URL action>
   ```
2. Create a **URL Action**: Dashboard → Actions → URL
   - URL: `<filing_url>`
   - Run on: Hover or Click

This is what makes the model interview-defensible: every data point is one click
away from its source SEC filing.

---

## 7. Publishing to Tableau Public

1. Sign in to Tableau Public (free account).
2. File → Save to Tableau Public.
3. Copy the published URL and embed it in the project README.

**Reminder**: Once published, data is world-readable. The SEC EDGAR data used
here is already public, so this is appropriate. Do not publish if you add
any non-public data sources.
