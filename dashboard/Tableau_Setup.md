# Tableau Setup — PANW Financial Model

> **⚠️ WARNING: Tableau Public publishes data WORLD-READABLE and Google-INDEXABLE.**
> **The SEC data here is already public, but if you ever extend this project to**
> **non-public sources, do NOT publish to Tableau Public.**

---

## 1. File Overview

The `/dashboard/tableau_data/` folder contains six CSVs plus a Hyper extract:

| File | Description |
|---|---|
| `fact_financials.csv` | Long-format quarterly actuals (all line items) with provenance |
| `fact_forecasts.csv` | Prophet + AutoARIMA + Lasso forecasts with 80%/95% CIs |
| `fact_fcf_bridge.csv` | Long-format NI → FCF waterfall components per quarter (Sheet 5) |
| `dim_date.csv` | Date dimension: fiscal year/quarter + calendar year/quarter |
| `dim_metric.csv` | Metric metadata: label, category, unit |
| `dim_filing.csv` | One row per `accession_no` with `filing_url`, `form_type`, `filed_date` |
| `PANW_financials.hyper` | Pre-built Tableau extract over `fact_financials` only (convenience; see §3) |

---

## 2. Star Schema

Connect all tables in Tableau using these join keys:

```
fact_financials  ──── dim_date    on  fact_financials.period_end  = dim_date.date_key
fact_financials  ──── dim_metric  on  fact_financials.line_item   = dim_metric.line_item
fact_financials  ──── dim_filing  on  fact_financials.accession_no = dim_filing.accession_no
fact_forecasts   ──── dim_date    on  fact_forecasts.period_end   = dim_date.date_key
fact_fcf_bridge  ──── dim_date    on  fact_fcf_bridge.period_end  = dim_date.date_key
fact_fcf_bridge  ──── dim_filing  on  fact_fcf_bridge.accession_no = dim_filing.accession_no
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

### Sheet 4: KPI Strip

Seven tiles across the top of the dashboard. Each shows a trailing-twelve-month
(TTM) figure with a sparkline + QoQ/YoY delta arrow underneath.

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

// Rule of 40 — durable SaaS quality bar
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

### Sheet 5: FCF Waterfall Bridge (Net Income → FCF)

True Gantt-style waterfall walking from Net Income to Free Cash Flow through
the four reconciling components. Each quarter renders seven bars in fixed
left-to-right order:

    NetIncome → Depreciation → SBC → ΔWorkingCapital → OperatingCashFlow → CapEx → FreeCashFlow

NI, D&A, SBC and ΔWC are *additive* bars whose running total reaches OCF
exactly (the ΔWC component is the residual plug `OCF − NI − D&A − SBC`, so
reconciliation is mathematical, not cosmetic). OCF is a *subtotal* bar; CapEx
is sign-flipped to negative; FCF is the *terminal* total bar.

**Data source: `fact_fcf_bridge.csv`** (one row per quarter × component).

- Columns: `period_end` (continuous, quarterly) — outer trellis
- Inside each quarter, place `[component_order]` (discrete, ascending) on the
  inner Columns shelf so bars render left-to-right in the canonical order
- Rows: a Gantt mark (one bar per component) — see calc fields below
- Marks: **Gantt Bar** (size = `[Bar Size]`, with `[Bar Start]` on Rows)
- Colour: `[component_role]` — five buckets:
  - `start` (NetIncome) — `#1f4e79` (Revenue/Operating positive)
  - `add` (Depreciation, SBC, ΔWC if positive) — `#2e7d32` (FCF green)
  - `subtract` (CapEx, ΔWC if negative) — `#c0504d` (red — outflow)
  - `subtotal` (OperatingCashFlow) — `#888888` (Operating margin grey)
  - `end` (FreeCashFlow) — `#2e7d32` (FCF green, matches Sheet 6)
- Filter to last 8 quarters (2 years) so the trellis stays legible
- Format: `$#,##0,.0 "M"` (millions, one decimal)

