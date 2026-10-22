"""The tool census reaches DynamoDB the way the profile reads it.

`toolCalls` rides the per-call `C#` cost row as an extra field (like
`turnAgentId`), and the session row's `toolCallCount` / `toolErrorCount` are
bumped in the same aggregate update as `totalCost`. With the kill switch off,
neither attribute exists at all — that absence is what lets the admin profile
say "not tracked" rather than render an honest-looking zero.
"""

from decimal import Decimal

import pytest

from apis.shared.sessions.models import MessageMetadata, ModelInfo, TokenUsage


def _meta(tool_calls=None):
    kwargs = dict(
        token_usage=TokenUsage(inputTokens=100, outputTokens=50, totalTokens=150),
        model_info=ModelInfo(modelId="claude-haiku-4-5", modelName="Claude Haiku 4.5"),
        cost=0.01,
    )
    if tool_calls is not None:
        kwargs["toolCalls"] = tool_calls
    return MessageMetadata(**kwargs)


def _seed_session(table, session_id="s1", user_id="u1"):
    table.put_item(Item={
        "PK": f"USER#{user_id}",
        "SK": f"S#{session_id}",
        "GSI_PK": f"SESSION#{session_id}",
        "GSI_SK": "META",
        "sessionId": session_id,
        "userId": user_id,
        "status": "active",
        "createdAt": "2026-09-01T00:00:00Z",
        "lastMessageAt": "2026-09-01T00:00:00Z",
        "messageCount": Decimal(0),
    })


def _session_row(table, session_id="s1", user_id="u1"):
    return table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"S#{session_id}"})["Item"]


@pytest.mark.asyncio
async def test_tool_calls_land_on_the_cost_row_and_roll_up_on_the_session(sessions_metadata_table, monkeypatch):
    monkeypatch.delenv("COST_DIAGNOSTICS_ENABLED", raising=False)
    from apis.shared.sessions.metadata import store_message_metadata

    _seed_session(sessions_metadata_table)
    census = {"list_courses": {"calls": 2, "errors": 0}, "calculator": {"calls": 1, "errors": 1}}
    await store_message_metadata(session_id="s1", user_id="u1", message_id=1, message_metadata=_meta(census))
    await store_message_metadata(session_id="s1", user_id="u1", message_id=2, message_metadata=_meta())

    cost_rows = [i for i in sessions_metadata_table.scan()["Items"] if i["SK"].startswith("C#")]
    assert len(cost_rows) == 2
    with_census = [r for r in cost_rows if "toolCalls" in r]
    assert len(with_census) == 1
    assert with_census[0]["toolCalls"]["calculator"] == {"calls": Decimal(1), "errors": Decimal(1)}

    row = _session_row(sessions_metadata_table)
    assert row["toolCallCount"] == Decimal(3)
    assert row["toolErrorCount"] == Decimal(1)


@pytest.mark.asyncio
async def test_kill_switch_leaves_no_trace(sessions_metadata_table, monkeypatch):
    monkeypatch.setenv("COST_DIAGNOSTICS_ENABLED", "false")
    from apis.shared.sessions.metadata import store_message_metadata

    _seed_session(sessions_metadata_table)
    # The coordinator would not attach toolCalls with the flag off; even if a
    # stale row carries one, the session rollups must not be written.
    await store_message_metadata(
        session_id="s1", user_id="u1", message_id=1,
        message_metadata=_meta({"a": {"calls": 1, "errors": 0}}),
    )
    row = _session_row(sessions_metadata_table)
    assert "toolCallCount" not in row and "toolErrorCount" not in row
    # The unrelated aggregates still bump — the switch is scoped to the census.
    assert row["totalCost"] == Decimal("0.01")


@pytest.mark.asyncio
async def test_malformed_census_entries_count_as_zero_not_as_a_failure(sessions_metadata_table, monkeypatch):
    monkeypatch.delenv("COST_DIAGNOSTICS_ENABLED", raising=False)
    from apis.shared.sessions.metadata import store_message_metadata

    _seed_session(sessions_metadata_table)
    await store_message_metadata(
        session_id="s1", user_id="u1", message_id=1,
        message_metadata=_meta({"ok": {"calls": 2, "errors": 0}, "weird": "not-a-dict", "half": {"calls": "x"}}),
    )
    row = _session_row(sessions_metadata_table)
    assert row["toolCallCount"] == Decimal(2)
    assert row["toolErrorCount"] == Decimal(0)
