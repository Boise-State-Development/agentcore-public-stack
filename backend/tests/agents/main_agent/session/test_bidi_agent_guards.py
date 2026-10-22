"""TurnBasedSessionManager's text-only work must not run for the voice BidiAgent.

Since strands-agents 1.56 a ``BidiAgent`` drives the ordinary session hooks
(``AgentInitializedEvent`` -> ``initialize``, ``MessageAddedEvent`` ->
``retrieve_customer_context``) rather than the dedicated Bidi callbacks it used
through 1.55. Without these guards the voice agent's history would be sliced by
the text agent's session-level compaction checkpoint, and every voice transcript
would trigger a long-term-memory retrieval spliced into the live Bidi history.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strands.bidi import BidiAgent

from agents.main_agent.session import turn_based_session_manager as tbsm


def _bidi_agent(messages):
    agent = BidiAgent.__new__(BidiAgent)
    agent.messages = messages
    agent.agent_id = "voice"
    return agent


def test_bidi_detection():
    assert tbsm._is_bidi_agent(_bidi_agent([])) is True
    assert tbsm._is_bidi_agent(SimpleNamespace(messages=[], agent_id="default")) is False


def test_retrieve_customer_context_skips_a_bidi_agent(make_session_manager):
    mgr = make_session_manager()
    mgr.config.retrieval_config = {"/ns": SimpleNamespace(top_k=5, relevance_score=0.1, strategy_id=None)}
    client = MagicMock()
    mgr._retrieval_client = client
    message = {"role": "user", "content": [{"text": "what's my name?"}]}

    mgr.retrieve_customer_context(SimpleNamespace(agent=_bidi_agent([message])))

    client.retrieve_memory_records.assert_not_called()
    assert message == {"role": "user", "content": [{"text": "what's my name?"}]}


def test_prefetch_skips_a_bidi_agent(make_session_manager, monkeypatch):
    """The prefetch (#1400) rides the same MessageAddedEvent the voice agent fires.

    retrieve_customer_context never consumes a lookup for a BidiAgent, so a
    prefetch started for one is a paid Memory retrieval per transcript, discarded.
    """
    monkeypatch.delenv("MEMORY_RETRIEVAL_PREFETCH_ENABLED", raising=False)
    pool = MagicMock()
    monkeypatch.setattr(tbsm, "_ltm_prefetch_pool", lambda: pool)
    mgr = make_session_manager()
    mgr.config.retrieval_config = {"/ns": SimpleNamespace(top_k=5, relevance_score=0.1, strategy_id=None)}
    message = {"role": "user", "content": [{"text": "what's my name?"}]}

    mgr._prefetch_customer_context(SimpleNamespace(agent=_bidi_agent([message]), message=message))
    pool.submit.assert_not_called()

    # The same message from a text agent is prefetched, so the guard is what stopped it.
    mgr._prefetch_customer_context(SimpleNamespace(agent=SimpleNamespace(messages=[message]), message=message))
    pool.submit.assert_called_once()


def test_initialize_restores_a_bidi_agent_without_text_processing(make_session_manager):
    mgr = make_session_manager()
    mgr.read_agent = MagicMock(return_value=SimpleNamespace())
    restored = [
        {"role": "user", "content": [{"text": "hi"}]},
        {"role": "assistant", "content": [{"text": "hello"}]},
    ]
    mgr.list_messages = MagicMock(return_value=[MagicMock(to_message=lambda m=m: m) for m in restored])
    mgr._apply_compaction = MagicMock(side_effect=AssertionError("text compaction ran for voice"))
    mgr._repair_restored_history = MagicMock(side_effect=AssertionError("text repair ran for voice"))
    agent = _bidi_agent([])

    mgr.initialize(agent)

    assert [m["role"] for m in agent.messages] == ["user", "assistant"]
    mgr._apply_compaction.assert_not_called()
    mgr._repair_restored_history.assert_not_called()


async def _drive_transcript(mgr, agent, persisted):
    """Replay strands 1.57's transcript lifecycle through a real HookRegistry."""
    from strands.hooks import HookRegistry, MessageAddedEvent, MessageUpdatedEvent

    registry = HookRegistry()
    mgr.register_hooks(registry)

    shell = {"role": "assistant", "content": [], "tracking_id": "t-1"}
    agent.messages.append(shell)
    await registry.invoke_callbacks_async(MessageAddedEvent(agent=agent, message=shell))

    filled = {"role": "assistant", "content": [{"text": "The capital of France is Paris."}], "tracking_id": "t-1"}
    await registry.invoke_callbacks_async(MessageUpdatedEvent(agent, "t-1", filled))


def _persisting_manager(make_session_manager, monkeypatch, async_mode):
    from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager

    persisted = []
    monkeypatch.setattr(
        AgentCoreMemorySessionManager,
        "append_message",
        lambda self, message, agent, **kw: persisted.append(message),
    )
    mgr = make_session_manager()
    mgr.config.async_mode = async_mode
    mgr.config.batch_size = 1
    mgr.sync_agent = MagicMock()
    return mgr, persisted



@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [True, False])
async def test_voice_transcript_is_persisted_once_with_its_text(make_session_manager, monkeypatch, async_mode):
    """1.57 appends a transcript empty and fills it via MessageUpdatedEvent.

    The empty shell is skipped by append_message; without the update handler
    nothing was ever saved, so voice worked live and the conversation came
    back empty on reload.
    """
    mgr, persisted = _persisting_manager(make_session_manager, monkeypatch, async_mode)
    agent = _bidi_agent([])

    await _drive_transcript(mgr, agent, persisted)

    assert [m["content"] for m in persisted] == [[{"text": "The capital of France is Paris."}]]


@pytest.mark.asyncio
async def test_message_update_from_a_text_agent_is_not_persisted(make_session_manager, monkeypatch):
    from strands.hooks import HookRegistry, MessageUpdatedEvent

    mgr, persisted = _persisting_manager(make_session_manager, monkeypatch, async_mode=True)
    registry = HookRegistry()
    mgr.register_hooks(registry)
    text_agent = SimpleNamespace(messages=[], agent_id="default")

    await registry.invoke_callbacks_async(
        MessageUpdatedEvent(text_agent, "t-1", {"role": "assistant", "content": [{"text": "x"}]})
    )

    assert persisted == []
