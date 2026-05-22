# Modeling Decisions

This document explains the key design decisions in the AI Financial Analyst pipeline
and the trade-offs considered. It is written for a technical interviewer who might ask
"why did you do X instead of Y?"

---

## 1. Why the reasoning-vs-computation split?

**Decision:** All arithmetic happens in deterministic Python/SQL. Claude generates
narrative only. Every number cited in the commentary must appear verbatim in the
input JSON.

**Rationale:** A common pattern in production financial LLM applications — sometimes
called the "reasoning vs. computation split" — is to do all arithmetic in deterministic
code and let the model write narrative only. The motivation: in finance, the model
can't be the whole system. LLMs are unreliable calculators — they hallucinate numeric
outputs, particularly with large dollar amounts, percentage conversions, and YoY
arithmetic. Pre-computing all variances in Python and feeding formatted strings (e.g.
`"$1.2B"`, `"3.2%"`) to Claude eliminates the arithmetic failure mode entirely, leaving
only the citation and attribution failure modes, which the parse-then-compare guard
addresses.

**What the guard catches:** number fabrication, unit drift (M↔B), forbidden word-forms
(`billion`/`bps`), parens-negative notation, bare numeric tokens without `$`/`%`
prefixes, missing citations, and citations to accession numbers not in the input.

**What the guard does NOT catch:** arithmetic errors (mitigated by the rule that Claude
is forbidden from doing math), wrong attribution (revenue described as margin), and
logical inconsistency across paragraphs. These require richer structured output
evaluation and are v2 work.

---

## 2. Why regularized linear models instead of XGBoost?

**Decision:** Use LassoCV (L1 regularization) for the macro-feature forecast. XGBoost
was explicitly considered and excluded.

**Rationale:**
- With ~20 quarterly observations, XGBoost will overfit severely. The model has more
  parameters than data points.
- LassoCV enforces sparsity — it automatically sets irrelevant macro coefficients to
  zero, producing a parsimonious model whose feature selection can be interpreted and
  explained to a CFO.
- Ridge/Lasso have analytical solutions and are numerically stable at n=20. Tree models
  require cross-validation to avoid overfitting, but with n=20 any CV estimate has high
  variance.
- The regularization parameter is selected via time-series cross-validation (expanding
  window), not random k-fold, to prevent look-ahead bias.

---

## 3. Why three independent models instead of one ensemble?

**Decision:** Run Prophet, AutoARIMA, and LassoCV independently; do NOT produce a
single "best" model.

**Rationale:** With ~20 observations, no single model is statistically defensible.
Forcing a comparison of three independent forecasts:
1. Honestly communicates the epistemic uncertainty to the CFO
2. Makes divergent forecasts visible (a $400M spread in the 80% CI is real information)
3. Avoids the false precision of a single-model point estimate

A production system with more data might justify a stacked ensemble, but for a
20-quarter training set the honest answer is "we have three models and they disagree."

---

## 4. Why not BSTS (tfp.sts) or hierarchical Bayesian peer-borrowing?

**Considered but not implemented.**

