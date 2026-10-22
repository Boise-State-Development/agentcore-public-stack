"""The ``F#`` message-feedback row family (``apis.shared.sessions.feedback``)
against a real (moto) sessions-metadata table.

Pins the key shape that makes the admin join a one-key lookup, replace-not-
append semantics, the session rollups' deltas, ownership, the content-free
guard, and the read-side merge into the messages-list metadata index.
"""

from __future__ import annotations

import boto3
import pytest
from boto3.dynamodb.conditions import Key

from apis.shared.sessions import feedback as fb
from apis.shared.sessions import metadata as md

SESSION = "sess-fb-1"
OWNER = "user-owner"
OTHER = "user-other"


def _table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table("test-sessions-metadata")


def _seed_session(user_id=OWNER, session_id=SESSION):
    _table().put_item(Item={
        "PK": f"USER#{user_id}",
        "SK": f"S#{session_id}",
        "GSI_PK": f"SESSION#{session_id}",
        "GSI_SK": "META",
        "sessionId": session_id,
        "userId": user_id,
        "status": "active",
        "messageCount": 4,
    })


def _session_row(user_id=OWNER, session_id=SESSION):
    return _table().get_item(Key={"PK": f"USER#{user_id}", "SK": f"S#{session_id}"}).get("Item") or {}


def _feedback_items(session_id=SESSION):
    resp = _table().query(
        IndexName="SessionLookupIndex",
        KeyConditionExpression=Key("GSI_PK").eq(f"SESSION#{session_id}") & Key("GSI_SK").begins_with("F#"),
    )
    return resp.get("Items", [])


@pytest.fixture()
def table(sessions_metadata_table, monkeypatch):
    monkeypatch.delenv("COST_DIAGNOSTICS_ENABLED", raising=False)
    _seed_session()
    return _table()


@pytest.mark.asyncio
async def test_put_writes_one_row_keyed_beside_the_cost_row(table):
    result = await fb.put_message_feedback(SESSION, OWNER, 3, 1)
    assert result.value == 1 and result.reason is None and result.updated_at

    items = _feedback_items()
    assert len(items) == 1
    row = items[0]
    assert row["PK"] == f"USER#{OWNER}"
    assert row["SK"] == f"F#{SESSION}#3"
    assert row["GSI_PK"] == f"SESSION#{SESSION}" and row["GSI_SK"] == "F#3"
    assert int(row["messageId"]) == 3 and row["sessionId"] == SESSION
    assert int(row["value"]) == 1
    assert "reason" not in row
    assert "ttl" in row
    # Content-free: nothing on the row but ids, a number, a timestamp and keys.
    assert row["signal"] == "explicit"
    assert set(row) <= {"PK", "SK", "GSI_PK", "GSI_SK", "GSI1PK", "GSI1SK", "sessionId", "messageId", "userId", "value", "signal", "retryMessageId", "updatedAt", "ttl"}


@pytest.mark.asyncio
async def test_second_thumb_replaces_the_first_and_rollups_follow(table):
    await fb.put_message_feedback(SESSION, OWNER, 3, 1)
    assert _session_row()["thumbsUp"] == 1 and _session_row()["thumbsDown"] == 0

    result = await fb.put_message_feedback(SESSION, OWNER, 3, -1, reason="instructions")
    assert result.value == -1 and result.reason == "instructions"

    items = _feedback_items()
    assert len(items) == 1, "a second click replaces, never appends"
    assert int(items[0]["value"]) == -1 and items[0]["reason"] == "instructions"
    row = _session_row()
    assert row["thumbsUp"] == 0 and row["thumbsDown"] == 1

    # Same value again: no rollup movement, reason updates in place.
    await fb.put_message_feedback(SESSION, OWNER, 3, -1, reason="wrong")
    row = _session_row()
    assert row["thumbsUp"] == 0 and row["thumbsDown"] == 1
    assert _feedback_items()[0]["reason"] == "wrong"


