"""The long-term-memory prefetch (docs/specs/turn-path-ttft.md P4a).

On ``MessageAddedEvent`` the SDK awaits two Memory writes and only then runs
the lookup: three network calls in series before the model is called, 450ms
of a 1687ms warm first token on dev. The prefetch starts the lookup first and
applies it where it always was.

What is expensive to get wrong is not the speed-up but the bytes. The lookup
prepends a context block to the LIVE user message after the SDK has persisted
it, so the persisted message has no block. Restore rebuilds history from the
persisted copy, and restored history must be byte-stable for the prompt cache.
These tests drive the SDK's own async-mode wiring through a real Strands
``HookRegistry`` and pin both copies, byte for byte, against the switch-off
path.
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from strands.hooks import HookRegistry, MessageAddedEvent

from agents.main_agent.session import turn_based_session_manager as tbsm

PREFS = "/strategies/pref-1/actors/{actorId}"
FACTS = "/strategies/sem-1/actors/{actorId}"


class _GatedClient:
    """Signals when a lookup starts; holds it until the writes are done.

    That makes overlap a yes/no question rather than a timing threshold: if
    the lookup only starts after the writes, ``append_message`` never sees it
    start.
    """

    def __init__(self, writes_done: threading.Event) -> None:
        self.started = threading.Event()
        self.writes_done = writes_done
        self.calls = 0

    def retrieve_memory_records(self, **kwargs):
        self.calls += 1
        self.started.set()
        self.writes_done.wait(timeout=5)
        return {"memoryRecordSummaries": [{"content": {"text": f"fact from {kwargs['namespacePath']}"}, "score": 0.9}]}


def _manager(make_session_manager, client):
    mgr = make_session_manager()
    mgr.config.async_mode = True
    mgr.config.batch_size = 1
    mgr.config.context_tag = "user_context"
    # One namespace: two would make the block's line order depend on which
    # finished first, a pre-existing property that is not what this pins.
    mgr.config.retrieval_config = {
        FACTS: SimpleNamespace(top_k=5, relevance_score=0.5, strategy_id=None),
    }
    mgr._retrieval_client = client
    return mgr


async def _run_turn(mgr, client, *, writes_done):
    """One user message through the real registry. Returns what was persisted,
    whether the lookup had started by the time ``append_message`` returned,
    and the live message afterwards."""
    persisted = {}

    def append_message(message, agent, **kwargs):
        # Serialize at write time — what reaches Memory is this string.
        persisted["message"] = json.dumps(message, sort_keys=True)
        persisted["lookup_started_during_write"] = client.started.wait(timeout=2)

    def sync_agent(agent, **kwargs):
        writes_done.set()

    mgr.append_message = append_message
    mgr.sync_agent = sync_agent

    registry = HookRegistry()
    mgr.register_hooks(registry)

    message = {"role": "user", "content": [{"text": "what is my name?"}]}
    agent = SimpleNamespace(agent_id="default", messages=[message])
    await registry.invoke_callbacks_async(MessageAddedEvent(agent=agent, message=message))
    return persisted, json.dumps(message, sort_keys=True)


@pytest.fixture
def prefetch_on(monkeypatch):
    monkeypatch.delenv("MEMORY_RETRIEVAL_PREFETCH_ENABLED", raising=False)


class TestThroughTheSdkWiring:
    @pytest.mark.asyncio
    async def test_the_lookup_overlaps_the_writes(self, make_session_manager, prefetch_on):
        writes_done = threading.Event()
        client = _GatedClient(writes_done)
        mgr = _manager(make_session_manager, client)

        persisted, _ = await _run_turn(mgr, client, writes_done=writes_done)

        assert persisted["lookup_started_during_write"] is True
        assert client.calls == 1  # prefetched, not fetched again inline

    @pytest.mark.asyncio
    async def test_persisted_message_has_no_context_block_and_live_one_does(
        self, make_session_manager, prefetch_on
    ):
        writes_done = threading.Event()
        client = _GatedClient(writes_done)
        mgr = _manager(make_session_manager, client)

        persisted, live = await _run_turn(mgr, client, writes_done=writes_done)

        assert "user_context" not in persisted["message"]
        assert json.loads(live)["content"][0]["text"].startswith("<user_context>")
        assert json.loads(live)["content"][-1] == {"text": "what is my name?"}

    @pytest.mark.asyncio
    async def test_bytes_match_the_switch_off_path(self, make_session_manager, monkeypatch):
        """The whole contract in one assertion: with the prefetch on, both
        copies are byte-identical to what the inline path produces."""
        results = {}
        for flag in ("true", "false"):
            monkeypatch.setenv("MEMORY_RETRIEVAL_PREFETCH_ENABLED", flag)
            writes_done = threading.Event()
            client = _GatedClient(writes_done)
            mgr = _manager(make_session_manager, client)
            persisted, live = await _run_turn(mgr, client, writes_done=writes_done)
            results[flag] = (persisted["message"], live)

        assert results["true"] == results["false"]

    @pytest.mark.asyncio
    async def test_switch_off_fetches_inline_after_the_writes(self, make_session_manager, monkeypatch):
        monkeypatch.setenv("MEMORY_RETRIEVAL_PREFETCH_ENABLED", "false")
        writes_done = threading.Event()
        client = _GatedClient(writes_done)
        mgr = _manager(make_session_manager, client)
        # Without overlap the write waits out its full 2s for a lookup that
        # cannot start yet; shorten that wait for this case only.
        client.started.wait = lambda timeout=None: client.started.is_set()

        persisted, _ = await _run_turn(mgr, client, writes_done=writes_done)

        assert persisted["lookup_started_during_write"] is False
        assert client.calls == 1


class _RecordingPool:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        from concurrent.futures import Future

        self.submitted.append(args)
        future = Future()
        future.set_result(fn(*args))
        return future


def _event(message):
    return SimpleNamespace(agent=SimpleNamespace(messages=[message]), message=message)


class TestPrefetchEligibility:
    @pytest.fixture
    def pool(self, monkeypatch, prefetch_on):
        pool = _RecordingPool()
        monkeypatch.setattr(tbsm, "_ltm_prefetch_pool", lambda: pool)
        return pool

    def _mgr(self, make_session_manager):
        client = _GatedClient(threading.Event())
        client.writes_done.set()
        return _manager(make_session_manager, client)

    @pytest.mark.parametrize(
        "message",
        [
            {"role": "assistant", "content": [{"text": "hello"}]},
            {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": []}}]},
            {"role": "user", "content": []},
        ],
    )
    def test_only_a_text_user_message_is_prefetched(self, make_session_manager, pool, message):
        mgr = self._mgr(make_session_manager)

        mgr._prefetch_customer_context(_event(message))

        assert pool.submitted == []

    def test_a_cancelled_session_is_not_prefetched(self, make_session_manager, pool):
        mgr = self._mgr(make_session_manager)
        mgr.cancelled = True

        mgr._prefetch_customer_context(_event({"role": "user", "content": [{"text": "hi"}]}))

        assert pool.submitted == []

    def test_the_query_is_the_capped_user_text(self, make_session_manager, pool):
        mgr = self._mgr(make_session_manager)
        text = "x" * (tbsm.MEMORY_RETRIEVAL_QUERY_MAX_CHARS + 50)

        mgr._prefetch_customer_context(_event({"role": "user", "content": [{"text": text}]}))

        assert pool.submitted == [(text[: tbsm.MEMORY_RETRIEVAL_QUERY_MAX_CHARS],)]


class TestTakingThePrefetch:
    def _mgr(self, make_session_manager):
        client = _GatedClient(threading.Event())
        client.writes_done.set()
        return _manager(make_session_manager, client), client

    def test_a_prefetch_for_another_message_is_never_applied(self, make_session_manager, prefetch_on):
        """A turn that died between the two callbacks leaves a prefetch behind.
        It must not land on the next message; that one fetches inline."""
        mgr, client = self._mgr(make_session_manager)
        from concurrent.futures import Future

        stale = Future()
        stale.set_result(["a fact for a different question"])
        mgr._ltm_prefetch = ({"role": "user", "content": [{"text": "old"}]}, stale, 0.0)
        message = {"role": "user", "content": [{"text": "new"}]}

        mgr.retrieve_customer_context(_event(message))

        assert "different question" not in message["content"][0]["text"]
        assert client.calls == 1
        assert mgr._ltm_prefetch is None

    def test_a_failed_prefetch_leaves_the_turn_without_context(self, make_session_manager, prefetch_on, caplog):
        mgr, client = self._mgr(make_session_manager)
        from concurrent.futures import Future

        failed = Future()
        failed.set_exception(RuntimeError("pool down"))
        message = {"role": "user", "content": [{"text": "hi"}]}
        mgr._ltm_prefetch = (message, failed, 0.0)

        mgr.retrieve_customer_context(_event(message))  # must not raise

        assert message["content"] == [{"text": "hi"}]
        assert "memory retrieval prefetch failed" in caplog.text

    def test_the_wait_is_logged_without_content(self, make_session_manager, prefetch_on, caplog):
        import logging

        caplog.set_level(logging.INFO, logger=tbsm.__name__)
        mgr, _ = self._mgr(make_session_manager)
        message = {"role": "user", "content": [{"text": "a private question"}]}
        mgr._prefetch_customer_context(_event(message))

        mgr.retrieve_customer_context(_event(message))

        lines = [r.getMessage() for r in caplog.records if "prefetch waitedMs" in r.getMessage()]
        assert len(lines) == 1
        assert "private" not in lines[0]


def test_register_hooks_puts_the_prefetch_first(make_session_manager):
    """Strands runs a hook's callbacks in registration order; the prefetch
    only overlaps the writes if it runs before the SDK's persist callback."""
    mgr = _manager(make_session_manager, _GatedClient(threading.Event()))
    registry = HookRegistry()

    mgr.register_hooks(registry)

    callbacks = list(registry.get_callbacks_for(MessageAddedEvent(agent=SimpleNamespace(), message={})))
    assert callbacks[0] == mgr._prefetch_customer_context
    assert len(callbacks) == 3  # prefetch, the SDK's persist, the SDK's lookup
