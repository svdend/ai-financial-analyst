"""SEC EDGAR XBRL ingestion with full provenance.

Downloads company facts from ``data.sec.gov/api/xbrl/companyfacts/`` and maps
them to canonical line items via an ordered concept-synonym table.  Every
output row carries seven provenance columns so numbers can be traced back to
their source filing at any point in the downstream pipeline.

Output schema
-------------
ticker, line_item, concept_used, period_end, period_type (Q/FY),
fiscal_year, fiscal_period, value, unit,
accession_no, fact_id, filing_url, form_type, filed_date, frame

Saved to: ``/data/processed/{ticker}_financials.parquet``

CLI::

    python -m src.ingest_edgar
    python -m src.ingest_edgar --ticker CRWD
    python -m src.ingest_edgar --ticker PANW --years 10
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_EDGAR_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_REQUEST_TIMEOUT_SECONDS = 30
_DEFAULT_YEARS = 5

# Placeholder shipped in .env.example; if SEC sees this address it returns 403
# (and rightly so). The user-agent helper rejects it explicitly.
_PLACEHOLDER_USER_AGENT = "ai-financial-analyst-portfolio/1.0 contact@example.com"

# Fiscal periods to retain.  Annual (FY) plus four standard quarters.
_VALID_FP: frozenset[str] = frozenset({"FY", "Q1", "Q2", "Q3", "Q4"})

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DATA_DIR = _REPO_ROOT / "data" / "processed"
_CONFIG_PATH = _REPO_ROOT / "config" / "company.yaml"

# ── Concept synonym table ─────────────────────────────────────────────────────
# Ordered: first concept found in the companyfacts JSON wins.
# SaaS-optimised ordering (ExcludingAssessedTax before IncludingAssessedTax)
# ensures PANW and CRWD resolve to different concepts, which the eval harness
# and tests assert explicitly.
CONCEPT_SYNONYMS: dict[str, list[str]] = {
    "Revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "CostOfRevenue": [
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
        "CostOfServices",
        "CostOfGoodsSold",
    ],
    "GrossProfit": ["GrossProfit"],
    "OperatingExpenses": ["OperatingExpenses"],
    "ResearchAndDevelopment": ["ResearchAndDevelopmentExpense"],
    "SellingGeneralAdmin": ["SellingGeneralAndAdministrativeExpense"],
    "OperatingIncome": ["OperatingIncomeLoss"],
    "NetIncome": ["NetIncomeLoss", "ProfitLoss"],
    "Cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashAndCashEquivalents",
    ],
    "AccountsReceivable": ["AccountsReceivableNetCurrent"],
    # LIVE for hardware vendors (PANW/FTNT/CHKP); absent for pure-SaaS (CRWD/SNOW/ZS).
    # has_physical_inventory flag in v_data_quality is derived from whether this resolves.
    "Inventory": ["InventoryNet"],
    "AccountsPayable": ["AccountsPayableCurrent"],
    "DeferredRevenue": [
        "ContractWithCustomerLiabilityCurrent",
        "DeferredRevenueCurrent",
    ],
    "TotalAssets": ["Assets"],
    "TotalLiabilities": ["Liabilities"],
    "TotalEquity": ["StockholdersEquity"],
    "OperatingCashFlow": ["NetCashProvidedByUsedInOperatingActivities"],
    "InvestingCashFlow": ["NetCashProvidedByUsedInInvestingActivities"],
    "FinancingCashFlow": ["NetCashProvidedByUsedInFinancingActivities"],
    "CapEx": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
    ],
    "Depreciation": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "Depreciation",
    ],
    "StockBasedCompensation": ["ShareBasedCompensation"],
    "TreasuryStockRepurchases": [
        "PaymentsForRepurchaseOfCommonStock",
        "TreasuryStockValueAcquiredCostMethod",
    ],
}


# ── Provenance helpers ────────────────────────────────────────────────────────


def _fact_id(
    ticker: str,
    concept: str,
    period_end: str,
    fiscal_period: str,
    accession_no: str,
) -> str:
    """Return a stable 16-char hex identifier for a single fact row.

    Computed as the first 16 hex characters of the SHA-256 of the
    pipe-delimited key fields.  Stable across runs as long as inputs don't
    change.
    """
    key = f"{ticker}|{concept}|{period_end}|{fiscal_period}|{accession_no}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _filing_url(cik_int: int, accession_no: str) -> str:
    """Construct the EDGAR filing index URL from CIK and accession number.

    Args:
        cik_int:      Integer CIK (e.g. 1327567).
        accession_no: Accession number with dashes (e.g. "0001327567-24-000009").

    Returns:
        URL to the EDGAR filing index page, e.g.
        ``https://www.sec.gov/Archives/edgar/data/1327567/000132756724000009/``
    """
    accn_nodash = accession_no.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_nodash}/"


def _period_type(fp: str) -> str:
    """Map EDGAR fiscal-period string to ``'FY'`` or ``'Q'``."""
    return "FY" if fp == "FY" else "Q"


# ── HTTP helpers ──────────────────────────────────────────────────────────────


def _user_agent() -> str:
    """Return SEC ``User-Agent`` header value.

    SEC fair-use policy requires a real contact email in the User-Agent header.
    Submissions with the placeholder address are rejected with 403, so we
    refuse to send the request at all rather than producing a confusing failure.

    Raises:
        RuntimeError: if SEC_USER_AGENT is unset or still equal to the
            placeholder shipped in ``.env.example``.
    """
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua or ua == _PLACEHOLDER_USER_AGENT:
        raise RuntimeError(
            "SEC_USER_AGENT is not set (or still set to the .env.example placeholder). "
            "SEC fair-use policy requires a real contact email — see .env.example for "
            "the recommended GitHub noreply form."
        )
    return ua


def fetch_company_facts(cik: str) -> dict[str, Any]:
    """Download XBRL company facts JSON from ``data.sec.gov``.

    Args:
        cik: 10-digit zero-padded CIK string.

    Returns:
        Parsed JSON dict from the EDGAR company facts endpoint.

    Raises:
        requests.HTTPError: On non-2xx response.
    """
    url = _EDGAR_FACTS_URL.format(cik=cik)
    logger.info("Fetching company facts: %s", url)
    response = requests.get(
        url,
        headers={"User-Agent": _user_agent()},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


# ── Extraction logic ──────────────────────────────────────────────────────────


def _extract_line_item(
    facts_json: dict[str, Any],
    line_item: str,
    ticker: str,
    cik_int: int,
    concepts: list[str],
    min_fiscal_year: int,
) -> list[dict[str, Any]]:
    """Extract rows for one canonical line item from a companyfacts JSON dict.

    Tries concepts in order; the first concept present in the ``us-gaap``
    namespace with ``USD`` unit data wins.  Logs which concept matched.

    Missing ``frame`` fields are stored as empty strings (not NULL) so that
    downstream SQL GROUP BY and QUALIFY clauses work uniformly.

    Args:
        facts_json:      Parsed EDGAR companyfacts response.
        line_item:       Canonical name (e.g. ``"Revenue"``).
        ticker:          Ticker symbol (e.g. ``"PANW"``).
        cik_int:         Integer CIK for constructing filing URLs.
        concepts:        Ordered list of XBRL concept synonyms to try.
        min_fiscal_year: Earliest fiscal year to retain.

    Returns:
        List of row dicts ready for DataFrame construction.
    """
    us_gaap: dict[str, Any] = facts_json.get("facts", {}).get("us-gaap", {})

    # Among the configured concept synonyms, prefer the one with the most
    # *recent* quarterly coverage (quarterly facts within the keep window).
    # This is the "concept with most recent coverage wins" rule — it self-heals
    # across companies that switch XBRL concepts mid-history (e.g. PANW reports
    # CapEx under PaymentsToAcquirePropertyPlantAndEquipment pre-2022 and
    # PaymentsToAcquireProductiveAssets after).
    #
    # Why recency matters: lifetime fact count alone would favour discontinued
    # concepts.  Example: PANW reported Revenue under SalesRevenueNet through
    # FY2018 (130 facts) and switched to RevenueFromContractWithCustomerExcluding-
    # AssessedTax from FY2019 onwards (113 facts within keep window).  A
    # naïve max-coverage rule would pick the obsolete concept.
    #
    # Ties broken by list order (stable, deterministic).
    candidates: list[tuple[str, list[dict[str, Any]], int]] = []
    for concept in concepts:
        raw_usd = us_gaap.get(concept, {}).get("units", {}).get("USD")
        if not raw_usd:
            continue
        # Score = number of quarterly facts within the keep window
        # (fiscal_year >= min_fiscal_year AND fp ∈ {Q1..Q4}).
        in_window = sum(
            1
            for e in raw_usd
            if str(e.get("fp", "")) in _VALID_FP
            and isinstance(e.get("fy"), int)
            and int(e["fy"]) >= min_fiscal_year
            and str(e.get("fp", "")) != "FY"
        )
        if in_window == 0:
            continue
        candidates.append((concept, raw_usd, in_window))

    if not candidates:
        logger.warning("  %s: no matching concept found (tried: %s)", line_item, concepts)
        return []

    # Sort by in-window quarterly count desc; stable sort preserves list-order tiebreak.
    candidates.sort(key=lambda c: -c[2])
    used_concept, usd_entries, top_count = candidates[0]

    # If two concepts both have >2 quarters of in-window coverage, that's a
    # concept-switch event we want to surface in logs — not silently mask.
    other_substantive = [(c, n) for c, _, n in candidates[1:] if n > 2]
    if other_substantive:
        logger.warning(
            "  %s: concept-switch detected — picked %s (%d in-window quarterly facts); also: %s",
            line_item,
            used_concept,
            top_count,
            ", ".join(f"{c} ({n})" for c, n in other_substantive),
        )

    logger.info("  %s → %s (%d raw entries)", line_item, used_concept, len(usd_entries))

    rows: list[dict[str, Any]] = []
    for entry in usd_entries:
        fp = str(entry.get("fp", ""))
        if fp not in _VALID_FP:
            continue

        raw_fy = entry.get("fy")
        if raw_fy is None:
            continue
        fiscal_year = int(raw_fy)
        if fiscal_year < min_fiscal_year:
            continue

        period_end = str(entry.get("end", ""))
        accn = str(entry.get("accn", ""))
        raw_val = entry.get("val")

        if not period_end or not accn or raw_val is None:
            continue

        form_type = str(entry.get("form", ""))
        filed_date = str(entry.get("filed", ""))
        frame = str(entry.get("frame", ""))  # empty string when absent

        rows.append(
            {
                "ticker": ticker,
                "line_item": line_item,
                "concept_used": used_concept,
                "period_end": period_end,
                "period_type": _period_type(fp),
                "fiscal_year": fiscal_year,
                "fiscal_period": fp,
                "value": float(raw_val),
                "unit": "USD",
                "accession_no": accn,
                "fact_id": _fact_id(ticker, used_concept, period_end, fp, accn),
                "filing_url": _filing_url(cik_int, accn),
                "form_type": form_type,
                "filed_date": filed_date,
                "frame": frame,
            }
        )

    return rows


# ── Public API ────────────────────────────────────────────────────────────────


def ingest(
    ticker: str | None = None,
    years: int = _DEFAULT_YEARS,
    facts_json: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Ingest XBRL company facts from SEC EDGAR and save to parquet.

    Reads company metadata from ``config/company.yaml`` (written by
    :mod:`src.company_resolver`).  If *ticker* is supplied and differs from
    the YAML, the resolver is invoked first to update the config.

    The *facts_json* parameter exists for testing: pass a pre-loaded dict to
    skip the HTTP request entirely.

    Args:
        ticker:     Ticker symbol override; if ``None``, uses ``company.yaml``.
        years:      Number of most-recent fiscal years to retain (default 5).
        facts_json: Pre-loaded companyfacts JSON for testing (optional).

    Returns:
        DataFrame written to ``/data/processed/{ticker}_financials.parquet``.

    Raises:
        FileNotFoundError: If ``config/company.yaml`` does not exist.
        RuntimeError:      If no facts are extracted.
    """
    # ── Load company config ───────────────────────────────────────────────────
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Company config not found: {_CONFIG_PATH}\n"
            "Run first:  python -m src.company_resolver <TICKER>"
        )

    with _CONFIG_PATH.open() as fh:
        config: dict[str, Any] = yaml.safe_load(fh)

    # Allow caller to override the ticker; if it differs, re-resolve.
    resolved_ticker = (ticker or str(config["ticker"])).upper().strip()
    if resolved_ticker != str(config["ticker"]).upper():
        from src.company_resolver import resolve, write_company_yaml  # noqa: PLC0415

        logger.info("Ticker mismatch — resolving %s first.", resolved_ticker)
        meta = resolve(resolved_ticker)
        write_company_yaml(meta)
        with _CONFIG_PATH.open() as fh:
            config = yaml.safe_load(fh)

    cik = str(config["cik"])
    cik_int = int(config["cik_int"])

    # ── Fetch facts ───────────────────────────────────────────────────────────
    if facts_json is None:
        facts_json = fetch_company_facts(cik)

    # ── Extract all line items ────────────────────────────────────────────────
    all_rows: list[dict[str, Any]] = []
    for line_item, concepts in CONCEPT_SYNONYMS.items():
        rows = _extract_line_item(
            facts_json,
            line_item,
            resolved_ticker,
            cik_int,
            concepts,
            min_fiscal_year=1990,  # broad; will filter by `years` below
        )
        all_rows.extend(rows)

    if not all_rows:
        raise RuntimeError(f"No facts extracted for {resolved_ticker}.")

    df = pd.DataFrame(all_rows)

    # ── Filter to the most recent `years` fiscal years ────────────────────────
    max_fy = int(df["fiscal_year"].max())
    min_fy = max_fy - years + 1
    df = df[df["fiscal_year"] >= min_fy].copy()

    df = df.sort_values(["line_item", "fiscal_year", "fiscal_period", "filed_date"]).reset_index(
        drop=True
    )

    # ── Save ──────────────────────────────────────────────────────────────────
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _DATA_DIR / f"{resolved_ticker}_financials.parquet"
    df.to_parquet(out_path, index=False)

    # ── Summary log ──────────────────────────────────────────────────────────
    found = sorted(df["line_item"].unique())
    missing = [li for li in CONCEPT_SYNONYMS if li not in found]
    logger.info(
        "Saved %d rows → %s  (FY%d–FY%d)",
        len(df),
        out_path,
        min_fy,
        max_fy,
    )
    logger.info("Line items resolved (%d): %s", len(found), found)
    if missing:
        logger.warning("Line items missing (%d): %s", len(missing), missing)

    return df


