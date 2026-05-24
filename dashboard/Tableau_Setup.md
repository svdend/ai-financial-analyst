# Tableau Setup — PANW Financial Model

> **⚠️ WARNING: Tableau Public publishes data WORLD-READABLE and Google-INDEXABLE.**
> **The SEC data here is already public, but if you ever extend this project to**
> **non-public sources, do NOT publish to Tableau Public.**

---

## 1. File Overview

The `/dashboard/tableau_data/` folder contains five files:

| File | Description |
|---|---|
| `fact_financials.csv` | Long-format quarterly actuals (all line items) with provenance |
| `fact_forecasts.csv` | Prophet + AutoARIMA + Lasso forecasts with 80%/95% CIs |
| `dim_date.csv` | Date dimension: fiscal year/quarter + calendar year/quarter |
| `dim_metric.csv` | Metric metadata: label, category, unit |
| `dim_filing.csv` | One row per `accession_no` with `filing_url`, `form_type`, `filed_date` |

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
- This is the only sheet with click-through provenance — the margins and
  growth sheets are derived metrics that don't map 1:1 to a single filing.

### Sheet 2: PANW Margins %
- Rows: gross margin %, operating margin %, net margin % (three measures
  on the same axis, or three rows)
- Columns: `period_end` (continuous, quarterly)
- Marks: Line, one colour per margin type
- Calculated fields:
  ```
  Gross Margin %     = SUM([GrossProfit])  / SUM([Revenue])
  Operating Margin % = SUM([OperatingIncome]) / SUM([Revenue])
  Net Margin %       = SUM([NetIncome])    / SUM([Revenue])
  ```
- Format axis as percentage; reference lines optional.

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

## 5. Sample Calculated Fields

Paste these into Tableau's **Calculated Field** editor:

```
// Revenue Variance %
([Revenue Actual] - [Revenue Forecast]) / ABS([Revenue Forecast])

// MAPE per fold
ABS(([Revenue Actual] - [Revenue Forecast]) / [Revenue Actual])

// YoY Revenue Growth
([Revenue] - LOOKUP([Revenue], -4)) / ABS(LOOKUP([Revenue], -4))
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
