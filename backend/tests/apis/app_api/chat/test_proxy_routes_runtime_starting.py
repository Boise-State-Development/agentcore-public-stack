"""A turn the AgentCore Runtime refuses while another invocation starts its session.

Found on dev (2026-11-21) validating saved workflows: the first message in a
conversation a workflow started was refused as "Already responding" with nothing
streaming. No turn lease held it. The composer's first keystroke fired
`POST /chat/prewarm`; the Runtime began a V2 snapshot restore for that runtime
session (the warm forward took 2,017 ms); the send 270 ms later was answered
**409 by the Runtime data plane itself** and never reached the container.

Pinned here: a raw 409 from the Runtime is waited out and the turn resent (on
this task's in-flight warm call when there is one, else by polling, within a
budget), while our container's own single-flight 409, which always arrives
wrapped in a 424, and a raw 409 from a local inference-api are relayed at once.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import List

import httpx
import pytest
from fastapi import FastAPI

from apis.app_api.chat import proxy_routes
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User

_RUNTIME_URL = (
    "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
    "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/foo-AbCdEf"
)
_SSE_DONE = b"event: done\ndata: {}\n\n"


def _user() -> User:
    user = User(email="alice@example.com", user_id="user-sub", name="Alice", roles=["user"])
    user.raw_token = "access.token.value"
    return user


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(proxy_routes.router)
    app.dependency_overrides[get_current_user_from_session] = _user
    return app


def _sse() -> httpx.Response:
    return httpx.Response(200, content=_SSE_DONE, headers={"content-type": "text/event-stream"})


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    proxy_routes._prewarm_ledger.reset()
    proxy_routes._prewarm_inflight.clear()
    monkeypatch.setenv("SESSION_PREWARM_ENABLED", "true")
    monkeypatch.delenv("RUNTIME_SESSION_AFFINITY_ENABLED", raising=False)
    monkeypatch.setenv("INFERENCE_API_URL", _RUNTIME_URL)
    yield
    proxy_routes._prewarm_ledger.reset()
    proxy_routes._prewarm_inflight.clear()


def _patch_turns(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(proxy_routes, "_build_upstream_client", lambda: httpx.AsyncClient(transport=transport))


async def _post(app: FastAPI, path: str, payload: dict) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        return await client.post(path, json=payload)


def test_a_send_during_the_prewarm_waits_for_it_then_goes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dev failure, end to end on one event loop: warm first, send while it is still starting."""
    # Polling would take far longer than this test allows, so only waiting on the warm call passes.
    monkeypatch.setattr(proxy_routes, "_RUNTIME_STARTING_POLL_SECONDS", (60.0,))
    starting = {"now": False}
    warm_done_at: List[float] = []
    turn_calls: List[float] = []

    async def warm_handler(request: httpx.Request) -> httpx.Response:
        starting["now"] = True
        await asyncio.sleep(0.2)  # the snapshot restore
        starting["now"] = False
        warm_done_at.append(time.monotonic())
        return httpx.Response(200, json={"warmed": True})

    def turn_handler(request: httpx.Request) -> httpx.Response:
        turn_calls.append(time.monotonic())
        if starting["now"]:
            return httpx.Response(409, json={"message": "Session is starting"})
        return _sse()

    monkeypatch.setattr(
        proxy_routes,
        "_build_prewarm_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(warm_handler)),
    )
    _patch_turns(monkeypatch, turn_handler)

    async def scenario() -> httpx.Response:
        app = _app()
        warmed = await _post(app, "/chat/prewarm", {"session_id": "conv-1"})
        assert warmed.json() == {"status": "accepted"}
        await asyncio.sleep(0.02)  # the warm call is now in flight
        return await _post(app, "/chat/stream", {"session_id": "conv-1", "message": "hi"})

    started = time.monotonic()
    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.content == _SSE_DONE
    assert len(turn_calls) == 2
    assert turn_calls[1] >= warm_done_at[0]
    assert time.monotonic() - started < 5


def test_a_raw_runtime_409_with_no_warm_here_is_polled_until_the_session_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warm call may have gone through another app-api task: poll instead."""
    monkeypatch.setattr(proxy_routes, "_RUNTIME_STARTING_POLL_SECONDS", (0.01,))
    calls: List[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content)
        return httpx.Response(409, json={"message": "starting"}) if len(calls) < 3 else _sse()

    _patch_turns(monkeypatch, handler)
    body = {"session_id": "conv-1", "message": "hi"}
    response = asyncio.run(_post(_app(), "/chat/stream", body))

    assert response.status_code == 200
    # The same turn each time, byte for byte.
    assert [json.loads(c) for c in calls] == [body, body, body]


def test_a_raw_runtime_409_that_outlasts_the_budget_is_relayed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy_routes, "_RUNTIME_STARTING_POLL_SECONDS", (0.01,))
    monkeypatch.setattr(proxy_routes, "_RUNTIME_STARTING_BUDGET_SECONDS", 0.05)
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(409, json={"message": "starting"})

    _patch_turns(monkeypatch, handler)
    response = asyncio.run(_post(_app(), "/chat/stream", {"session_id": "conv-1", "message": "hi"}))

    assert response.status_code == 409
    assert 1 < len(calls) < 20


def test_our_own_single_flight_409_is_never_resent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The container's lease conflict arrives as a 424 naming the 409: a real turn is streaming."""
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            424, content=b'{"message":"Received error (409) from runtime. Please check your CloudWatch logs."}'
        )

    _patch_turns(monkeypatch, handler)
    response = asyncio.run(_post(_app(), "/chat/stream", {"session_id": "conv-1", "message": "hi"}))

    assert response.status_code == 409
    assert calls == [1]


def test_a_local_inference_api_409_is_the_lease_and_is_relayed_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Locally there is no Runtime in front, so a raw 409 is the container's lease."""
    monkeypatch.setenv("INFERENCE_API_URL", "http://localhost:8001")
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(409, json={"detail": "A response is already streaming for this conversation."})

    _patch_turns(monkeypatch, handler)
    response = asyncio.run(_post(_app(), "/chat/stream", {"session_id": "conv-1", "message": "hi"}))

    assert response.status_code == 409
    assert calls == [1]


def test_an_accepted_turn_sends_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _sse()

    _patch_turns(monkeypatch, handler)
    response = asyncio.run(_post(_app(), "/chat/stream", {"session_id": "conv-1", "message": "hi"}))

    assert response.status_code == 200
    assert calls == [1]


def test_a_finished_warm_call_leaves_no_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        proxy_routes,
        "_build_prewarm_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
    )

    async def scenario() -> None:
        await _post(_app(), "/chat/prewarm", {"session_id": "conv-1"})
        for _ in range(100):
            if not proxy_routes._prewarm_inflight:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert proxy_routes._prewarm_inflight == {}
