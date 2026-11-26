"""``warm`` + ``prepare`` through the real ``/invocations`` handler (session prewarm, prepare mode).

A plain warm call starts the microVM and nothing else (``test_invocations_warm.py``). A prepare
also builds the agent the next send would use, from the selection on the request, so the send
finds it in the agent cache. The contract pinned here:

* it runs the turn's own phases and calls ``get_agent`` once, with the arguments the send would;
* it takes no lease, writes no session row, charges no quota, searches no knowledge base and
  calls no model;
* it never raises to the caller: a JSON body, always, with a ``reason`` when nothing was built;
* a send that arrives while a prepare is building waits for it instead of building a second agent.

The harness drives the handlers directly (no TestClient), because the in-flight tests need two
requests overlapping on one event loop.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import StreamingResponse

import apis.inference_api.chat.routes as routes
from apis.inference_api.chat import prepare as prepare_mode
from apis.inference_api.chat import service
from apis.inference_api.chat.models import InvocationRequest
from apis.inference_api.chat.routes import TurnQuota, TurnTools
from apis.inference_api.chat.turn_timing import TurnPrelude
from apis.shared.auth.models import User
from apis.shared.sessions.metadata import SessionMetaSnapshot

SESSION = "sess-prep-1"
SELECTION: Dict[str, Any] = {"enabled_tools": ["web_search"], "model_id": None}


def _user(user_id: str = "alice") -> User:
    user = User(email=f"{user_id}@example.com", user_id=user_id, name=user_id.title(), roles=["user"])
    user.raw_token = "access.token.value"
    return user


def _prepare_request(session_id: str = SESSION, **fields: Any) -> InvocationRequest:
    return InvocationRequest(session_id=session_id, warm=True, prepare=True, **{**SELECTION, **fields})


def _turn_request(session_id: str = SESSION, **fields: Any) -> InvocationRequest:
    return InvocationRequest(session_id=session_id, message="draft a rubric for this", **{**SELECTION, **fields})


def _agent() -> MagicMock:
    agent = MagicMock()

    async def fake_stream(*args, **kwargs):
        yield "event: done\ndata: {}\n\n"

    agent.stream_async = fake_stream
    return agent


def _cacheable_agent(**kwargs: Any) -> MagicMock:
    """A stand-in the real ``get_agent`` will cache: not paused, with a construction snapshot."""
    agent = _agent()
    agent.agent = SimpleNamespace(_interrupt_state=SimpleNamespace(activated=False))
    agent._construction_snapshot = {"system_prompt": kwargs.get("system_prompt")}
    return agent


def _kwargs(call) -> Dict[str, Any]:
    """``get_agent``'s arguments minus the per-call recorders (closures, never compared)."""
    return {k: v for k, v in call.kwargs.items() if k not in ("build_stage_recorder", "build_detail_recorder")}


class World:
    """What the route reaches, replaced: the agent factory and the lease, counting."""

    def __init__(self) -> None:
        self.agent = _agent()
        self.builds = 0
        self.new_build = True
        self.get_agent = AsyncMock(side_effect=self._build)
        self.acquire = AsyncMock(return_value=None)
        self.lease_held = AsyncMock(return_value=False)
        self.ensure_metadata = AsyncMock(return_value=False)

    async def _build(self, **kwargs: Any) -> Any:
        if self.new_build:
            self.builds += 1
        return self.agent


@pytest.fixture(autouse=True)
def _reset_prepare_state():
    prepare_mode.reset_for_tests()
    yield
    prepare_mode.reset_for_tests()


@pytest.fixture(autouse=True)
def _no_live_infrastructure_reads(monkeypatch):
    """The same stubs ``tests/routes/conftest.py`` applies to the invocation path: each of these
    is fail-open in production, so an unstubbed one would open a real socket and pass anyway."""
    stubs = [
        ("apis.inference_api.chat.system_prompt_resolver.get_session_metadata", None),
        ("apis.shared.files.document_read.session_attachment_kind", ""),
        ("apis.shared.files.document_read.session_has_tabular_files", False),
        ("apis.shared.files.document_read.session_has_presentation_files", False),
        ("apis.shared.tools.repository.ToolCatalogRepository.list_tools", []),
        ("apis.shared.models.managed_models.list_managed_models", []),
        ("apis.shared.rate_limit.RateLimiter.check_rate_limit", True),
        ("agents.main_agent.quota.repository.QuotaRepository.get_active_override", None),
        ("agents.main_agent.quota.repository.QuotaRepository.query_user_assignment", None),
        ("agents.main_agent.quota.repository.QuotaRepository.query_role_assignments", []),
        ("agents.main_agent.quota.repository.QuotaRepository.list_assignments_by_type", []),
        ("agents.main_agent.quota.repository.QuotaRepository.get_active_extensions", []),
        ("apis.shared.rbac.repository.AppRoleRepository.get_roles_for_jwt_role", []),
        ("apis.shared.rbac.repository.AppRoleRepository.get_role", None),
        ("apis.shared.rbac.repository.AppRoleRepository.get_user_grant", None),
        ("apis.shared.memory.repository.MemorySpaceRepository.get_space", None),
    ]
    patchers = [patch(target, new=AsyncMock(return_value=value)) for target, value in stubs]
    for patcher in patchers:
        patcher.start()
    try:
        yield
    finally:
        for patcher in reversed(patchers):
            patcher.stop()


