"""Assemble a NotebookLM source bundle from pipeline outputs.

Creates ``dashboard/notebooklm_bundle/`` with 11 files that give
NotebookLM enough context to answer provenance questions like:
*"For the $1.2B revenue figure in the commentary, what is the source filing?"*

Files generated:
    01_company_overview.md         — auto-generated from SEC facts + config
    02_latest_10K.pdf              — most recent 10-K from SEC EDGAR
    03_latest_10Q.pdf              — most recent 10-Q from SEC EDGAR
    03b_latest_earnings_8K.pdf     — most recent 8-K Item 2.02 (earnings
                                     release; management's prepared remarks
                                     — Q&A is paywalled and not bundled)
    04_historical_financials.csv   — last 12 quarters from DuckDB (with provenance)
    05_forecast_summary.md         — Prophet + AutoARIMA + Lasso forecasts
    06_excel_model_summary.md      — Base/Bull/Bear revenue/margin/FCF
    07_exec_commentary.md          — copy of most-recent exec commentary
    08_test_report.html            — pytest --cov=src --cov-report=html output
    09_eval_report.md              — make eval output (pass/fail per scenario)
    README_FOR_NOTEBOOKLM.md       — suggested prompts for NotebookLM

CLI::

    python -m src.build_notebooklm_bundle
    python -m src.build_notebooklm_bundle --ticker PANW
"""

from __future__ import annotations

import logging
import os
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


def _build_historical_financials(ticker: str, fy_end_month: int = 12) -> pd.DataFrame | None:
    """Last 12 quarters with provenance, deduped by the canonical export logic.

    Reuses ``src.export_for_tableau._export_fact_financials`` so the bundle
    inherits the same invariants the Tableau export enforces:

      1. Multi-fiscal-year comparatives are collapsed to one row per
         (ticker, line_item, period_end) — newer 10-Q's restated comparative
         wins on form priority + filed_date.
      2. YTD-vs-standalone XBRL duplicates are resolved by keeping the
         standalone quarterly row.
      3. ``fiscal_year``/``fiscal_period`` are recomputed from ``period_end``
         + ``fy_end_month`` so comparative rows don't inherit the newer
         filing's labels.

    The long-format dataframe from the canonical exporter is pivoted into the
    wide shape this bundle file has historically used: one row per period_end,
    with Revenue/GrossProfit/OperatingIncome/NetIncome columns and the
    Revenue row's accession_no + filing_url as provenance.

    Args:
        ticker:        Ticker symbol.
        fy_end_month:  Fiscal-year-end month from ``config/company.yaml``;
                       used to relabel comparative rows correctly.

    Returns:
        Wide DataFrame of the last 12 quarters, or None if no warehouse exists.
    """
    import duckdb  # noqa: PLC0415

    from src.export_for_tableau import _export_fact_financials  # noqa: PLC0415

    db_path = _PROCESSED_DIR / f"{ticker}.duckdb"
    if not db_path.exists():
        logger.warning("DuckDB not found: %s — skipping historical financials", db_path)
        return None

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        long_df = _export_fact_financials(con, fy_end_month=fy_end_month)
    finally:
        con.close()

    if len(long_df) == 0:
        return long_df

    # Pivot to wide: one row per period_end, columns per line_item value.
    # Keep only the income-statement line items the bundle's CSV has always
    # exposed; downstream consumers can read other metrics from the Tableau
    # CSVs if they need them.
    wanted = ["Revenue", "GrossProfit", "OperatingIncome", "NetIncome"]
    is_only = long_df[long_df["line_item"].isin(wanted)].copy()

    pivot_keys = ["ticker", "period_end", "fiscal_year", "fiscal_period", "period_type"]
    values = (
        is_only.pivot_table(
            index=pivot_keys,
            columns="line_item",
            values="value",
            aggfunc="first",
        )
        .reset_index()
        .rename_axis(None, axis=1)
    )

    # Carry the Revenue row's provenance triple as the canonical accession for
    # the period — the bundle's previous schema used the Revenue accession too.
    # ``concept_used`` is carried so a *derived* Q4 row (value computed as
    # FY − Q1 − Q2 − Q3, sourced from the 10-K's annual total) is flagged as
    # such: its accession points at the 10-K, but the 10-K reports only the
    # annual figure, so the provenance audit must not treat the quarterly value
    # as a verbatim line item in that filing.
    revenue_provenance = (
        is_only[is_only["line_item"] == "Revenue"][
            ["period_end", "fact_id", "accession_no", "filing_url", "concept_used"]
        ]
        .drop_duplicates(subset=["period_end"])
        .rename(columns={"fact_id": "revenue_fact_id"})
    )
    wide = values.merge(revenue_provenance, on="period_end", how="left")
    # Surface a compact, human-readable provenance flag rather than the raw
    # concept string: "derived" when the Revenue value was computed, else
    # "reported". Keeps the CSV self-describing for the NotebookLM audit.
    wide["revenue_provenance"] = (
        wide["concept_used"]
        .fillna("")
        .str.contains("derived", case=False)
        .map({True: "derived", False: "reported"})
    )
    wide = wide.drop(columns=["concept_used"])

    # Keep the most-recent 12 quarters (the bundle's prior LIMIT 12 contract).
    wide = wide.sort_values(["fiscal_year", "fiscal_period"], ascending=False).head(12)

    # Stable column order: identifiers, line items, provenance.
    ordered_cols = [
        "period_end",
        "fiscal_year",
        "fiscal_period",
        "period_type",
        *wanted,
        "revenue_fact_id",
        "accession_no",
        "filing_url",
        "revenue_provenance",
    ]
    return wide[[c for c in ordered_cols if c in wide.columns]].reset_index(drop=True)


