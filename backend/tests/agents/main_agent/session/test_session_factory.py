"""
Tests for SessionFactory — creates the correct session manager type based on
session ID and environment.

Requirements: 15.1–15.4
"""
import os
from unittest.mock import patch, MagicMock, call

import pytest

from agents.main_agent.session.preview_session_manager import PreviewSessionManager


# ---------------------------------------------------------------------------
# Req 15.1 — Preview session creation
# ---------------------------------------------------------------------------

class TestPreviewSessionCreation:
    """Verify that preview session IDs produce a PreviewSessionManager."""

    def test_preview_prefix_returns_preview_manager(self):
        """Req 15.1: session_id starting with 'preview-' → PreviewSessionManager."""
        from agents.main_agent.session.session_factory import SessionFactory

        result = SessionFactory.create_session_manager(
            session_id="preview-test-123",
            user_id="user-1",
        )
        assert isinstance(result, PreviewSessionManager)

    def test_preview_manager_has_correct_ids(self):
        """Req 15.1: Returned PreviewSessionManager stores session_id and user_id."""
        from agents.main_agent.session.session_factory import SessionFactory

        result = SessionFactory.create_session_manager(
            session_id="preview-abc",
            user_id="user-42",
        )
        assert result.session_id == "preview-abc"
        assert result.user_id == "user-42"


# ---------------------------------------------------------------------------
# Req 15.2 — Cloud session creation with memory available
# ---------------------------------------------------------------------------

class TestCloudSessionCreation:
    """Verify that non-preview sessions with AgentCore Memory produce TurnBasedSessionManager."""

    @patch("agents.main_agent.session.session_factory.AGENTCORE_MEMORY_AVAILABLE", True)
    @patch("agents.main_agent.session.session_factory.load_memory_config")
    @patch("agents.main_agent.session.session_factory.SessionFactory._create_cloud_session_manager")
    def test_cloud_session_returns_turn_based(self, mock_create_cloud, mock_load_config):
        """Req 15.2: Non-preview session_id + memory available → TurnBasedSessionManager."""
        from agents.main_agent.session.session_factory import SessionFactory

        mock_config = MagicMock()
        mock_config.memory_id = "mem-123"
        mock_config.region = "us-east-1"
        mock_load_config.return_value = mock_config

        sentinel = MagicMock()
        sentinel.__class__.__name__ = "TurnBasedSessionManager"
        mock_create_cloud.return_value = sentinel

        result = SessionFactory.create_session_manager(
            session_id="session-real-456",
            user_id="user-1",
        )

        assert result is sentinel
        mock_load_config.assert_called_once()
        mock_create_cloud.assert_called_once_with(
            memory_id="mem-123",
            session_id="session-real-456",
            user_id="user-1",
            aws_region="us-east-1",
            caching_enabled=True,
            compaction_enabled=None,
            compaction_threshold=None,
        )

    @patch("agents.main_agent.session.session_factory.AGENTCORE_MEMORY_AVAILABLE", True)
    @patch("agents.main_agent.session.session_factory.load_memory_config")
    @patch("agents.main_agent.session.session_factory.SessionFactory._create_cloud_session_manager")
    def test_cloud_session_passes_compaction_overrides(self, mock_create_cloud, mock_load_config):
        """Req 15.2: Compaction overrides are forwarded to _create_cloud_session_manager."""
        from agents.main_agent.session.session_factory import SessionFactory

        mock_config = MagicMock()
        mock_config.memory_id = "mem-999"
        mock_config.region = "us-west-2"
        mock_load_config.return_value = mock_config
        mock_create_cloud.return_value = MagicMock()

        SessionFactory.create_session_manager(
            session_id="session-xyz",
            user_id="user-2",
            caching_enabled=False,
            compaction_enabled=True,
            compaction_threshold=50000,
        )

        mock_create_cloud.assert_called_once_with(
            memory_id="mem-999",
            session_id="session-xyz",
            user_id="user-2",
            aws_region="us-west-2",
            caching_enabled=False,
            compaction_enabled=True,
            compaction_threshold=50000,
        )


# ---------------------------------------------------------------------------
# Req 15.3 — RuntimeError when memory unavailable
# ---------------------------------------------------------------------------

class TestMemoryUnavailable:
    """Verify RuntimeError when AgentCore Memory is not installed."""

    @patch("agents.main_agent.session.session_factory.AGENTCORE_MEMORY_AVAILABLE", False)
    def test_raises_runtime_error_for_non_preview(self):
        """Req 15.3: Non-preview session without memory package → RuntimeError."""
        from agents.main_agent.session.session_factory import SessionFactory

        with pytest.raises(RuntimeError, match="bedrock_agentcore package is required"):
            SessionFactory.create_session_manager(
                session_id="session-no-memory",
                user_id="user-1",
            )

    @patch("agents.main_agent.session.session_factory.AGENTCORE_MEMORY_AVAILABLE", False)
    def test_preview_still_works_without_memory(self):
        """Req 15.3: Preview sessions work even without memory package."""
        from agents.main_agent.session.session_factory import SessionFactory

        result = SessionFactory.create_session_manager(
            session_id="preview-ok",
            user_id="user-1",
        )
        assert isinstance(result, PreviewSessionManager)


