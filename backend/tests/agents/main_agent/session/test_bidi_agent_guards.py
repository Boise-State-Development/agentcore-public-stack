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
