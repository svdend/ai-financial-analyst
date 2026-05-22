"""Assemble a NotebookLM source bundle from pipeline outputs.

Creates ``dashboard/notebooklm_bundle/`` with 10 files that give
NotebookLM enough context to answer provenance questions like:
*"For the $1.2B revenue figure in the commentary, what is the source filing?"*

Files generated:
    01_company_overview.md       — auto-generated from SEC facts + config
    02_latest_10K.pdf            — most recent 10-K from SEC EDGAR
    03_latest_10Q.pdf            — most recent 10-Q from SEC EDGAR
    04_historical_financials.csv — last 12 quarters from DuckDB (with provenance)
    05_forecast_summary.md       — Prophet + AutoARIMA + Lasso forecasts
    06_excel_model_summary.md    — Base/Bull/Bear revenue/margin/FCF
    07_exec_commentary.md        — copy of most-recent exec commentary
    08_test_report.html          — pytest --cov=src --cov-report=html output
    09_eval_report.md            — make eval output (pass/fail per scenario)
    README_FOR_NOTEBOOKLM.md     — suggested prompts for NotebookLM

CLI::

    python -m src.build_notebooklm_bundle
    python -m src.build_notebooklm_bundle --ticker PANW
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_PATH = _REPO_ROOT / "config" / "company.yaml"
_PROCESSED_DIR = _REPO_ROOT / "data" / "processed"
_MODELS_DIR = _REPO_ROOT / "models"
_DASHBOARD_DIR = _REPO_ROOT / "dashboard"
_BUNDLE_DIR = _DASHBOARD_DIR / "notebooklm_bundle"

# ── File 01: Company overview ─────────────────────────────────────────────────


def _build_company_overview(ticker: str, config: dict[str, Any]) -> str:
    """Generate a markdown company overview from SEC facts and config.

    Args:
        ticker: Company ticker symbol.
        config: Loaded config/company.yaml contents.

    Returns:
        Markdown string for 01_company_overview.md.
    """
    name = config.get("name", ticker)
    cik = config.get("cik", "unknown")
    fy_end = config.get("fiscal_year_end_month", "?")
    etf = config.get("sector_etf", "")
    has_inv = config.get("has_physical_inventory", "unknown")

    month_names = {
        1: "January",
        2: "February",
        3: "March",
        4: "April",
        5: "May",
        6: "June",
        7: "July",
        8: "August",
        9: "September",
        10: "October",
        11: "November",
        12: "December",
    }
    fy_month_name = (
        month_names.get(int(fy_end), str(fy_end)) if str(fy_end).isdigit() else str(fy_end)
    )

    lines = [
        f"# {name} ({ticker}) — Company Overview",
        "",
        "## Basic Facts",
        f"- **Ticker:** {ticker}",
        f"- **SEC CIK:** {cik}",
        f"- **Fiscal year end:** {fy_month_name}",
        f"- **Sector ETF benchmark:** {etf}",
        f"- **Has physical inventory (Strata appliances):** {has_inv}",
        "",
        "## Data Sources",
        "- **Financial data:** SEC EDGAR XBRL structured facts (free, public)",
        "- **Macro features:** FRED (Federal Reserve Economic Data, free API)",
        "",
        "## How to use this bundle in NotebookLM",
        "Upload all files in this folder as sources. Then ask:",
        "- *For any number in the commentary, what is the source filing?*",
        "- *What are the largest variances between Prophet, AutoARIMA, and Lasso?*",
        "- *Summarize the company's revenue trajectory.*",
        "",
        "## Provenance",
        "Every number in 04_historical_financials.csv carries an `accession_no` column",
        "linking to the exact SEC EDGAR filing. The `filing_url` column provides a",
        "direct hyperlink to the source document on SEC.gov.",
        "",
        "## Limitations",
        "- Consolidated revenue only — disaggregated Product vs. Subscription & Support",
        "  forecasting is v2 work",
        "- ~20 quarterly observations per company — forecast intervals are wide by design",
        "- PANW NGS ARR is not structured XBRL and cannot be ingested automatically",
        "- SEC 10-Q filings arrive ~30–45 days post-quarter-end",
    ]
    return "\n".join(lines)


# ── File 04: Historical financials ────────────────────────────────────────────


def _build_historical_financials(ticker: str) -> pd.DataFrame | None:
    """Load last 12 quarters from DuckDB with provenance columns.

    Args:
        ticker: Company ticker symbol.

    Returns:
        DataFrame with 12 quarters × (line_items + accession_no + filing_url),
        or None if the warehouse doesn't exist.
    """
    import duckdb  # noqa: PLC0415

    db_path = _PROCESSED_DIR / f"{ticker}.duckdb"
    if not db_path.exists():
        logger.warning("DuckDB not found: %s — skipping historical financials", db_path)
        return None

    sql = """
        SELECT
            period_end,
            fiscal_year,
            fiscal_period,
            period_type,
            Revenue,
            GrossProfit,
            OperatingIncome,
            NetIncome,
            revenue_fact_id,
            revenue_accession    AS accession_no,
            revenue_filing_url   AS filing_url
        FROM v_income_statement_quarterly
        WHERE period_type = 'Q'
          AND Revenue IS NOT NULL
        ORDER BY fiscal_year DESC, fiscal_period DESC
        LIMIT 12
    """
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(sql).fetchdf()
    finally:
        con.close()
    return df


# ── File 05: Forecast summary ─────────────────────────────────────────────────


def _build_forecast_summary(ticker: str) -> str:
    """Generate forecast summary markdown from parquet files.

    Args:
        ticker: Company ticker symbol.

    Returns:
        Markdown string for 05_forecast_summary.md.
    """
    lines = [
        f"# {ticker} Revenue Forecast Summary",
        "",
        "Three independent revenue forecasting models were run and compared.",
        "With ~20 quarterly observations, no single model is statistically",
        "defensible — the ensemble characterises the *range* of plausible outcomes.",
        "",
    ]

    candidates = [
        (_MODELS_DIR / f"{ticker}_baseline_forecasts.parquet", "Prophet + AutoARIMA"),
        (_MODELS_DIR / f"{ticker}_macro_forecast.parquet", "Lasso (macro-regularized)"),
    ]

    found_any = False
    for path, label in candidates:
        if not path.exists():
            lines.append(f"## {label}\n\n_Parquet not found — run notebooks 02 and 03 first._\n")
            continue
        found_any = True
        try:
            df = pd.read_parquet(path)
            lines.append(f"## {label}")
            lines.append("")
            lines.append(f"Source: `{path.name}`")
            lines.append("")
            # Table of forecasts
            cols = ["model", "period_end", "yhat", "yhat_lower_80", "yhat_upper_80"]
            avail = [c for c in cols if c in df.columns]
            if avail:
                lines.append(df[avail].to_markdown(index=False))
            lines.append("")
        except Exception as exc:
            lines.append(f"## {label}\n\n_Error reading parquet: {exc}_\n")

    if not found_any:
        lines.append(
            "_No forecast parquets found. Run `make model TICKER=<ticker>` "
            "and `make forecast TICKER=<ticker>` first._"
        )

    return "\n".join(lines)


# ── File 06: Excel model summary ──────────────────────────────────────────────


def _build_excel_model_summary(ticker: str) -> str:
    """Generate Excel model summary markdown.

    Args:
        ticker: Company ticker symbol.

    Returns:
        Markdown string for 06_excel_model_summary.md.
    """
    excel_path = _DASHBOARD_DIR / f"{ticker}_model.xlsx"
    lines = [
        f"# {ticker} Excel Model Summary — Base / Bull / Bear",
        "",
    ]
    if not excel_path.exists():
        lines.append(
            f"_Excel model not found at `{excel_path.name}`. "
            "Run `make dashboard TICKER=<ticker>` to generate._"
        )
        return "\n".join(lines)

    lines += [
        f"Source: `{excel_path.name}`",
        "",
        "The model includes three scenarios (Base / Bull / Bear) with:",
        "- Revenue growth tied to historical CAGR ± scenario multiplier",
        "- Gross margin, operating margin, and FCF derived from historical trends",
        "- Working capital drivers: DSO, DPO, DIO (where applicable), deferred revenue",
        "- SBC and share buybacks split correctly per GAAP (not aggregated into capex)",
        "- BalanceCheck = TotalAssets − (TotalLiabilities + TotalEquity) = $0 by construction",
        "",
        "Sheets:",
        "- **Assumptions** — scenario multipliers and key driver inputs",
        "- **Income_Statement** — 12 historical + 16 forecast quarters",
        "- **Balance_Sheet** — full BS with BalanceCheck row",
        "- **Cash_Flow** — OCF / investing / financing with GAAP_OCF_residual check",
        "- **Key_Metrics** — revenue growth %, margins, FCF yield, EV/Revenue",
        "- **Revenue_Disaggregation** — Product vs. Subscription & Support (PANW only)",
        "- **Sources** — per-cell provenance with accession_no and filing_url",
        "",
        "See `dashboard/Tableau_Setup.md` for Tableau dashboard instructions.",
    ]
    return "\n".join(lines)


# ── File 07: Executive commentary ────────────────────────────────────────────


def _find_latest_commentary(ticker: str) -> Path | None:
    """Return the most recent exec commentary file for this ticker.

    Args:
        ticker: Company ticker symbol.

    Returns:
        Path to the most recent commentary .md file, or None.
    """
    pattern = f"{ticker}_exec_commentary_*.md"
    files = sorted(_DASHBOARD_DIR.glob(pattern), reverse=True)
    return files[0] if files else None


# ── File 08: Test report ──────────────────────────────────────────────────────


def _generate_test_report(bundle_dir: Path) -> Path:
    """Run pytest with HTML coverage report; copy to bundle.

    Runs pytest non-interactively and produces HTML coverage.
    Falls back to a plain text report if pytest fails.

    Args:
        bundle_dir: Target directory for the report file.

    Returns:
        Path to the test report file in the bundle.
    """
    html_report_dir = _REPO_ROOT / "htmlcov"
    out_file = bundle_dir / "08_test_report.html"

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "--cov=src",
            "--cov-report=html",
            "--cov-report=term-missing",
            "-q",
            "--tb=short",
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )

    # Try to use the full HTML coverage report
    html_index = html_report_dir / "index.html"
    if html_index.exists():
        content = html_index.read_text(encoding="utf-8", errors="replace")
        out_file.write_text(content, encoding="utf-8")
        logger.info("Test report written from htmlcov/index.html")
    else:
        # Fallback: plain text wrapped in minimal HTML
        plain = result.stdout + "\n" + result.stderr
        out_file.write_text(
            f"<html><body><pre>{plain}</pre></body></html>",
            encoding="utf-8",
        )
        logger.info("Test report written as plain-text fallback")

    return out_file


# ── File 09: Eval report ──────────────────────────────────────────────────────


def _generate_eval_report(bundle_dir: Path) -> Path:
    """Run eval harness and capture output as markdown.

    Args:
        bundle_dir: Target directory for the report file.

    Returns:
        Path to 09_eval_report.md.
    """
    out_file = bundle_dir / "09_eval_report.md"

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/eval/",
            "-v",
            "--no-header",
            "--tb=short",
            "--no-cov",
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )

    lines = [
        "# Eval Harness Report",
        "",
        "## Mechanical Driver Scenarios (5 ground-truth fixtures)",
        "",
        "```",
        result.stdout,
        "```",
        "",
    ]
    if result.returncode != 0:
        lines += ["## Failures", "", f"```\n{result.stderr}\n```", ""]

    out_file.write_text("\n".join(lines), encoding="utf-8")
    return out_file


# ── File 02/03: SEC PDF download ─────────────────────────────────────────────


def _download_sec_filing(cik_int: int, form_type: str, dest: Path) -> bool:
    """Download the most recent 10-K or 10-Q PDF from SEC EDGAR.

    Uses EDGAR's submissions API to find the latest filing, then fetches
    the primary document. Falls back gracefully if the filing is not a PDF
    or the download fails.

    Args:
        cik_int:   Integer CIK (e.g. 1327567 for PANW).
        form_type: SEC form type string, e.g. "10-K" or "10-Q".
        dest:      Target path for the downloaded file.

    Returns:
        True if the file was saved successfully, False otherwise.
    """
    headers = {"User-Agent": "ai-financial-analyst/1.0 (portfolio project)"}
    cik_padded = str(cik_int).zfill(10)

    # Fetch submission history
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    try:
        resp = requests.get(submissions_url, headers=headers, timeout=15)
        resp.raise_for_status()
        subs = resp.json()
    except Exception as exc:
        logger.warning("Could not fetch EDGAR submissions: %s", exc)
        return False

    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accns = recent.get("accessionNumber", [])
    docs_list = recent.get("primaryDocument", [])

    # Find first matching form type
    idx = next((i for i, f in enumerate(forms) if f == form_type), None)
    if idx is None:
        logger.warning("No %s found in recent filings for CIK %s", form_type, cik_padded)
        return False

    accn = accns[idx].replace("-", "")
    primary_doc = docs_list[idx] if idx < len(docs_list) else ""
    base_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn}/{primary_doc}"

    # Only download if likely a PDF
    if not primary_doc.lower().endswith(".pdf"):
        # Create a placeholder .txt file pointing to the filing
        dest = dest.with_suffix(".txt")
        dest.write_text(
            f"Filing {form_type} not in PDF format.\n"
            f"View at: https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={cik_padded}&type={form_type}&dateb=&owner=include&count=5\n"
            f"Primary document: {base_url}\n",
            encoding="utf-8",
        )
        logger.info("Non-PDF %s — placeholder written to %s", form_type, dest)
        return True

    try:
        resp = requests.get(base_url, headers=headers, timeout=30)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        logger.info("Downloaded %s → %s (%d bytes)", form_type, dest.name, len(resp.content))
        return True
    except Exception as exc:
        logger.warning("Could not download %s PDF: %s", form_type, exc)
        return False


# ── README for NotebookLM ─────────────────────────────────────────────────────


def _build_notebooklm_readme(ticker: str) -> str:
    """Build the README_FOR_NOTEBOOKLM.md with suggested prompts.

    Args:
        ticker: Company ticker symbol.

    Returns:
        Markdown string for README_FOR_NOTEBOOKLM.md.
    """
    return f"""# How to Use This Bundle in NotebookLM