@pytest.fixture
def world(monkeypatch) -> World:
    w = World()
    monkeypatch.setattr(routes, "get_agent", w.get_agent)
    monkeypatch.setattr(routes, "process_build_count", lambda: w.builds)
    monkeypatch.setattr(routes, "is_quota_enforcement_enabled", lambda: False)
    monkeypatch.setattr(routes, "ensure_session_metadata_exists", w.ensure_metadata)
    monkeypatch.setattr("apis.shared.sessions.session_lease.acquire_session_lease", w.acquire)
    monkeypatch.setattr("apis.shared.sessions.session_lease.is_session_lease_held", w.lease_held)
    return w


def _invoke(request: InvocationRequest, user: Optional[User] = None):
    return asyncio.run(routes.invocations(request, current_user=user or _user()))


async def _drain(response: Any) -> List[Any]:
    """Consume a turn's stream: the deferred agent build runs inside it."""
    assert isinstance(response, StreamingResponse), response
    return [chunk async for chunk in response.body_iterator]


def _run_turn(request: InvocationRequest, user: Optional[User] = None) -> None:
    async def go():
        await _drain(await routes.invocations(request, current_user=user or _user()))

    asyncio.run(go())


class _Streams:
    """Stands in for ``stream_conversational_message``: records whether a refusal's generator ever ran."""

    def __init__(self) -> None:
        self.created = 0
        self.ran = 0

    def __call__(self, **kwargs: Any):
        self.created += 1

        async def body():
            self.ran += 1
            yield "event: done\ndata: {}\n\n"

        return body()


def _meta(row: Optional[dict] = None, *, owned_by_other: bool = False) -> AsyncMock:
    return AsyncMock(return_value=SessionMetaSnapshot(row=row, owned_by_other=owned_by_other))


# --------------------------------------------------------------------------- 1. plain warm


class TestPlainWarmIsUnchanged:
    def test_warm_without_prepare_is_the_bare_acknowledgement(self, world):
        tripwire = AsyncMock(side_effect=AssertionError("a plain warm call must not touch session state"))
        with patch.object(routes, "load_session_meta", tripwire):
            result = _invoke(InvocationRequest(session_id=SESSION, warm=True, **SELECTION))

        assert result == {"warmed": True}
        tripwire.assert_not_called()
        world.get_agent.assert_not_called()

    def test_prepare_without_warm_is_an_ordinary_turn(self, world):
        sentinel = RuntimeError("reached the ownership read")
        with patch.object(routes, "load_session_meta", AsyncMock(side_effect=sentinel)):
            with pytest.raises(RuntimeError) as raised:
                _invoke(InvocationRequest(session_id=SESSION, message="hi", prepare=True))

        assert raised.value is sentinel


# --------------------------------------------------------------------------- 2. a prepare builds


class TestPrepareBuildsTheAgent:
    def test_runs_the_phases_calls_get_agent_once_and_returns_the_documented_body(self, world):
        body = _invoke(_prepare_request())

        assert world.get_agent.await_count == 1
        assert set(body) == {"warmed", "prepared", "reason", "cacheHit", "ms"}
        assert body["warmed"] is True
        assert body["prepared"] is True
        assert body["reason"] is None
        assert body["cacheHit"] is False
        assert isinstance(body["ms"], int) and body["ms"] >= 0
        json.dumps(body)

    def test_the_body_never_carries_the_stage_marks(self, world):
        assert "stages" not in _invoke(_prepare_request())

    def test_a_prepare_is_a_non_resume_build(self, world):
        _invoke(_prepare_request())

        kwargs = world.get_agent.await_args.kwargs
        assert kwargs["is_resume"] is False
        assert kwargs["session_id"] == SESSION
        assert kwargs["user_id"] == "alice"
        assert kwargs["enabled_tools"] == ["web_search"]

    def test_a_turn_phase_marks_are_not_a_turn_prelude(self, world):
        emitted: list = []
        with patch.object(TurnPrelude, "emit", lambda self, **kw: emitted.append(kw)):
            _invoke(_prepare_request())

        assert emitted == []

    def test_the_prepare_record_is_written_once(self, world):
        with patch.object(routes, "emit_prepare_record") as record:
            body = _invoke(_prepare_request())

        record.assert_called_once()
        kwargs = record.call_args.kwargs
        assert kwargs["session_id"] == SESSION
        assert kwargs["outcome"].prepared is True
        assert kwargs["elapsed_ms"] == body["ms"]
        assert kwargs["outcome"].stages and "stages" in kwargs["outcome"].stages


