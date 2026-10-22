"""Context-ledger extras land on the `C#` row and compaction decisions roll up
by kind on the session row (moto)."""

from decimal import Decimal

import pytest

from apis.shared.sessions.models import MessageMetadata, ModelInfo, TokenUsage


def _meta(**extra):
    return MessageMetadata(
        token_usage=TokenUsage(inputTokens=100, outputTokens=50, totalTokens=150),
        model_info=ModelInfo(modelId="claude-haiku-4-5", modelName="Claude Haiku 4.5"),
        cost=0.01,
        **extra,
    )


def _seed_session(table, session_id="s1", user_id="u1"):
    table.put_item(Item={
        "PK": f"USER#{user_id}", "SK": f"S#{session_id}",
        "GSI_PK": f"SESSION#{session_id}", "GSI_SK": "META",
        "sessionId": session_id, "userId": user_id, "status": "active",
        "createdAt": "2026-09-01T00:00:00Z", "lastMessageAt": "2026-09-01T00:00:00Z",
        "messageCount": Decimal(0),
    })


def _session_row(table, session_id="s1", user_id="u1"):
    return table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"S#{session_id}"})["Item"]


@pytest.mark.asyncio
async def test_ledger_lands_on_the_cost_row_and_events_roll_up_by_kind(sessions_metadata_table, monkeypatch):
    monkeypatch.delenv("COST_DIAGNOSTICS_ENABLED", raising=False)
    from apis.shared.sessions.metadata import store_message_metadata

    _seed_session(sessions_metadata_table)
    await store_message_metadata(
        session_id="s1", user_id="u1", message_id=1,
        message_metadata=_meta(
            prefixTokens={"system": 12_000, "tools": 48_000},
            windowRemovedMessages=0,
            compactionEvents=[
                {"kind": "applied", "checkpoint": 12, "summaryTokens": 900},
                {"kind": "checkpoint", "checkpoint": 12, "summaryTokens": 900},
            ],
        ),
    )
    await store_message_metadata(
        session_id="s1", user_id="u1", message_id=2,
        message_metadata=_meta(windowRemovedMessages=4, compactionEvents=[{"kind": "forced"}]),
    )

    cost_rows = sorted(
        (i for i in sessions_metadata_table.scan()["Items"] if i["SK"].startswith("C#")),
        key=lambda r: r["SK"],
    )
    assert len(cost_rows) == 2
    first = [r for r in cost_rows if "prefixTokens" in r][0]
    assert first["prefixTokens"] == {"system": Decimal(12_000), "tools": Decimal(48_000)}
    assert first["windowRemovedMessages"] == Decimal(0)
    assert first["compactionEvents"][0]["kind"] == "applied"
    assert [r["windowRemovedMessages"] for r in cost_rows if "prefixTokens" not in r] == [Decimal(4)]

    row = _session_row(sessions_metadata_table)
    assert row["compactionAppliedCount"] == Decimal(1)
    assert row["compactionForcedCount"] == Decimal(1)
    assert row["compactionFloorUnreachableCount"] == Decimal(0)
    # `checkpoint` is counted by `_save_compaction_state(record_event=True)`,
    # never here — two counters for one event would disagree.
    assert "compactionCheckpointCount" not in row


@pytest.mark.asyncio
async def test_kill_switch_writes_no_counters(sessions_metadata_table, monkeypatch):
    monkeypatch.setenv("COST_DIAGNOSTICS_ENABLED", "false")
    from apis.shared.sessions.metadata import store_message_metadata

    _seed_session(sessions_metadata_table)
    await store_message_metadata(
        session_id="s1", user_id="u1", message_id=1,
        message_metadata=_meta(compactionEvents=[{"kind": "applied"}]),
    )
    row = _session_row(sessions_metadata_table)
    for attr in ("compactionAppliedCount", "compactionForcedCount", "compactionFloorUnreachableCount"):
        assert attr not in row


@pytest.mark.asyncio
async def test_malformed_events_count_as_zero_not_as_a_failure(sessions_metadata_table, monkeypatch):
    monkeypatch.delenv("COST_DIAGNOSTICS_ENABLED", raising=False)
    from apis.shared.sessions.metadata import store_message_metadata

    _seed_session(sessions_metadata_table)
    await store_message_metadata(
        session_id="s1", user_id="u1", message_id=1,
        message_metadata=_meta(compactionEvents=["applied", {"nokind": 1}, {"kind": "applied"}]),
    )
    row = _session_row(sessions_metadata_table)
    assert row["compactionAppliedCount"] == Decimal(1)