**Gantt waterfall calculations.** A Gantt bar is anchored at `[Bar Start]`
and extends by `[Bar Size]`. The running total walks NI → OCF on the
additive bars, OCF holds while CapEx draws down, and FCF reads the final
total:

```
// Running total of additive bars NI + D&A + SBC + ΔWC, in component_order
Running Total =
  RUNNING_SUM(SUM(IF [component_role] IN ('start','add')
                  THEN [value] END))

// Bar Size: positive bars rise from the floor; subtract bars fall from the top.
// 'subtotal' (OCF) and 'end' (FCF) are total bars — they render as a single
// bar from 0 to the value, not as a delta.
Bar Size =
  IF [component_role] = 'subtract' THEN -[value]
  ELSEIF [component_role] IN ('subtotal','end') THEN [value]
  ELSE [value] END

// Bar Start: where the bar's bottom edge sits on the y-axis
Bar Start =
  IF [component_role] = 'start'      THEN 0
  ELSEIF [component_role] = 'add'    THEN [Running Total] - [value]
  ELSEIF [component_role] = 'subtract' THEN [Running Total]      // OCF level
  ELSEIF [component_role] = 'subtotal' THEN 0                    // OCF total bar
  ELSEIF [component_role] = 'end'      THEN 0                    // FCF total bar
  END
```

Set the Marks card type to **Gantt Bar**, drop `[Bar Start]` on Rows and
`[Bar Size]` on the Size shelf. The waterfall reads correctly on the first
render — no manual offset tweaking required.

**Reconciliation invariant** (verifiable in `tests/test_warehouse.py`):

    NetIncome + Depreciation + SBC + WorkingCapitalAndOther = OperatingCashFlow
    OperatingCashFlow + CapEx (signed)                        = FreeCashFlow

Both lines hold by construction in `v_fcf_bridge`, so the bridge is
guaranteed not to "almost" close.

