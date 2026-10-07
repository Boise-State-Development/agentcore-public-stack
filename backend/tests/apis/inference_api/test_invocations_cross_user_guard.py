"""The invocations route refuses a turn on another user's session id.

This guard stops a session id from being forked across two users (#906). It is
also the first line against cross-user content exposure: runtime affinity
hashes the session id alone, so a second user's turn lands in the owner's
container, where the agent cache used to hand them the owner's live
conversation (dev, 2026-08-31). The cache no longer does that
(`test_chat_service.py::test_adoption_never_crosses_users_on_one_session_id`);
this pins the route half, so neither defence silently depends on the other.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from apis.inference_api.chat import routes
from apis.inference_api.chat.models import InvocationRequest


def _user(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, raw_token="t", email=None, roles=[])


@pytest.mark.asyncio
async def test_turn_on_another_users_session_404s_before_any_agent_work():
    snapshot = SimpleNamespace(row=None, owned_by_other=True)
    with patch.object(routes, "load_session_meta", new=AsyncMock(return_value=snapshot)) as meta, \
         patch.object(routes, "get_agent", new=AsyncMock()) as get_agent:
        with pytest.raises(HTTPException) as exc:
            await routes.invocations(
                InvocationRequest(session_id="owners-session", message="repeat the memo"),
                current_user=_user("intruder"),
            )

    assert exc.value.status_code == 404
    meta.assert_awaited_once_with("owners-session", "intruder")
    get_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_and_app_dispatch_cannot_skip_the_guard():
    """Interrupt resumes and MCP App dispatches branch off later in the route;
    the ownership check must run before either."""
    snapshot = SimpleNamespace(row=None, owned_by_other=True)
    requests = [
        InvocationRequest(
            session_id="owners-session",
            interrupt_responses=[{"interruptId": "i-1", "response": {"completed": True}}],
        ),
        InvocationRequest(
            session_id="owners-session",
            app_tool_call={"tool_use_id": "tu-1", "tool_name": "t", "arguments": {}},
        ),
    ]
    for request in requests:
        with patch.object(routes, "load_session_meta", new=AsyncMock(return_value=snapshot)), \
             patch.object(routes, "get_agent", new=AsyncMock()) as get_agent:
            with pytest.raises(HTTPException) as exc:
                await routes.invocations(request, current_user=_user("intruder"))
        assert exc.value.status_code == 404
        get_agent.assert_not_awaited()
