# Tableau Setup — {ticker} Financial Model

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
| `{ticker}_financials.hyper` | Pre-built Tableau extract over `fact_financials` only (convenience; see §3) |

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

### Sheet 2: {ticker} Margins %
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

### Sheet 4: KPI Strip (Big Ass Numbers)

A row of seven BAN tiles across the top of the dashboard, each showing a
trailing-twelve-month (TTM) headline figure with a sparkline + QoQ/YoY delta
arrow underneath. Drives the "scannable in 5 seconds" reviewer experience.

Tiles (left → right): **TTM Revenue · TTM Operating Income · TTM FCF ·
FCF Margin · Cash · YoY Revenue Growth · Rule of 40**.

Build pattern (one BAN sheet per tile, identical recipe):
- Detail mark: `period_end` (continuous, exact date)
- Marks: Text — drag the calc field (below) to Text shelf
- Filter `period_end` to *Most Recent* via a relative-date filter
- Add a sparkline below the BAN: same calc on rows, `period_end` on columns,
  size 60 px tall, axes hidden, no title — see §"Sparkline pattern" below
- Add a delta-arrow caption: ▲ green for ≥0, ▼ red for <0, neutral grey at 0

```
// TTM aggregates — sum the trailing 4 quarters of Q facts
// (filter line_item + period_type='Q' on the data source first)
TTM Revenue        = WINDOW_SUM(SUM(IF [line_item]='Revenue'         THEN [value] END), -3, 0)
TTM Op Income      = WINDOW_SUM(SUM(IF [line_item]='OperatingIncome' THEN [value] END), -3, 0)
TTM OCF            = WINDOW_SUM(SUM(IF [line_item]='OperatingCashFlow' THEN [value] END), -3, 0)
TTM CapEx          = WINDOW_SUM(SUM(IF [line_item]='CapEx'           THEN [value] END), -3, 0)
TTM FCF            = [TTM OCF] - [TTM CapEx]
TTM FCF Margin %   = [TTM FCF] / [TTM Revenue]

// Cash is a stock (point-in-time), not a flow — use latest, not TTM
Cash (Latest)      = SUM(IF [line_item]='Cash' THEN [value] END)

// YoY revenue growth — uses the per-quarter Revenue (not TTM) so the
// number matches Sheet 3
YoY Revenue Growth = (SUM(IF [line_item]='Revenue' THEN [value] END)
                     - LOOKUP(SUM(IF [line_item]='Revenue' THEN [value] END), -4))
                   / ABS(LOOKUP(SUM(IF [line_item]='Revenue' THEN [value] END), -4))

// Rule of 40 — durable SaaS quality bar; >40 is the senior-analyst signal
Rule of 40         = [YoY Revenue Growth] + [TTM FCF Margin %]
```

**Format**:
- Dollars: `$#,##0,,.0 "B"` for $-billions (e.g. `$10.2B`); `$#,##0,.0 "M"` for $-millions (Cash if <$1B)
- Percentages: `#0.0%` (one decimal place)
- Rule of 40: `#0.0%` plus a conditional reference colour — green if ≥40%, amber 30–40%, red <30%

**Provenance**: every BAN tile's tooltip lists the *latest* `accession_no`
contributing to it (TTM aggregates pull from 4 filings — list all 4 in the
tooltip via `MIN([filed_date])` to `MAX([filed_date])` range and the *latest*
accession as the click-through).

### Sheet 5: FCF Cash Flow Bridge

Quarterly waterfall showing how OCF translates to FCF after CapEx — the
single most important slide for assessing capital efficiency at a security
software vendor.

- Rows: `[value]` (with CapEx sign-flipped to negative; FCF as a calculated bar)
- Columns: `period_end` (continuous, quarterly)
- Marks: Bar (stacked) + line overlay for FCF
- Three series on one axis, distinguished by colour:
  - **OCF** — blue `#1f4e79`, positive bar
  - **CapEx** — light blue `#7fa8c9`, negative bar (sign-flipped at the calc level)
  - **FCF** — green `#2e7d32`, line + circle markers
- Format: `$#,##0,.0 "M"` (millions, one decimal)
- Filter to last 12 quarters (3 years)

```
OCF (signed)       = SUM(IF [line_item]='OperatingCashFlow' THEN [value]  END)
CapEx (signed)     = SUM(IF [line_item]='CapEx'             THEN -[value] END)  // flip to negative
FCF                = [OCF (signed)] + [CapEx (signed)]
```

**Provenance** is two-source per FCF mark — both OCF and CapEx accessions
should appear in the tooltip:
```
Quarter:  <fiscal_year> <fiscal_period>
OCF:      $<OCF>M  (accession <OCF accession>)
CapEx:    $<CapEx>M (accession <CapEx accession>)
FCF:      $<FCF>M
Click to open: <URL action — fires on the OCF accession>
```

### Sheet 6: Profitability Stack (replaces Sheet 2)

Multi-line chart of three margins on a shared % axis. **Replaces** Sheet 2
(`{ticker} Margins %`) — same calc fields, but adds FCF Margin and a 30%
Operating Margin reference line. Drop Sheet 2 from the dashboard once Sheet 6
is wired up.