@pytest.mark.asyncio
async def test_retry_link_is_set_once_and_kept_across_later_thumbs(table):
    """Retry-with-correction (response-feedback §7): the row links the user
    message the correction was sent as, and a later re-thumb without the
    field must not drop it."""
    first = await fb.put_message_feedback(SESSION, OWNER, 3, -1, reason="wrong")
    assert first.retry_message_id is None
    linked = await fb.put_message_feedback(SESSION, OWNER, 3, -1, reason="wrong", retry_message_id=4)
    assert linked.retry_message_id == 4
    assert int(_feedback_items()[0]["retryMessageId"]) == 4

    # Re-thumb (say, the retry was better and they flip to up): link stays.
    again = await fb.put_message_feedback(SESSION, OWNER, 3, 1)
    assert again.value == 1 and again.retry_message_id == 4
    row = _feedback_items()[0]
    assert int(row["retryMessageId"]) == 4 and "reason" not in row
    assert _session_row()["thumbsUp"] == 1 and _session_row()["thumbsDown"] == 0

    with pytest.raises(ValueError):
        await fb.put_message_feedback(SESSION, OWNER, 3, -1, retry_message_id=-1)


@pytest.mark.asyncio
async def test_delete_removes_the_row_and_decrements(table):
    await fb.put_message_feedback(SESSION, OWNER, 1, -1, reason="tool_failed")
    await fb.put_message_feedback(SESSION, OWNER, 3, 1)
    assert await fb.delete_message_feedback(SESSION, OWNER, 1) is True
    assert [int(i["messageId"]) for i in _feedback_items()] == [3]
    row = _session_row()
    assert row["thumbsUp"] == 1 and row["thumbsDown"] == 0
    # Deleting what isn't there is a no-op, not an error.
    assert await fb.delete_message_feedback(SESSION, OWNER, 1) is False
    assert _session_row()["thumbsDown"] == 0


@pytest.mark.asyncio
async def test_rollups_are_not_written_while_diagnostics_are_off(table, monkeypatch):
    monkeypatch.setenv("COST_DIAGNOSTICS_ENABLED", "false")
    await fb.put_message_feedback(SESSION, OWNER, 3, 1)
    row = _session_row()
    assert "thumbsUp" not in row and "thumbsDown" not in row, "absent reads 'not tracked', never 0"
    assert len(_feedback_items()) == 1, "the row itself is still written"


@pytest.mark.asyncio
async def test_another_users_session_is_not_found(table):
    with pytest.raises(fb.SessionNotOwned):
        await fb.put_message_feedback(SESSION, OTHER, 3, 1)
    with pytest.raises(fb.SessionNotOwned):
        await fb.delete_message_feedback(SESSION, OTHER, 3)
    assert _feedback_items() == []


@pytest.mark.asyncio
async def test_storage_refuses_free_text_and_bad_values(table):
    with pytest.raises(ValueError):
        await fb.put_message_feedback(SESSION, OWNER, 3, 1, reason="it was rude to me")
    with pytest.raises(ValueError):
        await fb.put_message_feedback(SESSION, OWNER, 3, 2)
    assert _feedback_items() == []


@pytest.mark.asyncio
async def test_preview_sessions_echo_without_persisting(table):
    result = await fb.put_message_feedback("preview-abc", OWNER, 0, 1)
    assert result.value == 1
    assert _feedback_items("preview-abc") == []


@pytest.mark.asyncio
async def test_query_filters_by_user_unless_admin(table):
    _seed_session(OTHER, "sess-shared")
    _seed_session(OWNER, "sess-shared")
    await fb.put_message_feedback("sess-shared", OWNER, 1, 1)
    await fb.put_message_feedback("sess-shared", OTHER, 1, -1)

    mine = fb.query_session_feedback(table, "sess-shared", OWNER)
    assert {k: v.value for k, v in mine.items()} == {"1": 1}
    everyone = fb.query_session_feedback(table, "sess-shared", None)
    assert len(everyone) == 1  # keyed by message id; the admin reader returns rows, this map is per message


@pytest.mark.asyncio
async def test_implicit_signal_rows_share_the_family_but_never_read_as_thumbs(table):
    """Spec §10: implicit signals land in the same F# family under
    signal="implicit"; every thumb reader skips them and never sums the two."""
    await fb.put_message_feedback(SESSION, OWNER, 3, 1)
    table.put_item(Item={
        "PK": f"USER#{OWNER}", "SK": f"F#{SESSION}#5", "GSI_PK": f"SESSION#{SESSION}", "GSI_SK": "F#5",
        "sessionId": SESSION, "messageId": 5, "userId": OWNER, "value": 1, "signal": "implicit",
        "updatedAt": "2026-09-16T00:00:00Z",
    })
    assert set(fb.query_session_feedback(table, SESSION, OWNER)) == {"3"}
    index = await md.get_all_message_metadata(SESSION, OWNER)
    assert "5" not in index
    # A row written before the discriminator existed is explicit.
    assert fb.is_explicit({"value": 1}) and not fb.is_explicit({"value": 1, "signal": "implicit"})


