# AI Financial Analyst

> End-to-end AI-augmented financial forecasting for US-listed enterprise security
> and software vendors, built entirely on free public data (SEC EDGAR + FRED).

Demonstrated on **PANW** (hardware-plus-subscription hybrid: Strata appliances +
Prisma SASE + Cortex XDR/XSIAM) and validated on **CRWD** and **SNOW** (pure-SaaS
shape, no physical inventory).  One config change — `make demo TICKER=CRWD` —
switches the entire pipeline to a different company.

---

## Overview

This project automates the financial analyst workflow end-to-end:

1. **Ingest** — pulls structured XBRL facts from SEC EDGAR with full provenance
   (every number carries an accession number and filing URL)
2. **Warehouse** — DuckDB analytics layer with IS/BS/CF views, data-quality flags,
   restatement detection, and post-forecast variance views
3. **Model** — three independent revenue forecasts (Prophet, AutoARIMA, Lasso with
   FRED macro features) with honest uncertainty quantification at small sample sizes
4. **Excel model** — simplified three-statement model (Base/Bull/Bear scenarios)
   with a per-cell Sources sheet tracing every historical value to its filing
5. **Dashboard** — Tableau-ready star-schema CSVs (and `.hyper` extract) with
   click-through to source filings via the `dim_filing` dimension
6. **Commentary** — Claude-generated CFO-style variance commentary using the
   reasoning-vs-computation split (all arithmetic in Python; Claude writes
   narrative only, with inline accession-level citations)
7. **Eval harness** — 5 ground-truth variance scenarios including
   refusal-on-restatement, tested in CI

---

## Architecture

```
SEC EDGAR XBRL                FRED API           yfinance
     │                           │                   │
     ▼                           ▼                   ▼
src/ingest_edgar.py ──────► src/build_warehouse.py (DuckDB)
  (provenance columns:              │
   accession_no, fact_id,           ├── v_income_statement_quarterly
   form_type, filed_date)           ├── v_balance_sheet_quarterly
                                    ├── v_cash_flow_quarterly
                                    ├── v_key_metrics
                                    ├── v_data_quality (has_physical_inventory,
                                    │                   has_restatement)
                                    └── v_variance_facts (post-forecast)
                                         │
                    ┌────────────────────┼───────────────────────┐
                    ▼                    ▼                       ▼
          Prophet + AutoARIMA      Lasso (FRED macro)     build_excel_model.py
          (nb/02_baseline)         (nb/03_macro)           3-statement + Sources
                    │                    │                       │
                    └────────────────────┘                       │
                                    │                            │
                                    ▼                            ▼
                         src/generate_commentary.py      export_for_tableau.py
                          ┌──────────────────────────┐         │
                          │ STEP 1: Python computes   │         ▼
                          │   all variances in SQL    │   Tableau Public
                          │ STEP 2: refusal checks    │   (dim_filing tooltips
                          │ STEP 3: pre-format JSON   │    link to EDGAR)
                          │ STEP 4: Claude → narrative│
                          │ STEP 5: hallucination guard│
                          └──────────────────────────┘
```

**Key architectural invariant — reasoning vs. computation split:**
All arithmetic happens in deterministic Python/SQL before Claude is called.
Claude generates narrative only.  Every number cited in the commentary must
appear verbatim in the input JSON and traces back to an SEC accession number.

---

## Setup

```bash
git clone https://github.com/fluffypotato999/ai-financial-analyst
cd ai-financial-analyst
make setup          # creates .venv, installs all dependencies
cp .env.example .env
# Fill in ANTHROPIC_API_KEY, FRED_API_KEY, SEC_USER_AGENT
```

> **cmdstan warning:** The first `make forecast` triggers a ~200 MB cmdstan
> download and C++ compile (~5 min).  Pre-stage it once with:
> ```bash
> .venv/bin/python -c "import cmdstanpy; cmdstanpy.install_cmdstan()"
> ```
> Subsequent runs are instant.

