# How to Use This Bundle in NotebookLM

## Setup
1. Go to https://notebooklm.google.com
2. Create a new notebook called "PANW AI Financial Analyst"
3. Upload all files in this folder as sources (excluding this README)
4. Wait for NotebookLM to process (typically 1–2 minutes)

## Suggested Prompts

### Provenance queries (live commentary required)
_The bundled commentary file (`07_exec_commentary.md`) is currently
an illustrative SAMPLE — the in-file banner flags it explicitly. Its
accession numbers are NOT guaranteed to appear in
`04_historical_financials.csv`. To run the provenance demo, set
`ANTHROPIC_API_KEY` and re-run `make demo` (or `make commentary TICKER=PANW LIVE=1` followed by `make notebooklm`)._

### Financial analysis
- *Summarize the company's revenue trajectory over the last 3 years.*
- *What are the largest variances between Prophet, AutoARIMA, and Lasso forecasts?*
- *What are the top 3 risks in the latest 10-K that could affect next-quarter revenue?*
- *Three CFO talking points for the upcoming earnings call.*
- *One-page board memo on capital allocation under Base/Bull/Bear scenarios.*

### Methodology
- *How does the hallucination guard work in generate_commentary.py?*
- *Why does the eval harness use only mechanical drivers instead of causal narratives?*
- *What does the GAAP_OCF_residual check validate?*

## File Guide
| File | Description |
|------|-------------|
| 01_company_overview.md | Company facts + data sources |
| 02_latest_10K.pdf / .txt | Most recent annual report |
| 03_latest_10Q.pdf / .txt | Most recent quarterly report |
| 04_historical_financials.csv | Last 12 quarters with accession_no + filing_url |
| 05_forecast_summary.md | Prophet + AutoARIMA + Lasso outputs with CIs |
| 06_excel_model_summary.md | Base/Bull/Bear scenario description |
| 07_exec_commentary.md | LLM-generated variance commentary with citations (illustrative sample) |
| 08_test_report.html | pytest coverage report |
| 09_eval_report.md | Eval harness pass/fail (5 ground-truth scenarios) |

## Architecture note
This pipeline follows the reasoning-vs-computation pattern:
all arithmetic happens in Python/SQL, Claude generates narrative only. Every
number in the commentary traces to an SEC accession number.
