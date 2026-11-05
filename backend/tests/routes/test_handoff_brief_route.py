"""A ``handoff_brief`` invocation through the real ``/invocations`` handler (handoffs spec §4.5.1).

The property the brief depends on is that it builds the agent a turn on the same conversation
would. The strongest test of that is a direct one: send the same body as a turn and as a brief and
compare what ``get_agent`` is asked for. The rest pins what the brief must never do (write the
session, persist a refusal) and how each early exit becomes one fallback frame.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import apis.inference_api.chat.handoff_brief as brief_module
import apis.inference_api.chat.routes as routes_module
from apis.inference_api.chat.routes import TurnQuota, router
from apis.shared.auth.dependencies import get_current_user_trusted
from apis.shared.handoff.models import Handoff

BODY = {"session_id": "src", "message": "draft a rubric for this", "model_id": None, "enabled_tools": ["web_search"]}


def _frames(body: str) -> List[Dict[str, Any]]:
    out = []
    for chunk in body.split("\n\n"):
        if not chunk.startswith("event: "):
            continue
        head, data = chunk.split("\ndata: ", 1)
        out.append({"event": head.removeprefix("event: "), "data": json.loads(data)})
    return out


def _handoff(user_id: str, **overrides) -> Handoff:
    now = datetime.now(timezone.utc).isoformat()
    fields = dict(
        handoff_id="h1",
        user_id=user_id,
        session_id="src",
        kind="mention",
        agent_id="rubric",
        agent_name="Rubric Builder",
        request="draft a rubric for this",
        created_at=now,
        updated_at=now,
    )
    fields.update(overrides)
    return Handoff(**fields)


@pytest.fixture
def user(make_user):
    return make_user(raw_token="fake-jwt-token", user_id="alice")


@pytest.fixture
def client(user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_trusted] = lambda: user
    return TestClient(app)


@pytest.fixture
def flags(monkeypatch):
    monkeypatch.setenv("HANDOFFS_ENABLED", "true")
    monkeypatch.setenv("HANDOFF_BRIEF_ENABLED", "true")


class _Store:
    def __init__(self, row: Optional[Handoff]) -> None:
        self.row = row

    def get(self, user_id: str, handoff_id: str) -> Optional[Handoff]:
        return self.row if self.row is not None and self.row.user_id == user_id else None


def _agent() -> MagicMock:
    agent = MagicMock()

    async def fake_stream(*args, **kwargs):
        yield "event: done\ndata: {}\n\n"

    agent.stream_async = fake_stream
    return agent


def _post(client, body, *, store_row=None, get_agent=None, extra_patches=(), brief=True):
    """POST ``body`` (as a brief unless ``brief=False``), with the brief's side call stubbed."""
    get_agent = get_agent or AsyncMock(return_value=_agent())
    seen: Dict[str, Any] = {}

    async def fake_brief(**kwargs):
        seen.update(kwargs)
        await kwargs["build_agent"]()
        await kwargs["release_lease"]()
        yield 'event: done\ndata: {"type": "done", "handoffId": "h1"}\n\n'

    payload = dict(body)
    if brief:
        payload["handoff_brief"] = {"handoffId": "h1"}
    patches = [
        patch.object(routes_module, "get_agent", get_agent),
        patch.object(routes_module, "is_quota_enforcement_enabled", return_value=False),
        patch("apis.shared.handoff.get_handoff_store", return_value=_Store(store_row)),
        patch.object(brief_module, "stream_handoff_brief", fake_brief),
        *extra_patches,
    ]
    for p in patches:
        p.start()
    try:
        response = client.post("/invocations", json=payload)
        text = response.text
    finally:
        for p in reversed(patches):
            p.stop()
    return response, text, get_agent, seen


def _kwargs(call) -> Dict[str, Any]:
    """``get_agent``'s arguments, minus the per-call recorders (closures, never compared)."""
    return {k: v for k, v in call.kwargs.items() if k not in ("build_stage_recorder", "build_detail_recorder")}


