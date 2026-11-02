"""``handoff_context`` through the real ``/invocations`` handler (handoffs spec §4.2, PR-0.2).

``HandoffTurn`` itself is tested in ``tests/apis/shared/handoff/test_handoff_turn.py``. This pins how
the route uses it, which is where the TTFT and display contracts live:

1. Acceptance starts at the head of the stream, **before** the agent build, so its round trips
   overlap the build instead of adding to the first token.
2. The wrapped block is prepended to the model message, and ``original_message`` is what the user
   typed, so the bubble shows only that.
3. Every frame the turn streams is observed, and the status write is scheduled once, after the
   stream, with ``disconnected=False`` on a normal end.
4. The request field reaches ``for_turn`` parsed, with the turn's flags.
"""

from __future__ import annotations

from typing import Any, List, Optional
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import apis.inference_api.chat.routes as routes_module
from apis.inference_api.chat.routes import router
from apis.shared.auth.dependencies import get_current_user_trusted

MESSAGE = "draft a rubric for this"
BLOCK = '<handoff_context from_conversation="Course planning">\nctx\n</handoff_context>'


@pytest.fixture
def client(make_user):
    app = FastAPI()
    app.include_router(router)
    user = make_user(raw_token="fake-jwt-token", user_id="user-1")
    app.dependency_overrides[get_current_user_trusted] = lambda: user
    return TestClient(app)


class _FakeHandoffTurn:
    def __init__(self, log: List[str], block: str = BLOCK) -> None:
        self.log = log
        self.block = block
        self.observed: List[str] = []
        self.status_calls: List[bool] = []

    def start(self) -> None:
        self.log.append("handoff.start")

    async def context_block(self) -> str:
        self.log.append("handoff.context_block")
        return self.block

    def observe(self, chunk: str) -> None:
        self.observed.append(chunk)

    def schedule_status_write(self, *, disconnected: bool) -> None:
        self.status_calls.append(disconnected)


class _Turn:
    def __init__(self, handoff: Optional[_FakeHandoffTurn]) -> None:
        self.log: List[str] = []
        self.handoff = handoff
        if handoff is not None:
            handoff.log = self.log
        self.final_message: Any = None
        self.stream_kwargs: dict = {}
        self.for_turn_kwargs: dict = {}

    def get_agent(self, *args, **kwargs):
        self.log.append("agent.build")
        turn = self

        class _Agent:
            async def stream_async(self, message, **kw):
                turn.final_message = message
                turn.stream_kwargs = kw
                yield 'event: message_start\ndata: {"role": "assistant"}\n\n'
                yield 'event: tool_group_summary\ndata: {"summary": "Drafted it"}\n\n'
                yield "event: done\ndata: {}\n\n"

        return _Agent()

    def for_turn(self, **kwargs):
        self.for_turn_kwargs = kwargs
        return self.handoff

    def post(self, client: TestClient, body: dict):
        with patch.object(routes_module, "get_agent", side_effect=self.get_agent), patch.object(
            routes_module, "is_quota_enforcement_enabled", return_value=False
        ), patch.object(routes_module.HandoffTurn, "for_turn", side_effect=self.for_turn):
            return client.post("/invocations", json={"session_id": "preview-h1", "message": MESSAGE, **body})


def test_acceptance_starts_before_the_build_and_is_awaited_after_it(client):
    turn = _Turn(_FakeHandoffTurn([]))

    resp = turn.post(client, {"handoff_context": {"handoffId": "h1", "text": "ctx"}})

    assert resp.status_code == 200
    assert turn.log == ["handoff.start", "agent.build", "handoff.context_block"]


def test_the_block_is_prepended_and_the_bubble_shows_only_what_was_typed(client):
    turn = _Turn(_FakeHandoffTurn([]))

    turn.post(client, {"handoff_context": {"handoffId": "h1", "text": "ctx"}})

    assert turn.final_message == f"{BLOCK}\n\n{MESSAGE}"
    assert turn.stream_kwargs["original_message"] == MESSAGE


def test_a_removed_card_leaves_the_message_as_typed(client):
    turn = _Turn(_FakeHandoffTurn([], block=""))

    turn.post(client, {"handoff_context": {"handoffId": "h1"}})

    assert turn.final_message == MESSAGE
    assert turn.stream_kwargs["original_message"] is None


def test_every_frame_is_observed_and_the_status_is_written_once_after_the_stream(client):
    handoff = _FakeHandoffTurn([])
    body = _Turn(handoff).post(client, {"handoff_context": {"handoffId": "h1", "text": "ctx"}}).text

    observed = "".join(handoff.observed)
    assert "event: tool_group_summary" in observed
    assert "event: done" in observed
    assert observed in body
    assert handoff.status_calls == [False]


def test_the_field_reaches_for_turn_parsed_with_the_turns_flags(client):
    turn = _Turn(None)

    turn.post(client, {"handoff_context": {"handoffId": "h1", "text": "ctx"}})

    kwargs = turn.for_turn_kwargs
    assert kwargs["handoff_context"].handoff_id == "h1"
    assert kwargs["handoff_context"].text == "ctx"
    assert (kwargs["is_resume"], kwargs["is_continuation"], kwargs["is_preview"]) == (False, False, True)
    assert kwargs["session_id"] == "preview-h1"


def test_an_ordinary_turn_is_unchanged(client):
    turn = _Turn(None)

    turn.post(client, {})

    assert turn.final_message == MESSAGE
    assert turn.for_turn_kwargs["handoff_context"] is None


def test_the_real_gate_never_makes_a_preview_session_a_target(client, monkeypatch):
    """Without the patch: a preview session gets no handoff, even with the flag on."""
    monkeypatch.setenv("HANDOFFS_ENABLED", "true")
    captured = {}

    def get_agent(*args, **kwargs):
        class _Agent:
            async def stream_async(self, message, **kw):
                captured["message"] = message
                yield "event: done\ndata: {}\n\n"

        return _Agent()

    with patch.object(routes_module, "get_agent", side_effect=get_agent), patch.object(
        routes_module, "is_quota_enforcement_enabled", return_value=False
    ):
        resp = client.post(
            "/invocations",
            json={"session_id": "preview-h1", "message": MESSAGE, "handoff_context": {"handoffId": "h1", "text": "c"}},
        )

    assert resp.status_code == 200
    assert captured["message"] == MESSAGE
