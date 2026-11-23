"""`PROMPT_STEPS_ENABLED` is opt-in while in development and needs saved workflows (workflow-designer §21)."""

import pytest

from apis.shared.feature_flags import prompt_steps_enabled

_CHAIN = ("HANDOFFS_ENABLED", "WORKFLOW_RUNS_ENABLED", "WORKFLOWS_ENABLED")


@pytest.fixture
def app_api_chain(monkeypatch):
    """app-api's environment: every flag prompt steps need, on."""
    for name in _CHAIN:
        monkeypatch.setenv(name, "true")


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_only_true_enables(monkeypatch, app_api_chain, value):
    monkeypatch.setenv("PROMPT_STEPS_ENABLED", value)
    assert prompt_steps_enabled()


@pytest.mark.parametrize("value", ["", "false", "yes", "1", "on"])
def test_anything_else_is_off(monkeypatch, app_api_chain, value):
    monkeypatch.setenv("PROMPT_STEPS_ENABLED", value)
    assert not prompt_steps_enabled()


def test_unset_is_off(monkeypatch, app_api_chain):
    monkeypatch.delenv("PROMPT_STEPS_ENABLED", raising=False)
    assert not prompt_steps_enabled()


@pytest.mark.parametrize("off", _CHAIN)
def test_on_app_api_it_needs_saved_workflows(monkeypatch, app_api_chain, off):
    monkeypatch.setenv("PROMPT_STEPS_ENABLED", "true")
    monkeypatch.setenv(off, "")
    assert not prompt_steps_enabled()


def test_on_the_runtime_its_own_copy_carries_the_chain(monkeypatch):
    """The Runtime has HANDOFFS_ENABLED but no copy of the workflow flags; CDK writes this only with them on."""
    monkeypatch.setenv("HANDOFFS_ENABLED", "true")
    monkeypatch.delenv("WORKFLOW_RUNS_ENABLED", raising=False)
    monkeypatch.delenv("WORKFLOWS_ENABLED", raising=False)
    monkeypatch.setenv("PROMPT_STEPS_ENABLED", "true")
    assert prompt_steps_enabled()

    monkeypatch.setenv("HANDOFFS_ENABLED", "")
    assert not prompt_steps_enabled()
