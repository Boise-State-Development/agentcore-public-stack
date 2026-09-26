"""Bounded long-term memory retrieval (`TurnBasedSessionManager.retrieve_customer_context`).

The SDK retrieves long-term memory on every user message, awaited before the
model call, through its shared read/write boto client with default retries —
so a throttled RetrieveMemoryRecords put retry backoff on first-token latency.
The override keeps the SDK's contract (namespaces from `retrieval_config`,
relevance filter, `<context_tag>` block prepended to the last user message)
and swaps in a dedicated one-attempt, short-timeout client.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError, ConnectionClosedError, ReadTimeoutError, SSLError

from agents.main_agent.session import turn_based_session_manager as tbsm

PREFS = "/strategies/pref-1/actors/{actorId}"
FACTS = "/strategies/sem-1/actors/{actorId}"


class FakeRetrievalClient:
    def __init__(self, results=None, raise_with=None):
        self.results = results or {}
        self.raise_with = raise_with
        self.calls = []

    def retrieve_memory_records(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_with is not None:
            raise self.raise_with
        return {"memoryRecordSummaries": self.results.get(kwargs["namespacePath"], [])}


def _throttle():
    return ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "RetrieveMemoryRecords")


def _record(text, score=0.9):
    return {"content": {"text": text}, "score": score}


def _event(text="what is my name?"):
    agent = SimpleNamespace(messages=[
        {"role": "user", "content": [{"text": "earlier"}]},
        {"role": "assistant", "content": [{"text": "ok"}]},
        {"role": "user", "content": [{"text": text}]},
    ])
    return SimpleNamespace(agent=agent)


def _manager(make_session_manager, client, namespaces=(PREFS, FACTS), relevance=0.7):
    mgr = make_session_manager()
    mgr.config.retrieval_config = {
        ns: SimpleNamespace(top_k=5, relevance_score=relevance, strategy_id=None) for ns in namespaces
    }
    mgr.config.context_tag = "user_context"
    mgr._retrieval_client = client
    return mgr


class TestRetrieval:
    def test_prepends_context_from_every_namespace_and_resolves_templates(self, make_session_manager):
        client = FakeRetrievalClient(results={
            "/strategies/pref-1/actors/test-actor": [_record("likes short answers")],
            "/strategies/sem-1/actors/test-actor": [_record("name is Ada")],
        })
        mgr = _manager(make_session_manager, client)
        mgr.config.actor_id = "test-actor"
        event = _event()

        mgr.retrieve_customer_context(event)

        assert len(client.calls) == 2
        assert {c["namespacePath"] for c in client.calls} == {
            "/strategies/pref-1/actors/test-actor", "/strategies/sem-1/actors/test-actor",
        }
        assert all(c["searchCriteria"] == {"searchQuery": "what is my name?", "topK": 5} for c in client.calls)
        assert all(c["memoryId"] == mgr.config.memory_id for c in client.calls)
        first_block = event.agent.messages[-1]["content"][0]["text"]
        assert first_block.startswith("<user_context>") and first_block.endswith("</user_context>")
        assert "likes short answers" in first_block and "name is Ada" in first_block
        # The user's own words stay last.
        assert event.agent.messages[-1]["content"][-1] == {"text": "what is my name?"}

    def test_relevance_filter_applies(self, make_session_manager):
        client = FakeRetrievalClient(results={
            "/strategies/pref-1/actors/test-actor": [_record("weak", score=0.2), _record("strong", score=0.95)],
        })
        mgr = _manager(make_session_manager, client, namespaces=(PREFS,))
        mgr.config.actor_id = "test-actor"
        event = _event()

        mgr.retrieve_customer_context(event)

        block = event.agent.messages[-1]["content"][0]["text"]
        assert "strong" in block and "weak" not in block

    def test_nothing_retrieved_means_message_untouched(self, make_session_manager):
        mgr = _manager(make_session_manager, FakeRetrievalClient())
        event = _event()
        before = [dict(b) for b in event.agent.messages[-1]["content"]]

        mgr.retrieve_customer_context(event)

        assert event.agent.messages[-1]["content"] == before

    def test_skips_when_last_message_is_not_a_text_user_message(self, make_session_manager):
        client = FakeRetrievalClient()
        mgr = _manager(make_session_manager, client)
        assistant_last = SimpleNamespace(agent=SimpleNamespace(messages=[{"role": "assistant", "content": [{"text": "x"}]}]))
        tool_result = SimpleNamespace(agent=SimpleNamespace(messages=[{"role": "user", "content": [{"toolResult": {"toolUseId": "t", "content": []}}]}]))

        mgr.retrieve_customer_context(assistant_last)
        mgr.retrieve_customer_context(tool_result)
        mgr.retrieve_customer_context(SimpleNamespace(agent=SimpleNamespace(messages=[])))

        assert client.calls == []

    def test_no_retrieval_config_means_no_client(self, make_session_manager):
        mgr = make_session_manager()
        mgr.config.retrieval_config = {}
        with patch.object(tbsm.TurnBasedSessionManager, "_get_retrieval_client") as get_client:
            mgr.retrieve_customer_context(_event())
        get_client.assert_not_called()


class TestScoreLogging:
    """One score-only line per namespace per turn, for calibrating the cut.
    Runtime logs must carry no user identifiers or memory text."""

    def _lines(self, caplog):
        return sorted(r.getMessage() for r in caplog.records if r.getMessage().startswith("memory retrieval scores"))

    def test_logs_top_returned_kept_and_cut_per_namespace(self, make_session_manager, caplog):
        client = FakeRetrievalClient(results={
            "/strategies/pref-1/actors/secret-actor-7": [_record("likes short answers", 0.49), _record("x", 0.38)],
            "/strategies/sem-1/actors/secret-actor-7": [
                _record("name is Ada", 0.8123), _record("studies geology", 0.52), _record("owns a dog", 0.31)],
        })
        mgr = _manager(make_session_manager, client, relevance=0.5)
        mgr.config.actor_id = "secret-actor-7"

        with caplog.at_level("INFO", logger=tbsm.logger.name):
            mgr.retrieve_customer_context(_event())

        assert self._lines(caplog) == [
            "memory retrieval scores namespace=/strategies/pref-1/actors/{actorId} top=0.490 returned=2 kept=0 cut=0.5",
            "memory retrieval scores namespace=/strategies/sem-1/actors/{actorId} top=0.812 returned=3 kept=2 cut=0.5",
        ]
        assert len(client.calls) == 2  # computed from the response in hand, no extra calls

    def test_no_actor_id_or_record_text_in_any_log_line(self, make_session_manager, caplog):
        client = FakeRetrievalClient(results={
            "/strategies/sem-1/actors/secret-actor-7": [_record("name is Ada", 0.9)],
        })
        mgr = _manager(make_session_manager, client)
        mgr.config.actor_id = "secret-actor-7"

        event = _event()

        with caplog.at_level("DEBUG", logger=tbsm.logger.name):
            mgr.retrieve_customer_context(event)

        assert "name is Ada" in event.agent.messages[-1]["content"][0]["text"]  # it was injected
        assert "secret-actor-7" not in caplog.text
        assert "name is Ada" not in caplog.text

    def test_empty_namespace_logs_none(self, make_session_manager, caplog):
        mgr = _manager(make_session_manager, FakeRetrievalClient(), namespaces=(FACTS,), relevance=0.5)

        with caplog.at_level("INFO", logger=tbsm.logger.name):
            mgr.retrieve_customer_context(_event())

        assert self._lines(caplog) == [
            "memory retrieval scores namespace=/strategies/sem-1/actors/{actorId} top=none returned=0 kept=0 cut=0.5",
        ]

    def test_failed_namespace_logs_no_score_line(self, make_session_manager, caplog):
        mgr = _manager(make_session_manager, FakeRetrievalClient(raise_with=_throttle()), namespaces=(FACTS,))

        with caplog.at_level("INFO", logger=tbsm.logger.name):
            mgr.retrieve_customer_context(_event())

        assert self._lines(caplog) == []


class TestBounded:
    def test_throttle_costs_one_attempt_and_the_turn_proceeds_without_context(self, make_session_manager):
        client = FakeRetrievalClient(raise_with=_throttle())
        mgr = _manager(make_session_manager, client)
        event = _event()

        mgr.retrieve_customer_context(event)  # must not raise

        assert len(client.calls) == 2  # one per namespace, no retries at this layer
        assert event.agent.messages[-1]["content"] == [{"text": "what is my name?"}]

    def test_one_failing_namespace_does_not_sink_the_other(self, make_session_manager):
        class HalfBroken(FakeRetrievalClient):
            def retrieve_memory_records(self, **kwargs):
                self.calls.append(kwargs)
                if "pref-1" in kwargs["namespacePath"]:
                    raise RuntimeError("boom")
                return {"memoryRecordSummaries": [_record("name is Ada")]}

        mgr = _manager(make_session_manager, HalfBroken())
        event = _event()

        mgr.retrieve_customer_context(event)

        assert "name is Ada" in event.agent.messages[-1]["content"][0]["text"]

    def test_dedicated_client_is_lazy_and_bounded(self, make_session_manager, monkeypatch):
        monkeypatch.delenv(tbsm.MEMORY_RETRIEVAL_TIMEOUT_ENV, raising=False)
        monkeypatch.delenv(tbsm.MEMORY_RETRIEVAL_MAX_ATTEMPTS_ENV, raising=False)
        mgr = make_session_manager()
        assert getattr(mgr, "_retrieval_client", None) is None

        with patch("boto3.client") as boto_client:
            boto_client.return_value = object()
            first = mgr._get_retrieval_client()
            second = mgr._get_retrieval_client()

        assert first is second
        boto_client.assert_called_once()
        assert boto_client.call_args.args == ("bedrock-agentcore",)
        cfg = boto_client.call_args.kwargs["config"]
        assert cfg.retries == {"total_max_attempts": 1, "mode": "standard"}
        assert cfg.read_timeout == 2.0 and cfg.connect_timeout == 2.0
        assert boto_client.call_args.kwargs["region_name"] == mgr.region_name

    def test_env_overrides_and_garbage(self, monkeypatch):
        monkeypatch.setenv(tbsm.MEMORY_RETRIEVAL_TIMEOUT_ENV, "0.75")
        monkeypatch.setenv(tbsm.MEMORY_RETRIEVAL_MAX_ATTEMPTS_ENV, "2")
        assert tbsm.memory_retrieval_timeout_seconds() == 0.75
        assert tbsm.memory_retrieval_max_attempts() == 2

        monkeypatch.setenv(tbsm.MEMORY_RETRIEVAL_TIMEOUT_ENV, "later")
        monkeypatch.setenv(tbsm.MEMORY_RETRIEVAL_MAX_ATTEMPTS_ENV, "0")
        assert tbsm.memory_retrieval_timeout_seconds() == 2.0
        assert tbsm.memory_retrieval_max_attempts() == 1


URL = "https://bedrock-agentcore.us-west-2.amazonaws.com/memories/mem-1/retrieve"


def _dead_connection():
    return ConnectionClosedError(endpoint_url=URL)


def _ssl_eof():
    return SSLError(endpoint_url=URL, error="[SSL: UNEXPECTED_EOF_WHILE_READING] unexpected eof while reading")


class TestNoneResponseErrors:
    """botocore's ConnectionClosedError and ReadTimeoutError carry
    `response = None`. The per-namespace handler used to call `.get` on it,
    raise, and so discard every namespace's results for the turn."""

    def test_real_botocore_errors_have_none_response(self):
        assert _dead_connection().response is None
        assert ReadTimeoutError(endpoint_url=URL).response is None

    def test_read_timeout_on_one_namespace_keeps_the_other(self, make_session_manager, caplog):
        class SlowPrefs(FakeRetrievalClient):
            def retrieve_memory_records(self, **kwargs):
                self.calls.append(kwargs)
                if "pref-1" in kwargs["namespacePath"]:
                    raise ReadTimeoutError(endpoint_url=URL)
                return {"memoryRecordSummaries": [_record("name is Ada")]}

        client = SlowPrefs()
        mgr = _manager(make_session_manager, client)
        event = _event()

        with caplog.at_level("WARNING", logger=tbsm.logger.name):
            mgr.retrieve_customer_context(event)

        assert "name is Ada" in event.agent.messages[-1]["content"][0]["text"]
        assert len(client.calls) == 2  # a timeout is not retried
        assert "ReadTimeoutError" in caplog.text
        assert "Failed to retrieve customer context" not in caplog.text


