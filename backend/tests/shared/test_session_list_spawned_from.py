"""The session list carries a handoff target's ``spawnedFrom``, with a run step's ``runId``.

workflow-runs spec §19 PR-W.4c / §20.4: the sidebar nests a run's step conversations under their
source, from the list it already loads. The link is a field of the row the list reads (the
``SessionRecencyIndex`` projects ALL), so it costs no read per row.
"""

import pytest

from apis.shared.sessions.models import SessionMetadataResponse


def _put_migrated_row(table, sid, la, user_id="u1", **extra):
    item = {
        "PK": f"USER#{user_id}", "SK": f"S#{sid}",
        "GSI_PK": f"SESSION#{sid}", "GSI_SK": "META",
        "GSI4_PK": f"USER#{user_id}", "GSI4_SK": f"{la}#{sid}",
        "sessionId": sid, "userId": user_id, "title": "T", "status": "active",
        "createdAt": la, "lastMessageAt": la, "messageCount": 1,
    }
    item.update(extra)
    table.put_item(Item=item)


def _listed(sessions):
    # The exact construction GET /sessions uses.
    return {
        s.session_id: SessionMetadataResponse.model_validate(s.model_dump(by_alias=True)).model_dump(
            by_alias=True, exclude_none=True
        )
        for s in sessions
    }


@pytest.mark.asyncio
async def test_a_run_step_lists_with_its_source_and_run(sessions_metadata_table):
    from apis.shared.sessions.metadata import (
        list_user_sessions,
        set_session_spawned_from,
        settle_session_spawned_from,
    )

    _put_migrated_row(sessions_metadata_table, "src", "2026-01-01T00:00:00Z")
    _put_migrated_row(sessions_metadata_table, "step", "2026-01-02T00:00:00Z")
    _put_migrated_row(sessions_metadata_table, "single", "2026-01-03T00:00:00Z")
    assert set_session_spawned_from("step", "u1", "src", "h1", "r1")
    assert set_session_spawned_from("single", "u1", "src", "h2")
    settle_session_spawned_from("step", "u1")

    sessions, _ = await list_user_sessions("u1")
    listed = _listed(sessions)

    assert listed["step"]["spawnedFrom"] == {"sessionId": "src", "handoffId": "h1", "runId": "r1"}
    # A handoff of its own has no runId; the row's own bookkeeping (settled) stays off the wire.
    assert listed["single"]["spawnedFrom"] == {"sessionId": "src", "handoffId": "h2"}
    assert "spawnedFrom" not in listed["src"]


def test_the_stored_link_keeps_runid_next_to_what_it_already_held(sessions_metadata_table):
    from apis.shared.sessions.metadata import set_session_spawned_from

    _put_migrated_row(sessions_metadata_table, "step", "2026-01-02T00:00:00Z")
    set_session_spawned_from("step", "u1", "src", "h1", "r1")
    item = sessions_metadata_table.get_item(Key={"PK": "USER#u1", "SK": "S#step"})["Item"]
    assert item["spawnedFrom"] == {"sessionId": "src", "handoffId": "h1", "runId": "r1"}


@pytest.mark.parametrize("bad", ["src", {"handoffId": "h1"}, {"sessionId": 3}, ["src"]])
def test_a_malformed_link_drops_the_link_not_the_list(bad):
    response = SessionMetadataResponse.model_validate(
        {
            "sessionId": "s", "title": "T", "status": "active", "createdAt": "2026-01-01T00:00:00Z",
            "lastMessageAt": "2026-01-01T00:00:00Z", "messageCount": 1, "spawnedFrom": bad,
        }
    )
    assert response.spawned_from is None