# ---------------------------------------------------------------------------
# Req 15.4 — is_cloud_mode
# ---------------------------------------------------------------------------

class TestIsCloudMode:
    """Verify is_cloud_mode reflects memory availability and configuration."""

    @patch("agents.main_agent.session.session_factory.AGENTCORE_MEMORY_AVAILABLE", True)
    @patch("agents.main_agent.session.session_factory.load_memory_config")
    def test_returns_true_when_memory_available_and_configured(self, mock_load_config):
        """Req 15.4: Memory available + AGENTCORE_MEMORY_ID set → True."""
        from agents.main_agent.session.session_factory import SessionFactory

        mock_load_config.return_value = MagicMock()

        assert SessionFactory.is_cloud_mode() is True

    @patch("agents.main_agent.session.session_factory.AGENTCORE_MEMORY_AVAILABLE", False)
    def test_returns_false_when_memory_not_available(self):
        """Req 15.4: Memory package not installed → False."""
        from agents.main_agent.session.session_factory import SessionFactory

        assert SessionFactory.is_cloud_mode() is False

    @patch("agents.main_agent.session.session_factory.AGENTCORE_MEMORY_AVAILABLE", True)
    @patch("agents.main_agent.session.session_factory.load_memory_config", side_effect=RuntimeError("no memory id"))
    def test_returns_false_when_config_missing(self, mock_load_config):
        """Req 15.4: Memory available but AGENTCORE_MEMORY_ID not set → False."""
        from agents.main_agent.session.session_factory import SessionFactory

        assert SessionFactory.is_cloud_mode() is False


# ---------------------------------------------------------------------------
# Retrieval threshold env vars (AGENTCORE_MEMORY_RELEVANCE_SCORE / TOP_K)
# ---------------------------------------------------------------------------

class TestRetrievalThresholdEnvVars:
    """Verify that _create_cloud_session_manager reads retrieval threshold env vars."""

    @patch("agents.main_agent.session.session_factory.AGENTCORE_MEMORY_AVAILABLE", True)
    @patch("agents.main_agent.session.session_factory._discover_strategy_ids")
    @patch("agents.main_agent.session.session_factory.AgentCoreMemoryConfig")
    @patch("agents.main_agent.session.session_factory.RetrievalConfig")
    @patch("agents.main_agent.session.turn_based_session_manager.TurnBasedSessionManager", create=True)
    def test_uses_default_thresholds(
        self, mock_tbsm, mock_retrieval, mock_mem_config, mock_discover, monkeypatch
    ):
        """Default relevance_score=0.7 and top_k=10 when env vars not set."""
        from agents.main_agent.session.session_factory import SessionFactory

        monkeypatch.delenv("AGENTCORE_MEMORY_RELEVANCE_SCORE", raising=False)
        monkeypatch.delenv("AGENTCORE_MEMORY_TOP_K", raising=False)
        mock_discover.return_value = ("semantic-1", "pref-1", "sum-1")
        mock_tbsm.return_value = MagicMock()

        SessionFactory._create_cloud_session_manager(
            memory_id="mem-1", session_id="s-1", user_id="u-1",
            aws_region="us-west-2", caching_enabled=True,
        )

        assert mock_retrieval.call_count == 3
        for c in mock_retrieval.call_args_list:
            assert c == call(top_k=10, relevance_score=0.7)

    @patch("agents.main_agent.session.session_factory.AGENTCORE_MEMORY_AVAILABLE", True)
    @patch("agents.main_agent.session.session_factory._discover_strategy_ids")
    @patch("agents.main_agent.session.session_factory.AgentCoreMemoryConfig")
    @patch("agents.main_agent.session.session_factory.RetrievalConfig")
    @patch("agents.main_agent.session.turn_based_session_manager.TurnBasedSessionManager", create=True)
    def test_reads_custom_thresholds_from_env(
        self, mock_tbsm, mock_retrieval, mock_mem_config, mock_discover, monkeypatch
    ):
        """Custom env vars are passed to RetrievalConfig."""
        from agents.main_agent.session.session_factory import SessionFactory

        monkeypatch.setenv("AGENTCORE_MEMORY_RELEVANCE_SCORE", "0.85")
        monkeypatch.setenv("AGENTCORE_MEMORY_TOP_K", "20")
        mock_discover.return_value = ("semantic-1", "pref-1", "sum-1")
        mock_tbsm.return_value = MagicMock()

        SessionFactory._create_cloud_session_manager(
            memory_id="mem-1", session_id="s-1", user_id="u-1",
            aws_region="us-west-2", caching_enabled=True,
        )

        assert mock_retrieval.call_count == 3
        for c in mock_retrieval.call_args_list:
            assert c == call(top_k=20, relevance_score=0.85)
