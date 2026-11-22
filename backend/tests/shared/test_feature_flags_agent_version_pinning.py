"""`AGENT_VERSION_PINNING_ENABLED` is opt-in while in development and needs saved workflows (workflow-designer §23)."""

import pytest

from apis.shared.feature_flags import agent_version_pinning_enabled

_CHAIN = ("HANDOFFS_ENABLED", "WORKFLOW_RUNS_ENABLED", "WORKFLOWS_ENABLED")


@pytest.fixture
def app_api_chain(monkeypatch):
    """app-api's environment: every flag pinning needs, on."""
    for name in _CHAIN:
        monkeypatch.setenv(name, "true")


@pytest.mark.parametrize("value", ["true", "TRUE", " True "])
def test_only_true_enables(monkeypatch, app_api_chain, value):
    monkeypatch.setenv("AGENT_VERSION_PINNING_ENABLED", value)
    assert agent_version_pinning_enabled()


@pytest.mark.parametrize("value", ["", "false", "yes", "1", "on"])
def test_anything_else_is_off(monkeypatch, app_api_chain, value):
    monkeypatch.setenv("AGENT_VERSION_PINNING_ENABLED", value)
    assert not agent_version_pinning_enabled()


def test_unset_is_off(monkeypatch, app_api_chain):
    monkeypatch.delenv("AGENT_VERSION_PINNING_ENABLED", raising=False)
    assert not agent_version_pinning_enabled()


@pytest.mark.parametrize("off", _CHAIN)
def test_on_app_api_it_needs_saved_workflows_and_their_chain(monkeypatch, app_api_chain, off):
    monkeypatch.setenv("AGENT_VERSION_PINNING_ENABLED", "true")
    monkeypatch.setenv(off, "")
    assert not agent_version_pinning_enabled()


def test_on_the_runtime_its_own_copy_carries_the_chain(monkeypatch):
    """The Runtime has HANDOFFS_ENABLED and this flag, but no copy of the workflow flags."""
    monkeypatch.setenv("HANDOFFS_ENABLED", "true")
    monkeypatch.delenv("WORKFLOW_RUNS_ENABLED", raising=False)
    monkeypatch.delenv("WORKFLOWS_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_VERSION_PINNING_ENABLED", "true")
    assert agent_version_pinning_enabled()

    monkeypatch.setenv("HANDOFFS_ENABLED", "")
    assert not agent_version_pinning_enabled()