**Provenance** is per-component. Five of the seven components carry their
source `accession_no` directly (NI, D&A, SBC, OCF, CapEx, FCF — FCF inherits
OCF's accession). The `WorkingCapitalAndOther` plug carries
`accession_no = NULL` because it is derived from the four contributing
filings, not a single XBRL fact. The tooltip should make this explicit:

```
Quarter:     <fiscal_year> <fiscal_period>
Component:   <component>            $<value>M
Source:      <accession_no | "Derived (OCF − NI − D&A − SBC)">
Filed:       <filed_date>
Open SEC filing  ← URL action; suppressed for the WC plug
```

A quarter missing any of NI/D&A/SBC/OCF/CapEx is **excluded** from the
bridge entirely (the view's `complete` CTE drops it). Don't fill zeros — the
gap is real and `v_data_quality.missing_quarters` flags it elsewhere.

### Sheet 6: Profitability Stack (replaces Sheet 2)

Multi-line chart of three margins on a shared % axis. **Replaces** Sheet 2
(`PANW Margins %`) — same calc fields, but adds FCF Margin and a 30%
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

### Sheet 7: Revenue & Forecast Overlay

Actuals + 3-model forecast fan from `fact_forecasts.csv`. The point is to
show the *range* of plausible outcomes, not a point estimate.

**Cap the forecast horizon at 4 quarters out.** With ~20 historical
observations, the 95% PI on quarter 5+ is wide enough to be meaningless
(e.g. `$3.4B–$9.1B` for FY2027). Filter `fact_forecasts.period_end` to the
first 4 forecast points.

- Columns: `period_end` (continuous, quarterly)
- Rows: `[value]` from `fact_financials` (Revenue), and `yhat` / band fields
  from `fact_forecasts`
- Marks:
  - Actuals: solid line, `#1f4e79`
  - `yhat` per model: dashed line, one colour per model (Prophet `#5b8def`,
    AutoARIMA `#a169b8`, Lasso `#e07b00`)
  - 80% PI band: shaded area between `yhat_lower_80` and `yhat_upper_80`,
    20% opacity, model colour
  - 95% PI band: shaded area between `yhat_lower_95` and `yhat_upper_95`,
    10% opacity, same colour (gives the layered fan look)
- Format: `$#,##0,,.0 "B"` (billions)
- Add a **parameter** `[Selected Model]` with allowable values
  `Prophet | AutoARIMA | Lasso | All`; filter the forecast layer to match
- **Provenance note in the tooltip**: forecasts are *not* SEC-sourced —
  show "Forecast: <model>, generated YYYY-MM-DD, 95% PI [low, high]" and
  do **not** wire a sec.gov URL action on forecast marks (only on actuals)

### Sheet 8: DSO (Days Sales Outstanding)

`DSO = AccountsReceivable / Revenue × 91`. Rising = customers paying slower
(collections risk); falling = collections improving.

- Columns: `period_end` (continuous, quarterly)
- Rows: `[DSO]` (calc field below)
- Marks: Bar + an 8-quarter rolling average overlay line
- Format: `#0` with " d" suffix (e.g. `78 d`)
- Reference line: 75 days (industry median for enterprise security
  software), dashed grey

```
DSO = SUM(IF [line_item]='AccountsReceivable' THEN [value] END)
    / SUM(IF [line_item]='Revenue'             THEN [value] END)
    * 91
DSO 8Q Rolling Avg = WINDOW_AVG([DSO], -7, 0)
```

Provenance: two-source (AR + Revenue accessions, same quarter).

### Sheet 9: Billings (Derived)

Forward-revenue visibility. Billings is the standard analyst metric for
demand recognised this quarter regardless of revenue timing:

    Billings = Revenue + ΔDeferredRevenue

This matches the calc in `src/build_variance_facts.py`. Until RPO disclosure
ingests (bead `zh9`), this derivation is the closest GAAP-only equivalent.

- Columns: `period_end` (continuous, quarterly)
- Rows (dual axis):
  - Bar: `[Billings (Derived)]` — `$M`
  - Line: `[DefRev / Revenue]` (forward-cover ratio) — `%`
- Format bar: `$#,##0,.0 "M"`; format line axis: `#0.0%`

```
Revenue (Latest)   = SUM(IF [line_item]='Revenue' THEN [value] END)
DefRev (Latest)    = SUM(IF [line_item]='DeferredRevenue' THEN [value] END)
Δ DefRev           = [DefRev (Latest)] - LOOKUP([DefRev (Latest)], -1)
Billings (Derived) = [Revenue (Latest)] + [Δ DefRev]
DefRev / Revenue   = [DefRev (Latest)] / [Revenue (Latest)]
```

Caption to add on the sheet: "Billings = Revenue + ΔDeferredRevenue, the
standard analyst proxy. Replace with RPO disclosure when ingested
(bead `zh9`)."

### Sheet 10: Rule of 40 Quadrant

Scatter: X = YoY revenue growth %, Y = TTM FCF margin %. The 40% diagonal
is the durable-SaaS threshold — above-and-right = healthy, below-and-left
= at-risk.

- Columns: `[YoY Revenue Growth]` (continuous, %)
- Rows: `[TTM FCF Margin %]` (continuous, %)
- Marks: Circle, one mark per quarter
  - Colour: `fiscal_year` (sequential palette — older years lighter, newer darker)
  - Detail: `period_end` (one mark per quarter)
  - Size: small (8 px); ~20 quarters total
- Add a **trail line** (Path → period_end ascending) so the time order is
  visible — Tableau's "trail" mark or a connected line via `period_end` on Path
- **Reference line**: the 40% diagonal — add a calc field `[40 Line] = 0.40 - [YoY Revenue Growth]` and plot as a reference band, *or* draw a manual annotation line through `(0%, 40%)` and `(40%, 0%)`
- **Annotate** the most recent quarter with its label (e.g. `FY2026 Q2`) and the Rule-of-40 score
- Format axes: `#0.0%`

Provenance: each dot traces to *4* filings (TTM aggregates) — list the
range `MIN(filed_date) … MAX(filed_date)` in the tooltip; URL action fires
on the latest quarter's accession.

### Sheet 11: Forecast vs Actuals Scorecard

Model accountability. For the most-recent realised quarter, show the prior
forecast (per model) vs the actual that landed.

- One small table, four rows (Actual, Prophet, AutoARIMA, Lasso), three columns:
  Quarter, Forecast / Actual, Δ vs Actual %
- Pull the actual from `fact_financials`; pull the forecast from
  `fact_forecasts` filtered to the same `period_end`
- Format Δ: `+0.0%;-0.0%`, green if `|Δ| < 5%`, amber 5–10%, red >10%
- Caption: "Forecasts generated YYYY-MM-DD, before this quarter's filing.
  No data leakage."

It's small, but it's the falsifiable one — the model has to be wrong in public.

### Future work (v2 — not yet published)

The pipeline already produces `fact_forecasts.csv` and `v_variance_facts`.
Sheet 7 + Sheet 11 land the forecast overlay and accountability scorecard;
the following remain v2:

- **Variance Drivers** — bar chart of `revenue_variance_vs_forecast` per
  quarter, coloured by driver type (volume / margin / mix / one-time) from
  `v_variance_facts`.
- **Forecast Accuracy (CV folds)** — MAE and MAPE per expanding-window CV
  fold, grouped by model, with a 10% MAPE reference line.
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

Every data point is one click away from its source SEC filing.

---

## 6b. Dashboard Layout (Day 3 target — adds Forecast / Working-Capital / Rule of 40)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  PANW — Financial Model        Data through FYYYYY Q# (10-Q YYYY-MM-DD) │
├────────┬────────┬────────┬────────┬────────┬────────┬─────────────────────────┤
│ Sheet 4 (KPI strip — 7 BAN tiles, equal width):                             │
│ TTM Rev │ TTM OI │ TTM FCF│ FCF M %│  Cash  │ YoY Rev│  Rule of 40           │
├────────┴────────┴────────┴────────┴────────┴────────┴─────────────────────────┤
│ Sheet 7 — Revenue & Forecast Overlay (capped 4Q out)  │ Sheet 11 — Forecast  │
│                                                       │ vs Actuals scorecard │
├───────────────────────────────┬──────────────────────────────────────────────┤
│ Sheet 6 — Profitability Stack │  Sheet 5 — FCF Cash Flow Bridge              │
├───────────────────────────────┼──────────────────────────────────────────────┤
│ Sheet 8 — DSO                 │  Sheet 9 — Billings (Derived)                │
├───────────────────────────────┴──────────────────────────────────────────────┤
│ Sheet 10 — Rule of 40 Quadrant (full width)                                  │
├──────────────────────────────────────────────────────────────────────────────┤
│ Footer: warning + "Source: SEC EDGAR · model snapshot YYYY-MM-DD · CC0"      │
└──────────────────────────────────────────────────────────────────────────────┘
```

Drop Sheets 1 and 3 from the dashboard once Sheet 7 (which carries actuals
inline) and Sheet 4's BAN tiles cover their content. Drop Sheet 2 once
Sheet 6 is in (already noted in §Sheet 6).

---

## 6c. Color palette (lock once, reuse everywhere)

| Series                       | Hex       | Where used                       |
|------------------------------|-----------|----------------------------------|
| Revenue / Operating positive | `#1f4e79` | Sheet 1, Sheet 5 NetIncome       |
| FCF / cash positive          | `#2e7d32` | Sheet 5 add bars + FCF, Sheet 6  |
| CapEx / outflow              | `#c0504d` | Sheet 5 subtract bars (CapEx)    |
| Subtotal (OCF)               | `#888888` | Sheet 5 OCF subtotal, Sheet 6 OM |
| Gross margin                 | `#444444` | Sheet 6                          |
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
