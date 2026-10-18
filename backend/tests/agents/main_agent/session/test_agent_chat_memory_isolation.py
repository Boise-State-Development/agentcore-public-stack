"""An Agent conversation is isolated from the user's long-term memory.

Long-term memory is keyed to the user alone (``/strategies/{id}/actors/{actorId}``),
and an Agent turn persists its knowledge-base-augmented prompt. So facts from one
Agent's knowledge base were extracted under the user and retrieved into a
different Agent's chat as ``<user_context>``: an instructor's new assistant, whose
only file never mentioned an assignment, answered about it from "the context
attached to your question".

These tests build REAL ``TurnBasedSessionManager`` instances through the factory,
with only the SDK's network calls stubbed (the same fixture shape as
``test_session_factory_shared_clients.py``), so they cover the shared-client
path the Runtime actually runs on.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from strands.types.session import SessionMessage

from agents.main_agent.session import session_factory as factory
from apis.shared import aws_clients
from bedrock_agentcore.memory.integrations.strands import session_manager as sdk

AUGMENTED_PROMPT = (
    "The following excerpts were retrieved from the assistant's knowledge base.\n\n"
    "[Context 1]\nVideo Post: AI use is neutral.\n---\n"
    "User Question: Can I use AI on my assignment?"
)


@pytest.fixture
def memory_env(monkeypatch):
    monkeypatch.setenv("AGENTCORE_MEMORY_ID", "mem-test")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.delenv("AGENT_BUILD_SHARED_SESSION_ENABLED", raising=False)  # default on
    monkeypatch.setattr(
        factory, "_discover_strategy_ids", lambda memory_id, region, **kwargs: ("sem-1", "pref-1", "sum-1")
    )
    monkeypatch.setattr(sdk.AgentCoreMemorySessionManager, "read_session", lambda self, session_id, **k: None)
    monkeypatch.setattr(sdk.AgentCoreMemorySessionManager, "create_session", lambda self, session, **k: session)
    aws_clients.reset_cached_clients()
    yield
    aws_clients.reset_cached_clients()


def _build(session_id: str, *, long_term_memory: bool = True) -> Any:
    return factory.SessionFactory.create_session_manager(
        session_id=session_id, user_id="user-1", long_term_memory=long_term_memory
    )


class _RecordingRetrievalClient:
    """Answers every namespace with a fact from another Agent's knowledge base."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def retrieve_memory_records(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return {"memoryRecordSummaries": [{"content": {"text": "Video Post AI policy is neutral"}, "score": 0.9}]}


def _event(text: str) -> Any:
    return SimpleNamespace(agent=SimpleNamespace(messages=[{"role": "user", "content": [{"text": text}]}]))


def _data_plane(manager: Any) -> Any:
    """The boto3 client under the manager's view of it (the shared one)."""
    client = manager.memory_client.gmdp_client
    return getattr(client, "_client", client)


def _record_writes(monkeypatch, manager: Any) -> List[Dict[str, Any]]:
    """Every ``create_event`` that reaches the shared data-plane client."""
    writes: List[Dict[str, Any]] = []
    shared = _data_plane(manager)

    def create_event(**kwargs: Any) -> Dict[str, Any]:
        writes.append(kwargs)
        return {"event": {"eventId": f"evt-{len(writes)}"}}

    monkeypatch.setattr(shared, "create_event", create_event)
    return writes


def _write_user_message(manager: Any, text: str) -> None:
    message = SessionMessage.from_message({"role": "user", "content": [{"text": text}]}, 0)
    manager.create_message(manager.config.session_id, "default", message)


class TestRetrieval:
    def test_a_plain_chat_still_retrieves_the_users_memory(self, memory_env):
        manager = _build("session-plain")
        client = _RecordingRetrievalClient()
        manager._retrieval_client = client
        event = _event("what did we decide?")

        manager.retrieve_customer_context(event)

        assert manager.long_term_memory is True
        assert set(manager.config.retrieval_config) == {
            "/strategies/pref-1/actors/{actorId}",
            "/strategies/sem-1/actors/{actorId}",
        }
        assert client.calls
        assert event.agent.messages[-1]["content"][0]["text"].startswith("<user_context>")

    def test_an_agent_chat_gets_no_user_context(self, memory_env):
        manager = _build("session-agent", long_term_memory=False)
        client = _RecordingRetrievalClient()
        manager._retrieval_client = client
        event = _event("Can I use AI on my assignment?")

        manager.retrieve_customer_context(event)

        assert manager.config.retrieval_config == {}
        assert client.calls == []
        assert event.agent.messages[-1]["content"] == [{"text": "Can I use AI on my assignment?"}]

    def test_an_agent_chat_retrieves_nothing_even_with_namespaces_configured(self, memory_env):
        """The manager's own switch holds even if a config somehow carries namespaces."""
        manager = _build("session-agent", long_term_memory=False)
        manager.config.retrieval_config = {
            "/strategies/sem-1/actors/{actorId}": SimpleNamespace(top_k=10, relevance_score=0.4, strategy_id=None)
        }
        client = _RecordingRetrievalClient()
        manager._retrieval_client = client
        event = _event("Can I use AI on my assignment?")

        manager._prefetch_customer_context(event)
        manager.retrieve_customer_context(event)

        assert client.calls == []
        assert not any("<user_context>" in block.get("text", "") for block in event.agent.messages[-1]["content"])


