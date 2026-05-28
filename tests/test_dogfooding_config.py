"""Tests for the centralised dogfooding workflow configuration (#266).

The file ``.github/dogfooding-config.env`` carries the non-secret model
value the dogfooding workflow sources into its credential-projection
step. The workflow's strict-mode guard fails fast if
``GATE_KEEPER_OPENAI_MODEL`` is unset/blank, but a unit test gives us a
PR-time signal as well so a typo or accidental deletion is caught
without waiting for a workflow run.

These tests are deliberately lightweight: they verify the tracked file
is parseable, carries the expected key with a non-blank value, and that
the workflow YAML actually references the file. They are not a
full workflow integration test (that lives in the dogfooding job
itself).
"""

from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / ".github" / "dogfooding-config.env"
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "dogfooding.yml"


class TestDogfoodingConfigFile:
    def test_config_file_exists(self):
        assert _CONFIG_PATH.is_file(), (
            f"{_CONFIG_PATH} is missing — the dogfooding workflow sources it "
            "for GATE_KEEPER_OPENAI_MODEL (#266)."
        )

    def test_config_file_carries_openai_model_key(self):
        values = dotenv_values(_CONFIG_PATH)
        assert "GATE_KEEPER_OPENAI_MODEL" in values

    def test_config_file_openai_model_value_is_non_blank(self):
        """The value must be present and non-blank: blank would defeat the
        purpose of centralising it (the workflow's strict-mode guard would
        immediately fail the run)."""
        values = dotenv_values(_CONFIG_PATH)
        value = values.get("GATE_KEEPER_OPENAI_MODEL")
        assert value is not None and value.strip(), (
            "GATE_KEEPER_OPENAI_MODEL is blank in .github/dogfooding-config.env"
        )


class TestDogfoodingWorkflowWiring:
    """End-to-end wiring: the workflow must actually source the config file
    and project the strict-mode key into the runtime dotenv."""

    def test_workflow_sources_the_config_file(self):
        body = _WORKFLOW_PATH.read_text(encoding="utf-8")
        assert ". .github/dogfooding-config.env" in body, (
            "dogfooding.yml no longer sources .github/dogfooding-config.env — "
            "the model name would revert to a workflow literal (#266)."
        )

    def test_workflow_projects_model_via_interpolation(self):
        """The printf must interpolate `$GATE_KEEPER_OPENAI_MODEL` rather than
        a hard-pinned literal. A regression would re-introduce the very state
        #266 was opened to remove."""
        body = _WORKFLOW_PATH.read_text(encoding="utf-8")
        # The %s slot is fed from "$GATE_KEEPER_OPENAI_MODEL" in the same
        # block; verify both pieces are present.
        assert "GATE_KEEPER_OPENAI_MODEL=%s" in body
        assert '"$GATE_KEEPER_OPENAI_MODEL"' in body

    def test_workflow_projects_strict_mode_key(self):
        """`GATE_KEEPER_REQUIRE_MODEL=1` must be projected so the backend
        fails closed on a blank/missing model rather than silently falling
        back (#266)."""
        body = _WORKFLOW_PATH.read_text(encoding="utf-8")
        assert "GATE_KEEPER_REQUIRE_MODEL=1" in body

    def test_workflow_has_blank_model_guard(self):
        """The credentials step must fail fast on a blank model value."""
        body = _WORKFLOW_PATH.read_text(encoding="utf-8")
        # Match the actual guard introduced by #266; do not pin the exact
        # wording of the error message — just verify the guard structure.
        assert 'if [ -z "${GATE_KEEPER_OPENAI_MODEL:-}" ]' in body