# --------------------------------------------------------------------------- 3. same key as the send


class TestTheSameAgentAsTheSend:
    def test_get_agent_is_asked_for_exactly_what_the_turn_asks(self, world):
        _invoke(_prepare_request(enabled_skills=None))
        _run_turn(_turn_request())

        assert world.get_agent.await_count == 2
        prepare_call, turn_call = world.get_agent.await_args_list
        assert _kwargs(prepare_call) == _kwargs(turn_call)

    def test_a_different_message_does_not_move_the_key(self, world):
        _invoke(_prepare_request())
        _run_turn(_turn_request())
        _run_turn(InvocationRequest(session_id=SESSION, message="something else entirely", **SELECTION))

        first, second, third = world.get_agent.await_args_list
        assert _kwargs(first) == _kwargs(second) == _kwargs(third)

    def test_a_different_model_does_move_it(self, world):
        """The control: the comparison above is not vacuous."""
        _invoke(_prepare_request())
        _run_turn(_turn_request(inference_params={"temperature": 0.2}))

        prepare_call, turn_call = world.get_agent.await_args_list
        assert _kwargs(prepare_call) != _kwargs(turn_call)

    def test_through_the_real_cache_the_send_is_a_hit(self, monkeypatch):
        """The real ``get_agent`` with only the factory stubbed: prepare then send builds once."""
        service.clear_agent_cache()
        created: list = []

        def fake_create_agent(**kwargs):
            created.append(kwargs)
            return _cacheable_agent(**kwargs)

        monkeypatch.setattr(service, "create_agent", fake_create_agent)
        monkeypatch.setattr(routes, "is_quota_enforcement_enabled", lambda: False)
        monkeypatch.setattr(routes, "ensure_session_metadata_exists", AsyncMock(return_value=False))
        monkeypatch.setattr("apis.shared.sessions.session_lease.acquire_session_lease", AsyncMock(return_value=None))
        monkeypatch.setattr("apis.shared.sessions.session_lease.is_session_lease_held", AsyncMock(return_value=False))
        monkeypatch.setattr("apis.shared.tools.freshness.get_freshness_hash", AsyncMock(return_value="fresh"))
        monkeypatch.setattr("apis.shared.tools.freshness.get_disabled_tool_ids", AsyncMock(return_value=frozenset()))
        try:
            first = _invoke(_prepare_request())
            _run_turn(_turn_request())
            second = _invoke(_prepare_request())
        finally:
            service.clear_agent_cache()

        assert first["prepared"] is True and first["cacheHit"] is False
        assert len(created) == 1, "the send after a prepare must hit the cached agent"
        assert second["prepared"] is True and second["cacheHit"] is True


# --------------------------------------------------------------------------- 4. writes nothing


TRIPWIRES = [
    "_resolve_turn_attachments",
    "_prepare_session_state",
    "_search_and_augment",
    "generate_conversation_title",
]


def _tripwires(stack: ExitStack) -> Dict[str, AsyncMock]:
    mocks: Dict[str, AsyncMock] = {}
    for name in TRIPWIRES:
        mock = AsyncMock(side_effect=AssertionError(f"a prepare must not run {name}"))
        stack.enter_context(patch.object(routes, name, mock))
        mocks[name] = mock
    store = AsyncMock(side_effect=AssertionError("a prepare must not persist session metadata"))
    stack.enter_context(patch("apis.shared.sessions.metadata.store_session_metadata", store))
    mocks["store_session_metadata"] = store
    return mocks