---

## Pipeline

Run steps in order, or run everything with `make demo TICKER=PANW`:

| Step | Command | What it does |
|---|---|---|
| 1 | `make ingest TICKER=PANW` | Fetch XBRL facts from SEC EDGAR → parquet |
| 2 | `make warehouse TICKER=PANW` | Build DuckDB analytics views |
| 3 | `make model TICKER=PANW` | Prophet + AutoARIMA forecasts |
| 4 | `make forecast TICKER=PANW` | Lasso macro forecast + variance facts view |
| 5 | `make dashboard TICKER=PANW` | Excel model + Tableau CSVs |
| 6 | `make commentary TICKER=PANW` | LLM commentary (dry-run by default) |
| 7 | `make notebooklm TICKER=PANW` | NotebookLM source bundle |

For live API commentary: `make commentary TICKER=PANW LIVE=1`

---

## Modeling

Three independent revenue forecasting models are run and compared:

- **Prophet** (Bayesian, Stan backend) — trend + seasonality decomposition with
  proper Bayesian credible intervals
- **AutoARIMA** (statsforecast) — auto-selects ARIMA order via information criteria
- **LassoCV** (scikit-learn) — regularised linear model with FRED macro features
  (yield curve, CPI YoY, Fed funds rate, sector ETF return, industrial production)

All three models are honest about uncertainty: with ~20 quarterly observations,
no single model is statistically defensible.  The ensemble characterises the
*range* of plausible outcomes rather than claiming predictive precision.

---

## Provenance

Every fact in the pipeline carries seven provenance columns from ingestion through
to the Excel Sources sheet and Tableau tooltips:

| Column | Example |
|---|---|
| `concept_used` | `RevenueFromContractWithCustomerExcludingAssessedTax` |
| `accession_no` | `0001327567-26-000123` |
| `fact_id` | SHA-256 of (ticker, concept, period, accession) |
| `filing_url` | `https://www.sec.gov/Archives/edgar/data/…` |
| `form_type` | `10-K` |
| `filed_date` | `2026-02-20` |
| `frame` | `CY2025Q3I` |

The `form_type` + `filed_date` fields power the **restatement detection** logic:
only true 10-K/A or 10-Q/A amendments are flagged — routine 10-Q → 10-K
preliminary-to-final value drift is handled silently.

---

## Dashboard

Tableau Public dashboard *(link added after Prompt 7)*:

- **Actual vs Forecast** — three-model ensemble with CI bands
- **Variance Drivers** — mechanical decomposition (volume / margin / mix /
  one-time)
- **Forecast Accuracy** — MAE / MAPE across expanding-window CV folds
- **Scenario Toggle** — Base / Bull / Bear from the Excel model

Every data point has a "Source" tooltip showing `accession_no` with a
clickable link to the SEC filing.

---

## LLM Commentary

`src/generate_commentary.py` follows a reasoning-vs-computation split — a
common production pattern for LLMs over numeric data:

1. Python pulls pre-computed variances from DuckDB — Claude never sees raw data
2. Refusal checks: restatement detected → exit non-zero, no API call
3. Python pre-formats every number with its `accession_no`
4. Claude writes narrative with inline citations (`[0001327567-26-000123]`)
5. Parse-then-compare hallucination guard validates every numeric token

The guard catches: number fabrication, unit drift (M↔B), word-form numbers
(`billion`/`million`), parens-negatives, bare numeric tokens, missing citations,
citations to accessions not in the input.

Model selection happens at runtime via `/v1/models` — no hardcoded snapshot IDs.

---

## Eval Harness

Five ground-truth variance scenarios in `tests/eval/fixtures/`. The harness exercises
the **mechanical-driver detection logic and the hallucination-guard plumbing** end to
end — Claude itself is *not* called from CI. Each scenario builds a synthetic
commentary string that exercises the relevant guard rule and asserts the expected
refusal/driver outcome.

