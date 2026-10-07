"""Tests for the bearer the voice relay presents on the upstream upgrade.

inference-api takes the voice identity from the ``Authorization`` header on
the upgrade — the token the Runtime's JWT authorizer validated and forwards —
so the relay must send it there in the cloud, not in the browser-only
``base64UrlBearerAuthorization`` subprotocol the container may never see.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.app_api.voice import proxy

CLOUD_BASE = (
    "https://bedrock-agentcore.us-east-1.amazonaws.com"
    "/runtimes/arn:aws:bedrock-agentcore:us-east-1:123:runtime/abc"
)


async def _captured_handshake(monkeypatch: pytest.MonkeyPatch, base_url: str) -> dict:
    monkeypatch.setenv("INFERENCE_API_URL", base_url)
    session = MagicMock()
    session.ws_connect = AsyncMock(side_effect=RuntimeError("stop after handshake"))
    session.close = AsyncMock()
    monkeypatch.setattr(proxy.aiohttp, "ClientSession", MagicMock(return_value=session))

    await proxy.relay_voice_stream(
        client_ws=MagicMock(), cognito_access_token="access-token", user_id="user-1"
    )
    return session.ws_connect.await_args.kwargs


@pytest.mark.asyncio
async def test_cloud_upgrade_carries_the_bearer_in_the_authorization_header(monkeypatch) -> None:
    kwargs = await _captured_handshake(monkeypatch, CLOUD_BASE)

    assert kwargs["headers"] == {"Authorization": "Bearer access-token"}
    assert not kwargs.get("protocols")


@pytest.mark.asyncio
async def test_local_upgrade_carries_the_same_header(monkeypatch) -> None:
    kwargs = await _captured_handshake(monkeypatch, "http://localhost:8001")

    assert kwargs["headers"] == {"Authorization": "Bearer access-token"}