class TestPrepareWritesNothingAndTakesNoLease:
    def test_a_plain_selection(self, world):
        with ExitStack() as stack:
            mocks = _tripwires(stack)
            body = _invoke(_prepare_request())

        assert body["prepared"] is True
        for name, mock in mocks.items():
            assert mock.await_count == 0 and mock.call_count == 0, name
        assert world.acquire.await_count == 0
        assert world.ensure_metadata.await_count == 0

    def test_an_agent_selection(self, world):
        resolved = routes.ResolvedTurnAgent(assistant=SimpleNamespace(assistant_id="ast-1", instructions="Build rubrics.", name="Rubric Builder"), permission="owner")
        resolve = AsyncMock(return_value=resolved)
        with ExitStack() as stack:
            mocks = _tripwires(stack)
            stack.enter_context(patch.object(routes, "resolve_turn_agent", resolve))
            body = _invoke(_prepare_request(rag_assistant_id="ast-1"))

        assert body["prepared"] is True
        for name, mock in mocks.items():
            assert mock.await_count == 0 and mock.call_count == 0, name
        assert world.acquire.await_count == 0
        assert resolve.await_args.kwargs["record_use"] is False
        assert resolve.await_args.kwargs["is_continuation"] is True
        assert world.get_agent.await_args.kwargs["assistant_id"] == "ast-1"

    def test_a_turn_with_the_same_selection_does_take_the_lease_and_the_phases(self, world):
        """The control: the same tripwire patches DO fire on a turn, so the zeros above mean something."""
        _run_turn(_turn_request())

        assert world.acquire.await_count == 1

    def test_a_prepare_sends_nothing_to_the_model(self, world):
        stream = AsyncMock(side_effect=AssertionError("a prepare must not run the agent"))
        world.agent.stream_async = stream
        world.agent.invoke_async = stream

        assert _invoke(_prepare_request())["prepared"] is True
        stream.assert_not_called()


# --------------------------------------------------------------------------- 5. the allowlist


class TestTheAllowlistReachesTheRoute:
    def test_a_prepare_that_also_carries_turn_fields_runs_none_of_them(self, world):
        seen: List[InvocationRequest] = []
        real = routes._run_invocation

        async def spy(request, current_user, **kw):
            seen.append(request)
            return await real(request, current_user, **kw)

        request = _prepare_request(
            message="a real turn",
            file_upload_ids=["u1"],
            interrupt_responses=[{"interruptId": "i1", "response": {"approved": True}}],
            continue_truncated=True,
            deliveries=[{"deliveryId": "d1"}],
            handoff_context={"handoffId": "h1"},
            steering=[{"id": "e1", "text": "x"}],
        )
        with patch.object(routes, "_run_invocation", spy), patch.object(
            routes, "get_paused_turn", create=True
        ) as paused:
            body = _invoke(request)

        assert len(seen) == 1
        forwarded = seen[0]
        for dropped in (
            "message", "file_upload_ids", "interrupt_responses", "continue_truncated",
            "deliveries", "handoff_context", "steering", "app_tool_call", "warm", "prepare",
        ):
            assert not getattr(forwarded, dropped), dropped
        assert forwarded.enabled_tools == ["web_search"]
        assert body["prepared"] is True
        assert world.get_agent.await_args.kwargs["is_resume"] is False
        assert world.acquire.await_count == 0
        paused.assert_not_called()

    def test_an_app_tool_call_is_never_dispatched(self, world):
        dispatch = AsyncMock(side_effect=AssertionError("a prepare must not dispatch an App call"))
        request = _prepare_request(
            app_tool_call={"tool_use_id": "t1", "tool_name": "x", "arguments": {}},
        )
        with patch.object(routes, "dispatch_app_tool_call", dispatch):
            body = _invoke(request)

        assert body["prepared"] is True
        dispatch.assert_not_called()


# --------------------------------------------------------------------------- 6. prechecks


class TestPrechecks:
    def test_a_paused_turn_on_the_row(self, world):
        with patch.object(routes, "load_session_meta", _meta({"pausedTurn": {"toolUseId": "t"}})):
            body = _invoke(_prepare_request())

        assert body["prepared"] is False and body["reason"] == "paused"
        assert body["warmed"] is True
        world.get_agent.assert_not_called()

    def test_a_held_lease(self, world):
        world.lease_held.return_value = True

        body = _invoke(_prepare_request())

        assert body["prepared"] is False and body["reason"] == "turn_active"
        world.lease_held.assert_awaited_once_with(SESSION, "alice")
        world.get_agent.assert_not_called()

    def test_a_preview_session(self, world):
        from apis.shared.sessions.metadata import is_preview_session  # noqa: F401  (confirms the id shape below)

        session_id = "preview-abc123"
        assert is_preview_session(session_id), "the fixture id must be a preview id"

        body = _invoke(_prepare_request(session_id=session_id))

        assert body["prepared"] is False and body["reason"] == "preview"
        world.get_agent.assert_not_called()

    def test_a_paused_session_reports_paused_before_checking_the_lease(self, world):
        world.lease_held.return_value = True
        with patch.object(routes, "load_session_meta", _meta({"pausedTurn": {}})):
            body = _invoke(_prepare_request())

        assert body["reason"] == "paused"

    def test_a_row_without_a_paused_turn_does_not_block(self, world):
        with patch.object(routes, "load_session_meta", _meta({"title": "hello"})):
            assert _invoke(_prepare_request())["prepared"] is True