# ── File 05: Forecast summary ─────────────────────────────────────────────────


def _df_to_markdown_table(df: pd.DataFrame) -> str:
    """Render a DataFrame as a GitHub-flavored markdown table.

    Why: pandas' df.to_markdown() requires the optional ``tabulate`` package,
    which we do not declare as a runtime dependency — without it, pandas emits
    a string "Missing optional dependency 'tabulate'" instead of the table.
    A NotebookLM source file with that error string is worse than no table.
    """
    headers = list(df.columns)
    header_row = "| " + " | ".join(str(h) for h in headers) + " |"
    sep_row = "| " + " | ".join("---" for _ in headers) + " |"
    body_rows = [
        "| " + " | ".join(_format_cell(v) for v in row) + " |"
        for row in df.itertuples(index=False, name=None)
    ]
    return "\n".join([header_row, sep_row, *body_rows])


def _format_cell(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


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
                lines.append(_df_to_markdown_table(df[avail]))
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
    excel_path = _DASHBOARD_DIR / f"{ticker}_3Statement_Model.xlsx"
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


def _is_sample_commentary(path: Path) -> bool:
    """True when the commentary file is the illustrative SAMPLE, not a live run."""
    return "_SAMPLE" in path.name


_SAMPLE_FALLBACK_LOG_MSG = (
    "ANTHROPIC_API_KEY not set — bundling SAMPLE commentary. "
    "To regenerate live, set the key and re-run `make demo` "
    "(or `make commentary LIVE=1`)."
)


def _maybe_regenerate_live_commentary(ticker: str) -> Path | None:
    """If ``ANTHROPIC_API_KEY`` is set, regenerate a live commentary; else fall back to SAMPLE.

    The bundle is the canonical NotebookLM hand-off. When the key is present
    we re-run the full ``generate_commentary`` pipeline so 07_exec_commentary.md
    is tied to real EDGAR data for the current period. When the key is absent
    (e.g. on a Claude-subscription-only machine) we skip live regen and let
    ``_find_latest_commentary`` return the committed SAMPLE file.

    Either branch keeps ``make demo`` exiting 0 — the fallback is intentional.

    Args:
        ticker: Ticker symbol whose commentary we want refreshed.

    Returns:
        Path to the freshly-generated live commentary, or ``None`` if we fell
        back to the SAMPLE / live regen failed gracefully.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.info(_SAMPLE_FALLBACK_LOG_MSG)
        return None

    # Lazy import: we only want the anthropic SDK loaded on the live branch so
    # the fallback path stays usable on machines that don't ship the SDK.
    from src import generate_commentary as _gc  # noqa: PLC0415

    try:
        out_path = _gc.generate(ticker=ticker, dry_run=False)
    except Exception as exc:  # noqa: BLE001 — broad catch is the entire point: never fail the bundle
        logger.warning(
            "Live commentary regeneration failed (%s: %s); falling back to SAMPLE.",
            type(exc).__name__,
            exc,
        )
        return None

    if out_path is None:  # pragma: no cover — generate() returns None only in dry-run
        logger.warning("generate() returned None in live mode; falling back to SAMPLE.")
        return None

    logger.info("Live commentary regenerated: %s", out_path)
    return out_path


_SAMPLE_BANNER = (
    "> ⚠️ **SAMPLE — illustrative only.** This file shows the citation format\n"
    "> and narrative structure for executive commentary. Accession numbers in\n"
    "> brackets are illustrative and may NOT appear in `04_historical_financials.csv`.\n"
    "> To generate a live commentary tied to real EDGAR data for the current\n"
    "> period, run `make commentary TICKER=<ticker> LIVE=1` and re-run\n"
    "> `make notebooklm`. Do not use this file for provenance demos.\n\n"
)


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
        sibling_pdf = dest
        dest = dest.with_suffix(".txt")
        dest.write_text(
            f"Filing {form_type} not in PDF format.\n"
            f"View at: https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={cik_padded}&type={form_type}&dateb=&owner=include&count=5\n"
            f"Primary document: {base_url}\n",
            encoding="utf-8",
        )
        logger.info("Non-PDF %s — placeholder written to %s", form_type, dest)
        # Remove any stale .pdf left from a prior run; NotebookLM would otherwise
        # ingest both the placeholder and the real (now-superseded) PDF.
        if sibling_pdf.exists() and sibling_pdf.suffix == ".pdf":
            sibling_pdf.unlink()
            logger.info("Removed stale PDF: %s", sibling_pdf.name)
        return True

    try:
        resp = requests.get(base_url, headers=headers, timeout=30)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        logger.info("Downloaded %s → %s (%d bytes)", form_type, dest.name, len(resp.content))
        # Remove any stale .txt placeholder from a prior non-PDF run; NotebookLM
        # would otherwise ingest both and cite the placeholder alongside the real PDF.
        sibling_txt = dest.with_suffix(".txt")
        if sibling_txt.exists():
            sibling_txt.unlink()
            logger.info("Removed stale placeholder: %s", sibling_txt.name)
        return True
    except Exception as exc:
        logger.warning("Could not download %s PDF: %s", form_type, exc)
        return False


def _download_earnings_8k(cik_int: int, dest: Path) -> bool:
    """Download the most recent 8-K Item 2.02 (earnings release) from EDGAR.

    SEC Form 8-K Item 2.02 is "Results of Operations and Financial Condition"
    — the earnings release that ships with each quarterly results announcement.
    It contains management's prepared remarks; it does NOT include the
    earnings call Q&A (those transcripts are paywalled and not on EDGAR).

    Mirrors :func:`_download_sec_filing` exactly:
      - same User-Agent + EDGAR submissions API call
      - same .pdf-or-.txt fallback when the primary doc is not a PDF
      - same stale-sibling cleanup on success

    The picker filters on ``form == "8-K"`` AND ``items`` contains ``"2.02"``,
    then takes the most recent (lowest index in the recent-filings arrays —
    EDGAR returns them in reverse-chronological order).

    Args:
        cik_int: Integer CIK (e.g. 1327567 for PANW).
        dest:    Target path for the downloaded file (typically
            ``03b_latest_earnings_8K.pdf``).

    Returns:
        True if a file (PDF or .txt placeholder) was saved successfully,
        False if no matching 8-K was found or the download failed.
    """
    headers = {"User-Agent": "ai-financial-analyst/1.0 (portfolio project)"}
    cik_padded = str(cik_int).zfill(10)

    submissions_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    try:
        resp = requests.get(submissions_url, headers=headers, timeout=15)
        resp.raise_for_status()
        subs = resp.json()
    except Exception as exc:
        logger.warning("Could not fetch EDGAR submissions for 8-K Item 2.02: %s", exc)
        return False

    recent = subs.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accns = recent.get("accessionNumber", [])
    docs_list = recent.get("primaryDocument", [])
    items_list = recent.get("items", [])

    # EDGAR returns recent filings in reverse-chronological order, so the
    # first index that matches is the newest. Item 2.02 may appear alone
    # ("2.02") or alongside exhibits ("2.02,9.01") — split-and-membership
    # check rather than substring to avoid false positives like "12.02".
    idx = next(
        (
            i
            for i, form in enumerate(forms)
            if form == "8-K"
            and i < len(items_list)
            and "2.02" in {part.strip() for part in str(items_list[i]).split(",")}
        ),
        None,
    )
    if idx is None:
        logger.warning(
            "No 8-K with Item 2.02 (earnings release) found in recent filings for CIK %s",
            cik_padded,
        )
        return False

    accn = accns[idx].replace("-", "")
    primary_doc = docs_list[idx] if idx < len(docs_list) else ""
    base_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn}/{primary_doc}"

    if not primary_doc.lower().endswith(".pdf"):
        sibling_pdf = dest
        dest = dest.with_suffix(".txt")
        dest.write_text(
            f"Earnings 8-K (Item 2.02) not in PDF format.\n"
            f"View at: https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={cik_padded}&type=8-K&dateb=&owner=include&count=10\n"
            f"Primary document: {base_url}\n",
            encoding="utf-8",
        )
        logger.info("Non-PDF earnings 8-K — placeholder written to %s", dest)
        if sibling_pdf.exists() and sibling_pdf.suffix == ".pdf":
            sibling_pdf.unlink()
            logger.info("Removed stale PDF: %s", sibling_pdf.name)
        return True

    try:
        resp = requests.get(base_url, headers=headers, timeout=30)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        logger.info("Downloaded earnings 8-K → %s (%d bytes)", dest.name, len(resp.content))
        sibling_txt = dest.with_suffix(".txt")
        if sibling_txt.exists():
            sibling_txt.unlink()
            logger.info("Removed stale placeholder: %s", sibling_txt.name)
        return True
    except Exception as exc:
        logger.warning("Could not download earnings 8-K PDF: %s", exc)
        return False


# ── README for NotebookLM ─────────────────────────────────────────────────────


def _build_notebooklm_readme(ticker: str, is_sample: bool = False) -> str:
    """Build the README_FOR_NOTEBOOKLM.md with suggested prompts.

    Args:
        ticker: Company ticker symbol.
        is_sample: True when the bundled commentary is the illustrative SAMPLE.
            The provenance demo prompt is suppressed in that case so reviewers
            don't ask NotebookLM to trace fake accessions to the warehouse.

    Returns:
        Markdown string for README_FOR_NOTEBOOKLM.md.
    """
    # Bundle filename is always 07_exec_commentary.md; the SAMPLE marker is
    # carried by an in-file banner, not the filename. README narrates which
    # variant is bundled so reviewers know whether to trust the accessions.
    commentary_filename = "07_exec_commentary.md"
    commentary_suffix = " (illustrative sample)" if is_sample else ""
    if is_sample:
        provenance_section = (
            "### Provenance queries (live commentary required)\n"
            f"_The bundled commentary file (`{commentary_filename}`) is currently\n"
            "an illustrative SAMPLE — the in-file banner flags it explicitly. Its\n"
            "accession numbers are NOT guaranteed to appear in\n"
            "`04_historical_financials.csv`. To run the provenance demo, set\n"
            "`ANTHROPIC_API_KEY` and re-run `make demo` (or `make commentary "
            f"TICKER={ticker} LIVE=1` followed by `make notebooklm`)._\n"
        )
    else:
        provenance_section = (
            "### Provenance queries (the key demo questions)\n"
            "- *For the $X.XB revenue figure in the commentary, what is the source filing?*\n"
            f"  → NotebookLM should cite the accession_no from {commentary_filename} and\n"
            "  trace it to the row in 04_historical_financials.csv with a matching accession_no.\n"
            "- *What SEC filing did the Q1 FY2026 revenue figure come from?*\n"
        )
    return f"""# How to Use This Bundle in NotebookLM

## Setup
1. Go to https://notebooklm.google.com
2. Create a new notebook called "{ticker} AI Financial Analyst"
3. Upload all files in this folder as sources (excluding this README)
4. Wait for NotebookLM to process (typically 1–2 minutes)

## Suggested Prompts

{provenance_section}
### Verification & hygiene
These prompts make the bundle audit-able. Every fact row in
`04_historical_financials.csv` carries `accession_no` + `filing_url`, so
NotebookLM can answer "where did this number come from?" — but only if you
ask it to.

- *List every cited number in `07_exec_commentary.md` with: the figure, the
  period it refers to, the source type (10-K / 10-Q / 8-K / forecast / model),
  and the source filename. Mark any number whose source you cannot identify.*
- *Compare `03b_latest_earnings_8K.pdf` (or `.txt`) against the latest 10-Q
  in this bundle. List every contradiction or material inconsistency between
  what management said in the earnings release and what the SEC filing
  reports — note these are contradictions, not just differences in framing.*
- *Flag every claim in `07_exec_commentary.md` that is not supported by a
  citation to a specific row in `04_historical_financials.csv`, the 10-K,
  the 10-Q, the 8-K, or the forecast outputs. Quote the unsupported claim
  verbatim.*
- *What would you need to see to confirm or refute the thesis in
  `07_exec_commentary.md`? List the specific evidence types that are NOT in
  this bundle (e.g. earnings call Q&A, sell-side estimates, peer comps).*
- *What is the data-as-of date for this bundle? Reference the latest
  `period_end` in `04_historical_financials.csv` and the filing date of the
  newest 10-Q/10-K. Treat that date as the cutoff for any "latest" question.*

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
| 03b_latest_earnings_8K.pdf / .txt | Most recent 8-K Item 2.02 (earnings release — management's prepared remarks; does NOT include Q&A) |
| 04_historical_financials.csv | Last 12 quarters with accession_no + filing_url |
| 05_forecast_summary.md | Prophet + AutoARIMA + Lasso outputs with CIs |
| 06_excel_model_summary.md | Base/Bull/Bear scenario description |
| {commentary_filename} | LLM-generated variance commentary with citations{commentary_suffix} |
| 08_test_report.html | pytest coverage report |
| 09_eval_report.md | Eval harness pass/fail (5 ground-truth scenarios) |

## Architecture note
This pipeline follows the reasoning-vs-computation pattern:
all arithmetic happens in Python/SQL, Claude generates narrative only. Every
number in the commentary traces to an SEC accession number.

## Automation note
Consumer NotebookLM (notebooklm.google.com) has no public API as of 2026,
so this bundle is uploaded manually. NotebookLM Enterprise on Google Cloud
exposes an API and a compliance path; migrating the upload step to that API
is a future-state option but is not wired into this repo.
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

    # 03b — Latest earnings 8-K (Item 2.02). Adds management's prepared
    # remarks to the bundle so verification prompts can compare what
    # management said against the 10-Q/10-K. Q&A is not in scope (paywalled).
    # Bundle is still considered complete if no recent 8-K Item 2.02 is found —
    # we just write a placeholder so NotebookLM's source list is stable across
    # runs, which avoids "missing file" surprises in the upload step.
    path_8k = _BUNDLE_DIR / "03b_latest_earnings_8K.pdf"
    if not _download_earnings_8k(cik_int, path_8k):
        if not path_8k.exists():
            path_8k = path_8k.with_suffix(".txt")
            path_8k.write_text(
                f"No recent 8-K Item 2.02 (earnings release) found for CIK "
                f"{cik_int:010d}.\nView all 8-Ks at: "
                "https://www.sec.gov/cgi-bin/browse-edgar"
                f"?action=getcompany&CIK={cik_int:010d}&type=8-K",
                encoding="utf-8",
            )
    elif not path_8k.exists():
        # _download_earnings_8k chose the .txt fallback path.
        path_8k = path_8k.with_suffix(".txt")
    written["03b_latest_earnings_8K"] = path_8k
    logger.info("Written: %s", path_8k.name)

    # 04 — Historical financials
    fy_end_month = int(config.get("fiscal_year_end_month", 12))
    df_hist = _build_historical_financials(resolved_ticker, fy_end_month=fy_end_month)
    path = _BUNDLE_DIR / "04_historical_financials.csv"
    if df_hist is not None:
        df_hist.to_csv(path, index=False)
    else:
        path.write_text(
            "period_end,fiscal_year,fiscal_period,Revenue,accession_no,filing_url,"
            "revenue_provenance\n"
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

    # 07 — Exec commentary (most recent).
    # Try a live regen first; if ANTHROPIC_API_KEY is missing this is a no-op
    # and we fall through to whatever's already on disk (typically the SAMPLE).
    live_regen_path = _maybe_regenerate_live_commentary(resolved_ticker)
    # Prefer the freshly-regenerated live file when present — picking it
    # explicitly avoids an alpha-sort race where ``_SAMPLE`` outranks today's
    # ``YYYYMMDD`` stamp under reverse-lexicographic ordering.
    commentary_path = live_regen_path or _find_latest_commentary(resolved_ticker)
    is_sample = bool(commentary_path and _is_sample_commentary(commentary_path))
    # Bundle filename is always 07_exec_commentary.md regardless of source —
    # one canonical name avoids NotebookLM having to pick between two siblings.
    # The SAMPLE marker carries through as an in-file banner so reviewers can
    # still tell illustrative content from a live, provenance-checked run.
    dest = _BUNDLE_DIR / "07_exec_commentary.md"
    # Remove the legacy _SAMPLE-suffixed file if it lingers from an older run.
    legacy = _BUNDLE_DIR / "07_exec_commentary_SAMPLE.md"
    if legacy.exists():
        legacy.unlink()
    if commentary_path and commentary_path.exists():
        body = commentary_path.read_text(encoding="utf-8")
        if is_sample:
            body = _SAMPLE_BANNER + body
        dest.write_text(body, encoding="utf-8")
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
    readme_path.write_text(
        _build_notebooklm_readme(resolved_ticker, is_sample=is_sample),
        encoding="utf-8",
    )
    written["README_FOR_NOTEBOOKLM"] = readme_path
    logger.info("Written: %s", readme_path.name)

    return written


# ── CLI ────────────────────────────────────────────────────────────────────────


_NOTEBOOKLM_UPLOAD_URL = "https://notebooklm.google.com"


def _print_upload_instructions(bundle_dir: Path, files: dict[str, Path]) -> None:
    """Print the bundle path + manual NotebookLM upload URL after build.

    Consumer NotebookLM has no public API as of 2026 — the upload step is a
    manual drag-drop in the browser. NotebookLM Enterprise on Google Cloud
    exposes an API; that's a future-state option, not wired into this repo.
    Surfacing the absolute bundle path and the upload URL here keeps runtime
    output aligned with the README/HOW_TO_DEMO framing and saves the operator
    a docs lookup.
    """
    print(f"\nBundle written to: {bundle_dir.resolve()}")
    print(f"Files: {len(files)}")
    for _label, p in sorted(files.items()):
        size = p.stat().st_size if p.exists() else 0
        print(f"  {p.name:<40}  {size:>8,} bytes")
    print(
        "\nNext step (manual): consumer NotebookLM has no public API. "
        f"Upload the bundle's files to {_NOTEBOOKLM_UPLOAD_URL} to create "
        "the notebook. (NotebookLM Enterprise on Google Cloud exposes an "
        "API for automated uploads — future option.)"
    )


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
        _print_upload_instructions(_BUNDLE_DIR, files)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