class TestExtraction:
    def test_an_agent_chat_writes_its_augmented_prompt_with_extraction_skipped(self, memory_env, monkeypatch):
        manager = _build("session-agent", long_term_memory=False)
        writes = _record_writes(monkeypatch, manager)

        _write_user_message(manager, AUGMENTED_PROMPT)

        # Short-term history keeps the prompt (restore needs it); long-term
        # extraction never sees it.
        assert len(writes) == 1
        assert writes[0]["extractionMode"] == "SKIP"

    def test_a_plain_chat_is_extracted_as_before(self, memory_env, monkeypatch):
        manager = _build("session-plain")
        writes = _record_writes(monkeypatch, manager)

        _write_user_message(manager, "remember that I prefer short answers")

        assert len(writes) == 1
        assert "extractionMode" not in writes[0]

    def test_skipping_never_leaks_onto_the_shared_client(self, memory_env, monkeypatch):
        """The data-plane client is process-wide; only the Agent chat's manager skips."""
        agent_chat = _build("session-agent", long_term_memory=False)
        plain_chat = _build("session-plain")
        writes = _record_writes(monkeypatch, plain_chat)

        _write_user_message(agent_chat, AUGMENTED_PROMPT)
        _write_user_message(plain_chat, "remember that I prefer short answers")

        assert _data_plane(agent_chat) is _data_plane(plain_chat)  # one client, process-wide
        assert [w.get("extractionMode") for w in writes] == ["SKIP", None]
        assert plain_chat.memory_client.gmdp_client is _data_plane(plain_chat)

    def test_an_oversized_blob_event_also_skips(self, memory_env, monkeypatch):
        manager = _build("session-agent", long_term_memory=False)
        writes = _record_writes(monkeypatch, manager)

        _write_user_message(manager, "x" * 200_000)

        assert writes and all(w["extractionMode"] == "SKIP" for w in writes)


class TestSyntheticWrites:
    def test_the_stream_coordinators_fresh_manager_inherits_the_turns_setting(self):
        from agents.main_agent.streaming.stream_coordinator import _long_term_memory_of

        assert _long_term_memory_of(SimpleNamespace(session_manager=SimpleNamespace(long_term_memory=False))) is False
        assert _long_term_memory_of(SimpleNamespace(session_manager=SimpleNamespace(long_term_memory=True))) is True
        # A preview manager (or none at all) carries no flag: default to the old behavior.
        assert _long_term_memory_of(SimpleNamespace(session_manager=SimpleNamespace())) is True
        assert _long_term_memory_of(None) is True


@pytest.mark.parametrize("long_term_memory", [True, False])
def test_base_agent_forwards_the_setting_to_the_session_factory(long_term_memory):
    from unittest.mock import MagicMock, patch

    from agents.main_agent import base_agent as ba

    class Probe(ba.BaseAgent):
        def _create_agent(self):
            pass

        async def stream_async(self, *a, **k):  # pragma: no cover - abstract stub
            yield None

    with patch.object(ba, "create_default_registry", return_value=MagicMock()), \
         patch.object(ba, "ToolFilter", return_value=MagicMock()), \
         patch.object(ba.BaseAgent, "_register_external_mcp_tools", lambda self: None), \
         patch.object(ba, "GatewayIntegration", return_value=MagicMock()), \
         patch.object(ba, "SessionFactory") as sf, \
         patch.object(ba, "StreamCoordinator", return_value=MagicMock()):
        Probe(session_id="s", user_id="u", long_term_memory=long_term_memory)

    assert sf.create_session_manager.call_args.kwargs["long_term_memory"] is long_term_memory
