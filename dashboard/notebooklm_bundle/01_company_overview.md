# Palo Alto Networks Inc (PANW) — Company Overview

## Basic Facts
- **Ticker:** PANW
- **SEC CIK:** 0001327567
- **Fiscal year end:** July
- **Sector ETF benchmark:** XLK
- **Has physical inventory (Strata appliances):** True

## Data Sources
- **Financial data:** SEC EDGAR XBRL structured facts (free, public)
- **Macro features:** FRED (Federal Reserve Economic Data, free API)

## How to use this bundle in NotebookLM
Upload all files in this folder as sources. Then ask:
- *For any number in the commentary, what is the source filing?*
- *What are the largest variances between Prophet, AutoARIMA, and Lasso?*
- *Summarize the company's revenue trajectory.*

## Provenance
Every number in 04_historical_financials.csv carries an `accession_no` column
linking to the exact SEC EDGAR filing. The `filing_url` column provides a
direct hyperlink to the source document on SEC.gov.

## Limitations
- Consolidated revenue only — disaggregated Product vs. Subscription & Support
  forecasting is v2 work
- ~20 quarterly observations per company — forecast intervals are wide by design
- PANW NGS ARR is not structured XBRL and cannot be ingested automatically
- SEC 10-Q filings arrive ~30–45 days post-quarter-end
