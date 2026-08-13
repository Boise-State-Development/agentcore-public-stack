"""AgentCore Memory writes belong off the asyncio event loop.

``batch_size`` is 1, so every message appended during a turn — the user message,
each assistant message, each tool result — fired a synchronous boto3
``create_event`` plus a ``sync_agent`` from inside the SSE stream generator,
blocking the event loop for the whole container. Every other boto3 caller on the
chat path already offloads with ``asyncio.to_thread``; session persistence was
the last blocking one.

``async_mode`` (bedrock-agentcore 1.21.0) wraps exactly those calls. The risk is
not that it fails loudly — it is that it silently persists *less*: async mode
replaces the base ``register_hooks`` wholesale rather than decorating it, so a
future SDK that adds an event to the sync path and forgets the async path would
drop those writes with no error. ``TestAsyncModeSdkContract`` is the guard, and
it binds against the installed SDK because a stub cannot notice that drift.
"""

from unittest.mock import MagicMock, patch

from agents.main_agent.session.session_factory import (
    SESSION_ASYNC_PERSISTENCE_ENABLED_ENV,
    session_async_persistence_enabled,
)


class TestKillSwitch:
    """Default ON, disabled only by the literal string "false"."""

    def test_defaults_on_when_unset(self, monkeypatch):
        monkeypatch.delenv(SESSION_ASYNC_PERSISTENCE_ENABLED_ENV, raising=False)
        assert session_async_persistence_enabled() is True

    def test_empty_string_stays_on(self, monkeypatch):
        """A workflow env var can materialize as "" — that must not disable it."""
        monkeypatch.setenv(SESSION_ASYNC_PERSISTENCE_ENABLED_ENV, "")
        assert session_async_persistence_enabled() is True

    def test_false_disables(self, monkeypatch):
        monkeypatch.setenv(SESSION_ASYNC_PERSISTENCE_ENABLED_ENV, "false")
        assert session_async_persistence_enabled() is False

    def test_false_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv(SESSION_ASYNC_PERSISTENCE_ENABLED_ENV, "FALSE")
        assert session_async_persistence_enabled() is False

    def test_other_values_stay_on(self, monkeypatch):
        monkeypatch.setenv(SESSION_ASYNC_PERSISTENCE_ENABLED_ENV, "true")
        assert session_async_persistence_enabled() is True


class TestFactoryWiring:
    """The flag has to actually reach AgentCoreMemoryConfig."""

    @patch("agents.main_agent.session.session_factory.AGENTCORE_MEMORY_AVAILABLE", True)
    @patch("agents.main_agent.session.session_factory._discover_strategy_ids")
    @patch("agents.main_agent.session.session_factory.AgentCoreMemoryConfig")
    @patch("agents.main_agent.session.session_factory.RetrievalConfig")
    @patch("agents.main_agent.session.turn_based_session_manager.TurnBasedSessionManager", create=True)
    def test_async_mode_on_by_default(
        self, mock_tbsm, mock_retrieval, mock_mem_config, mock_discover, monkeypatch
    ):
        from agents.main_agent.session.session_factory import SessionFactory

        monkeypatch.delenv(SESSION_ASYNC_PERSISTENCE_ENABLED_ENV, raising=False)
        mock_discover.return_value = ("semantic-1", "pref-1", "sum-1")
        mock_tbsm.return_value = MagicMock()

        SessionFactory._create_cloud_session_manager(
            memory_id="mem-1", session_id="s-1", user_id="u-1",
            aws_region="us-west-2", caching_enabled=True,
        )

        assert mock_mem_config.call_args.kwargs["async_mode"] is True

    @patch("agents.main_agent.session.session_factory.AGENTCORE_MEMORY_AVAILABLE", True)
    @patch("agents.main_agent.session.session_factory._discover_strategy_ids")
    @patch("agents.main_agent.session.session_factory.AgentCoreMemoryConfig")
    @patch("agents.main_agent.session.session_factory.RetrievalConfig")
    @patch("agents.main_agent.session.turn_based_session_manager.TurnBasedSessionManager", create=True)
    def test_kill_switch_reaches_the_config(
        self, mock_tbsm, mock_retrieval, mock_mem_config, mock_discover, monkeypatch
    ):
        """The revert lever has to work without a redeploy."""
        from agents.main_agent.session.session_factory import SessionFactory

        monkeypatch.setenv(SESSION_ASYNC_PERSISTENCE_ENABLED_ENV, "false")
        mock_discover.return_value = ("semantic-1", "pref-1", "sum-1")
        mock_tbsm.return_value = MagicMock()

        SessionFactory._create_cloud_session_manager(
            memory_id="mem-1", session_id="s-1", user_id="u-1",
            aws_region="us-west-2", caching_enabled=True,
        )

        assert mock_mem_config.call_args.kwargs["async_mode"] is False


class TestAsyncModeSdkContract:
    """Bind against the installed SDK, not a stub."""

    def test_config_accepts_async_mode(self):
        from bedrock_agentcore.memory.integrations.strands.config import (
            AgentCoreMemoryConfig,
        )

        config = AgentCoreMemoryConfig(
            memory_id="mem-1", session_id="s-1", actor_id="u-1", async_mode=True
        )
        assert config.async_mode is True

    def test_defaults_to_sync_in_the_sdk(self):
        """Our factory is the only thing turning this on."""
        from bedrock_agentcore.memory.integrations.strands.config import (
            AgentCoreMemoryConfig,
        )

        config = AgentCoreMemoryConfig(memory_id="mem-1", session_id="s-1", actor_id="u-1")
        assert config.async_mode is False

    def test_async_hooks_cover_every_event_the_sync_path_covers(self):
        """The failure mode is silent under-persistence, not an exception.

        Async mode replaces the base ``register_hooks`` wholesale. If a future
        bedrock-agentcore adds an event to the sync path and forgets the async
        path, the writes behind it vanish with no error anywhere. Compare the
        two registrations directly rather than trusting they stay in step.
        """
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )

        def _events_registered(async_mode: bool) -> set:
            manager = AgentCoreMemorySessionManager.__new__(AgentCoreMemorySessionManager)
            manager.config = MagicMock()
            manager.config.async_mode = async_mode
            # batch_size > 1 so the batching callback registers on both paths.
            manager.config.batch_size = 2

            registry = MagicMock()
            seen = set()
            registry.add_callback.side_effect = lambda event, _cb: seen.add(event)

            AgentCoreMemorySessionManager.register_hooks(manager, registry)
            return seen

        sync_events = _events_registered(async_mode=False)
        async_events = _events_registered(async_mode=True)

        # Sync mode delegates its core registrations to RepositorySessionManager,
        # which the MagicMock registry above also records, so the two sets are
        # directly comparable.
        missing = sync_events - async_events
        assert not missing, f"async_mode drops hooks the sync path registers: {missing}"
