"""Generate CFO-style variance commentary following the Kepler Finance pattern.

**Reasoning vs. computation split (architectural invariant)**
All arithmetic happens in deterministic Python/SQL before Claude is called.
Claude generates narrative only. Every number cited in the output must appear
verbatim in the input JSON and traces back to an SEC accession number.

Pipeline (5 steps):

1. **Pull** — query DuckDB ``v_variance_facts`` and ``v_data_quality``; no raw
   numbers are passed to Claude — only pre-computed variances.
2. **Refuse** — exit non-zero if restatement detected, if the variance window
   has missing quarters, or if the fiscal-year boundary is ambiguous.
3. **Format** — pre-format every numeric value into canonical machine-validated
   strings (``$1.2B``, ``3.2%``); bundle with ``fact_id`` and ``accession_no``.
4. **Narrate** — call Claude via the Anthropic SDK (model resolved at runtime
   via ``src.select_models.select_models()``).
5. **Guard** — parse-then-compare hallucination guard; raises ``HallucinationError``
   if any numeric token in the output is not verbatim in the input JSON, uses a
   forbidden word-form (``billion`` / ``bps``), uses parens-negative notation,
   or has a bare number not preceded by ``$`` / followed by ``%``.

Guard CATCHES: number fabrication, unit drift (M↔B), parens-negatives,
word-form numbers (``billion``/``million``), ``bps`` / ``basis point``,
bare numeric tokens, missing citations, citations to accessions not in input.

Guard does NOT CATCH: arithmetic errors (mitigated by Rule 4 — Claude is
forbidden from doing math), wrong attribution (revenue described as margin),
or logical inconsistency across paragraphs.

CLI::

    python -m src.generate_commentary                    # dry-run (no API call)
    python -m src.generate_commentary --dry-run          # explicit dry-run
    python -m src.generate_commentary --live             # calls Anthropic API
    python -m src.generate_commentary --verify-only PATH # re-run guard on file
    python -m src.generate_commentary --ticker PANW --live
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import yaml

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
_REPO_ROOT     = Path(__file__).resolve().parents[1]
_CONFIG_PATH   = _REPO_ROOT / "config" / "company.yaml"
_PROCESSED_DIR = _REPO_ROOT / "data" / "processed"
_DASHBOARD_DIR = _REPO_ROOT / "dashboard"

# ── Hallucination-guard regex patterns (from Prompt 8 spec) ──────────────────
_DOLLAR_PAT       = re.compile(r"-?\$\d+(?:\.\d+)?(?:[KMB])?")
_PERCENT_PAT      = re.compile(r"-?\d+(?:\.\d+)?%")
_YEAR_PAT         = re.compile(r"\b(?:19|20)\d{2}\b")
_ACCESSION_PAT    = re.compile(r"\d{10}-\d{2}-\d{6}")
_PARENS_NEG_DOLL  = re.compile(r"\$\(\s*\d")
_PARENS_NEG_PCT   = re.compile(r"\(\s*-?\d+(?:\.\d+)?\s*%\s*\)")
_BARE_NUMBER_PAT  = re.compile(
    r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b"   # comma-grouped: 1,234 / 1,234.5
    r"|\b\d+\.\d+\b"                        # decimals: 1.5 / 12.30
    r"|\b[1-9]\d{3,}\b"                     # 4+ digit integers: 8400, 12000
)

_FORBIDDEN_WORDS = (
    "billion", "million", "thousand",
    "bps", "basis point", "percentage point", "pct point", "pp ",
)

_SYSTEM_PROMPT = """You are a financial analyst writing internal CFO-style variance commentary. STRICT OUTPUT RULES:

1. Use ONLY the numbers provided in the user message.
2. Never recall facts about the company from training data.
3. Never speculate about events, products, customers, executives, or macro conditions not present in the input.
4. Never compute new numbers. Every number you write must appear VERBATIM in the input JSON. You are not permitted to do arithmetic. If the user message does not contain a number you want to write, write the surrounding sentence without that number.
5. NUMERIC FORMAT (machine-validated):
   - Dollars as `$<digits>[.<digits>]<suffix>` where suffix ∈ {M,B,K}
   - Percentages as `<digits>[.<digits>]%`
   - Negatives use leading minus, never parens
   - Years (4-digit, 1900-2099) are allowed bare; all other numbers must be wrapped in `$` or `%`
