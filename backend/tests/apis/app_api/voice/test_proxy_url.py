"""Tests for the voice WS upstream-URL builder.

Mirrors the contract enforced by ``proxy_routes._build_invocations_url``
for the WebSocket path: cloud routes encode the runtime ARN, local-dev
routes hit ``/voice/stream`` directly.
"""

from __future__ import annotations

from apis.app_api.voice.proxy import build_upstream_ws_url


CLOUD_BASE = (
    "https://bedrock-agentcore.us-east-1.amazonaws.com"
    "/runtimes/arn:aws:bedrock-agentcore:us-east-1:123:runtime/abc"
)


def test_cloud_url_encodes_arn_segment() -> None:
    url = build_upstream_ws_url(CLOUD_BASE)
    # Colons and slashes inside the ARN must be percent-encoded.
    assert "arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A123%3Aruntime%2Fabc" in url
    assert url.startswith("wss://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/")
    assert url.endswith("/ws")


def test_cloud_url_uses_wss_scheme() -> None:
    url = build_upstream_ws_url(CLOUD_BASE)
    assert url.startswith("wss://")


def test_local_dev_url_skips_runtime_path() -> None:
    url = build_upstream_ws_url("http://localhost:8001")
    assert url == "ws://localhost:8001/voice/stream"


def test_local_dev_url_with_https_uses_wss() -> None:
    url = build_upstream_ws_url("https://localhost:8001")
    assert url == "wss://localhost:8001/voice/stream"
