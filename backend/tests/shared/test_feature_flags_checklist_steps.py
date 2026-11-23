"""`CHECKLIST_STEPS_ENABLED` is opt-in while in development and needs prompt steps (workflow-designer §22)."""

import pytest

from apis.shared.feature_flags import checklist_steps_enabled

_CHAIN = ("HANDOFFS_ENABLED", "WORKFLOW_RUNS_ENABLED", "WORKFLOWS_ENABLED", "PROMPT_STEPS_ENABLED")


@pytest.fixture
def app_api_chain(monkeypatch):
    """app-api's environment: every flag checklist steps need, on."""
    for name in _CHAIN:
        monkeypatch.setenv(name, "true")


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_only_true_enables(monkeypatch, app_api_chain, value):
    monkeypatch.setenv("CHECKLIST_STEPS_ENABLED", value)
    assert checklist_steps_enabled()


@pytest.mark.parametrize("value", ["", "false", "yes", "1", "on"])
def test_anything_else_is_off(monkeypatch, app_api_chain, value):
    monkeypatch.setenv("CHECKLIST_STEPS_ENABLED", value)
    assert not checklist_steps_enabled()


def test_unset_is_off(monkeypatch, app_api_chain):
    monkeypatch.delenv("CHECKLIST_STEPS_ENABLED", raising=False)
    assert not checklist_steps_enabled()


@pytest.mark.parametrize("off", _CHAIN)
def test_on_app_api_it_needs_prompt_steps_and_their_chain(monkeypatch, app_api_chain, off):
    monkeypatch.setenv("CHECKLIST_STEPS_ENABLED", "true")
    monkeypatch.setenv(off, "")
    assert not checklist_steps_enabled()


def test_on_the_runtime_the_prompt_steps_copy_carries_the_chain(monkeypatch):
    """The Runtime has HANDOFFS_ENABLED and PROMPT_STEPS_ENABLED but no copy of the workflow flags."""
    monkeypatch.setenv("HANDOFFS_ENABLED", "true")
    monkeypatch.delenv("WORKFLOW_RUNS_ENABLED", raising=False)
    monkeypatch.delenv("WORKFLOWS_ENABLED", raising=False)
    monkeypatch.setenv("PROMPT_STEPS_ENABLED", "true")
    monkeypatch.setenv("CHECKLIST_STEPS_ENABLED", "true")
    assert checklist_steps_enabled()

    monkeypatch.setenv("PROMPT_STEPS_ENABLED", "")
    assert not checklist_steps_enabled()
