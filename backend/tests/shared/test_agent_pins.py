"""Agent Marketplace Phase 5 — user pin state (D8, D9 user side).

Two things here are load-bearing beyond "does the write round-trip":

* **The dismissal tombstone.** Role-seeded pins (Phase 6) resolve live, so an unpin that
  does not remember itself is an unpin the next request undoes. It is written in Phase 5,
  before anything reads it, so that resolver inherits a real history.
* **Pin ↔ dismiss is one toggle.** Re-pinning must clear the tombstone, or a user who
  changed their mind is permanently unreachable by a future role seed for that Agent.

These go through a real (moto) table rather than mocking the item, because the shape of
the stored item *is* the contract the Phase 6 resolver will read.
"""

import boto3
import pytest
from moto import mock_aws

from apis.shared.assistants.pins import (
    MAX_PINS,
    PinLimitError,
    add_pin,
    get_pin_state,
    remove_pin,
)

REGION = "us-east-1"
TABLE = "test-user-settings"
USER = "user-001"


async def _pinned_ids(user_id: str = USER):
    """The user's pinned ids in shelf order — what the pin read sorts by."""
    state = await get_pin_state(user_id)
    return [ref.agent_id for ref in sorted(state.pinned, key=lambda ref: ref.order)]


async def _ref(agent_id: str, user_id: str = USER):
    state = await get_pin_state(user_id)
    return next((ref for ref in state.pinned if ref.agent_id == agent_id), None)


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_USER_SETTINGS_TABLE_NAME", TABLE)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb.Table(TABLE)


# ── the stored shape ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_user_with_no_pins_reads_as_empty(table):
    state = await get_pin_state(USER)
    assert state.pinned == []
    assert state.dismissed == []


@pytest.mark.asyncio
async def test_pin_writes_the_documented_item(table):
    await add_pin(USER, "ast-001")

    item = table.get_item(Key={"PK": f"USER#{USER}", "SK": "PINNED_AGENTS"})["Item"]
    assert item["pinned"][0]["agentId"] == "ast-001"
    assert "pinnedAt" in item["pinned"][0]
    assert item["dismissed"] == []


@pytest.mark.asyncio
async def test_pins_keep_their_order(table):
    for agent_id in ("ast-001", "ast-002", "ast-003"):
        await add_pin(USER, agent_id)

    assert await _pinned_ids() == ["ast-001", "ast-002", "ast-003"]


@pytest.mark.asyncio
async def test_pinning_twice_is_idempotent(table):
    await add_pin(USER, "ast-001")
    first = await _ref("ast-001")
    await add_pin(USER, "ast-001")
    second = await _ref("ast-001")

    assert await _pinned_ids() == ["ast-001"]
    # A double tap must not reshuffle the shelf or rewrite when it was pinned.
    assert second.pinned_at == first.pinned_at
    assert second.order == first.order


# ── the tombstone (D9.3) ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_unpin_records_a_dismissal(table):
    await add_pin(USER, "ast-001")
    await remove_pin(USER, "ast-001")

    state = await get_pin_state(USER)
    assert state.pinned == []
    assert state.dismissed == ["ast-001"]


@pytest.mark.asyncio
async def test_unpinning_something_never_pinned_still_tombstones(table):
    """The Phase 6 case: dismissing a role-seeded pin the user never pinned themselves."""
    await remove_pin(USER, "ast-seeded")

    assert (await get_pin_state(USER)).dismissed == ["ast-seeded"]


@pytest.mark.asyncio
async def test_repinning_clears_the_tombstone(table):
    """Otherwise a user who changed their mind is unreachable by a future role seed."""
    await remove_pin(USER, "ast-001")
    await add_pin(USER, "ast-001")

    state = await get_pin_state(USER)
    assert state.dismissed == []
    assert [ref.agent_id for ref in state.pinned] == ["ast-001"]


@pytest.mark.asyncio
async def test_repinning_an_already_pinned_agent_still_clears_a_stale_tombstone(table):
    """A pin and a tombstone must never coexist — the resolver would have to guess."""
    await add_pin(USER, "ast-001")
    table.update_item(
        Key={"PK": f"USER#{USER}", "SK": "PINNED_AGENTS"},
        UpdateExpression="SET dismissed = :d",
        ExpressionAttributeValues={":d": ["ast-001"]},
    )

    await add_pin(USER, "ast-001")

    state = await get_pin_state(USER)
    assert state.dismissed == []
    assert [ref.agent_id for ref in state.pinned] == ["ast-001"]


@pytest.mark.asyncio
async def test_dismissals_do_not_accumulate_duplicates(table):
    await remove_pin(USER, "ast-001")
    await remove_pin(USER, "ast-001")

    assert (await get_pin_state(USER)).dismissed == ["ast-001"]


# ── the ceiling ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_pinning_past_the_ceiling_is_refused_with_an_actionable_message(table):
    for index in range(MAX_PINS):
        await add_pin(USER, f"ast-{index:03d}")

    with pytest.raises(PinLimitError) as excinfo:
        await add_pin(USER, "ast-one-too-many")

    assert str(MAX_PINS) in str(excinfo.value)
    assert len((await get_pin_state(USER)).pinned) == MAX_PINS


@pytest.mark.asyncio
async def test_repinning_at_the_ceiling_is_not_refused(table):
    """The limit is on growth. An idempotent re-pin adds nothing, so it cannot exceed it."""
    for index in range(MAX_PINS):
        await add_pin(USER, f"ast-{index:03d}")

    await add_pin(USER, "ast-000")

    assert len((await get_pin_state(USER)).pinned) == MAX_PINS


@pytest.mark.asyncio
async def test_pins_are_per_user(table):
    await add_pin(USER, "ast-001")
    await add_pin("user-002", "ast-002")

    assert await _pinned_ids() == ["ast-001"]
    assert await _pinned_ids("user-002") == ["ast-002"]