@pytest.mark.asyncio
async def test_implicit_signals_count_under_their_own_key_beside_the_thumb(table):
    """Spec §10: copy / continue rows share the F# family, keyed per kind so
    they never collide with the thumb, ADD a count, and are invisible to
    every thumb reader."""
    await fb.put_message_feedback(SESSION, OWNER, 3, -1, reason="wrong")
    await fb.record_implicit_signal(SESSION, OWNER, 3, "copy")
    await fb.record_implicit_signal(SESSION, OWNER, 3, "copy")
    await fb.record_implicit_signal(SESSION, OWNER, 3, "continue")

    items = {i["SK"]: i for i in _feedback_items()}
    assert set(items) == {f"F#{SESSION}#3", f"F#{SESSION}#3#copy", f"F#{SESSION}#3#continue"}
    copy = items[f"F#{SESSION}#3#copy"]
    assert copy["signal"] == "implicit" and copy["kind"] == "copy" and int(copy["count"]) == 2
    assert copy["GSI_SK"] == "F#3#copy" and int(copy["messageId"]) == 3 and "value" not in copy
    assert int(items[f"F#{SESSION}#3"]["value"]) == -1, "the thumb is untouched"

    # Thumb readers see only the thumb; the session rollups did not move.
    assert {k: v.value for k, v in fb.query_session_feedback(table, SESSION, OWNER).items()} == {"3": -1}
    assert _session_row()["thumbsDown"] == 1 and _session_row()["thumbsUp"] == 0

    with pytest.raises(ValueError):
        await fb.record_implicit_signal(SESSION, OWNER, 3, "abandon")
    with pytest.raises(fb.SessionNotOwned):
        await fb.record_implicit_signal(SESSION, OTHER, 3, "copy")
    await fb.record_implicit_signal("preview-x", OWNER, 0, "copy")
    assert _feedback_items("preview-x") == []


@pytest.mark.asyncio
async def test_messages_list_metadata_index_carries_feedback(table, monkeypatch):
    """The read path the SPA uses on reload: F# rows merge into the metadata
    index by message id, beside (or in place of) the cost record."""
    table.put_item(Item={
        "PK": f"USER#{OWNER}", "SK": "C#2026-09-16T00:00:00Z#u1",
        "GSI_PK": f"SESSION#{SESSION}", "GSI_SK": "C#2026-09-16T00:00:00Z",
        "sessionId": SESSION, "messageId": 3, "userId": OWNER,
        "timestamp": "2026-09-16T00:00:00Z", "cost": {"total": 1},
    })
    await fb.put_message_feedback(SESSION, OWNER, 3, -1, reason="wrong")
    await fb.put_message_feedback(SESSION, OWNER, 5, 1)

    index = await md.get_all_message_metadata(SESSION, OWNER)
    assert index["3"]["cost"] == {"total": 1}
    assert index["3"]["feedback"] == {"value": -1, "reason": "wrong", "updatedAt": index["3"]["feedback"]["updatedAt"]}
    assert index["5"] == {"feedback": {"value": 1, "updatedAt": index["5"]["feedback"]["updatedAt"]}}

    monkeypatch.setenv("RESPONSE_FEEDBACK_ENABLED", "false")
    index = await md.get_all_message_metadata(SESSION, OWNER)
    assert "feedback" not in index["3"] and "5" not in index


@pytest.mark.asyncio
async def test_message_metadata_model_round_trips_feedback(table):
    from apis.shared.sessions.models import MessageMetadata

    meta = MessageMetadata(**{"cost": 0.1, "feedback": {"value": 1, "updatedAt": "2026-09-16T00:00:00Z"}})
    assert meta.feedback is not None and meta.feedback.value == 1
    assert meta.model_dump(by_alias=True, exclude_none=True)["feedback"] == {"value": 1, "updatedAt": "2026-09-16T00:00:00Z"}