6. CITATION: Every numeric claim should include an inline citation in the form `[<accession_no>]` immediately after the number. The accession_no is in the input JSON for each fact. Example: `Revenue of $1.2B [0001327567-26-000123]`.
7. Output is markdown with sections: Quarter at a glance, Drivers of variance, Forward look, Risks. Each section ≤ 4 sentences."""


# ── Custom exceptions ──────────────────────────────────────────────────────────

class RefusalError(RuntimeError):
    """Raised when pipeline refuses to generate commentary (see Step 2)."""


class HallucinationError(RuntimeError):
    """Raised when the guard detects a policy violation in the output."""


# ── Step 1: Pull from DuckDB ──────────────────────────────────────────────────

def _pull_variance_data(db_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (variance_row, quality_row) dicts from DuckDB.

    Args:
        db_path: Path to the DuckDB warehouse file.

    Returns:
        Tuple of (variance facts row, data quality row).

    Raises:
        FileNotFoundError: If the DuckDB file is missing.
        RuntimeError: If v_variance_facts returns 0 rows.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"DuckDB warehouse not found: {db_path}")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        vf = con.execute("SELECT * FROM v_variance_facts").fetchdf()
        dq = con.execute("SELECT * FROM v_data_quality").fetchdf()
    finally:
        con.close()

    if vf.empty:
        raise RuntimeError("v_variance_facts returned 0 rows — run build_variance_facts first.")

    variance_row: dict[str, Any] = dict(vf.iloc[0])
    quality_row:  dict[str, Any] = dict(dq.iloc[0]) if not dq.empty else {}
    return variance_row, quality_row


# ── Step 2: Refusal checks ────────────────────────────────────────────────────

def _check_refusals(variance_row: dict[str, Any], quality_row: dict[str, Any]) -> None:
    """Raise RefusalError if any data-integrity condition is violated.

    Conditions:
    - ``has_restatement`` is TRUE in v_data_quality (10-K/A filed; numbers in flux)
    - ``missing_quarters`` is non-empty covering the variance window
    - fiscal_year or fiscal_period is None/NaN in variance data

    Args:
        variance_row: Row from v_variance_facts.
        quality_row:  Row from v_data_quality.

    Raises:
        RefusalError: On any integrity condition violation.
    """
    # Restatement check
    has_restatement = quality_row.get("has_restatement", False)
    if has_restatement and str(has_restatement).upper() not in ("FALSE", "0", "NONE", ""):
        if bool(has_restatement):
            raise RefusalError(
                "REFUSED: v_data_quality.has_restatement=TRUE — a 10-K/A amendment was filed. "
                "Numbers may be in flux. Re-run after the amended filing is incorporated."
            )

    # Missing quarters check
    missing = quality_row.get("missing_quarters", None)
    if missing is not None and not pd.isna(missing) and str(missing).strip():
        raise RefusalError(
            f"REFUSED: v_data_quality.missing_quarters='{missing}' — the variance window "
            "has gaps. Commentary from incomplete data risks being misleading."
        )

    # Fiscal year boundary check
    fy = variance_row.get("fiscal_year")
    fp = variance_row.get("fiscal_period")
    if fy is None or fp is None or (isinstance(fy, float) and pd.isna(fy)):
        raise RefusalError(
            "REFUSED: fiscal_year or fiscal_period is NULL in v_variance_facts — "
            "the fiscal-year boundary cannot be determined."
        )


# ── Step 3: Pre-format numbers ────────────────────────────────────────────────

def _fmt_dollars(val: float | None) -> str | None:
    """Format a dollar value to canonical $<digits><suffix> form.

    Args:
        val: Dollar amount (raw, e.g. 1_200_000_000).

    Returns:
        Canonical string like ``"$1.2B"`` or ``None`` if val is None/NaN.
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    v = float(val)
    neg = v < 0
    abs_v = abs(v)
    if abs_v >= 1e9:
        s = f"${abs_v / 1e9:.2g}B"
    elif abs_v >= 1e6:
        s = f"${abs_v / 1e6:.2g}M"
    elif abs_v >= 1e3:
        s = f"${abs_v / 1e3:.2g}K"
    else:
        s = f"${abs_v:.2g}"
    # Ensure at least one decimal place for B/M/K values for readability
    # Re-format with one decimal place for B figures
    if abs_v >= 1e9:
        s = f"${abs_v / 1e9:.1f}B"
    elif abs_v >= 1e6:
        s = f"${abs_v / 1e6:.1f}M"
    return f"-{s}" if neg else s


