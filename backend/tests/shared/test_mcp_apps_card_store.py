"""Tests for the app-initiated tool-card store (MCP Apps PR #6, Option A).

The store reuses the existing `sessions-metadata` table. The no-table path
is a silent no-op (matches dev); writes run against a moto table so the
`update_item` re-run semantics are DynamoDB's own, and a fake table feeds
the read path rows in the legacy one-per-run shape.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apis.shared.mcp_apps.card_store import AppCardStore


class _FakeTable:
    def __init__(self, items=None) -> None:
        self.items = items or []

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


def _store_call(store: AppCardStore, **over) -> None:
    kwargs = dict(
        user_id="u1",
        session_id="s1",
        tool_use_id="tu1",
        tool_name="widget_tool",
        arguments={"q": "x", "n": 1.5},
        content=[{"type": "text", "text": "ok"}],
        is_error=False,
    )
    kwargs.update(over)
    store.store(**kwargs)


def _rows(table) -> list[dict]:
    return table.scan()["Items"]


def test_store_writes_appcard_record_shape(sessions_metadata_table):
    s = AppCardStore()
    assert s.enabled is True
    _store_call(s)

    (item,) = _rows(sessions_metadata_table)
    assert item["PK"] == "USER#u1"
    assert item["SK"].startswith("APPCARD#")
    assert item["GSI_PK"] == "SESSION#s1"
    assert item["GSI_SK"] == f"APPCARD#{item['createdAt']}"
    assert item["toolName"] == "widget_tool"
    assert item["toolUseId"] == "tu1"
    assert item["isError"] is False
    # floats are stored as Decimal for DynamoDB.
    assert item["arguments"]["n"] == Decimal("1.5")
    assert "ttl" in item
    assert item["cardId"]


def test_rerun_of_the_same_call_updates_one_card(sessions_metadata_table):
    # An App re-runs its setup lookups every time its frame mounts, and the
    # frame re-mounts on every reload. Reopening a conversation must not
    # add cards (the frame's "N actions" chip grew on each reload).
    s = AppCardStore()
    _store_call(s, is_error=True, content=[{"type": "text", "text": "404"}])
    (first,) = _rows(sessions_metadata_table)

    # Same call, arguments in a different key order: still the same action.
    _store_call(s, arguments={"n": 1.5, "q": "x"})

    (row,) = _rows(sessions_metadata_table)
    assert row["SK"] == first["SK"]
    assert row["cardId"] == first["cardId"]
    # Keeps its first position, shows the latest result.
    assert row["createdAt"] == first["createdAt"]
    assert row["GSI_SK"] == first["GSI_SK"]
    assert row["isError"] is False
    assert row["content"] == [{"type": "text", "text": "ok"}]
    assert row["updatedAt"] >= first["updatedAt"]

    cards = s.list_for_session(session_id="s1", user_id="u1")
    assert len(cards) == 1


def test_github_issue_write_reload_keeps_the_card_count(sessions_metadata_table):
    # The case found on dev: five ui_get lookups per mount (one 404s for a
    # user-owned repo), then the App's real write. Two mounts must list six
    # cards, not eleven, and the failure count must not double.
    s = AppCardStore()
    lookups = ["assignees", "labels", "milestones", "issue_fields", "issue_types"]

    def mount() -> None:
        for method in lookups:
            _store_call(
                s,
                tool_name="ui_get",
                arguments={"method": method, "owner": "o", "repo": "r"},
                is_error=method == "issue_types",
            )

    mount()
    _store_call(
        s,
        tool_name="issue_write",
        arguments={"method": "create", "title": "Bug", "_ui_submitted": True},
    )
    mount()  # reload

    cards = s.list_for_session(session_id="s1", user_id="u1")
    assert len(cards) == 6
    assert sum(1 for c in cards if c["isError"]) == 1
    assert [c["toolName"] for c in cards].count("issue_write") == 1


@pytest.mark.parametrize(
    "over",
    [
        {"arguments": {"q": "y", "n": 1.5}},
        {"tool_name": "other_tool"},
        {"tool_use_id": "tu2"},
        {"session_id": "s2"},
        {"user_id": "u2"},
    ],
)
def test_a_different_call_is_its_own_card(sessions_metadata_table, over):
    s = AppCardStore()
    _store_call(s)
    _store_call(s, **over)
    assert len(_rows(sessions_metadata_table)) == 2


def test_store_caps_oversized_content(sessions_metadata_table):
    s = AppCardStore()
    huge = [{"type": "text", "text": "z" * 300_000}]
    _store_call(s, content=huge)
    (item,) = _rows(sessions_metadata_table)
    assert item["content"] == [
        {"type": "text", "text": "[result omitted from history — too large to persist]"}
    ]


def test_write_failure_is_swallowed():
    class _Broken:
        def update_item(self, **kwargs):
            raise RuntimeError("throttled")

    s = _store_with(_Broken())
    _store_call(s)  # must not raise into the proxy path


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


def _legacy_row(sk_suffix: str, created_at: str, **over) -> dict:
    """A row in the pre-dedupe shape: one per run, SK keyed by time."""
    row = {
        "PK": "USER#u1",
        "SK": f"APPCARD#{created_at}#{sk_suffix}",
        "GSI_PK": "SESSION#s1",
        "GSI_SK": f"APPCARD#{created_at}",
        "userId": "u1",
        "sessionId": "s1",
        "cardId": sk_suffix,
        "toolUseId": "tu1",
        "toolName": "ui_get",
        "arguments": {"method": "labels", "repo": "r"},
        "content": [{"type": "text", "text": "[]"}],
        "isError": False,
        "createdAt": created_at,
    }
    row.update(over)
    return row


def test_list_collapses_legacy_one_per_run_rows():
    # Conversations reopened before the write-side fix already hold one row
    # per run; reading them must give the same count a fresh one would.
    items = [
        _legacy_row("a", "2026-10-18T00:00:00", isError=True),
        _legacy_row(
            "b",
            "2026-10-18T00:00:01",
            toolName="issue_write",
            arguments={"title": "Bug"},
        ),
        # Same call as "a", re-run on the next mount, key order flipped.
        _legacy_row(
            "c",
            "2026-10-18T00:05:00",
            arguments={"repo": "r", "method": "labels"},
            content=[{"type": "text", "text": "[\"bug\"]"}],
        ),
    ]
    cards = _store_with(_FakeTable(items)).list_for_session(
        session_id="s1", user_id="u1"
    )

    assert [c["cardId"] for c in cards] == ["a", "b"]
    # First position, latest result.
    assert cards[0]["createdAt"] == "2026-10-18T00:00:00"
    assert cards[0]["isError"] is False
    assert cards[0]["content"] == [{"type": "text", "text": "[\"bug\"]"}]
