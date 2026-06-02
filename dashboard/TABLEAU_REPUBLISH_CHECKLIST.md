# Tableau Republish Checklist (for fluffypotato999)

A condensed, click-by-click version of `Tableau_Setup.md` for the operator step.
Use this when the spec in the main doc has drifted ahead of what's currently
published on Tableau Public.

**Prereqs:** Tableau Desktop installed; signed into Tableau Public; latest
`make demo TICKER=PANW` run so `dashboard/tableau_data/` has fresh CSVs and
the `.hyper` extract.

---

## A. One-time data wiring (Sheet 5 — FCF Gantt waterfall)

The pipeline now writes `fact_fcf_bridge.csv`, but the workbook doesn't connect
to it yet. Add it as a new datasource:

1. Open `dashboard/tableau_workbook/PANW_Dashboard.twb` in Desktop.
2. Data → New Data Source → Text File → select
   `dashboard/tableau_data/fact_fcf_bridge.csv`.
3. Drag `dim_date.csv` into the canvas; relate on
   `period_end ↔ date_key`.
4. Drag `dim_filing.csv` in; relate on
   `accession_no ↔ accession_no` (left join — the WC-plug rows have no
   accession and that's intentional).
5. Rename the datasource to `fact_fcf_bridge+` for symmetry with
   `fact_financials+`.

**Spec to match:** `Tableau_Setup.md` §1 (file overview) + §2 (star schema).

---

## B. Rebuild Sheet 5 as a Gantt waterfall

Delete the current 3-line "FCF Bridge" worksheet. Create a new one:

1. **Datasource:** `fact_fcf_bridge+`.
2. **Marks card:** change shape to **Gantt Bar**.
3. **Columns shelf** (in order):
   - `period_end` (continuous, Quarter)
   - `component_order` (discrete, ascending) — inner axis
4. **Rows shelf:** drag `[Bar Start]` (calc field below).
5. **Size shelf:** drag `[Bar Size]` (calc field below).
6. **Color shelf:** `component_role`. Edit colors:
   - `start` → `#1f4e79`
   - `add` → `#2e7d32`
   - `subtract` → `#c0504d`
   - `subtotal` → `#888888`
   - `end` → `#2e7d32`
7. **Filter:** `period_end` → last 8 quarters.
8. **Format:** value axis as `$#,##0,.0 "M"`.
9. **Tooltip:** include `component`, `value`, `accession_no` (suppress URL
   action for the WC-plug row — it has no accession).
10. **Title:** `PANW Free Cash Flow Bridge ({fiscal_quarter})`.

**Calculated fields** (paste verbatim — these are in `Tableau_Setup.md` Sheet 5):

```
// Running Total — additive bars (start + add)
Running Total = RUNNING_SUM(SUM(IF [component_role] IN ('start','add') THEN [value] END))

// Bar Size — positive bars rise, subtract bars fall, subtotals/ends span full height
Bar Size = IF [component_role] = 'subtract' THEN -[value]
           ELSEIF [component_role] IN ('subtotal','end') THEN [value]
           ELSE [value] END

// Bar Start — y-axis position of each bar's bottom edge
Bar Start = IF [component_role] = 'start' THEN 0
            ELSEIF [component_role] = 'add' THEN [Running Total] - [value]
            ELSEIF [component_role] = 'subtract' THEN [Running Total]
            ELSEIF [component_role] = 'subtotal' THEN 0
            ELSEIF [component_role] = 'end' THEN 0 END
```

---

## C. GAAP labeling on margin sheets

For each margin sheet (`Margins %`, `All Margins %`):

1. Worksheet title → append ` (GAAP)`.
2. Tooltip → add line: `GAAP per XBRL; non-GAAP not exposed`.

On each dashboard (Dashboard 1, 2, 3) → add a footer text object:
> *All metrics shown are GAAP unless otherwise noted.*

Spec: `Tableau_Setup.md` Sheet 2 + Sheet 6 + global header.

---

## D. Use `dim_metric.label` instead of raw line_item codes

Reviewer-facing axes/legends currently show codes like `OperatingCashFlow`,
`CapEx`, `FreeCashFlow`. Swap to the human labels from `dim_metric.csv`:

1. On every worksheet that puts `line_item` on Color, Detail, or a filter pill:
2. Right-click the pill → **Edit Alias** → set to the matching `label` from
   `dim_metric.csv`, OR
3. Replace the dimension with a **calculated field**:
   ```
   [Metric Label] = ATTR([dim_metric].[label])
   ```
   then drop `[Metric Label]` in place of `[line_item]`.

The calc-field route is safer — aliases are per-worksheet and drift over time.

---

## E. Hide redundant worksheets from the published view

Tableau Public currently shows **9 worksheets + 3 dashboards = 12 tabs**.
Single-chart worksheets that are already embedded in a dashboard should be
hidden so a reviewer sees the dashboards as the entry point.

For each worksheet that's *not* a click-through target of a dashboard action:
1. Right-click the worksheet tab → **Hide**.
2. Keep visible: Dashboard 1, Dashboard 2, Dashboard 3 + the 2–3
   click-through-target sheets used by cross-sheet actions.

Confirm by previewing → Server → Tableau Public → "View as Anonymous User."

---

## F. Republish

1. **Save** the workbook locally (`PANW_Dashboard.twb`).
2. Server → Tableau Public → Save to Tableau Public As… → name
   `PANW_Dashboard` (overwrites the existing viz at the live link).
3. Verify: open https://public.tableau.com/app/profile/sid.den/viz/PANW_Dashboard/Dashboard1
   in an incognito tab. Confirm:
   - Dashboard 1 → margin sheets carry "(GAAP)"
   - Dashboard 2 → FCF Bridge sheet now shows the Gantt waterfall
   - Tab strip shows 3 dashboards + ≤3 supporting sheets (not 12)
4. Commit the updated `.twb` + `.twbx` to the repo:
   ```bash
   git add dashboard/tableau_workbook/PANW_Dashboard.twb \
           dashboard/tableau_workbook/PANW_Dashboard.twbx
   git commit -m "Republish PANW dashboard: GAAP labels, FCF Gantt bridge, dim_metric labels, hidden sheets"
   git push origin main
   ```

---

## Bead mapping

This checklist closes three open beads when complete:

- `2f7` — Wire FCF waterfall bridge into Tableau .twb workbook (Sections A + B)
- `8h1` — Use dim_metric.label in Tableau workbook (Section D)
- `0z0-workbook` — Tableau tab cleanup + GAAP labels in workbook (Sections C + E)

Close them with `bd close <id> -m "Republished via TABLEAU_REPUBLISH_CHECKLIST.md"`
once the live link reflects the updated workbook.
