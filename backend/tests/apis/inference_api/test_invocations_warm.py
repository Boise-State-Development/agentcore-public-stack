"""The `warm` invocation (docs/specs/agentcore-runtime-v2.md §5a).

app-api's `POST /chat/prewarm` sends `{"session_id": ..., "warm": true}` so
AgentCore starts the conversation's microVM before the first send. Reaching
the handler is the whole point; past that it must do nothing a real turn does:
no ownership read, no rows, no single-flight lease (the user's first send would
collide with it), no quota, no model.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from apis.inference_api.chat import routes
from apis.inference_api.chat.models import InvocationRequest
from apis.shared.auth.models import User


def _user() -> User:
    user = User(email="alice@example.com", user_id="user-sub", name="Alice", roles=["user"])
    user.raw_token = "access.token.value"
    return user


def test_warm_returns_before_any_turn_work() -> None:
    tripwire = AsyncMock(side_effect=AssertionError("a warm call must not touch session state"))
    with patch.object(routes, "load_session_meta", tripwire):
        result = asyncio.run(
            routes.invocations(InvocationRequest(session_id="conv-1", warm=True), current_user=_user())
        )
    assert result == {"warmed": True}
    tripwire.assert_not_called()


def test_warm_ignores_a_message_and_every_other_field() -> None:
    tripwire = AsyncMock(side_effect=AssertionError("must not run"))
    request = InvocationRequest(
        session_id="conv-1",
        warm=True,
        message="this would be a real turn",
        model_id="some-model",
        enabled_tools=["calculator"],
    )
    with patch.object(routes, "load_session_meta", tripwire):
        assert asyncio.run(routes.invocations(request, current_user=_user())) == {"warmed": True}


def test_a_request_without_warm_still_runs_the_turn_path() -> None:
    # Absent and false both mean a real turn: the ownership read is reached.
    sentinel = RuntimeError("reached the ownership read")
    for request in (
        InvocationRequest(session_id="conv-1", message="hi"),
        InvocationRequest(session_id="conv-1", message="hi", warm=False),
    ):
        with patch.object(routes, "load_session_meta", AsyncMock(side_effect=sentinel)):
            try:
                asyncio.run(routes.invocations(request, current_user=_user()))
            except RuntimeError as exc:
                assert exc is sentinel
            else:
                raise AssertionError("a real turn returned without reading the session")
