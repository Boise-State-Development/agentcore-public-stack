"""``smoke_turns.py``'s RuntimeTransport must pin turns the way the harness does.

The ``--auth headless-grant`` mode reuses the harness runner's
``apply_runtime_session_header`` so a smoke turn is byte-for-byte what a
scheduled run sends. When that helper grew a ``user_id`` parameter (runtime
affinity is pinned per ``(user, session)``, see ``runtime_session_id_for``),
the script kept calling it with two arguments and every headless-grant run died
on its first turn with a ``TypeError`` (found on dev, 2026-10-09). Nothing ran
the script hermetically, so the drift was invisible until a live run.

The spy here calls straight through to the real helper, so a future signature
change fails this test rather than a validation pass on dev.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
from typing import Any, Dict, List, Optional

import httpx
import pytest

from apis.shared.harness import auth as harness_auth
from apis.shared.harness import runner

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "smoke_turns.py"
_spec = importlib.util.spec_from_file_location("smoke_turns", _SCRIPT)
smoke_turns = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# Registered before exec: the script's dataclasses resolve their module via sys.modules.
sys.modules[_spec.name] = smoke_turns
_spec.loader.exec_module(smoke_turns)

USER_ID = "user-sub-1234"
SESSION_ID = "sess-abc"
BASE_URL = "http://inference.test"


class _StubAuth:
    """Stands in for CognitoRefreshBearerAuth: no grants table, no Cognito."""

    async def mint_bearer_for_user(self, user_id: str) -> str:
        return f"bearer-for-{user_id}"


@pytest.fixture()
def header_calls(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    real_apply = runner.apply_runtime_session_header

    def spy(headers: Dict[str, str], *args: Any, **kwargs: Any) -> Dict[str, str]:
        calls.append({"args": args, "kwargs": kwargs})
        return real_apply(headers, *args, **kwargs)

    monkeypatch.setattr(runner, "apply_runtime_session_header", spy)
    monkeypatch.setattr(harness_auth, "CognitoRefreshBearerAuth", _StubAuth)
    monkeypatch.delenv(runner.AGENTCORE_SESSION_AFFINITY_ENABLED_ENV, raising=False)
    return calls


def _transport_capturing(captured: List[httpx.Request]) -> Any:
    transport = smoke_turns.RuntimeTransport(USER_ID, "test-prefix", "us-west-2", BASE_URL)

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = 'event: message_start\ndata: {"type": "message_start"}\n\nevent: done\ndata: {}\n\n'
        return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})

    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return transport


async def _one_turn(transport: Any, session_id: Optional[str]) -> Any:
    try:
        return await transport.stream_turn({"session_id": session_id, "message": "hi"})
    finally:
        await transport.close()


def test_stream_turn_passes_the_user_to_the_affinity_header(header_calls: List[Dict[str, Any]]) -> None:
    captured: List[httpx.Request] = []
    transport = _transport_capturing(captured)

    result = asyncio.run(_one_turn(transport, SESSION_ID))

    assert result.status_code == 200, result.error
    assert header_calls == [{"args": (SESSION_ID, USER_ID), "kwargs": {}}]
    sent = captured[0].headers
    assert sent[runner.RUNTIME_SESSION_ID_HEADER] == runner.runtime_session_id_for(SESSION_ID, USER_ID)
    assert sent["Authorization"] == f"Bearer bearer-for-{USER_ID}"
    assert str(captured[0].url) == runner.build_invocations_url(BASE_URL)


def test_stream_turn_without_a_session_sends_no_affinity_header(header_calls: List[Dict[str, Any]]) -> None:
    captured: List[httpx.Request] = []
    transport = _transport_capturing(captured)

    result = asyncio.run(_one_turn(transport, None))

    assert result.status_code == 200, result.error
    assert header_calls == [{"args": (None, USER_ID), "kwargs": {}}]
    assert runner.RUNTIME_SESSION_ID_HEADER not in captured[0].headers
