"""Tests for the ``SRCV#`` source-provenance store.

Reuses the existing ``sessions-metadata`` table, so there is no DynamoDB here:
the no-table path is a silent no-op (which is what dev looks like), and a fake
table asserts the record shape, the key layout, the ownership re-check and the
clamps.
"""

from __future__ import annotations

from apis.shared.files.provenance_store import SourceProvenanceStore


class _FakeTable:
    def __init__(self, items=None) -> None:
        self.items = items or []
        self.puts: list = []

    def put_item(self, Item):  # noqa: N803 - boto3 kwarg name
        self.puts.append(Item)

    def query(self, **kwargs):
        return {"Items": self.items}


def _store_with(table) -> SourceProvenanceStore:
    store = SourceProvenanceStore()  # __init__ leaves _table None without the env var
    store._table = table
    return store


def _claim(**overrides):
    base = {
        "id": "total",
        "state": "supported",
        "method": "literal",
        "quote": "The total approved budget is $487,500",
        "locator": {"kind": "page", "value": 3, "precision": "measured"},
        "uploadId": "up-1",
        "filename": "award.pdf",
    }
    base.update(overrides)
    return base


def test_no_table_is_a_silent_noop():
    store = SourceProvenanceStore()
    assert store.enabled is False
    # Must not raise: a deployment without the table still streams verdicts.
    store.store(user_id="u1", session_id="s1", tool_use_id="tu1", claims=[_claim()])
    assert store.list_for_session(session_id="s1", user_id="u1") == []


def test_the_row_keys_on_the_tool_use_id_and_the_session_gsi():
    table = _FakeTable()
    _store_with(table).store(
        user_id="u1", session_id="s1", tool_use_id="tu1", claims=[_claim()]
    )
    [item] = table.puts
    assert item["PK"] == "USER#u1"
    assert item["SK"] == "SRCV#tu1"  # last-write-wins per call
    assert item["GSI_PK"] == "SESSION#s1"
    assert item["GSI_SK"].startswith("SRCV#")
    assert item["ttl"] > 0


def test_the_stored_claim_keeps_precision():
    """A locator a reader cannot tell apart from a measured one is the whole
    defect this feature exists to avoid, so the flag is never dropped."""
    table = _FakeTable()
    _store_with(table).store(
        user_id="u1",
        session_id="s1",
        tool_use_id="tu1",
        claims=[_claim(locator={"kind": "page", "value": 9, "precision": "estimated"})],
    )
    [stored] = table.puts[0]["claims"]
    assert stored["locator"] == {"kind": "page", "value": 9, "precision": "estimated"}


def test_a_claim_with_no_locator_stores_none():
    table = _FakeTable()
    _store_with(table).store(
        user_id="u1",
        session_id="s1",
        tool_use_id="tu1",
        claims=[_claim(state="unverified", method="quote_not_located", locator=None)],
    )
    [stored] = table.puts[0]["claims"]
    assert "locator" not in stored
    assert stored["state"] == "unverified"


def test_a_long_quote_is_clamped_rather_than_failing_the_write():
    """An item past DynamoDB's 400KB limit loses the whole turn's provenance,
    which is a bad trade for a longer quote."""
    table = _FakeTable()
    _store_with(table).store(
        user_id="u1",
        session_id="s1",
        tool_use_id="tu1",
        claims=[_claim(quote="x" * 50_000)],
    )
    assert len(table.puts[0]["claims"][0]["quote"]) == 1200


def test_the_claim_count_is_bounded():
    table = _FakeTable()
    _store_with(table).store(
        user_id="u1",
        session_id="s1",
        tool_use_id="tu1",
        claims=[_claim(id=str(i)) for i in range(100)],
    )
    assert len(table.puts[0]["claims"]) == 24


def test_nothing_to_store_writes_nothing():
    table = _FakeTable()
    store = _store_with(table)
    store.store(user_id="u1", session_id="s1", tool_use_id="tu1", claims=[])
    store.store(user_id="u1", session_id="s1", tool_use_id="", claims=[_claim()])
    assert table.puts == []


def test_a_put_failure_is_swallowed():
    class _Boom(_FakeTable):
        def put_item(self, Item):  # noqa: N803
            raise RuntimeError("throughput")

    # Reload survival is the only cost; failing the turn would turn a degraded
    # citation into a lost answer.
    _store_with(_Boom()).store(
        user_id="u1", session_id="s1", tool_use_id="tu1", claims=[_claim()]
    )


def test_list_returns_the_claims_for_the_owner():
    table = _FakeTable(
        items=[
            {"userId": "u1", "toolUseId": "tu1", "claims": [_claim()]},
        ]
    )
    [row] = _store_with(table).list_for_session(session_id="s1", user_id="u1")
    assert row["toolUseId"] == "tu1"
    assert row["claims"][0]["quote"] == "The total approved budget is $487,500"


def test_a_guessed_session_id_cannot_surface_another_users_quotes():
    """These rows carry text from someone's document, so the ownership
    re-check after the GSI query matters more here than for a summary."""
    table = _FakeTable(
        items=[
            {"userId": "owner", "toolUseId": "tu1", "claims": [_claim()]},
            {"userId": "u1", "toolUseId": "tu2", "claims": [_claim(id="mine")]},
        ]
    )
    rows = _store_with(table).list_for_session(session_id="s1", user_id="u1")
    assert [r["toolUseId"] for r in rows] == ["tu2"]
