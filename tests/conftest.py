"""Shared pytest fixtures for the ai-financial-analyst test suite."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _sec_user_agent_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a valid SEC_USER_AGENT for any test that triggers EDGAR HTTP code paths.

    The real ``_user_agent()`` helpers refuse to send a request when the env var
    is unset or equal to the .env.example placeholder. Tests use the ``responses``
    library to mock the network, but the User-Agent helper still runs first — so
    we set a deterministic, clearly-fake address that no test should ever assert
    against.
    """
    if not os.environ.get("SEC_USER_AGENT"):
        monkeypatch.setenv("SEC_USER_AGENT", "ai-fin-analyst-tests/0.0 tests@invalid.test")


@pytest.fixture(autouse=True)
def _clear_select_models_cache() -> None:
    """Drop the ``select_models()`` lru_cache between tests.

    Without this, a test that triggers /v1/models discovery pins the cached
    result for every subsequent test in the run (including tests that mock
    the SDK to expect a specific outcome).
    """
    try:
        from src.select_models import select_models  # noqa: PLC0415

        select_models.cache_clear()
    except ImportError:
        pass
