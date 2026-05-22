"""Company resolver — maps stock tickers to SEC EDGAR metadata.

Downloads the SEC ticker-to-CIK mapping on first use, caches it locally
for seven days, and enriches each record with fiscal-year-end and
sector-ETF data from a built-in fallback table.

All EDGAR requests use the ``SEC_USER_AGENT`` environment variable as the
``User-Agent`` header, as required by SEC fair-use policy.  The module-level
default fallback value intentionally contains no real email address.

CLI usage::

    python -m src.company_resolver PANW

Prints resolved metadata to stdout and writes ``/config/company.yaml``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import TypedDict

import requests
import yaml

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_REQUEST_TIMEOUT_SECONDS = 30
_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days
_CIK_ZERO_PAD_WIDTH = 10
_DEFAULT_FY_END_DAY = 31  # Conservative; actual end day varies by month

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RAW_DIR = _REPO_ROOT / "data" / "raw"
_CONFIG_DIR = _REPO_ROOT / "config"
_CACHE_PATH = _RAW_DIR / "sec_tickers.json"

# Privacy guard: this placeholder never contains a real email address.
# SEC requires an email in the User-Agent header for fair-use compliance.
# Set SEC_USER_AGENT in .env to "your-project/1.0 yourname+sec@gmail.com".
# _user_agent() refuses to send a request with this placeholder.
_PLACEHOLDER_USER_AGENT = "ai-financial-analyst-portfolio/1.0 contact@example.com"
# Backwards-compatible alias retained for the privacy-regression test.
_DEFAULT_USER_AGENT = _PLACEHOLDER_USER_AGENT

# Fallback metadata table: ticker → (fiscal_year_end_month, sector_etf)
# Month integers: 1=January … 12=December.  fy7=July (PANW), fy1=January, etc.
_TICKER_METADATA: dict[str, tuple[int, str]] = {
    "PANW": (7, "XLK"),
    "AAPL": (9, "XLK"),
    "MSFT": (6, "XLK"),
    "NVDA": (1, "XLK"),
    "ORCL": (5, "XLK"),
    "CRWD": (1, "XLK"),
    "SNOW": (1, "XLK"),
    "DDOG": (12, "XLK"),
    "WDAY": (1, "XLK"),
    "GOOGL": (12, "XLK"),
    "META": (12, "XLK"),
    "AMZN": (12, "XLK"),
}


# ── Types ─────────────────────────────────────────────────────────────────────
class CompanyMetadata(TypedDict):
    """Fully resolved company metadata dict returned by :func:`resolve`."""

    cik: str  # 10-digit zero-padded CIK, e.g. "0001327567"
    cik_int: int  # CIK as a plain integer
    ticker: str  # Normalised uppercase ticker symbol
    name: str  # Company name from SEC EDGAR
    fiscal_year_end_month: int  # 1–12
    fiscal_year_end_day: int  # Conservative last day of the fiscal-year-end month
    sector_etf: str  # Representative sector ETF, e.g. "XLK"


# ── Internal helpers ──────────────────────────────────────────────────────────
def _user_agent() -> str:
    """Return the SEC ``User-Agent`` header value.

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


def _cache_is_fresh() -> bool:
    """Return ``True`` if the local ticker cache exists and is within TTL."""
    if not _CACHE_PATH.exists():
        return False
    age_seconds = time.time() - _CACHE_PATH.stat().st_mtime
    return age_seconds < _CACHE_TTL_SECONDS