# --------------------------------------------------------------------------- 7. quota


class TestQuota:
    def test_an_exceeded_quota_builds_nothing_and_persists_nothing(self, world):
        quota = TurnQuota(exceeded_event=MagicMock(message="over"))
        persist = AsyncMock(side_effect=AssertionError("a prepare must not persist a refusal"))
        refuse = MagicMock(side_effect=AssertionError("a prepare must not build a refusal stream"))
        with patch.object(routes, "_check_turn_quota", AsyncMock(return_value=quota)), patch.object(
            routes, "stream_conversational_message", persist
        ), patch.object(routes, "_refuse_turn", refuse):
            body = _invoke(_prepare_request())

        assert body == {**body, "warmed": True, "prepared": False, "reason": "quota", "cacheHit": False}
        world.get_agent.assert_not_called()
        persist.assert_not_called()
        refuse.assert_not_called()
        assert world.acquire.await_count == 0

    def test_the_quota_check_runs_with_the_turns_arguments_and_charges_nothing(self, world):
        check = AsyncMock(return_value=TurnQuota())
        with patch.object(routes, "_check_turn_quota", check):
            assert _invoke(_prepare_request())["prepared"] is True

        assert check.await_count == 1
        assert check.await_args.kwargs["is_resume"] is False
        assert check.await_args.kwargs["is_continuation"] is False


# --------------------------------------------------------------------------- 8. refusals and errors


class TestRefusalsAndErrors:
    def test_model_access_denied_is_refused_and_nothing_streams(self, world):
        streams = _Streams()
        service_double = MagicMock()
        service_double.can_access_model = AsyncMock(return_value=False)
        with patch.object(routes, "get_app_role_service", return_value=service_double), patch.object(
            routes, "stream_conversational_message", streams
        ):
            body = _invoke(_prepare_request(model_id="some.model-v1:0"))

        assert body["prepared"] is False and body["reason"] == "refused"
        world.get_agent.assert_not_called()
        assert streams.ran == 0, "the refusal's generator must never run (it is what persists a refusal)"

    def test_a_refusal_from_the_shared_phases_never_runs_its_generator(self, world):
        started = {"refusal": False}

        async def refusal_body():
            started["refusal"] = True
            yield "event: done\ndata: {}\n\n"

        refusal = StreamingResponse(refusal_body(), media_type="text/event-stream")
        with patch.object(routes, "resolve_turn_agent", AsyncMock(return_value=refusal)):
            body = _invoke(_prepare_request(rag_assistant_id="ast-1"))

        assert body["prepared"] is False and body["reason"] == "refused"
        assert started["refusal"] is False
        world.get_agent.assert_not_called()

    def test_another_users_session_is_refused_as_a_body_not_a_404(self, world):
        with patch.object(routes, "load_session_meta", _meta({"userId": "bob"}, owned_by_other=True)):
            body = _invoke(_prepare_request())

        assert body["prepared"] is False and body["reason"] == "refused"
        assert body["warmed"] is True
        world.get_agent.assert_not_called()

    def test_an_error_in_the_build_is_a_body_and_clears_the_in_flight_record(self, world):
        """The safe half of the contract: a 200 body, nothing built, nothing persisted, record cleared."""
        streams = _Streams()
        world.get_agent.side_effect = RuntimeError("tool registry exploded")

        with patch.object(routes, "stream_conversational_message", streams):
            body = _invoke(_prepare_request())

        assert body["warmed"] is True and body["prepared"] is False
        assert body["reason"] in ("error", "refused")
        assert streams.ran == 0, "the turn path's canned error stream must not run for a prepare"
        assert prepare_mode._in_flight == {}
        assert prepare_mode.prepared_age_ms("alice", SESSION) is None

    def test_an_error_in_the_build_is_reported_as_error(self, world):
        world.get_agent.side_effect = RuntimeError("tool registry exploded")

        assert _invoke(_prepare_request())["reason"] == "error"

    def test_an_error_before_the_build_is_also_a_body(self, world):
        with patch.object(routes, "_resolve_effective_tools", AsyncMock(side_effect=ValueError("bad"))):
            body = _invoke(_prepare_request())

        assert body["prepared"] is False and body["reason"] in ("error", "refused")
        assert prepare_mode._in_flight == {}

    def test_after_an_error_the_next_prepare_may_begin(self, world):
        world.get_agent.side_effect = [RuntimeError("once"), world.agent]

        first = _invoke(_prepare_request())
        second = _invoke(_prepare_request())

        assert first["prepared"] is False
        assert second["prepared"] is True

    def test_a_refusal_is_recorded_not_raised(self, world):
        with patch.object(routes, "load_session_meta", _meta(owned_by_other=True)), patch.object(
            routes, "emit_prepare_record"
        ) as record:
            _invoke(_prepare_request())

        assert record.call_args.kwargs["outcome"].reason == "refused"