class TestReconnect:
    def _stale_then_fresh(self, make_session_manager, error):
        stale = FakeRetrievalClient(raise_with=error)
        fresh = FakeRetrievalClient(results={
            "/strategies/pref-1/actors/test-actor": [_record("likes short answers")],
            "/strategies/sem-1/actors/test-actor": [_record("name is Ada")],
        })
        mgr = _manager(make_session_manager, stale)
        mgr.config.actor_id = "test-actor"
        return mgr, stale, fresh

    @pytest.mark.parametrize("error", [_dead_connection, _ssl_eof], ids=["connection_closed", "ssl_eof"])
    def test_dead_connection_retries_once_on_one_fresh_client(self, make_session_manager, error, caplog):
        mgr, stale, fresh = self._stale_then_fresh(make_session_manager, error())
        event = _event()

        with patch("boto3.client", return_value=fresh) as boto_client, caplog.at_level("INFO", logger=tbsm.logger.name):
            mgr.retrieve_customer_context(event)

        block = event.agent.messages[-1]["content"][0]["text"]
        assert "likes short answers" in block and "name is Ada" in block
        assert len(stale.calls) == 2 and len(fresh.calls) == 2  # one attempt each, per namespace
        # Both namespaces failed on the same client; it is replaced once.
        boto_client.assert_called_once()
        assert mgr._retrieval_client is fresh
        assert caplog.text.count("memory retrieval reconnected") == 2
        # The fresh request is the same request.
        assert sorted(map(str, stale.calls)) == sorted(map(str, fresh.calls))

    def test_reconnect_that_fails_again_gives_up_quietly(self, make_session_manager):
        mgr, stale, _ = self._stale_then_fresh(make_session_manager, _dead_connection())
        still_dead = FakeRetrievalClient(raise_with=_dead_connection())
        event = _event()

        with patch("boto3.client", return_value=still_dead):
            mgr.retrieve_customer_context(event)  # must not raise

        assert len(still_dead.calls) == 2  # exactly one retry per namespace
        assert event.agent.messages[-1]["content"] == [{"text": "what is my name?"}]

    def test_throttle_does_not_replace_the_client(self, make_session_manager):
        client = FakeRetrievalClient(raise_with=_throttle())
        mgr = _manager(make_session_manager, client)

        with patch("boto3.client") as boto_client:
            mgr.retrieve_customer_context(_event())

        boto_client.assert_not_called()
        assert mgr._retrieval_client is client

    def test_replace_only_swaps_the_stale_client(self, make_session_manager):
        current = FakeRetrievalClient()
        mgr = _manager(make_session_manager, current)

        with patch("boto3.client") as boto_client:
            # Another namespace already replaced it: hand back the current one.
            assert mgr._replace_retrieval_client(object()) is current
        boto_client.assert_not_called()


