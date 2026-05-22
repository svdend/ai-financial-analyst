"""Tests for src/company_resolver.py.

All SEC HTTP requests are mocked via the ``responses`` library so the suite
runs offline and deterministically.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import responses as resp_lib

from src.company_resolver import (
    _DEFAULT_USER_AGENT,
    _TICKER_METADATA,
    CompanyMetadata,
    resolve,
    write_company_yaml,
)

# ── Shared fixture data ────────────────────────────────────────────────────────

_MOCK_TICKERS: dict[str, object] = {
    "0": {"cik_str": "1327567", "ticker": "PANW", "title": "Palo Alto Networks Inc"},
    "1": {"cik_str": "815097", "ticker": "AAPL", "title": "Apple Inc"},
    "2": {"cik_str": "1517396", "ticker": "CRWD", "title": "CrowdStrike Holdings Inc"},
    "3": {"cik_str": "1640147", "ticker": "SNOW", "title": "Snowflake Inc"},
}

_SEC_URL = "https://www.sec.gov/files/company_tickers.json"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _activate_mock() -> None:
    """Add the SEC ticker endpoint mock.  Must be called inside @responses.activate."""
    resp_lib.add(resp_lib.GET, _SEC_URL, json=_MOCK_TICKERS, status=200)


# ── Tests ──────────────────────────────────────────────────────────────────────


@resp_lib.activate
def test_resolve_panw_cik() -> None:
    """PANW should resolve to CIK 0001327567."""
    _activate_mock()
    result: CompanyMetadata = resolve("PANW")
    assert result["cik"] == "0001327567"
    assert result["cik_int"] == 1327567


@resp_lib.activate
def test_resolve_panw_fy_end_month() -> None:
    """PANW fiscal year ends in July (month 7)."""
    _activate_mock()
    result = resolve("PANW")
    assert result["fiscal_year_end_month"] == 7


@resp_lib.activate
def test_resolve_aapl_fy_end_month() -> None:
    """AAPL fiscal year ends in September (month 9)."""
    _activate_mock()
    result = resolve("aapl")  # case-insensitive
    assert result["fiscal_year_end_month"] == 9


@resp_lib.activate
def test_resolve_name() -> None:
    """Resolved name should come from the SEC EDGAR title field."""
    _activate_mock()
    result = resolve("PANW")
    assert "Palo Alto" in result["name"]


@resp_lib.activate
def test_resolve_sector_etf() -> None:
    """All tickers in the fallback table should resolve to XLK."""
    _activate_mock()
    result = resolve("PANW")
    assert result["sector_etf"] == "XLK"


def test_resolve_unknown_ticker_raises() -> None:
    """Unknown tickers must raise ValueError without hitting the network."""
    # The ticker lookup in SEC data will fail first; no HTTP mock needed.
    with pytest.raises(ValueError, match="ZZZZNOTREAL"):
        # Patch _load_sec_tickers to return empty so we avoid cache files
        with patch("src.company_resolver._load_sec_tickers", return_value={}):
            resolve("ZZZZNOTREAL")


@resp_lib.activate
def test_resolve_ticker_not_in_metadata_raises() -> None:
    """A ticker present in SEC but absent from _TICKER_METADATA raises ValueError."""
    # Use a ticker that exists in mock SEC data but not in our metadata table
    mock_with_unknown = dict(_MOCK_TICKERS)
    mock_with_unknown["99"] = {"cik_str": "9999999", "ticker": "NEWCO", "title": "New Co Inc"}
    resp_lib.add(resp_lib.GET, _SEC_URL, json=mock_with_unknown, status=200)
    with pytest.raises(ValueError, match="NEWCO"):
        resolve("NEWCO")


def test_default_user_agent_no_personal_email() -> None:
    """Default fallback User-Agent must not contain a real personal email domain.

    Privacy regression test: the default must be safe to ship in a public repo.
    We permit @example.com (RFC 2606 reserved) but not real mail providers.
    """
    personal_domains = ["@gmail.com", "@yahoo.com", "@outlook.com", "@hotmail.com"]
    ua_lower = _DEFAULT_USER_AGENT.lower()
    for domain in personal_domains:
        assert (
            domain not in ua_lower
        ), f"Default User-Agent contains '{domain}' — use a placeholder address."


def test_user_agent_fails_fast_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """_user_agent() must raise if SEC_USER_AGENT is unset (no silent fallback)."""
    from src.company_resolver import _user_agent

    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        _user_agent()


def test_user_agent_fails_fast_on_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """_user_agent() must reject the .env.example placeholder (would 403 at SEC)."""
    from src.company_resolver import _PLACEHOLDER_USER_AGENT, _user_agent

    monkeypatch.setenv("SEC_USER_AGENT", _PLACEHOLDER_USER_AGENT)
    with pytest.raises(RuntimeError, match="placeholder"):
        _user_agent()


def test_all_metadata_tickers_have_valid_fy_month() -> None:
    """Every entry in _TICKER_METADATA must have a valid fiscal-year-end month (1–12)."""
    for ticker, (fy_month, _) in _TICKER_METADATA.items():
        assert 1 <= fy_month <= 12, f"{ticker}: invalid fiscal_year_end_month {fy_month}"


@resp_lib.activate
def test_write_company_yaml(tmp_path: Path) -> None:
    """write_company_yaml persists all metadata fields to YAML correctly."""
    _activate_mock()
    meta = resolve("PANW")
    out = tmp_path / "company.yaml"
    written = write_company_yaml(meta, out_path=out)
    assert written == out
    assert out.exists()
    content = out.read_text()
    assert "0001327567" in content
    assert "PANW" in content