| Scenario | Expected outcome |
|---|---|
| VOLUME-driven | Commentary names volume as dominant driver |
| MARGIN-driven | Commentary names margin compression/expansion |
| ONE-TIME | Commentary names one-time item (tagged in fixture) |
| MIX-NOT-COMPUTABLE | Commentary hedges; does not guess |
| RESTATEMENT | Pipeline refuses; exits non-zero; never calls API |

Drivers are restricted to mechanical decompositions computable from input data.
Causal narratives ("Cortex platform momentum") are not tested because rewarding
the model for emitting them contradicts the anti-speculation rules in Prompt 8.

> **Limitation:** the harness validates the deterministic plumbing (refusal logic,
> driver classification, guard rules), not real LLM output. Live model evaluation —
> running the actual narrator and grading its commentary against the fixtures — is
> v2 work. See `docs/MODELING_DECISIONS.md` §7.

---

## NotebookLM

`make notebooklm` assembles a source bundle at `dashboard/notebooklm_bundle/`
including the 10-K PDF, historical financials CSV with provenance, forecast
summary, exec commentary, and test + eval reports.  Upload to NotebookLM and
ask: *"For the $1.2B revenue figure in the commentary, what is the source filing?"*

---

## Tests

```bash
make test      # pytest + coverage (≥60% target)
make eval      # eval harness only
make qa        # lint + typecheck + test + eval
```

Coverage excludes `build_excel_model.py` and `build_notebooklm_bundle.py`
(tested via end-to-end integration, not unit tests).

---

## Limitations

- **Small sample:** ~20 quarterly observations per company — forecast intervals
  are wide by design and honestly disclosed
- **Filing lag:** 10-Q filings arrive ~30–45 days post-quarter-end
- **NGS ARR:** PANW's headline non-GAAP metric is not structured XBRL; out of scope
- **Simplified BS:** Working capital beyond AR/AP/Inventory/DeferredRevenue is
  aggregated into `OtherWC` — full SaaS-grade modeling is v2 work
- **Commentary guard:** Does not catch wrong attribution (revenue described as
  margin) or logical inconsistency across paragraphs

---

## Run for Any Company

```bash
make demo TICKER=CRWD   # pure-SaaS: no Inventory row, no Revenue_Disaggregation sheet
make demo TICKER=SNOW
make demo TICKER=PANW   # hardware-plus-subscription: Inventory live, DIO driver applies
```

Switching tickers changes:
- XBRL concept synonym resolution (CRWD uses `RevenueFromContractWithCustomer...Including...`)
- `has_physical_inventory` flag (TRUE for PANW/FTNT, FALSE for CRWD/SNOW)
- Excel model Inventory row (rendered for PANW, suppressed for CRWD/SNOW)
- Revenue_Disaggregation sheet (rendered for PANW only)

---

## Privacy & Safe-to-Share

Before pushing to a public repo:

1. Set `SEC_USER_AGENT` in `.env` using a GitHub noreply address —
   **not your primary email**
2. Verify: `git log --all -p | grep -E "@(gmail|outlook|yahoo)\.com"` → empty
3. Verify: `grep -rE "sk-[a-zA-Z0-9]" src/ tests/ config/` → empty
4. Only PANW outputs are committed; other tickers' outputs go to gitignored
   `dashboard/scratch/`

---

## Relationship to Anthropic's `financial-services` plugins

Anthropic publishes an official
[financial-services plugin pack](https://github.com/anthropics/financial-services)
with skills like `/3-statement-model`, `/dcf`, `/audit-xls`, and MCP servers for
FactSet, S&P, LSEG, Daloopa, and Morningstar.

This project is built from scratch on free SEC EDGAR + FRED data to demonstrate
the underlying craft — XBRL synonym mapping, BalanceCheck logic, provenance
threading, hallucination prevention — and to remain fully reproducible without
any paid data subscriptions.  See `docs/MODELING_DECISIONS.md` for the full
trade-off discussion.  In production work I would compose the official plugins.