class TestTheSameAgent:
    def test_a_brief_asks_get_agent_for_exactly_what_the_turn_did(self, client, user, flags):
        _, _, turn_get_agent, _ = _post(client, BODY, brief=False)
        response, _, brief_get_agent, seen = _post(client, BODY, store_row=_handoff(user.user_id))

        assert response.status_code == 200
        assert turn_get_agent.await_count == brief_get_agent.await_count == 1
        assert _kwargs(brief_get_agent.await_args) == _kwargs(turn_get_agent.await_args)
        assert seen["handoff"].handoff_id == "h1"

    def test_a_brief_writes_nothing_to_the_session_before_the_call(self, client, user, flags):
        never = AsyncMock(side_effect=AssertionError("a brief must not run this phase"))
        response, _, _, _ = _post(
            client,
            BODY,
            store_row=_handoff(user.user_id),
            extra_patches=[
                patch.object(routes_module, "_resolve_turn_attachments", never),
                patch.object(routes_module, "_prepare_session_state", never),
            ],
        )

        assert response.status_code == 200
        assert never.await_count == 0


class TestFallbacks:
    def test_flag_off(self, client, user, monkeypatch):
        monkeypatch.setenv("HANDOFFS_ENABLED", "true")
        monkeypatch.delenv("HANDOFF_BRIEF_ENABLED", raising=False)

        _, text, get_agent, _ = _post(client, BODY, store_row=_handoff(user.user_id))

        assert _frames(text)[0]["data"] == {"type": "handoff_brief_fallback", "handoffId": "h1", "reason": "flag_off"}
        assert get_agent.await_count == 0

    @pytest.mark.parametrize(
        "row",
        [
            None,
            {"session_id": "another-conversation"},
            {"status": "dismissed"},
            {"status": "running", "target_session_id": "tgt"},
        ],
    )
    def test_a_handoff_that_is_not_this_conversations_open_one(self, client, user, flags, row):
        store_row = None if row is None else _handoff(user.user_id, **row)

        _, text, get_agent, _ = _post(client, BODY, store_row=store_row)

        assert _frames(text)[0]["data"]["reason"] == "not_open"
        assert get_agent.await_count == 0

    def test_another_users_handoff_reads_as_absent(self, client, flags):
        _, text, _, _ = _post(client, BODY, store_row=_handoff("bob"))

        assert _frames(text)[0]["data"]["reason"] == "not_open"

    def test_quota_is_its_own_reason_and_persists_nothing(self, client, user, flags):
        quota = TurnQuota(exceeded_event=MagicMock(message="over"))
        persisted = AsyncMock(side_effect=AssertionError("a brief must not persist a refusal"))

        _, text, get_agent, _ = _post(
            client,
            BODY,
            store_row=_handoff(user.user_id),
            extra_patches=[
                patch.object(routes_module, "_check_turn_quota", AsyncMock(return_value=quota)),
                patch.object(routes_module, "stream_conversational_message", persisted),
            ],
        )

        assert [f["event"] for f in _frames(text)] == ["handoff_brief_fallback", "done"]
        assert _frames(text)[0]["data"]["reason"] == "quota"
        assert get_agent.await_count == 0

    def test_a_refusal_from_the_turn_path_is_discarded_unstreamed(self, client, user, flags):
        started = {"refusal": False}

        async def refusal_body():
            started["refusal"] = True
            yield "event: done\ndata: {}\n\n"

        refusal = StreamingResponse(refusal_body(), media_type="text/event-stream")

        _, text, get_agent, _ = _post(
            client,
            {**BODY, "rag_assistant_id": "ast-1"},
            store_row=_handoff(user.user_id),
            extra_patches=[patch.object(routes_module, "resolve_turn_agent", AsyncMock(return_value=refusal))],
        )

        assert _frames(text)[0]["data"]["reason"] == "refused"
        assert started["refusal"] is False
        assert get_agent.await_count == 0

    def test_a_busy_source_is_busy_not_a_409(self, client, user, flags):
        from apis.shared.sessions.session_lease import SessionBusyError

        response, text, _, _ = _post(
            client,
            BODY,
            store_row=_handoff(user.user_id),
            extra_patches=[
                patch(
                    "apis.shared.sessions.session_lease.acquire_session_lease",
                    AsyncMock(side_effect=SessionBusyError("src")),
                )
            ],
        )

        assert response.status_code == 200
        assert _frames(text)[0]["data"]["reason"] == "busy"

    def test_an_agent_brief_skips_the_use_writes(self, client, user, flags):
        resolve = AsyncMock(return_value=StreamingResponse(iter(()), media_type="text/event-stream"))

        _post(
            client,
            {**BODY, "rag_assistant_id": "ast-1"},
            store_row=_handoff(user.user_id),
            extra_patches=[patch.object(routes_module, "resolve_turn_agent", resolve)],
        )

        assert resolve.await_args.kwargs == {"is_continuation": True, "record_use": False}
