"""`awaitingInput` on the session list: a paused turn the user can still answer.

The sidebar marks a conversation whose turn is paused on the user (OAuth
consent, tool approval, a clarifying question, a browser sign-in). The field is
derived from the `pendingInterrupts` breadcrumbs and the `pausedTurn` snapshot
the list already loads, and only while the snapshot is live: the resume route
refuses an expired one, so a marker for it would lead nowhere.
"""

from datetime import datetime, timedelta, timezone

import pytest

from apis.shared.sessions.models import (
    PausedTurnSnapshot,
    PendingInterrupt,
    SessionMetadata,
    SessionMetadataResponse,
    derive_awaiting_input,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
LIVE = (NOW + timedelta(minutes=30)).isoformat()
EXPIRED = (NOW - timedelta(minutes=1)).isoformat()


def _interrupt(kind: str, interrupt_id: str = "interrupt-fake-1") -> PendingInterrupt:
    return PendingInterrupt(interruptId=interrupt_id, kind=kind, createdAt="2026-10-22T11:00:00+00:00")


def _snapshot(expires_at: str) -> PausedTurnSnapshot:
    return PausedTurnSnapshot(
        modelId="model-fake", capturedAt="2026-10-22T11:00:00+00:00", expiresAt=expires_at,
    )


def _session(**kw) -> SessionMetadata:
    defaults = dict(
        sessionId="session-fake-1", userId="user-fake-1", title="Paused",
        status="active", createdAt="2026-10-22T10:00:00Z",
        lastMessageAt="2026-10-22T11:00:00Z", messageCount=2,
    )
    defaults.update(kw)
    return SessionMetadata(**defaults)


def _response(session: SessionMetadata) -> SessionMetadataResponse:
    # The exact construction every sessions route uses.
    return SessionMetadataResponse.model_validate(session.model_dump(by_alias=True))


class TestDeriveAwaitingInput:
    def test_none_when_nothing_is_pending(self):
        assert derive_awaiting_input([], LIVE, now=NOW) is None

    def test_live_pause_names_its_kind(self):
        assert derive_awaiting_input(["tool_approval"], LIVE, now=NOW) == "tool_approval"

    def test_expired_snapshot_is_not_answerable(self):
        assert derive_awaiting_input(["oauth"], EXPIRED, now=NOW) is None

    def test_expiry_instant_counts_as_expired(self):
        assert derive_awaiting_input(["oauth"], NOW.isoformat(), now=NOW) is None

    def test_missing_snapshot_is_not_answerable(self):
        assert derive_awaiting_input(["user_question"], None, now=NOW) is None

    def test_unparseable_expiry_is_not_answerable(self):
        assert derive_awaiting_input(["oauth"], "not-a-timestamp", now=NOW) is None

    def test_z_suffix_and_naive_timestamps_read_as_utc(self):
        assert derive_awaiting_input(["oauth"], "2026-10-22T12:30:00Z", now=NOW) == "oauth"
        assert derive_awaiting_input(["oauth"], "2026-10-22T12:30:00", now=NOW) == "oauth"

    def test_kindless_breadcrumb_is_oauth(self):
        assert derive_awaiting_input([None], LIVE, now=NOW) == "oauth"

    @pytest.mark.parametrize(
        "kinds, expected",
        [
            (["oauth", "browser_login", "tool_approval"], "browser_login"),
            (["oauth", "user_question", "tool_approval"], "tool_approval"),
            (["oauth", "user_question"], "user_question"),
            (["user_question", "oauth"], "user_question"),
        ],
    )
    def test_several_kinds_pick_one_regardless_of_order(self, kinds, expected):
        assert derive_awaiting_input(kinds, LIVE, now=NOW) == expected
        assert derive_awaiting_input(list(reversed(kinds)), LIVE, now=NOW) == expected

    def test_unknown_kinds_alone_name_nothing(self):
        assert derive_awaiting_input(["some_future_kind"], LIVE, now=NOW) is None


class TestSessionMetadataResponse:
    def test_idle_session_awaits_nothing(self):
        assert _response(_session()).awaiting_input is None

    def test_live_pause_sets_the_field(self):
        live = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        response = _response(_session(
            pendingInterrupts=[_interrupt("tool_approval")], pausedTurn=_snapshot(live),
        ))
        assert response.awaiting_input == "tool_approval"

    def test_expired_pause_does_not(self):
        expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        response = _response(_session(
            pendingInterrupts=[_interrupt("oauth")], pausedTurn=_snapshot(expired),
        ))
        assert response.awaiting_input is None

    def test_breadcrumb_without_snapshot_does_not(self):
        response = _response(_session(pendingInterrupts=[_interrupt("oauth")]))
        assert response.awaiting_input is None

    def test_snapshot_without_breadcrumb_does_not(self):
        live = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        assert _response(_session(pausedTurn=_snapshot(live))).awaiting_input is None

    def test_breadcrumbs_and_snapshot_never_reach_the_wire(self):
        live = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        wire = _response(_session(
            pendingInterrupts=[_interrupt("browser_login")], pausedTurn=_snapshot(live),
        )).model_dump(by_alias=True, exclude_none=True)
        assert wire["awaitingInput"] == "browser_login"
        assert "pendingInterrupts" not in wire
        assert "pausedTurn" not in wire


class TestListUserSessions:
    @pytest.mark.asyncio
    async def test_list_rows_carry_the_derived_kind(self, sessions_metadata_table):
        """The real list read: breadcrumb + snapshot written the way the turn path writes them."""
        from apis.shared.sessions.metadata import (
            add_pending_interrupt, list_user_sessions, set_paused_turn, store_session_metadata,
        )

        live = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        for sid, at in (("session-fake-paused", "2026-10-22T11:00:00Z"), ("session-fake-idle", "2026-10-22T10:00:00Z")):
            await store_session_metadata(
                session_id=sid, user_id="user-fake-1",
                session_metadata=_session(sessionId=sid, lastMessageAt=at),
            )
        await add_pending_interrupt(
            session_id="session-fake-paused", user_id="user-fake-1",
            interrupt=_interrupt("user_question"),
        )
        await set_paused_turn("session-fake-paused", "user-fake-1", _snapshot(live))

        sessions, _ = await list_user_sessions("user-fake-1")
        by_id = {s.session_id: _response(s) for s in sessions}
        assert by_id["session-fake-paused"].awaiting_input == "user_question"
        assert by_id["session-fake-idle"].awaiting_input is None