# --------------------------------------------------------------------------- 9. uncacheable


def _turn_tools(*, extra: list, described: bool) -> TurnTools:
    return TurnTools(
        extra_tools=extra,
        document_tools=[],
        extra_tools_key_described=described,
        memory_binding_key=None,
    )


class TestUncacheable:
    def test_injected_tools_the_key_does_not_describe_are_not_built(self, world):
        with patch.object(routes, "_build_turn_tools", AsyncMock(return_value=_turn_tools(extra=[object()], described=False))):
            body = _invoke(_prepare_request())

        assert body["prepared"] is False and body["reason"] == "uncacheable"
        world.get_agent.assert_not_called()

    def test_key_described_tools_with_the_cache_flag_off_are_not_built(self, world):
        with patch.object(
            routes, "_build_turn_tools", AsyncMock(return_value=_turn_tools(extra=[object()], described=True))
        ), patch.object(routes, "agent_cache_injected_tools_enabled", return_value=False):
            body = _invoke(_prepare_request())

        assert body["reason"] == "uncacheable"
        world.get_agent.assert_not_called()

    def test_key_described_tools_with_the_flag_on_are_built(self, world):
        with patch.object(
            routes, "_build_turn_tools", AsyncMock(return_value=_turn_tools(extra=[object()], described=True))
        ), patch.object(routes, "agent_cache_injected_tools_enabled", return_value=True):
            body = _invoke(_prepare_request())

        assert body["prepared"] is True
        assert world.get_agent.await_count == 1

    def test_no_injected_tools_is_always_cacheable(self, world):
        with patch.object(routes, "agent_cache_injected_tools_enabled", return_value=False):
            assert _invoke(_prepare_request())["prepared"] is True


# --------------------------------------------------------------------------- 10. cacheHit


class TestCacheHit:
    def test_true_when_get_agent_returned_without_a_new_build(self, world):
        world.new_build = False

        body = _invoke(_prepare_request())

        assert body["prepared"] is True and body["cacheHit"] is True

    def test_false_when_get_agent_built(self, world):
        body = _invoke(_prepare_request())

        assert body["prepared"] is True and body["cacheHit"] is False

    def test_the_second_of_two_prepares_through_the_real_cache_is_a_hit(self, monkeypatch):
        service.clear_agent_cache()
        monkeypatch.setattr(service, "create_agent", lambda **kw: _cacheable_agent(**kw))
        monkeypatch.setattr(routes, "is_quota_enforcement_enabled", lambda: False)
        monkeypatch.setattr("apis.shared.sessions.session_lease.is_session_lease_held", AsyncMock(return_value=False))
        monkeypatch.setattr("apis.shared.tools.freshness.get_freshness_hash", AsyncMock(return_value="fresh"))
        monkeypatch.setattr("apis.shared.tools.freshness.get_disabled_tool_ids", AsyncMock(return_value=frozenset()))
        try:
            first = _invoke(_prepare_request())
            second = _invoke(_prepare_request())
        finally:
            service.clear_agent_cache()

        assert (first["cacheHit"], second["cacheHit"]) == (False, True)


# --------------------------------------------------------------------------- 11. in flight


class _Gate:
    """A ``get_agent`` whose first call parks until released; later calls return at once."""

    def __init__(self, world: World, *, fail_first: bool = False) -> None:
        self.world = world
        self.release = asyncio.Event()
        self.first_entered = asyncio.Event()
        self.events: List[str] = []
        self.fail_first = fail_first
        self._calls = 0

    async def call(self, **kwargs: Any) -> Any:
        index = self._calls
        self._calls += 1
        if index == 0:
            self.events.append("first_start")
            self.first_entered.set()
            await self.release.wait()
            self.events.append("first_end")
            if self.fail_first:
                raise RuntimeError("first build failed")
        else:
            self.events.append(f"call_{index}_start")
        self.world.builds += 1
        return self.world.agent