## Setup
1. Go to https://notebooklm.google.com
2. Create a new notebook called "{ticker} AI Financial Analyst"
3. Upload all files in this folder as sources (excluding this README)
4. Wait for NotebookLM to process (typically 1–2 minutes)

## Suggested Prompts

### Provenance queries (the key demo questions)
- *For the $X.XB revenue figure in the commentary, what is the source filing?*
  → NotebookLM should cite the accession_no from 07_exec_commentary.md and
  trace it to the row in 04_historical_financials.csv with a matching accession_no.
- *What SEC filing did the Q1 FY2026 revenue figure come from?*

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
| 07_exec_commentary.md | LLM-generated variance commentary with citations |
| 08_test_report.html | pytest coverage report |
| 09_eval_report.md | Eval harness pass/fail (5 ground-truth scenarios) |

## Architecture note
This pipeline follows the reasoning-vs-computation pattern:
all arithmetic happens in Python/SQL, Claude generates narrative only. Every
number in the commentary traces to an SEC accession number.
"""


# ── Main entry point ──────────────────────────────────────────────────────────


def build(ticker: str | None = None) -> dict[str, Path]:
    """Assemble the NotebookLM source bundle.

    Creates or updates ``dashboard/notebooklm_bundle/`` with up to 10 files.
    Missing upstream outputs (Excel, parquets, commentary) produce placeholder
    files so the bundle is always complete.

    Args:
        ticker: Ticker symbol override; falls back to config/company.yaml.

    Returns:
        Dict mapping file label → Path for every file written.

    Raises:
        FileNotFoundError: If config/company.yaml is missing.
    """
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config not found: {_CONFIG_PATH}")
    with _CONFIG_PATH.open() as fh:
        config: dict[str, Any] = yaml.safe_load(fh)

    resolved_ticker = (ticker or str(config["ticker"])).upper().strip()
    cik_int = int(str(config.get("cik_int", config.get("cik", 0))).lstrip("0") or "0")

    _BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    # 01 — Company overview
    path = _BUNDLE_DIR / "01_company_overview.md"
    path.write_text(_build_company_overview(resolved_ticker, config), encoding="utf-8")
    written["01_company_overview"] = path
    logger.info("Written: %s", path.name)

    # 02 — Latest 10-K
    path_10k = _BUNDLE_DIR / "02_latest_10K.pdf"
    if not _download_sec_filing(cik_int, "10-K", path_10k):
        # Try .txt fallback
        path_10k = path_10k.with_suffix(".txt") if not path_10k.exists() else path_10k
        if not path_10k.exists():
            path_10k.with_suffix(".txt").write_text(
                f"10-K download failed for CIK {cik_int}. "
                "View at: https://www.sec.gov/cgi-bin/browse-edgar"
                f"?action=getcompany&CIK={cik_int:010d}&type=10-K",
                encoding="utf-8",
            )
            path_10k = path_10k.with_suffix(".txt")
    written["02_latest_10K"] = path_10k
    logger.info("Written: %s", path_10k.name)

    # 03 — Latest 10-Q
    path_10q = _BUNDLE_DIR / "03_latest_10Q.pdf"
    if not _download_sec_filing(cik_int, "10-Q", path_10q):
        path_10q = path_10q.with_suffix(".txt") if not path_10q.exists() else path_10q
        if not path_10q.exists():
            path_10q.with_suffix(".txt").write_text(
                f"10-Q download failed for CIK {cik_int}. "
                "View at: https://www.sec.gov/cgi-bin/browse-edgar"
                f"?action=getcompany&CIK={cik_int:010d}&type=10-Q",
                encoding="utf-8",
            )
            path_10q = path_10q.with_suffix(".txt")
    written["03_latest_10Q"] = path_10q
    logger.info("Written: %s", path_10q.name)

    # 04 — Historical financials
    df_hist = _build_historical_financials(resolved_ticker)
    path = _BUNDLE_DIR / "04_historical_financials.csv"
    if df_hist is not None:
        df_hist.to_csv(path, index=False)
    else:
        path.write_text(
            "period_end,fiscal_year,fiscal_period,Revenue,accession_no,filing_url\n"
            "(run make warehouse TICKER=<ticker> to populate)\n",
            encoding="utf-8",
        )
    written["04_historical_financials"] = path
    logger.info("Written: %s (%d rows)", path.name, 0 if df_hist is None else len(df_hist))

    # 05 — Forecast summary
    path = _BUNDLE_DIR / "05_forecast_summary.md"
    path.write_text(_build_forecast_summary(resolved_ticker), encoding="utf-8")
    written["05_forecast_summary"] = path
    logger.info("Written: %s", path.name)

    # 06 — Excel model summary
    path = _BUNDLE_DIR / "06_excel_model_summary.md"
    path.write_text(_build_excel_model_summary(resolved_ticker), encoding="utf-8")
    written["06_excel_model_summary"] = path
    logger.info("Written: %s", path.name)

    # 07 — Exec commentary (most recent)
    commentary_path = _find_latest_commentary(resolved_ticker)
    dest = _BUNDLE_DIR / "07_exec_commentary.md"
    if commentary_path and commentary_path.exists():
        dest.write_text(commentary_path.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info("Written: %s (from %s)", dest.name, commentary_path.name)
    else:
        dest.write_text(
            f"# {resolved_ticker} Executive Commentary\n\n"
            "_Commentary not yet generated. Run `make commentary TICKER=<ticker> LIVE=1`._\n",
            encoding="utf-8",
        )
        logger.info("Written: %s (placeholder — no commentary found)", dest.name)
    written["07_exec_commentary"] = dest

    # 08 — Test report (run pytest, capture HTML coverage)
    logger.info("Running pytest for test report...")
    test_report_path = _generate_test_report(_BUNDLE_DIR)
    written["08_test_report"] = test_report_path
    logger.info("Written: %s", test_report_path.name)

    # 09 — Eval report
    logger.info("Running eval harness for eval report...")
    eval_report_path = _generate_eval_report(_BUNDLE_DIR)
    written["09_eval_report"] = eval_report_path
    logger.info("Written: %s", eval_report_path.name)

    # README
    readme_path = _BUNDLE_DIR / "README_FOR_NOTEBOOKLM.md"
    readme_path.write_text(_build_notebooklm_readme(resolved_ticker), encoding="utf-8")
    written["README_FOR_NOTEBOOKLM"] = readme_path
    logger.info("Written: %s", readme_path.name)

    return written


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="Assemble NotebookLM source bundle.",
    )
    parser.add_argument("--ticker", default=None, help="Ticker symbol (e.g. PANW)")
    args = parser.parse_args()

    try:
        files = build(ticker=args.ticker)
        print(f"\nBundle written to: {_BUNDLE_DIR}")
        print(f"Files: {len(files)}")
        for _label, p in sorted(files.items()):
            size = p.stat().st_size if p.exists() else 0
            print(f"  {p.name:<40}  {size:>8,} bytes")
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
