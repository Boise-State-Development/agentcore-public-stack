"""`mcp_parallel_preflight_enabled`: default ON, `false` is the kill switch."""

import pytest

from apis.shared.feature_flags import mcp_parallel_preflight_enabled


@pytest.mark.parametrize("value", [None, "", "true", "on", "nonsense"])
def test_everything_but_false_is_on(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("MCP_PARALLEL_PREFLIGHT_ENABLED", raising=False)
    else:
        monkeypatch.setenv("MCP_PARALLEL_PREFLIGHT_ENABLED", value)

    assert mcp_parallel_preflight_enabled()


@pytest.mark.parametrize("value", ["false", "FALSE", " False "])
def test_false_is_off(monkeypatch, value):
    monkeypatch.setenv("MCP_PARALLEL_PREFLIGHT_ENABLED", value)
    assert not mcp_parallel_preflight_enabled()
