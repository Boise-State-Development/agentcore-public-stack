"""The content-free storage readers, against a real (moto) table.

Seeds rows that *carry* every kind of content the denylist names — a title, a
custom prompt, a compaction summary, citation text, display text — and asserts
none of it comes back through the three admin readers, while the numbers next
to it do. The one reader that keeps `title` by decision is pinned too, so a
future "harden everything" sweep does not change it by accident.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apis.shared.observability.content_policy import content_bearing_paths

SESSION_ID = "sess-diag-1"
USER_ID = "user-diag-1"
SUMMARY = "s" * 2_000


def _seed(storage):
    table = storage.sessions_metadata_table
    table.put_item(Item={
        "PK": f"USER#{USER_ID}",
        "SK": f"S#{SESSION_ID}",
        "GSI_PK": f"SESSION#{SESSION_ID}",
        "GSI_SK": "META",
        "sessionId": SESSION_ID,
        "userId": USER_ID,
        "title": "SECRET TITLE",
        "tags": ["secret-tag"],
        "status": "active",
        "createdAt": "2026-09-01T00:00:00Z",
        "lastMessageAt": "2026-09-02T00:00:00Z",
        "messageCount": Decimal(4),
        "totalCost": Decimal("1.25"),
        "lastContextTokens": Decimal(120_000),
        "contextWindow": Decimal(200_000),
        "totalCacheReadTokens": Decimal(10_000),
        "totalCacheWriteTokens": Decimal(40_000),
        "partialMissCount": Decimal(2),
        "partialMissUsd": Decimal("0.9"),
        "wastedUsd": Decimal("0.9"),
        "preferences": {
            "lastModel": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "enabledTools": ["calculator", "analyze_spreadsheet"],
            "customPromptText": "SECRET PROMPT",
            "assistantId": "agent-1",
        },
        "compaction": {
            "checkpoint": Decimal(34),
            "truncationAnchor": Decimal(68),
            "totalSummarizedTurns": Decimal(12),
            "summary": SUMMARY,
        },
        "pausedTurn": {"modelId": "m", "prompt": "SECRET"},
    })
    table.put_item(Item={
        "PK": f"USER#{USER_ID}",
        "SK": "C#2026-09-02T00:00:00Z#abc",
        "GSI_PK": f"SESSION#{SESSION_ID}",
        "GSI_SK": "C#2026-09-02T00:00:00Z",
        "GSI1PK": f"USER#{USER_ID}",
        "GSI1SK": "2026-09-02T00:00:00Z",
        "sessionId": SESSION_ID,
        "messageId": Decimal(3),
        "timestamp": "2026-09-02T00:00:00Z",
        "tokenUsage": {
            "inputTokens": Decimal(100),
            "outputTokens": Decimal(50),
            "cacheReadInputTokens": Decimal(10_000),
            "cacheWriteInputTokens": Decimal(40_000),
        },
        "modelInfo": {
            "modelId": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "modelName": "Claude Haiku 4.5",
            "pricingSnapshot": {"inputPricePerMtok": Decimal("1.1")},
        },
        "cost": {"total": Decimal("0.05")},
        "cacheStatus": "partial_miss",
        "wastedUsd": Decimal("0.04"),
        "agentSwitched": False,
        "prefixFingerprints": {"toolConfigHash": "abc", "systemPromptHash": "def", "historyHash": "ghi", "messageCount": Decimal(3)},
        "citations": [{"text": "SECRET CITATION", "fileName": "secret.pdf", "documentId": "d1"}],
        "displayText": "SECRET USER MESSAGE",
    })


@pytest.mark.asyncio
async def test_user_session_diagnostics_returns_numbers_and_no_content(storage):
    _seed(storage)
    rows = await storage.get_user_session_diagnostics(USER_ID)
    assert len(rows) == 1
    row = rows[0]

    assert content_bearing_paths(row) == []
    assert "title" not in row and "tags" not in row and "pausedTurn" not in row
    # The summary was measured, then dropped.
    assert row["compaction"]["summaryChars"] == len(SUMMARY)
    assert "summary" not in row["compaction"]
    assert row["compaction"]["checkpoint"] == 34 and row["compaction"]["truncationAnchor"] == 68
    # Preferences reduced to the allowlisted keys — the custom prompt is gone.
    assert row["preferences"] == {
        "lastModel": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "enabledTools": ["calculator", "analyze_spreadsheet"],
        "assistantId": "agent-1",
    }
    # And the numbers came through as floats/ints.
    assert row["totalCost"] == 1.25 and row["lastContextTokens"] == 120_000


@pytest.mark.asyncio
async def test_session_diagnostic_row_resolves_by_session_id_only(storage):
    _seed(storage)
    row = await storage.get_session_diagnostic_row(SESSION_ID)
    assert row is not None and row["userId"] == USER_ID
    assert content_bearing_paths(row) == []
    assert row["compaction"]["summaryChars"] == len(SUMMARY)
    assert await storage.get_session_diagnostic_row("no-such-session") is None


@pytest.mark.asyncio
async def test_session_cost_records_no_longer_carry_citation_text(storage):
    _seed(storage)
    records = await storage.get_session_cost_records(SESSION_ID)
    assert len(records) == 1
    rec = records[0]
    assert content_bearing_paths(rec) == []
    assert "citations" not in rec and "displayText" not in rec
    # Only the model id survives from modelInfo — pricing is not the anatomy's business.
    assert rec["modelInfo"] == {"modelId": "us.anthropic.claude-haiku-4-5-20251001-v1:0"}
    # Everything the anatomy reads is still there.
    assert rec["cacheStatus"] == "partial_miss"
    assert rec["tokenUsage"]["cacheWriteInputTokens"] == 40_000
    assert rec["prefixFingerprints"]["systemPromptHash"] == "def"
    assert rec["cost"] == {"total": 0.05}


@pytest.mark.asyncio
async def test_top_sessions_reader_keeps_title_by_decision(storage):
    # Pinned on purpose: the "most expensive conversations" table shows the
    # title. If this ever changes it should change in a PR that says so.
    _seed(storage)
    rows = await storage.get_user_session_costs(USER_ID)
    assert rows[0]["title"] == "SECRET TITLE"
    assert "compaction" not in rows[0]  # and it never widened into the diagnostic fields


def _seed_deleted(storage):
    storage.sessions_metadata_table.put_item(Item={
        "PK": f"USER#{USER_ID}", "SK": "S#gone", "GSI_PK": "SESSION#gone", "GSI_SK": "META",
        "sessionId": "gone", "userId": USER_ID, "deleted": True, "status": "deleted",
        "totalCost": Decimal("9"), "title": "DELETED TITLE",
    })


@pytest.mark.asyncio
async def test_deleted_sessions_are_excluded_from_the_diagnostic_list_by_default(storage):
    _seed(storage)
    _seed_deleted(storage)
    rows = await storage.get_user_session_diagnostics(USER_ID)
    assert [r["sessionId"] for r in rows] == [SESSION_ID]


@pytest.mark.asyncio
async def test_deleted_sessions_are_listed_on_request_and_stay_content_free(storage):
    # A delete is a tombstone, not a refund: the row's cost survives it, so
    # the audit must be able to see it to account for the period total.
    _seed(storage)
    _seed_deleted(storage)
    rows = await storage.get_user_session_diagnostics(USER_ID, include_deleted=True)
    by_id = {r["sessionId"]: r for r in rows}
    assert set(by_id) == {SESSION_ID, "gone"}
    assert by_id["gone"]["deleted"] is True
    assert by_id["gone"]["status"] == "deleted"
    assert by_id["gone"]["totalCost"] == 9
    assert "title" not in by_id["gone"]


@pytest.mark.asyncio
async def test_feedback_rows_come_back_content_free_and_keyed_to_the_call(storage):
    _seed(storage)
    storage.sessions_metadata_table.put_item(Item={
        "PK": f"USER#{USER_ID}", "SK": f"F#{SESSION_ID}#3",
        "GSI_PK": f"SESSION#{SESSION_ID}", "GSI_SK": "F#3",
        "sessionId": SESSION_ID, "messageId": Decimal(3), "userId": USER_ID,
        "value": Decimal(-1), "reason": "wrong", "signal": "explicit", "updatedAt": "2026-09-16T00:00:00Z",
        "ttl": Decimal(1_800_000_000),
        # A stray content-bearing attribute must never leave the reader even
        # if something wrote one (the writer cannot, but the reader is the guard).
        "displayText": "SECRET",
    })
    rows = await storage.get_session_feedback_rows(SESSION_ID)
    assert len(rows) == 1
    row = rows[0]
    assert content_bearing_paths(row) == []
    assert row == {"sessionId": SESSION_ID, "messageId": 3, "value": -1, "reason": "wrong", "signal": "explicit", "updatedAt": "2026-09-16T00:00:00Z"}
    # Joins the C# row on messageId.
    records = await storage.get_session_cost_records(SESSION_ID)
    assert records[0]["messageId"] == row["messageId"]
    assert await storage.get_session_feedback_rows("no-such-session") == []


@pytest.mark.asyncio
async def test_recent_down_thumbs_queue_is_content_free_and_newest_first(storage):
    _seed(storage)
    for i, ts in ((1, "2026-09-16T00:00:01Z"), (2, "2026-09-16T00:00:02Z")):
        storage.sessions_metadata_table.put_item(Item={
            "PK": f"USER#{USER_ID}", "SK": f"F#{SESSION_ID}#{i}",
            "GSI_PK": f"SESSION#{SESSION_ID}", "GSI_SK": f"F#{i}",
            "GSI1PK": "FEEDBACK#down", "GSI1SK": ts,
            "sessionId": SESSION_ID, "messageId": Decimal(i), "userId": USER_ID,
            "value": Decimal(-1), "reason": "wrong", "signal": "explicit", "updatedAt": ts,
            "evaluation": {"reason": "wrong", "scores": {"Builtin.Correctness": {"value": Decimal("0.5"), "n": Decimal(1), "explanation": "SECRET"}}},
            "displayText": "SECRET",
        })
    rows = await storage.get_recent_down_thumbs(limit=10)
    assert [r["messageId"] for r in rows] == [2, 1]
    assert all(content_bearing_paths(r) == [] for r in rows)
    assert "userId" not in rows[0] and "displayText" not in rows[0]
    assert rows[0]["evaluation"]["scores"]["Builtin.Correctness"] == {"value": 0.5, "n": 1}


def _seed_thumb(storage, message_id, value, ts, *, reason=None, signal="explicit", session_id=SESSION_ID):
    item = {
        "PK": f"USER#{USER_ID}", "SK": f"F#{session_id}#{message_id}",
        "GSI_PK": f"SESSION#{session_id}", "GSI_SK": f"F#{message_id}",
        "GSI1PK": f"FEEDBACK#{'down' if value == -1 else 'up'}", "GSI1SK": ts,
        "sessionId": session_id, "messageId": Decimal(message_id), "userId": USER_ID,
        "value": Decimal(value), "signal": signal, "updatedAt": ts,
        "displayText": "SECRET",
    }
    if reason:
        item["reason"] = reason
    storage.sessions_metadata_table.put_item(Item=item)


@pytest.mark.asyncio
async def test_feedback_window_reads_both_polarities_content_free_and_time_bounded(storage):
    _seed(storage)
    _seed_thumb(storage, 1, -1, "2026-09-02T00:00:00Z", reason="wrong")
    _seed_thumb(storage, 2, 1, "2026-09-03T00:00:00Z")
    _seed_thumb(storage, 3, -1, "2026-08-01T00:00:00Z")   # before the window
    _seed_thumb(storage, 4, -1, "2026-10-01T00:00:00Z")   # after the window

    rows = await storage.get_feedback_in_window(start="2026-09-01T00:00:00Z", end="2026-09-30T00:00:00Z")

    assert sorted(r["messageId"] for r in rows) == [1, 2]
    assert {r["value"] for r in rows} == {-1, 1}
    # No user id reaches an aggregate surface, and no content of any kind.
    assert all(content_bearing_paths(r) == [] for r in rows)
    assert all("userId" not in r and "displayText" not in r for r in rows)
    assert [r for r in rows if r["value"] == -1][0]["reason"] == "wrong"


@pytest.mark.asyncio
async def test_feedback_window_is_empty_when_nothing_falls_in_it(storage):
    _seed(storage)
    _seed_thumb(storage, 1, -1, "2026-08-01T00:00:00Z")
    assert await storage.get_feedback_in_window(start="2026-09-01T00:00:00Z", end="2026-09-30T00:00:00Z") == []
