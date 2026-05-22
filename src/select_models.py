"""Runtime model selection for the AI Financial Analyst pipeline.

Queries Anthropic's ``/v1/models`` endpoint at process startup, picks the
highest-generation opus-class model as *planner* and sonnet-class as
*narrator*, and caches the result for the process lifetime.

Fallback behaviour
------------------
Any failure during live discovery (network error, API key missing, no
matching family found) causes a transparent fall-through to the documented
snapshot IDs in ``/config/model_selection.yaml``.  Those IDs are
intentionally stale — the live path is primary.

Usage::

    from src.select_models import select_models

    models = select_models()
    # {"planner": "claude-opus-4-6-20260101", "narrator": "claude-sonnet-4-6-20260101"}
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import anthropic
import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "model_selection.yaml"


def _parse_version(model_id: str) -> tuple[int, ...]:
    """Extract a comparable version tuple from a model-ID string.

    Numeric segments are extracted left-to-right, so later/higher model IDs
    sort greater regardless of naming convention.

    Examples::

        >>> _parse_version("claude-opus-4-6-20260101")
        (4, 6, 20260101)
        >>> _parse_version("claude-sonnet-4-5")
        (4, 5)
    """
    return tuple(int(part) for part in re.findall(r"\d+", model_id))


def _best_match(model_ids: list[str], families: list[str]) -> str | None:
    """Return the highest-generation model ID matching any of *families*.

    Args:
        model_ids: All model IDs available for the current API key.
        families:  Case-insensitive family substrings, e.g. ``["opus"]``.

    Returns:
        The matching model ID with the highest version, or ``None``.
    """
    candidates = [mid for mid in model_ids if any(f.lower() in mid.lower() for f in families)]
    if not candidates:
        return None
    return max(candidates, key=_parse_version)


@lru_cache(maxsize=1)
def select_models() -> dict[str, str]:
    """Resolve ``{planner: snapshot_id, narrator: snapshot_id}`` at runtime.

    Calls ``/v1/models`` to enumerate models available to the current API
    key, then applies the family/generation policy from
    ``/config/model_selection.yaml``.  Falls back to that file's documented
    snapshot IDs only on *network or API* failures — policy-parsing errors
    propagate so real bugs aren't silently masked.

    The result is cached for the lifetime of the current process. Tests that
    need a fresh resolution should call ``select_models.cache_clear()``.

    Returns:
        Dict with ``"planner"`` and ``"narrator"`` keys, each a valid
        Anthropic snapshot-ID string.
    """
    # Policy parsing failures (missing keys, bad YAML) are real bugs — let them raise.
    with _CONFIG_PATH.open() as fh:
        policy: dict[str, Any] = yaml.safe_load(fh)
    fallback: dict[str, str] = policy["fallback"]
    planner_families: list[str] = policy["selection"]["planner"]["prefer_family"]
    narrator_families: list[str] = policy["selection"]["narrator"]["prefer_family"]

    try:
        client = anthropic.Anthropic()
        # client.models.list() returns a paginated SyncPage; list() exhausts it.
        all_models: list[Any] = list(client.models.list())
        model_ids: list[str] = [m.id for m in all_models if isinstance(getattr(m, "id", None), str)]

        planner = _best_match(model_ids, planner_families)
        narrator = _best_match(model_ids, narrator_families)

        if planner and narrator:
            result: dict[str, str] = {"planner": planner, "narrator": narrator}
            logger.info("Models resolved via /v1/models: %s", result)
            return result

        logger.warning(
            "Model discovery returned incomplete results "
            "(planner=%s narrator=%s); falling back to policy IDs.",
            planner,
            narrator,
        )

    except anthropic.AnthropicError as exc:
        # Network, auth, rate-limit, or any other SDK-surface failure → fallback.
        logger.warning("Model discovery failed (%s); using policy fallback IDs.", exc)

    result = {"planner": fallback["planner"], "narrator": fallback["narrator"]}
    logger.info("Using fallback model IDs: %s", result)
    return result
