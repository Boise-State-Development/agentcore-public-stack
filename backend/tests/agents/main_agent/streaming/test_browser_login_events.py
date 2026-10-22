"""`browser_login_required` extraction from a paused turn.

Same shape as `test_user_question_events.py` — the extractor reads
`agent._interrupt_state` at the `done` event — so these concentrate on what is
specific to this flavor:

* the conversation id is stamped on here, not in the tool, and must not be
  confused with the browser session id;
* the D4 projection is written alongside the breadcrumb, because app-api reads
  the metadata row and cannot read `agent.state`;
* neither the event nor the projection may carry a live-view URL;
* persistence is best-effort — a DynamoDB failure costs the viewer, never the
  prompt.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator
from apis.shared.browser_takeover import assert_no_url


def _parse(sse: str) -> dict:
    data_line = next(line for line in sse.splitlines() if line.startswith("data: "))
    return json.loads(data_line[len("data: ") :])


def _coordinator() -> StreamCoordinator:
    return StreamCoordinator.__new__(StreamCoordinator)


def _agent(*interrupts, activated: bool = True):
    return SimpleNamespace(
        _interrupt_state=SimpleNamespace(
            activated=activated,
            interrupts={f"i-{n}": i for n, i in enumerate(interrupts)},
        )
    )


def _interrupt(reason, interrupt_id: str = "v1:tool_call:tu-1:abc"):
    return SimpleNamespace(id=interrupt_id, reason=reason)


def _reason(**overrides):
    payload = {
        "type": "browser_login_required",
        "toolUseId": "tu-1",
        "reason": "Sign in to JSTOR so I can check the results page.",
        "browserSessionId": "bs-1",
        "browserId": "browser-abc",
        "viewport": {"width": 1280, "height": 800},
        "controlState": "user",
        "deadlineAt": "2026-09-18T12:08:00+00:00",
        "targetUrl": "https://www.jstor.org/action/showLogin",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def persistence():
    """Patch both writers at their definition sites."""
    with patch(
        "apis.shared.sessions.metadata.add_pending_interrupt",
        new_callable=AsyncMock,
    ) as add_pending, patch(
        "apis.shared.sessions.metadata.set_browser_session",
        new_callable=AsyncMock,
    ) as set_browser:
        yield SimpleNamespace(add_pending=add_pending, set_browser=set_browser)


class TestEventEmission:
    @pytest.mark.asyncio
    async def test_emits_one_event_describing_the_handover(self, persistence):
        events = await _coordinator()._extract_browser_login_required_events(
            _agent(_interrupt(_reason())), session_id="s1", user_id="u1"
        )

        assert len(events) == 1
        assert events[0].startswith("event: browser_login_required\n")
        payload = _parse(events[0])
        assert payload["interruptId"] == "v1:tool_call:tu-1:abc"
        assert payload["toolUseId"] == "tu-1"
        assert payload["browserSessionId"] == "bs-1"
        assert payload["viewport"] == {"width": 1280, "height": 800}
        assert payload["reason"].startswith("Sign in to JSTOR")

    @pytest.mark.asyncio
    async def test_session_id_is_the_conversation_not_the_browser(self, persistence):
        events = await _coordinator()._extract_browser_login_required_events(
            _agent(_interrupt(_reason())), session_id="conv-42", user_id="u1"
        )

        payload = _parse(events[0])
        # app-api's live-view route is scoped by conversation owner and
        # resolves the browser session server-side, so these must stay distinct.
        assert payload["sessionId"] == "conv-42"
        assert payload["browserSessionId"] == "bs-1"

    @pytest.mark.asyncio
    async def test_no_live_view_url_reaches_the_client(self, persistence):
        events = await _coordinator()._extract_browser_login_required_events(
            _agent(_interrupt(_reason())), session_id="s1", user_id="u1"
        )

        assert_no_url(_parse(events[0]))  # must not raise
        assert "liveViewUrl" not in _parse(events[0])

    @pytest.mark.asyncio
    async def test_the_viewport_rides_the_event_so_dcv_does_not_crop(
        self, persistence
    ):
        events = await _coordinator()._extract_browser_login_required_events(
            _agent(_interrupt(_reason(viewport={"width": 1920, "height": 1080}))),
            session_id="s1",
            user_id="u1",
        )

        assert _parse(events[0])["viewport"] == {"width": 1920, "height": 1080}

    @pytest.mark.asyncio
    async def test_other_interrupt_flavors_are_ignored(self, persistence):
        events = await _coordinator()._extract_browser_login_required_events(
            _agent(
                _interrupt({"type": "user_question_required", "questions": []}),
                _interrupt({"type": "oauth_required", "providerId": "p1"}),
            ),
            session_id="s1",
            user_id="u1",
        )

        assert events == []

    @pytest.mark.asyncio
    async def test_nothing_emitted_when_the_turn_did_not_pause(self, persistence):
        events = await _coordinator()._extract_browser_login_required_events(
            _agent(_interrupt(_reason()), activated=False),
            session_id="s1",
            user_id="u1",
        )

        assert events == []

    @pytest.mark.asyncio
    async def test_an_undescribable_session_is_dropped_not_emitted(self, persistence):
        # An event the SPA cannot act on is worse than none: it would show a
        # viewer affordance for a browser nothing can resolve.
        events = await _coordinator()._extract_browser_login_required_events(
            _agent(_interrupt(_reason(browserSessionId=None, viewport=None))),
            session_id="s1",
            user_id="u1",
        )

        assert events == []
        persistence.set_browser.assert_not_called()


class TestPersistence:
    @pytest.mark.asyncio
    async def test_projects_the_browser_session_onto_the_metadata_row(
        self, persistence
    ):
        await _coordinator()._extract_browser_login_required_events(
            _agent(_interrupt(_reason())), session_id="s1", user_id="u1"
        )

        persistence.set_browser.assert_awaited_once()
        kwargs = persistence.set_browser.await_args.kwargs
        assert kwargs["session_id"] == "s1"
        assert kwargs["user_id"] == "u1"
        assert kwargs["ref"]["browserSessionId"] == "bs-1"
        assert_no_url(kwargs["ref"])

    @pytest.mark.asyncio
    async def test_writes_a_breadcrumb_so_the_prompt_survives_a_refresh(
        self, persistence
    ):
        await _coordinator()._extract_browser_login_required_events(
            _agent(_interrupt(_reason())), session_id="s1", user_id="u1"
        )

        interrupt = persistence.add_pending.await_args.kwargs["interrupt"]
        assert interrupt.kind == "browser_login"
        assert interrupt.tool_name == "request_user_login"
        assert json.loads(interrupt.browser_session)["browserSessionId"] == "bs-1"
        assert interrupt.reason.startswith("Sign in to JSTOR")

    @pytest.mark.asyncio
    async def test_a_dynamo_failure_still_emits_the_prompt(self, persistence):
        # Losing the breadcrumb costs a refresh; losing the event costs the turn.
        persistence.add_pending.side_effect = RuntimeError("throttled")
        persistence.set_browser.side_effect = RuntimeError("throttled")

        events = await _coordinator()._extract_browser_login_required_events(
            _agent(_interrupt(_reason())), session_id="s1", user_id="u1"
        )

        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_anonymous_flows_emit_without_persisting(self, persistence):
        events = await _coordinator()._extract_browser_login_required_events(
            _agent(_interrupt(_reason())), session_id=None, user_id=None
        )

        assert len(events) == 1
        persistence.set_browser.assert_not_called()
        persistence.add_pending.assert_not_called()
