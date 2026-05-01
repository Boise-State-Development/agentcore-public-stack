"""Smoke tests to verify shared fixtures load and have the expected shape."""

from unittest.mock import MagicMock

from agents.main_agent.core.model_config import ModelConfig, ModelProvider, RetryConfig
from agents.main_agent.tools.tool_registry import ToolRegistry
from agents.main_agent.tools.tool_filter import ToolFilter
from agents.main_agent.session.preview_session_manager import PreviewSessionManager


def test_model_config_fixture(model_config):
    assert isinstance(model_config, ModelConfig)
    assert model_config.provider == ModelProvider.BEDROCK
    assert model_config.inference_params == {}
    assert model_config.caching_enabled is True


def test_retry_config_fixture(retry_config):
    assert isinstance(retry_config, RetryConfig)
    assert retry_config.boto_max_attempts == 3
    assert retry_config.sdk_max_attempts == 4
    assert retry_config.sdk_initial_delay <= retry_config.sdk_max_delay


def test_tool_registry_fixture(tool_registry):
    assert isinstance(tool_registry, ToolRegistry)
    assert tool_registry.get_tool_count() == 5
    assert tool_registry.has_tool("calculator")
    assert tool_registry.has_tool("weather")
    assert tool_registry.has_tool("search")
    assert tool_registry.has_tool("code_interpreter")
    assert tool_registry.has_tool("browser")


def test_tool_filter_fixture(tool_filter):
    assert isinstance(tool_filter, ToolFilter)
    # Should be backed by the same 5-tool registry
    local, gw = tool_filter.filter_tools(["calculator", "weather"])
    assert len(local) == 2
    assert len(gw) == 0


def test_preview_session_fixture(preview_session):
    assert isinstance(preview_session, PreviewSessionManager)
    assert preview_session.session_id.startswith("preview-")
    assert preview_session.message_count == 0


def test_mock_agent_fixture(mock_agent):
    assert isinstance(mock_agent, MagicMock)
    assert mock_agent.messages == []


def test_sample_files_fixture(sample_files):
    assert len(sample_files) == 3
    # image
    assert sample_files[0].filename == "photo.png"
    assert sample_files[0].content_type == "image/png"
    # document
    assert sample_files[1].filename == "report.pdf"
    assert sample_files[1].content_type == "application/pdf"
    # unsupported
    assert sample_files[2].filename == "script.py"