def _load_sec_tickers() -> dict[str, object]:
    """Return the SEC ticker→CIK mapping, fetching and caching as needed.

    Returns:
        Raw JSON dict keyed by integer strings (SEC's internal ordering).
        Each value is a dict with ``cik_str``, ``ticker``, and ``title``.

    Raises:
        requests.HTTPError: If the SEC EDGAR endpoint returns a non-2xx status.
    """
    if _cache_is_fresh():
        logger.debug("SEC tickers: loading from cache (%s)", _CACHE_PATH)
        with _CACHE_PATH.open() as fh:
            data: dict[str, object] = json.load(fh)
        return data

    logger.info("SEC tickers: fetching from %s", _SEC_TICKERS_URL)
    response = requests.get(
        _SEC_TICKERS_URL,
        headers={"User-Agent": _user_agent()},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    fetched: dict[str, object] = response.json()

    _RAW_DIR.mkdir(parents=True, exist_ok=True)
    with _CACHE_PATH.open("w") as fh:
        json.dump(fetched, fh)
    logger.debug("SEC tickers: cached to %s", _CACHE_PATH)

    return fetched


# ── Public API ────────────────────────────────────────────────────────────────
def resolve(ticker: str) -> CompanyMetadata:
    """Resolve a ticker symbol to SEC EDGAR company metadata.

    Looks up the ticker in SEC's canonical CIK mapping, then enriches the
    result with fiscal-year-end month and sector-ETF from the built-in
    fallback table.

    Args:
        ticker: Stock ticker symbol (case-insensitive, e.g. ``"PANW"``).

    Returns:
        :class:`CompanyMetadata` with CIK, name, fiscal-year info, and
        sector ETF.

    Raises:
        ValueError: If the ticker is absent from SEC EDGAR, or if no
            fiscal-year / sector-ETF entry exists for it in
            :data:`_TICKER_METADATA`.
    """
    normalised = ticker.upper().strip()
    data = _load_sec_tickers()

    sec_entry: dict[str, object] | None = None
    for raw_value in data.values():
        if not isinstance(raw_value, dict):
            continue
        if str(raw_value.get("ticker", "")).upper() == normalised:
            sec_entry = raw_value
            break

    if sec_entry is None:
        raise ValueError(
            f"Ticker '{normalised}' not found in SEC EDGAR ticker mapping. "
            "Verify the symbol, or the SEC tickers cache may be stale "
            f"(delete {_CACHE_PATH} to force a fresh fetch)."
        )

    cik_int = int(str(sec_entry["cik_str"]))
    cik = str(cik_int).zfill(_CIK_ZERO_PAD_WIDTH)

    if normalised not in _TICKER_METADATA:
        raise ValueError(
            f"Ticker '{normalised}' is in SEC EDGAR but has no fiscal-year / "
            "sector-ETF entry.  Add it to _TICKER_METADATA in "
            "src/company_resolver.py."
        )

    fy_month, sector_etf = _TICKER_METADATA[normalised]

    return CompanyMetadata(
        cik=cik,
        cik_int=cik_int,
        ticker=normalised,
        name=str(sec_entry["title"]),
        fiscal_year_end_month=fy_month,
        fiscal_year_end_day=_DEFAULT_FY_END_DAY,
        sector_etf=sector_etf,
    )


def write_company_yaml(metadata: CompanyMetadata, out_path: Path | None = None) -> Path:
    """Persist resolved metadata to ``/config/company.yaml``.

    Args:
        metadata: Resolved :class:`CompanyMetadata` dict.
        out_path: Override output path; defaults to ``/config/company.yaml``.

    Returns:
        Path to the written YAML file.
    """
    target = out_path if out_path is not None else _CONFIG_DIR / "company.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w") as fh:
        yaml.dump(dict(metadata), fh, default_flow_style=False, sort_keys=True)
    return target


# ── CLI entry-point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="Resolve a stock ticker to SEC EDGAR metadata.",
        epilog="Writes resolved metadata to /config/company.yaml.",
    )
    parser.add_argument("ticker", help="Ticker symbol, e.g. PANW")
    args = parser.parse_args()

    try:
        info = resolve(args.ticker)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    yaml_path = write_company_yaml(info)

    print("\nResolved metadata:")
    print(yaml.dump(dict(info), default_flow_style=False, sort_keys=True))
    print(f"Written to: {yaml_path}")
