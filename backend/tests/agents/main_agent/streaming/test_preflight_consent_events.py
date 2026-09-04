"""`oauth_required` for OAuth-gated MCP tools dropped at agent-build time.

When an OAuth-gated MCP server refuses the pre-flight `tools/list`, the tool
never enters the registry — so `OAuthConsentHook`, which is a
`BeforeToolCall` hook, can never fire for it and the user is never told the
tool is missing. `_extract_preflight_consent_events` closes that gap by
draining the integration's recorded consents at the end of the turn.

These events carry NO `interruptId`: nothing is paused, so there is nothing
to resume. Sending a synthetic id instead would be actively worse — the
resume guard in `inference_api/chat/routes.py` rejects unknown ids with a
400, so the user would complete consent and then be shown an error.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


def _parse(sse: str) -> dict:
    """Pull the JSON payload out of an SSE frame."""
    data_line = next(
        line for line in sse.splitlines() if line.startswith("data: ")
    )
    return json.loads(data_line[len("data: ") :])


def _coordinator() -> StreamCoordinator:
    return StreamCoordinator.__new__(StreamCoordinator)


def _integration(pending: dict[str, dict[str, str]]):
    store = dict(pending)
    return SimpleNamespace(take_pending_consents=lambda uid: store.pop(uid, {}))


class TestPreflightConsentEvents:
    def test_emits_oauth_required_without_interrupt_id(self):
        coordinator = _coordinator()
        integration = _integration(
            {"alice": {"github-oauth": "https://consent.example/authorize"}}
        )

        with patch(
            "agents.main_agent.integrations.external_mcp_client."
            "get_external_mcp_integration",
            return_value=integration,
        ):
            events = coordinator._extract_preflight_consent_events("alice")

        assert len(events) == 1
        assert events[0].startswith("event: oauth_required\n")
        payload = _parse(events[0])
        assert payload["providerId"] == "github-oauth"
        assert payload["authorizationUrl"] == "https://consent.example/authorize"
        # The whole point: no resumable id, and the key must be absent
        # rather than null — the SPA validator rejects an empty string.
        assert "interruptId" not in payload

    def test_emits_one_event_per_provider_deterministically(self):
        coordinator = _coordinator()
        integration = _integration(
            {
                "alice": {
                    "zeta-oauth": "https://consent.example/z",
                    "alpha-oauth": "https://consent.example/a",
                }
            }
        )

        with patch(
            "agents.main_agent.integrations.external_mcp_client."
            "get_external_mcp_integration",
            return_value=integration,
        ):
            events = coordinator._extract_preflight_consent_events("alice")

        # Sorted so the frame order is stable across turns.
        assert [_parse(e)["providerId"] for e in events] == [
            "alpha-oauth",
            "zeta-oauth",
        ]

    def test_no_events_when_nothing_pending(self):
        coordinator = _coordinator()
        integration = _integration({})

        with patch(
            "agents.main_agent.integrations.external_mcp_client."
            "get_external_mcp_integration",
            return_value=integration,
        ):
            assert coordinator._extract_preflight_consent_events("alice") == []

    def test_anonymous_turn_emits_nothing(self):
        """No user id means no per-user bucket to drain — and draining the
        wrong one would leak another user's consent into this stream."""
        coordinator = _coordinator()
        assert coordinator._extract_preflight_consent_events(None) == []

    def test_integration_failure_does_not_break_the_stream(self):
        """Consent prompts are a nicety; a raise here must not kill the
        turn's `done` frame."""
        coordinator = _coordinator()
        boom = SimpleNamespace(
            take_pending_consents=lambda uid: (_ for _ in ()).throw(
                RuntimeError("boom")
            )
        )

        with patch(
            "agents.main_agent.integrations.external_mcp_client."
            "get_external_mcp_integration",
            return_value=boom,
        ):
            assert coordinator._extract_preflight_consent_events("alice") == []
