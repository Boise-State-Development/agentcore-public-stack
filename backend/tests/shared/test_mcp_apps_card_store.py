"""Tests for the app-initiated tool-card store (MCP Apps PR #6, Option A).

The store reuses the existing `sessions-metadata` table. No DynamoDB in
tests — the no-table path is a silent no-op (matches dev), and a fake
table asserts the record shape, the ownership re-check, and the size cap.
"""

from __future__ import annotations

from decimal import Decimal

from apis.shared.mcp_apps.card_store import AppCardStore


class _FakeTable:
    def __init__(self, items=None) -> None:
        self.items = items or []
        self.puts: list = []

    def put_item(self, Item):  # noqa: N803 - boto3 kwarg name
        self.puts.append(Item)

    def query(self, **kwargs):
        return {"Items": self.items}


def _store_with(table) -> AppCardStore:
    s = AppCardStore()  # __init__ sets _table=None without the env var
    s._table = table
    return s


def test_no_table_is_silent_noop():
    s = AppCardStore()
    assert s.enabled is False
    # Must not raise.
    s.store(
        user_id="u1",
        session_id="s1",
        tool_use_id="tu1",
        tool_name="t",
        arguments={},
        content=[],
        is_error=False,
    )
    assert s.list_for_session(session_id="s1", user_id="u1") == []


def test_store_writes_appcard_record_shape():
    table = _FakeTable()
    s = _store_with(table)
    s.store(
        user_id="u1",
        session_id="s1",
        tool_use_id="tu1",
        tool_name="widget_tool",
        arguments={"q": "x", "n": 1.5},
        content=[{"type": "text", "text": "ok"}],
        is_error=False,
    )
    assert len(table.puts) == 1
    item = table.puts[0]
    assert item["PK"] == "USER#u1"
    assert item["SK"].startswith("APPCARD#")
    assert item["GSI_PK"] == "SESSION#s1"
    assert item["GSI_SK"].startswith("APPCARD#")
    assert item["toolName"] == "widget_tool"
    assert item["isError"] is False
    # floats are stored as Decimal for DynamoDB.
    assert item["arguments"]["n"] == Decimal("1.5")
    assert "ttl" in item


def test_store_caps_oversized_content():
    table = _FakeTable()
    s = _store_with(table)
    huge = [{"type": "text", "text": "z" * 300_000}]
    s.store(
        user_id="u1",
        session_id="s1",
        tool_use_id="tu1",
        tool_name="t",
        arguments={},
        content=huge,
        is_error=False,
    )
    stored = table.puts[0]["content"]
    assert stored == [
        {"type": "text", "text": "[result omitted from history — too large to persist]"}
    ]


def test_list_filters_by_owner_and_cleans_record():
    items = [
        {
            "PK": "USER#u1",
            "SK": "APPCARD#2026-01-01T00:00:00#aaa",
            "GSI_PK": "SESSION#s1",
            "GSI_SK": "APPCARD#2026-01-01T00:00:00",
            "ttl": 123,
            "userId": "u1",
            "sessionId": "s1",
            "toolName": "mine",
            "isError": False,
            "producedByMessageIndex": Decimal("4"),
        },
        {
            "PK": "USER#someone-else",
            "SK": "APPCARD#2026-01-01T00:00:01#bbb",
            "GSI_PK": "SESSION#s1",
            "GSI_SK": "APPCARD#2026-01-01T00:00:01",
            "userId": "other",
            "toolName": "not-mine",
            "isError": False,
        },
    ]
    s = _store_with(_FakeTable(items))
    cards = s.list_for_session(session_id="s1", user_id="u1")

    assert len(cards) == 1
    card = cards[0]
    assert card["toolName"] == "mine"
    # Key attributes are stripped from the returned card.
    for k in ("PK", "SK", "GSI_PK", "GSI_SK", "ttl"):
        assert k not in card
    # Decimals are converted back to native ints/floats.
    assert card["producedByMessageIndex"] == 4
    assert isinstance(card["producedByMessageIndex"], int)
