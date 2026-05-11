"""Root conftest for gate-keeper's test suite.

Hermetic LLM isolation
----------------------
By default every test runs with the llm_rubric dotenv loader stubbed out so
that a developer's local credential file (``DOTENV_PATH``) is never read.
This makes ``uv run pytest`` deterministic regardless of host configuration.

Tests that intentionally exercise a real LLM provider must be decorated with
``@pytest.mark.live_llm``.  They are skipped automatically unless both
conditions are met:

1. The environment variable ``GATE_KEEPER_RUN_LIVE_LLM_TESTS=1`` is set.
2. The required provider keys are present in the project dotenv file.

Run live tests explicitly::

    GATE_KEEPER_RUN_LIVE_LLM_TESTS=1 uv run pytest -m live_llm
"""

from __future__ import annotations

import os

import pytest

from gate_keeper.backends import llm_rubric as _llm_backend

# ---------------------------------------------------------------------------
# Hermetic dotenv fixture (autouse — applies to every non-live_llm test)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def disable_llm_dotenv(request, monkeypatch):
    """Stub the llm_rubric dotenv loader to return {} by default.

    Tests marked ``live_llm`` or ``dotenv_loader`` skip this fixture so they
    get the real ``_load_env_file`` implementation.  Any other test has the
    function patched to return an empty dict, ensuring no host credential file
    can bleed into the test run.
    """
    if request.node.get_closest_marker("live_llm"):
        # Live tests: do not patch — let the real loader run.
        return
    if request.node.get_closest_marker("dotenv_loader"):
        # Tests that exercise the loader implementation itself: do not patch.
        return
    monkeypatch.setattr(_llm_backend, "_load_env_file", lambda *a, **kw: {})


# ---------------------------------------------------------------------------
# live_llm marker: skip unless opt-in flag + dotenv keys are present
# ---------------------------------------------------------------------------


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_llm: mark test as a live-provider integration test; "
        "skipped unless GATE_KEEPER_RUN_LIVE_LLM_TESTS=1 and required dotenv keys are present",
    )
    config.addinivalue_line(
        "markers",
        "dotenv_loader: mark test as directly exercising the _load_env_file implementation; "
        "bypasses the default hermetic dotenv stub",
    )


def pytest_collection_modifyitems(config, items):
    """Skip live_llm tests unless the opt-in flag is set and credentials exist."""
    run_live = os.environ.get("GATE_KEEPER_RUN_LIVE_LLM_TESTS", "").strip() == "1"

    if not run_live:
        skip_no_flag = pytest.mark.skip(
            reason="live LLM tests disabled; set GATE_KEEPER_RUN_LIVE_LLM_TESTS=1 and run with -m live_llm"
        )
        for item in items:
            if item.get_closest_marker("live_llm"):
                item.add_marker(skip_no_flag)
        return

    # Flag is set — still skip if required dotenv keys are absent.
    env = _llm_backend._load_env_file()
    if not _llm_backend._is_configured(env):
        skip_no_creds = pytest.mark.skip(
            reason=(
                "live LLM tests skipped: GATE_KEEPER_RUN_LIVE_LLM_TESTS=1 "
                "but provider not configured in dotenv"
            )
        )
        for item in items:
            if item.get_closest_marker("live_llm"):
                item.add_marker(skip_no_creds)