# ── CLI entry-point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="Ingest SEC EDGAR XBRL facts for a company.",
        epilog="Saves to data/processed/{TICKER}_financials.parquet",
    )
    parser.add_argument(
        "--ticker",
        metavar="TICKER",
        default=None,
        help="Ticker symbol (e.g. PANW). Defaults to config/company.yaml.",
    )
    parser.add_argument(
        "--years",
        metavar="N",
        type=int,
        default=_DEFAULT_YEARS,
        help=f"Number of most-recent fiscal years to retain (default {_DEFAULT_YEARS}).",
    )
    args = parser.parse_args()

    try:
        result_df = ingest(ticker=args.ticker, years=args.years)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"\nShape: {result_df.shape}")
    print(f"Fiscal years: {sorted(result_df['fiscal_year'].unique())}")
    print(f"Line items: {sorted(result_df['line_item'].unique())}")
    print("\nSample rows (head):")
    print(
        result_df[["line_item", "fiscal_year", "fiscal_period", "value", "accession_no"]]
        .head(10)
        .to_string(index=False)
    )
    print("\nProvenance sample:")
    print(
        result_df[["line_item", "fact_id", "accession_no", "form_type", "filed_date"]]
        .head(5)
        .to_string(index=False)
    )
    missing_items = [li for li in CONCEPT_SYNONYMS if li not in result_df["line_item"].values]
    print(f"\nMissing line items: {missing_items}")