Bayesian Structural Time Series (BSTS) via TensorFlow Probability `tfp.sts` was
evaluated. It offers richer uncertainty quantification and principled handling of
structural breaks (e.g., PANW's FY21 platformization shift). However:
- The Stan backend required for Prophet already produces proper Bayesian credible
  intervals at low computational cost
- With n=20, the prior dominates the posterior in any Bayesian model — the choice of
  prior matters more than the likelihood. Calibrating BSTS priors for a 20-quarter
  revenue series is as much an art as a science.
- BSTS would be the right choice if we had 5–10 years of monthly data.

**Hierarchical peer-borrowing** across enterprise-security comps (PANW, FTNT, CRWD,
ZS, S, SNOW, CHKP) was also considered. It would:
- Pool strength across companies with correlated revenue cycles
- Benefit hardware-carriers (PANW, FTNT, CHKP) more than pure-SaaS (CRWD, SNOW) —
  there are fewer hardware-carriers and their cycles are more correlated
- Require careful handling of the hardware/SaaS shape discontinuity

This is legitimate v2 work. For a portfolio project on free public data, regularized
linear models suffice to demonstrate the pipeline and the honest-uncertainty principle.

---

## 5. Why the OtherWC simplification on the balance sheet?

**Decision:** Working capital items beyond AR, AP, Inventory, and DeferredRevenue are
aggregated into `OtherWC`.

**Rationale:** Full SaaS-grade BS modeling (unbundling deferred costs, contract assets,
operating lease ROU assets, etc.) requires parsing MD&A footnotes, not just structured
XBRL — it is out of scope for a free-data portfolio project. The simplification is
explicitly documented in the Sources sheet and in the Limitations section of the README.

The GAAP_OCF_residual check validates that the simplified model doesn't materially
diverge from reported GAAP OCF (threshold: $5M), catching the worst-case simplification
errors.

**Why $5M and not $1M or $10M?** A mid-cap enterprise tech company runs a few billion
dollars of revenue per quarter, so $5M sits at roughly 0.1–0.25% of quarterly top-line —
small enough to catch a structural OtherWC omission, loose enough to absorb rounding
and the immaterial line items the simplification deliberately ignores. This is a
portfolio-project policy choice, not a universal rule. The right threshold depends on
the use case: audit-grade reconciliation needs much tighter (≤$100K), while a
directional-forecast tool can tolerate ~1% of revenue. The constant lives at
`_OCF_RESIDUAL_TOL` in `src/build_excel_model.py` (line 68) and is meant to be tuned per
deployment.

---

## 6. Why Python-side BalanceCheck instead of openpyxl data_only?

**Decision:** `build_excel_model.py` verifies BalanceCheck by re-computing it in Python
against the same source data arrays, not by reading back the Excel file.

**Rationale:** `openpyxl` with `data_only=True` reads *cached* formula results from the
last Excel/LibreOffice session. On a freshly-generated workbook (which has never been
opened in Excel), the formula cache is empty and `cell.value` returns `None`. Any
assertion based on `data_only=True` would silently pass on a corrupt workbook. This
was a real bug in v6 that made all 48 BalanceCheck assertions vacuously true.

v7 duplicates the arithmetic in Python (Cash = BegCash + OCF + InvestingCF +
FinancingCF; BalanceCheck = TotalAssets − (TotalLiabilities + TotalEquity)) and asserts
directly. The `|BalanceCheck| < $1M` threshold catches floating-point rounding, not
logic errors (the computation is designed to balance exactly).

Optional LibreOffice headless recalc (`libreoffice --headless --calc --convert-to xlsx`)
is available as a QA step for defense-in-depth.

---

## 7. Why the eval harness uses only mechanical drivers?

**Decision:** The five ground-truth eval scenarios test only four mechanical
decompositions — pure-volume, pure-margin, pure-one-time, and mix-not-computable —
not causal narratives like "Cortex platform momentum."

**Rationale:** Causal narratives ("deal-cycle elongation," "platformization tailwind")
require knowledge about events, products, and customers — knowledge that is explicitly
forbidden by Prompt 8's anti-speculation rules. If the eval harness rewarded the model
for emitting causal narratives, it would directly reward the failure mode the system
is designed to prevent.

Testing only mechanical drivers that are computable from the input data means:
1. Every test is deterministic (the expected output is derived from the input math)
2. A model that passes can't have succeeded by hallucinating business context
3. The gap between "passed eval" and "human-quality analysis" is honestly disclosed

Production-grade causal eval would require sourced inputs (e.g. press-release text
grounding the "platformization" claim) — that is v2 work.

---

## 8. Why restatement detection was rewritten (v7 vs. v6)?

**v6 bug:** Flagged any `(line_item, period_end)` pair with more than one value as a
restatement. This produced false positives on routine **10-Q → 10-K
preliminary-to-final flow**: PANW files a 10-Q with a preliminary revenue figure, then
files a 10-K three months later with the audited final figure. Both are correct — the
10-K supersedes the 10-Q. v6 flagged this as a restatement.

**v7 fix:** Forms are ranked by priority: `10-K/A > 10-K > 10-Q/A > 10-Q`. The latest,
highest-priority value is used as the canonical fact. A **true restatement** is defined
as: a same-form-type later filing (e.g. a 10-K/A following a 10-K) that materially
differs from the prior filing (absolute delta > $10K OR relative delta > 0.1%).

This means `has_restatement=TRUE` is meaningful: it identifies genuine amendments, not
routine quarterly-to-annual finalization.

---

## 9. Why runtime model selection instead of hardcoded model IDs?

**Decision:** `src/select_models.py` calls Anthropic's `/v1/models` endpoint at runtime
and picks the highest-generation opus and sonnet snapshots available to the current API
key. No model IDs are hardcoded.

**Rationale:** Hardcoded snapshot IDs (e.g. `claude-opus-4-1-20250805`) become stale as
Anthropic ships new model releases. A portfolio project committing to a specific model
ID in version control would fail for any recruiter who runs it after Anthropic deprecates
that snapshot. Runtime discovery ensures the pipeline always uses the best available
model without requiring code changes. The policy file (`config/model_selection.yaml`)
encodes the selection *rules* (prefer opus for planning, sonnet for narration), not IDs.

---

## 10. Why built from scratch instead of `anthropics/financial-services` plugins?

Anthropic publishes an official
[`financial-services` plugin pack](https://github.com/anthropics/financial-services)
with skills like `/3-statement-model`, `/dcf`, `/audit-xls`, and MCP servers for
FactSet, S&P, LSEG, Daloopa, and Morningstar. These tools cover similar ground at
higher polish.

**This project was built from scratch for two reasons:**

1. **Free public data only.** The partner MCP servers (FactSet, S&P, LSEG, etc.) require
   paid subscriptions. Using only SEC EDGAR and FRED keeps the project fully
   reproducible for any recruiter without a data subscription.

2. **Portfolio depth.** Building from scratch demonstrates the underlying craft that
   the official plugins abstract away: XBRL synonym mapping, BalanceCheck verification,
   provenance threading from ingestion through to commentary citations, and the specific
   failure modes (restatement false positives, openpyxl data_only bug) that arise in
   production financial data pipelines.

For production work, the official plugin pack and partner MCP servers would be the
right starting point. The XBRL ingestion, provenance architecture, and hallucination
guard in this project are complementary to, not in competition with, that toolchain.

---

## 11. PANW-specific modeling notes

- **Segment vs. revenue category:** PANW reports as **one operating and reportable
  segment** under ASC 280, with revenue disaggregated into two categories: **Product**
  (next-gen firewall appliances) and **Subscription and Support**. Strata, Prisma, and
  Cortex are *commercial product families* spanning both categories — calling them
  "segments" is a 30-second-rejection error in a financial analyst interview.

- **Physical inventory:** PANW carries `InventoryNet` on the balance sheet (Strata
  appliances). The `has_physical_inventory` flag in `v_data_quality` is TRUE for
  PANW/FTNT and FALSE for CRWD/SNOW. This drives the Inventory row in the Excel model
  and the DIO working-capital driver.

- **NGS ARR:** PANW's headline non-GAAP metric is not structured XBRL. It appears in
  press-release text and 10-Q narrative but is not tagged. This pipeline forecasts
  consolidated GAAP revenue only and explicitly discloses the gap.

- **Platformization:** CEO Nikesh Arora's multi-year bundling strategy creates unusual
  deferred-revenue swings that a DSO/DPO model can't fully capture. This is
  acknowledged in the limitations section but not separately modeled (v2 work).

- **Fiscal year:** PANW's fiscal year ends in July (month 7). The company resolver
  handles this correctly via the fallback lookup table.

- **Disaggregated forecasting:** Forecasting Product and Subscription & Support
  separately would require structured XBRL for each category (available in 10-K filings
  but not consistently tagged in 10-Qs). Consolidated-only forecasting is v7 scope;
  disaggregated forecasting is v2 work.