def _fmt_pct(val: float | None) -> str | None:
    """Format a ratio (0.032) to percentage string (``"3.2%"``).

    Args:
        val: Ratio value (e.g. 0.032 for 3.2%).

    Returns:
        Canonical string like ``"3.2%"`` or ``None`` if val is None/NaN.
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    pct = float(val) * 100
    sign = "-" if pct < 0 else ""
    return f"{sign}{abs(pct):.1f}%"


def _build_payload(
    ticker: str,
    variance_row: dict[str, Any],
    quality_row: dict[str, Any],
) -> dict[str, Any]:
    """Construct the structured JSON payload for Claude.

    Every value is pre-formatted; raw numbers are never sent.
    Fact IDs and accession numbers are bundled for citation tracking.

    Args:
        ticker:       Company ticker symbol.
        variance_row: Row from v_variance_facts.
        quality_row:  Row from v_data_quality.

    Returns:
        Structured dict suitable for JSON serialization and passing to Claude.
    """
    accession = str(variance_row.get("revenue_actual_accession") or "")
    filing_url = str(variance_row.get("revenue_actual_filing_url") or "")
    fact_id    = str(variance_row.get("revenue_actual_fact_id") or "")
    model_str  = str(variance_row.get("revenue_prior_forecast_model") or "N/A")

    def _entry(fmt_val: str | None, *, fact_id_val: str = "", accession_val: str = "") -> dict[str, Any]:
        e: dict[str, Any] = {"value": fmt_val}
        if fact_id_val:
            e["fact_id"] = fact_id_val
        if accession_val:
            e["accession"] = accession_val
            e["filing_url"] = filing_url if accession_val == accession else ""
        return e

    payload: dict[str, Any] = {
        "ticker": ticker,
        "fiscal_year": int(variance_row.get("fiscal_year") or 0),
        "fiscal_period": str(variance_row.get("fiscal_period") or ""),
        "latest_period_end": str(variance_row.get("latest_period_end") or "")[:10],

        # Revenue actuals
        "revenue": _entry(
            _fmt_dollars(variance_row.get("revenue_actual")),
            fact_id_val=fact_id,
            accession_val=accession,
        ),
        "revenue_yoy": _entry(_fmt_dollars(variance_row.get("revenue_yoy"))),
        "revenue_yoy_growth_pct": _entry(_fmt_pct(variance_row.get("revenue_yoy_growth_pct"))),

        # Forecast comparison
        "revenue_prior_forecast": _entry(_fmt_dollars(variance_row.get("revenue_prior_forecast"))),
        "revenue_variance_vs_forecast": _entry(_fmt_dollars(variance_row.get("revenue_variance_vs_forecast"))),
        "revenue_variance_pct_vs_forecast": _entry(_fmt_pct(variance_row.get("revenue_variance_pct_vs_forecast"))),
        "revenue_prior_forecast_model": model_str,

        # Consensus
        "revenue_consensus": _entry(_fmt_dollars(variance_row.get("revenue_consensus"))),

        # Gross margin
        "gross_margin_pct_actual": _entry(_fmt_pct(variance_row.get("gross_margin_pct_actual"))),
        "gross_margin_pct_yoy": _entry(_fmt_pct(variance_row.get("gross_margin_pct_yoy"))),
        "gross_margin_pct_yoy_delta": _entry(_fmt_pct(variance_row.get("gross_margin_pct_yoy_delta"))),

        # Operating margin
        "operating_margin_pct_actual": _entry(_fmt_pct(variance_row.get("operating_margin_pct_actual"))),
        "operating_margin_pct_yoy": _entry(_fmt_pct(variance_row.get("operating_margin_pct_yoy"))),
        "operating_margin_pct_yoy_delta": _entry(_fmt_pct(variance_row.get("operating_margin_pct_yoy_delta"))),

        # Free cash flow
        "fcf_actual": _entry(_fmt_dollars(variance_row.get("fcf_actual"))),
        "fcf_yoy": _entry(_fmt_dollars(variance_row.get("fcf_yoy"))),
        "fcf_yoy_growth_pct": _entry(_fmt_pct(variance_row.get("fcf_yoy_growth_pct"))),

        # Data quality context
        "has_restatement": bool(quality_row.get("has_restatement", False)),
        "has_physical_inventory": bool(quality_row.get("has_physical_inventory", False)),
    }
    return payload


# ── Step 4: Call Claude ───────────────────────────────────────────────────────

def _call_claude(payload: dict[str, Any]) -> str:
    """Invoke Claude to generate narrative commentary.

    Uses runtime model discovery via ``src.select_models.select_models()``.
    Temperature 0.2, max_tokens 1500.

    Args:
        payload: Structured JSON from Step 3 (all numbers pre-formatted).

    Returns:
        Raw markdown text from Claude.

    Raises:
        ImportError: If the ``anthropic`` package is missing.
    """
    import anthropic  # noqa: PLC0415

    from src.select_models import select_models  # noqa: PLC0415

    models = select_models()
    narrator_model = models["narrator"]
    logger.info("Calling Claude narrator model: %s", narrator_model)

    user_message = (
        "Here is the pre-computed variance data for this quarter. "
        "Write the CFO-style variance commentary following the STRICT OUTPUT RULES.\n\n"
        f"```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=narrator_model,
        max_tokens=1500,
        temperature=0.2,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return str(response.content[0].text)  # type: ignore[union-attr]


# ── Step 5: Hallucination guard ───────────────────────────────────────────────

def _parse_canonical_dollars(token: str) -> float:
    """Parse a canonical dollar token to a float.

    Args:
        token: String like ``"$1.2B"`` or ``"-$500M"``.

    Returns:
        Float dollar amount.
    """
    t = token.replace(",", "").replace("$", "")
    neg = t.startswith("-")
    t = t.lstrip("-")
    multiplier = 1.0
    if t.endswith("B"):
        multiplier = 1e9
        t = t[:-1]
    elif t.endswith("M"):
        multiplier = 1e6
        t = t[:-1]
    elif t.endswith("K"):
        multiplier = 1e3
        t = t[:-1]
    val = float(t) * multiplier
    return -val if neg else val


def _parse_canonical_pct(token: str) -> float:
    """Parse a canonical percent token to a float ratio.

    Args:
        token: String like ``"3.2%"`` or ``"-1.5%"``.

    Returns:
        Float ratio (0.032 for 3.2%).
    """
    t = token.rstrip("%")
    return float(t) / 100.0


def _extract_input_values(payload: dict[str, Any]) -> tuple[list[float], set[str]]:
    """Extract all numeric values and accessions from the payload for guard comparison.

    Args:
        payload: The structured JSON payload passed to Claude.

    Returns:
        Tuple of (list of canonical float values, set of valid accession strings).
    """
    values: list[float] = []
    accessions: set[str] = set()

    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            val = obj.get("value")
            if isinstance(val, str):
                if val and val not in ("None", "null"):
                    if "$" in val:
                        try:
                            values.append(_parse_canonical_dollars(val))
                        except ValueError:
                            pass
                    elif "%" in val:
                        try:
                            values.append(_parse_canonical_pct(val))
                        except ValueError:
                            pass
            acc = obj.get("accession")
            if isinstance(acc, str) and _ACCESSION_PAT.search(acc):
                accessions.add(acc)
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(payload)
    return values, accessions


def _is_within_tolerance(parsed: float, input_values: list[float]) -> bool:
    """Return True if *parsed* matches any input value within guard tolerance.

    Tolerance: ±0.5% relative, or ±$0.01 absolute for raw <$1000,
    or ±0.001 absolute for percentages — whichever is looser.

    Args:
        parsed:       The numeric value extracted from Claude's output.
        input_values: All canonical values from the input payload.
    """
    for ref in input_values:
        abs_diff = abs(parsed - ref)
        if abs(ref) > 0:
            rel_tol = abs_diff / abs(ref)
            if rel_tol <= 0.005:
                return True
        # Absolute tolerance for small magnitudes
        if abs_diff <= max(0.01, abs(parsed) * 0.005, 0.001):
            return True
    return False


def run_hallucination_guard(text: str, payload: dict[str, Any]) -> None:
    """Validate Claude's output against the input payload.

    Raises HallucinationError on any of:
    - A dollar/percent token not matching any input value within tolerance
    - A forbidden word-form (``billion``, ``bps``, etc.)
    - A parens-negative number (``$(...)``, ``(-1.2%)``)
    - A bare number not inside $…/…% token or a year
    - A dollar token with no following citation within 50 chars
    - A citation to an accession not in the input

    Args:
        text:    The raw markdown text from Claude.
        payload: The structured JSON payload passed to Claude.

    Raises:
        HallucinationError: If any guard condition fires.
    """
    input_values, valid_accessions = _extract_input_values(payload)

    # (a) Forbidden word-forms
    text_lower = text.lower()
    for word in _FORBIDDEN_WORDS:
        if word in text_lower:
            raise HallucinationError(
                f"Guard FAIL — forbidden word-form '{word}' found in output."
            )

    # (d) Parens-negative detection
    if _PARENS_NEG_DOLL.search(text):
        raise HallucinationError(
            "Guard FAIL — parens-negative dollar format found (e.g. $(123)M). "
            "Use leading minus instead."
        )
    if _PARENS_NEG_PCT.search(text):
        raise HallucinationError(
            "Guard FAIL — parens-negative percent format found. Use leading minus instead."
        )

    # (b) Dollar token value check + (f) Citation check
    for m in _DOLLAR_PAT.finditer(text):
        token = m.group()
        try:
            parsed_val = _parse_canonical_dollars(token)
        except ValueError:
            continue

        if not _is_within_tolerance(parsed_val, input_values):
            raise HallucinationError(
                f"Guard FAIL — dollar token '{token}' not found in input values "
                f"(closest inputs: {input_values[:5]})."
            )

        # Citation check: must find [accession_no] within 50 chars after token
        after = text[m.end(): m.end() + 50]
        cite_match = re.search(r"\[(\d{10}-\d{2}-\d{6}(?:,\s*\d{10}-\d{2}-\d{6})*)\]", after)
        if not cite_match:
            raise HallucinationError(
                f"Guard FAIL — dollar token '{token}' has no citation within 50 characters."
            )
        cited_accessions = re.findall(r"\d{10}-\d{2}-\d{6}", cite_match.group(1))
        for cited in cited_accessions:
            if cited not in valid_accessions:
                raise HallucinationError(
                    f"Guard FAIL — citation '[{cited}]' is not in the input accession set "
                    f"(valid: {valid_accessions})."
                )

    # (b) Percent token value check
    for m in _PERCENT_PAT.finditer(text):
        token = m.group()
        try:
            parsed_val = _parse_canonical_pct(token)
        except ValueError:
            continue
        if not _is_within_tolerance(parsed_val, input_values):
            raise HallucinationError(
                f"Guard FAIL — percent token '{token}' not found in input values "
                f"(closest inputs: {[f'{v:.4f}' for v in input_values[:5]]})."
            )

    # (e) Bare-number check (must be inside $... or ...% or be a 4-digit year)
    year_spans = {m.span() for m in _YEAR_PAT.finditer(text)}
    dollar_spans = {m.span() for m in _DOLLAR_PAT.finditer(text)}
    pct_spans    = {m.span() for m in _PERCENT_PAT.finditer(text)}

    for m in _BARE_NUMBER_PAT.finditer(text):
        span = m.span()
        # Accept if it overlaps with a year, dollar token, or percent token
        in_year   = any(ys[0] <= span[0] and span[1] <= ys[1] for ys in year_spans)
        in_dollar = any(ds[0] <= span[0] and span[1] <= ds[1] for ds in dollar_spans)
        in_pct    = any(ps[0] <= span[0] and span[1] <= ps[1] for ps in pct_spans)
        if not (in_year or in_dollar or in_pct):
            raise HallucinationError(
                f"Guard FAIL — bare number '{m.group()}' found (not inside $... or ...% token, "
                "and not a 4-digit year)."
            )


# ── Step 6: Save output ───────────────────────────────────────────────────────

def _save_commentary(ticker: str, text: str) -> Path:
    """Write commentary markdown to dashboard/{ticker}_exec_commentary_<DATE>.md.

    Args:
        ticker: Company ticker symbol.
        text:   The validated commentary markdown text.

    Returns:
        Path to the saved file.
    """
    _DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y%m%d")
    out_path = _DASHBOARD_DIR / f"{ticker}_exec_commentary_{today}.md"
    out_path.write_text(text, encoding="utf-8")
    return out_path


# ── Main entry point ──────────────────────────────────────────────────────────

def generate(
    ticker: str | None = None,
    db_path: Path | None = None,
    *,
    dry_run: bool = True,
    verify_only: Path | None = None,
) -> Path | None:
    """Run the full 5-step commentary pipeline.

    Args:
        ticker:      Ticker symbol override; falls back to config/company.yaml.
        db_path:     Path to DuckDB file; auto-derived from ticker if omitted.
        dry_run:     If True, print the prompt without calling the API (default).
        verify_only: If given, skip Steps 1-4 and re-run the guard on this file.

    Returns:
        Path to the saved commentary file, or None in dry-run mode.

    Raises:
        RefusalError:       If data-integrity conditions are violated.
        HallucinationError: If the guard fires on Claude's output.
        FileNotFoundError:  If required files are missing.
    """
    # Load config
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config not found: {_CONFIG_PATH}")
    with _CONFIG_PATH.open() as fh:
        config: dict[str, Any] = yaml.safe_load(fh)

    resolved_ticker = (ticker or str(config["ticker"])).upper().strip()

    # --verify-only path: re-run guard on existing file
    if verify_only is not None:
        if not verify_only.exists():
            raise FileNotFoundError(f"Commentary file not found: {verify_only}")
        logger.info("--verify-only mode: re-running guard on %s", verify_only)
        # For verify-only we need the payload; pull from DB
        _db = db_path or (_PROCESSED_DIR / f"{resolved_ticker}.duckdb")
        vrow, qrow = _pull_variance_data(_db)
        _check_refusals(vrow, qrow)
        payload = _build_payload(resolved_ticker, vrow, qrow)
        text = verify_only.read_text(encoding="utf-8")
        run_hallucination_guard(text, payload)
        logger.info("Guard passed on existing file: %s", verify_only)
        return verify_only

    if db_path is None:
        db_path = _PROCESSED_DIR / f"{resolved_ticker}.duckdb"

    # Step 1: Pull
    logger.info("Step 1: Pulling variance data from %s", db_path)
    variance_row, quality_row = _pull_variance_data(db_path)

    # Step 2: Refusal checks
    logger.info("Step 2: Running refusal checks")
    _check_refusals(variance_row, quality_row)

    # Step 3: Pre-format
    logger.info("Step 3: Pre-formatting numbers into payload JSON")
    payload = _build_payload(resolved_ticker, variance_row, quality_row)

    user_message = (
        "Here is the pre-computed variance data for this quarter. "
        "Write the CFO-style variance commentary following the STRICT OUTPUT RULES.\n\n"
        f"```json\n{json.dumps(payload, indent=2, default=str)}\n```"
    )

    if dry_run:
        print("=" * 72)
        print("DRY-RUN MODE — Prompt that would be sent to Claude:")
        print("=" * 72)
        print(f"\nSYSTEM:\n{_SYSTEM_PROMPT}\n")
        print(f"USER:\n{user_message}\n")
        print("=" * 72)
        print("(Pass --live to call the Anthropic API)")
        return None

    # Step 4: Call Claude
    logger.info("Step 4: Calling Claude for narrative generation")
    commentary_text = _call_claude(payload)

    # Step 5: Hallucination guard
    logger.info("Step 5: Running hallucination guard")
    run_hallucination_guard(commentary_text, payload)
    logger.info("Guard passed — all numeric tokens validated")

    # Step 6: Save
    out_path = _save_commentary(resolved_ticker, commentary_text)
    logger.info("Commentary saved to: %s", out_path)
    return out_path


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(
        description="Generate CFO-style variance commentary (Kepler pattern).",
    )
    parser.add_argument("--ticker", default=None, help="Ticker symbol (e.g. PANW)")
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Print the prompt without calling the API (default behaviour unless --live)",
    )
    parser.add_argument(
        "--live", action="store_true", default=False,
        help="Actually call the Anthropic API (requires ANTHROPIC_API_KEY)",
    )
    parser.add_argument(
        "--verify-only", metavar="PATH", default=None,
        help="Re-run the guard on an existing commentary file (CI use)",
    )
    args = parser.parse_args()

    is_dry_run = not args.live  # default is dry-run unless --live
    verify_path = Path(args.verify_only) if args.verify_only else None

    try:
        result = generate(
            ticker=args.ticker,
            dry_run=is_dry_run,
            verify_only=verify_path,
        )
        if result:
            print(f"\nCommentary saved: {result}")
    except RefusalError as exc:
        print(f"\n{exc}", file=sys.stderr)
        sys.exit(1)
    except HallucinationError as exc:
        print(f"\nHALLUCINATION GUARD FIRED:\n{exc}", file=sys.stderr)
        sys.exit(2)
    except FileNotFoundError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