class TestInFlight:
    def test_a_second_prepare_for_the_same_user_and_session_is_refused(self, world):
        gate = _Gate(world)
        world.get_agent.side_effect = gate.call

        async def go():
            first = asyncio.create_task(routes.invocations(_prepare_request(), current_user=_user()))
            await asyncio.wait_for(gate.first_entered.wait(), 2)
            second = await routes.invocations(_prepare_request(), current_user=_user())
            gate.release.set()
            return await first, second

        first, second = asyncio.run(go())

        assert second["prepared"] is False and second["reason"] == "in_flight"
        assert second["warmed"] is True
        assert first["prepared"] is True
        assert world.get_agent.await_count == 1

    def test_a_refused_second_prepare_does_not_clear_the_first_ones_record(self, world):
        gate = _Gate(world)
        world.get_agent.side_effect = gate.call

        async def go():
            first = asyncio.create_task(routes.invocations(_prepare_request(), current_user=_user()))
            await asyncio.wait_for(gate.first_entered.wait(), 2)
            await routes.invocations(_prepare_request(), current_user=_user())
            still_in_flight = ("alice", SESSION) in prepare_mode._in_flight
            gate.release.set()
            await first
            return still_in_flight

        assert asyncio.run(go()) is True
        assert prepare_mode._in_flight == {}

    def test_another_user_on_the_same_session_id_is_not_blocked(self, world):
        gate = _Gate(world)
        world.get_agent.side_effect = gate.call

        async def go():
            alices = asyncio.create_task(routes.invocations(_prepare_request(), current_user=_user("alice")))
            await asyncio.wait_for(gate.first_entered.wait(), 2)
            bobs = await asyncio.wait_for(routes.invocations(_prepare_request(), current_user=_user("bob")), 2)
            gate.release.set()
            return await alices, bobs

        alices, bobs = asyncio.run(go())

        assert bobs["prepared"] is True and bobs["reason"] is None
        assert alices["prepared"] is True

    def test_a_turn_whose_build_starts_during_a_prepare_waits_for_it(self, world):
        gate = _Gate(world)
        world.get_agent.side_effect = gate.call

        async def go():
            prepare = asyncio.create_task(routes.invocations(_prepare_request(), current_user=_user()))
            await asyncio.wait_for(gate.first_entered.wait(), 2)
            turn_response = await routes.invocations(_turn_request(), current_user=_user())
            turn = asyncio.create_task(_drain(turn_response))
            for _ in range(20):
                await asyncio.sleep(0.01)
            events_while_parked = list(gate.events)
            gate.release.set()
            prepare_body = await asyncio.wait_for(prepare, 2)
            await asyncio.wait_for(turn, 2)
            return events_while_parked, prepare_body

        parked, prepare_body = asyncio.run(go())

        assert parked == ["first_start"], "the turn built its own agent while the prepare was still building"
        assert gate.events == ["first_start", "first_end", "call_1_start"]
        assert prepare_body["prepared"] is True
        assert world.get_agent.await_count == 2

    def test_after_a_failed_prepare_the_turn_builds_its_own(self, world):
        gate = _Gate(world, fail_first=True)
        world.get_agent.side_effect = gate.call

        async def go():
            prepare = asyncio.create_task(routes.invocations(_prepare_request(), current_user=_user()))
            await asyncio.wait_for(gate.first_entered.wait(), 2)
            turn_response = await routes.invocations(_turn_request(), current_user=_user())
            turn = asyncio.create_task(_drain(turn_response))
            await asyncio.sleep(0.05)
            gate.release.set()
            prepare_body = await asyncio.wait_for(prepare, 2)
            chunks = await asyncio.wait_for(turn, 2)
            return prepare_body, chunks

        prepare_body, chunks = asyncio.run(go())

        assert prepare_body["prepared"] is False and prepare_body["reason"] in ("error", "refused")
        assert gate.events == ["first_start", "first_end", "call_1_start"]
        assert chunks, "the turn must still stream"

    def test_a_turn_with_no_prepare_in_flight_does_not_wait(self, world):
        _run_turn(_turn_request())

        assert world.get_agent.await_count == 1

    def test_a_prepare_never_waits_on_itself(self, world):
        """A prepare calls ``_build_main_agent`` while its own record is open; waiting on it would deadlock."""
        waited = AsyncMock()
        with patch.object(prepare_mode, "wait_for_in_flight", waited):
            body = _invoke(_prepare_request())

        assert body["prepared"] is True
        waited.assert_not_called()

    def test_a_turn_awaits_the_in_flight_record_for_its_own_user_and_session(self, world):
        waited = AsyncMock()
        with patch.object(prepare_mode, "wait_for_in_flight", waited):
            _run_turn(_turn_request(), _user("carol"))

        waited.assert_awaited_once_with("carol", SESSION)