- Rows: `[Gross Margin %]`, `[Operating Margin %]`, `[FCF Margin %]` (use
  Measure Names / Measure Values to put three on one axis)
- Columns: `period_end` (continuous, quarterly)
- Marks: Line, one colour per margin type
  - Gross — dark grey `#444444`
  - Operating — medium grey `#888888`
  - FCF — green `#2e7d32` (reuse the FCF bridge colour for visual continuity)
- Format axis: `#0.0%`
- **Reference line**: 30% on Operating Margin, dashed grey, label "Rule-of-thumb durable Op Margin"
- **GAAP Net Margin remains excluded** for the same reason as Sheet 2 (FY2024
  Q2 DTA valuation allowance release distorts the print). Calc field stays at
  data-source level for future reuse.

Provenance: each margin mark traces to two accessions (numerator + denominator).
The tooltip should list both — same template as Sheet 2.

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

// FCF is not a fact row — computed from OCF − CapEx (CapEx values are positive in
// fact_financials.csv, so we subtract). Each FCF mark therefore traces to two
// source filings — list both accession_no values in the tooltip.
Free Cash Flow     = SUM(IF [line_item]='OperatingCashFlow' THEN [value] END)
                   - SUM(IF [line_item]='CapEx'             THEN [value] END)

FCF Margin %       = [Free Cash Flow]
                   / SUM(IF [line_item]='Revenue'           THEN [value] END)

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
   Source: 10-Q filed <filed_date>  ·  accession <accession_no>
   Click to open on SEC.gov
   ```
2. Create a **URL Action**: Dashboard → Actions → URL
   - Source sheets: every actuals/margin/FCF sheet on the dashboard
   - Run on: **Menu** (avoids accidental nav on hover; reviewers right-click → "Open SEC filing")
   - URL: `<filing_url>`  (drag the field in via the URL editor — Tableau substitutes per row)
   - Name shown in tooltip: "Open SEC filing"

For multi-source marks (FCF, margins, growth), the URL action fires on the
*primary* accession (OCF for FCF; numerator for margins; current-quarter for
growth). The tooltip still lists all contributing accessions in plain text so
no source is hidden.

This is what makes the model interview-defensible: every data point is one click
away from its source SEC filing.

---

## 6b. Dashboard Layout (Day 2 target)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  {ticker} — Financial Model        Data through FYYYYY Q# (10-Q YYYY-MM-DD) │
├────────┬────────┬────────┬────────┬────────┬────────┬─────────────────────────┤
│ Sheet 4 (KPI strip — 7 BAN tiles, equal width):                             │
│ TTM Rev │ TTM OI │ TTM FCF│ FCF M %│  Cash  │ YoY Rev│  Rule of 40           │
├────────┴────────┴────────┴────────┴────────┴────────┴─────────────────────────┤
│ Sheet 1 — Revenue Actuals     │  Sheet 6 — Profitability Stack (Gross/Op/FCF)│
├───────────────────────────────┼──────────────────────────────────────────────┤
│ Sheet 3 — YoY Revenue Growth  │  Sheet 5 — FCF Cash Flow Bridge              │
├───────────────────────────────┴──────────────────────────────────────────────┤
│ Footer: warning + "Source: SEC EDGAR · model snapshot YYYY-MM-DD · CC0"      │
└──────────────────────────────────────────────────────────────────────────────┘
```

Drop Sheet 2 once Sheet 6 is wired in (Sheet 6 is the strict superset).

---

## 6c. Color palette (lock once, reuse everywhere)

| Series                       | Hex       | Where used                       |
|------------------------------|-----------|----------------------------------|
| Revenue / Operating positive | `#1f4e79` | Sheet 1, Sheet 5 OCF             |
| FCF / cash positive          | `#2e7d32` | Sheet 5 FCF line, Sheet 6 FCF M  |
| CapEx / outflow              | `#7fa8c9` | Sheet 5 CapEx                    |
| Gross margin                 | `#444444` | Sheet 6                          |
| Operating margin             | `#888888` | Sheet 6                          |
| Forecast 80% / 95% bands     | translucent `#e07b00` | (v2 forecast overlay)|
| Reference lines              | dashed grey `#bbbbbb` | All sheets                       |

Set these as a custom Tableau colour palette in `Preferences.tps` so the
choices are diff-visible in `PANW_Dashboard.twb`.

---

## 6d. Sparkline pattern (KPI strip)

Each KPI tile has a 60-px tall sparkline directly under the BAN:
- New worksheet, drag BAN's calc to Rows, `period_end` to Columns (continuous)
- Marks: Line, no markers, weight 1.5px
- Format: hide both axes, hide gridlines, hide title, no tooltip
- Filter `period_end` to last 12 quarters
- On the dashboard, place this sheet directly below the BAN tile in a vertical
  layout container; pin height at 60 px

---

## 7. Publishing to Tableau Public

1. Sign in to Tableau Public (free account).
2. File → Save to Tableau Public.
3. Copy the published URL and embed it in the project README.

**Reminder**: Once published, data is world-readable. The SEC EDGAR data used
here is already public, so this is appropriate. Do not publish if you add
any non-public data sources.
