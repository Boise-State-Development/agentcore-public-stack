"""Tests for StreamCoordinator._extract_artifact_events.

Covers the post-turn `artifact` SSE emit: turn-window filtering (only
artifacts touched this turn), action derivation, fail-closed behavior,
and the no-session guard.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agents.builtin_tools.artifacts import service as artifact_service
from agents.main_agent.streaming.stream_coordinator import StreamCoordinator

SESSION = "sess-9"
USER = "user-123"


def _parse_sse(raw: str) -> dict:
    assert raw.startswith("event: artifact\ndata: ")
    assert raw.endswith("\n\n")
    return json.loads(raw[len("event: artifact\ndata: ") :].strip())


@pytest.fixture
def turn_start() -> datetime:
    return datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def coord() -> StreamCoordinator:
    return StreamCoordinator()


def _row(**kw) -> dict:
    base = {
        "artifact_id": "art-1",
        "version": 1,
        "title": "Doc",
        "content_type": "text/html; charset=utf-8",
        "updated_at": "2026-05-15T12:00:05+00:00",
        "created_at": "2026-05-15T12:00:05+00:00",
    }
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_emits_created_for_v1(coord, turn_start, monkeypatch) -> None:
    monkeypatch.setattr(
        artifact_service, "list_session_artifacts", lambda u, s: [_row()]
    )
    out = await coord._extract_artifact_events(SESSION, USER, turn_start)
    assert len(out) == 1
    payload = _parse_sse(out[0])
    assert payload == {
        "type": "artifact",
        "artifactId": "art-1",
        "version": 1,
        "title": "Doc",
        "contentType": "text/html; charset=utf-8",
        "sessionId": SESSION,
        "updatedAt": "2026-05-15T12:00:05+00:00",
        "action": "created",
        "producedByMessageIndex": None,
    }


@pytest.mark.asyncio
async def test_stamps_and_emits_produced_by_message_index(
    coord, turn_start, monkeypatch
) -> None:
    monkeypatch.setattr(
        artifact_service,
        "list_session_artifacts",
        lambda u, s: [_row(artifact_id="a"), _row(artifact_id="b")],
    )
    stamped: list[tuple] = []
    monkeypatch.setattr(
        artifact_service,
        "set_produced_by_message_index",
        lambda u, aid, ver, idx: stamped.append((u, aid, ver, idx)),
    )
    out = await coord._extract_artifact_events(
        SESSION, USER, turn_start, produced_by_message_index=7
    )
    assert {_parse_sse(e)["producedByMessageIndex"] for e in out} == {7}
    # Each artifact's own version is threaded to the stamp so the right
    # version row is linked (both rows are v1 here).
    assert stamped == [(USER, "a", 1, 7), (USER, "b", 1, 7)]


@pytest.mark.asyncio
async def test_stamp_failure_is_swallowed(
    coord, turn_start, monkeypatch
) -> None:
    monkeypatch.setattr(
        artifact_service, "list_session_artifacts", lambda u, s: [_row()]
    )

    def _boom(u, aid, ver, idx):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(
        artifact_service, "set_produced_by_message_index", _boom
    )
    out = await coord._extract_artifact_events(
        SESSION, USER, turn_start, produced_by_message_index=3
    )
    # Stamp failure must not drop the live event.
    assert _parse_sse(out[0])["producedByMessageIndex"] == 3


@pytest.mark.asyncio
async def test_version_gt_1_is_updated(coord, turn_start, monkeypatch) -> None:
    monkeypatch.setattr(
        artifact_service,
        "list_session_artifacts",
        lambda u, s: [_row(version=4)],
    )
    out = await coord._extract_artifact_events(SESSION, USER, turn_start)
    assert _parse_sse(out[0])["action"] == "updated"
    assert _parse_sse(out[0])["version"] == 4


@pytest.mark.asyncio
async def test_filters_artifacts_from_earlier_turns(
    coord, turn_start, monkeypatch
) -> None:
    stale = _row(artifact_id="old", updated_at="2026-05-15T11:59:59+00:00")
    fresh = _row(artifact_id="new", updated_at="2026-05-15T12:00:30+00:00")
    monkeypatch.setattr(
        artifact_service,
        "list_session_artifacts",
        lambda u, s: [fresh, stale],
    )
    out = await coord._extract_artifact_events(SESSION, USER, turn_start)
    ids = [_parse_sse(e)["artifactId"] for e in out]
    assert ids == ["new"]


@pytest.mark.asyncio
async def test_unparseable_updated_at_is_included(
    coord, turn_start, monkeypatch
) -> None:
    monkeypatch.setattr(
        artifact_service,
        "list_session_artifacts",
        lambda u, s: [_row(updated_at="")],
    )
    out = await coord._extract_artifact_events(SESSION, USER, turn_start)
    assert len(out) == 1


@pytest.mark.asyncio
async def test_config_error_is_swallowed(coord, turn_start, monkeypatch) -> None:
    def _raise(u, s):
        raise artifact_service.ArtifactConfigError("not configured")

    monkeypatch.setattr(artifact_service, "list_session_artifacts", _raise)
    assert await coord._extract_artifact_events(SESSION, USER, turn_start) == []


@pytest.mark.asyncio
async def test_unexpected_error_is_swallowed(
    coord, turn_start, monkeypatch
) -> None:
    def _raise(u, s):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(artifact_service, "list_session_artifacts", _raise)
    assert await coord._extract_artifact_events(SESSION, USER, turn_start) == []


@pytest.mark.asyncio
async def test_no_session_or_user_is_noop(coord, turn_start) -> None:
    assert await coord._extract_artifact_events(None, USER, turn_start) == []
    assert await coord._extract_artifact_events(SESSION, None, turn_start) == []


@pytest.mark.asyncio
async def test_multiple_artifacts_one_turn(coord, turn_start, monkeypatch) -> None:
    monkeypatch.setattr(
        artifact_service,
        "list_session_artifacts",
        lambda u, s: [
            _row(artifact_id="a", version=1),
            _row(artifact_id="b", version=2),
        ],
    )
    out = await coord._extract_artifact_events(SESSION, USER, turn_start)
    actions = {
        _parse_sse(e)["artifactId"]: _parse_sse(e)["action"] for e in out
    }
    assert actions == {"a": "created", "b": "updated"}