# --------------------------------------------------------------------------- 12. preparedAgeMs


class TestPreparedAgeOnTheTurnPrelude:
    def _prelude_extras(self, request: InvocationRequest, user: User) -> List[dict]:
        extras: List[dict] = []

        def capture(self, **kwargs):
            extras.append(kwargs.get("extra") or {})

        with patch.object(TurnPrelude, "emit", capture):
            _run_turn(request, user)
        return extras

    def test_none_without_a_prepare(self, world):
        extras = self._prelude_extras(_turn_request(), _user())

        assert len(extras) == 1
        assert "preparedAgeMs" in extras[0] and extras[0]["preparedAgeMs"] is None

    def test_a_non_negative_int_after_a_successful_prepare(self, world):
        assert _invoke(_prepare_request())["prepared"] is True

        extras = self._prelude_extras(_turn_request(), _user())

        assert isinstance(extras[0]["preparedAgeMs"], int) and extras[0]["preparedAgeMs"] >= 0

    def test_none_for_another_user_on_the_same_session(self, world):
        assert _invoke(_prepare_request())["prepared"] is True

        extras = self._prelude_extras(_turn_request(), _user("bob"))

        assert extras[0]["preparedAgeMs"] is None

    def test_none_after_a_prepare_that_built_nothing(self, world):
        world.lease_held.return_value = True
        assert _invoke(_prepare_request())["reason"] == "turn_active"
        world.lease_held.return_value = False

        extras = self._prelude_extras(_turn_request(), _user())

        assert extras[0]["preparedAgeMs"] is None


# --------------------------------------------------------------------------- the A/B arm record


class TestPrepareArm:
    def _extras(self, request: InvocationRequest, user: User) -> dict:
        extras: List[dict] = []

        def capture(self, **kwargs):
            extras.append(kwargs.get("extra") or {})

        with patch.object(TurnPrelude, "emit", capture):
            _run_turn(request, user)
        return extras[0]

    def test_a_holdout_warm_is_the_bare_acknowledgement_with_no_reads_or_lease(self, world):
        tripwire = AsyncMock(side_effect=AssertionError("a holdout warm must not read session state"))
        with patch.object(routes, "load_session_meta", tripwire):
            result = _invoke(InvocationRequest(session_id=SESSION, warm=True, prepare_arm="holdout", **SELECTION))

        assert result == {"warmed": True}
        tripwire.assert_not_called()
        world.get_agent.assert_not_called()
        assert world.acquire.await_count == 0
        assert prepare_mode.arm_of("alice", SESSION) == "holdout"

    def test_a_turn_after_a_holdout_warm_reports_holdout(self, world):
        _invoke(InvocationRequest(session_id=SESSION, warm=True, prepare_arm="holdout"))

        assert self._extras(_turn_request(), _user())["prepareArm"] == "holdout"

    def test_a_holdout_for_one_user_is_not_another_users_arm(self, world):
        _invoke(InvocationRequest(session_id=SESSION, warm=True, prepare_arm="holdout"))

        assert self._extras(_turn_request(), _user("bob"))["prepareArm"] is None

    def test_a_prepare_then_a_turn_reports_treated(self, world):
        assert _invoke(_prepare_request())["prepared"] is True

        assert self._extras(_turn_request(), _user())["prepareArm"] == "treated"

    def test_treated_even_when_the_prepare_built_nothing(self, world):
        quota = TurnQuota(exceeded_event=MagicMock(message="over"))
        with patch.object(routes, "_check_turn_quota", AsyncMock(return_value=quota)):
            assert _invoke(_prepare_request())["reason"] == "quota"

        assert self._extras(_turn_request(), _user())["prepareArm"] == "treated"

    def test_a_turn_with_neither_reports_none(self, world):
        extras = self._extras(_turn_request(), _user())

        assert "prepareArm" in extras and extras["prepareArm"] is None

    @pytest.mark.parametrize("fields", [{}, {"prepare_arm": "treated"}, {"prepare_arm": "bogus"}, {"prepare_arm": ""}])
    def test_a_plain_warm_without_the_holdout_value_records_nothing(self, world, fields):
        result = _invoke(InvocationRequest(session_id=SESSION, warm=True, **fields))

        assert result == {"warmed": True}
        assert prepare_mode.arm_of("alice", SESSION) is None

    def test_a_prepare_cannot_be_relabelled_holdout(self, world):
        body = _invoke(_prepare_request(prepare_arm="holdout"))

        assert body["prepared"] is True
        assert prepare_mode.arm_of("alice", SESSION) == "treated"