class TestQueryLength:
    def test_long_message_is_cut_to_the_api_limit(self, make_session_manager):
        client = FakeRetrievalClient()
        mgr = _manager(make_session_manager, client, namespaces=(PREFS,))
        text = "q" * 25_000

        event = _event(text)

        mgr.retrieve_customer_context(event)

        query = client.calls[0]["searchCriteria"]["searchQuery"]
        assert query == "q" * tbsm.MEMORY_RETRIEVAL_QUERY_MAX_CHARS
        # Only the search query is cut; the message the model sees is not.
        assert event.agent.messages[-1]["content"] == [{"text": text}]

    def test_short_message_is_unchanged(self):
        assert tbsm.memory_retrieval_query("what is my name?") == "what is my name?"
        exact = "x" * tbsm.MEMORY_RETRIEVAL_QUERY_MAX_CHARS
        assert tbsm.memory_retrieval_query(exact) == exact

    def test_astral_characters_count_as_two(self):
        # Each emoji is two UTF-16 code units; the cut never splits one.
        query = tbsm.memory_retrieval_query("\U0001F600" * 6_000)
        assert len(query) == tbsm.MEMORY_RETRIEVAL_QUERY_MAX_CHARS // 2
        assert len(query.encode("utf-16-le")) // 2 <= tbsm.MEMORY_RETRIEVAL_QUERY_MAX_CHARS


class TestSdkWiring:
    def test_sdk_register_hooks_dispatches_to_our_override(self):
        """The SDK registers `self.retrieve_customer_context` (both modes), so
        the override is what runs. Guards against an SDK bump that starts
        registering a private method instead."""
        import inspect

        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )

        src = inspect.getsource(AgentCoreMemorySessionManager.register_hooks)
        assert src.count("self.retrieve_customer_context") >= 2
        assert tbsm.TurnBasedSessionManager.retrieve_customer_context is not AgentCoreMemorySessionManager.retrieve_customer_context
