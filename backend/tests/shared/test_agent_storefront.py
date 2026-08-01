"""Agent Marketplace Phase 5 — the curated store front (D10).

The featured row is the store's **only** ranking lever: browse is newest-first because
``GSI5_SK`` is ``created_at``, and v1 deliberately ships no popularity sort. That makes two
properties worth testing rather than assuming:

* **Order is the admin's, exactly.** Not "roughly", not re-sorted by anything the read
  path happens to know. The array is the ordering.
* **Only published agents render.** The sparse-index physics that make this free for
  browse do not apply to a hand-curated id list, so the state check is explicit — and a
  taken-down agent must leave the shelf without an admin touching anything.
"""

import boto3
import pytest
from moto import mock_aws

from apis.app_api.agent_designer.services.store_service import resolve_featured, store_front
from apis.shared.assistants.listing_repository import batch_get_agents

from .conftest import publish_agent_version, unpublish_agent_version
from apis.shared.assistants.models import AgentListing, PublisherProfile
from apis.shared.assistants.publishers import put_publisher
from apis.shared.assistants.storefront import (
    MAX_FEATURED,
    get_featured_ids,
    put_featured_ids,
)

REGION = "us-east-1"
TABLE = "test-rag-assistants"


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
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
                {"AttributeName": "GSI5_PK", "AttributeType": "S"},
                {"AttributeName": "GSI5_SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "AgentDirectoryIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI5_PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI5_SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb.Table(TABLE)


def _seed_agent(table, agent_id: str, *, name: str, created_at="2026-07-01T00:00:00Z"):
    table.put_item(
        Item={
            "PK": f"AST#{agent_id}",
            "SK": "METADATA",
            "assistantId": agent_id,
            "ownerId": "user-author",
            "ownerName": "Ada Author",
            "name": name,
            "description": "A description the shelf must not show",
            "instructions": "SECRET SYSTEM PROMPT",
            "vectorIndexId": "assistants-index",
            "visibility": "PUBLIC",
            "usageCount": 0,
            "createdAt": created_at,
            "updatedAt": created_at,
            "status": "COMPLETE",
            "emoji": "📋",
            "tagline": "One line",
        }
    )


async def _publish(agent_id: str, category="Administration", created_at="2026-07-01T00:00:00Z"):
    """Cut a snapshot and shelve it — see ``conftest.publish_agent_version``.

    The featured row resolves through the snapshot too, and it takes two reads to get
    there (the Agent row names the version, the version renders). That is the point of
    keeping this helper honest rather than writing the keys by hand: a curated shelf that
    silently fell back to the live record would be the least noticed place for it to happen.
    """
    await publish_agent_version(agent_id, category, created_at)


async def _unpublish(agent_id: str, state="taken_down", created_at="2026-07-01T00:00:00Z"):
    await unpublish_agent_version(agent_id, "Administration", created_at, state=state)


# ── the config item ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_unset_store_front_is_empty(table):
    assert await get_featured_ids() == []


@pytest.mark.asyncio
async def test_the_array_is_the_order(table):
    await put_featured_ids(["ast-c", "ast-a", "ast-b"], updated_by="admin-1")

    assert await get_featured_ids() == ["ast-c", "ast-a", "ast-b"]


@pytest.mark.asyncio
async def test_duplicates_collapse_to_their_first_position(table):
    saved = await put_featured_ids(["ast-a", "ast-b", "ast-a"], updated_by="admin-1")

    assert saved == ["ast-a", "ast-b"]


@pytest.mark.asyncio
async def test_the_row_has_a_ceiling(table):
    with pytest.raises(ValueError):
        await put_featured_ids(
            [f"ast-{index}" for index in range(MAX_FEATURED + 1)], updated_by="admin-1"
        )


@pytest.mark.asyncio
async def test_a_later_put_replaces_the_whole_row(table):
    await put_featured_ids(["ast-a", "ast-b"], updated_by="admin-1")
    await put_featured_ids(["ast-c"], updated_by="admin-2")

    assert await get_featured_ids() == ["ast-c"]


# ── resolution ───────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_featured_rows_render_in_the_configured_order(table):
    for agent_id, name in (("ast-a", "Alpha"), ("ast-b", "Beta"), ("ast-c", "Gamma")):
        _seed_agent(table, agent_id, name=name)
        await _publish(agent_id)

    rows, unavailable = await resolve_featured(["ast-c", "ast-a", "ast-b"])

    assert [row.name for row in rows] == ["Gamma", "Alpha", "Beta"]
    assert unavailable == []


@pytest.mark.asyncio
async def test_a_featured_row_carries_no_behavior(table):
    """The featured tile is a shelf row (D4), so it gets the same narrow projection."""
    _seed_agent(table, "ast-a", name="Alpha")
    await _publish("ast-a")

    rows, _ = await resolve_featured(["ast-a"])
    payload = rows[0].model_dump(by_alias=True)

    for leaked in ("instructions", "description", "bindings", "ownerId", "ownerName"):
        assert leaked not in payload


@pytest.mark.asyncio
async def test_the_publisher_is_resolved_for_a_featured_tile(table):
    await put_publisher(
        PublisherProfile(
            id="pub-registrar", label="Office of the Registrar", kind="department", verified=True
        )
    )
    _seed_agent(table, "ast-a", name="Alpha")
    await _publish("ast-a")

    rows, _ = await resolve_featured(["ast-a"])

    assert rows[0].publisher.label == "Office of the Registrar"
    assert rows[0].publisher.verified is True


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["taken_down", "private", "in_review", "changes_requested"])
async def test_an_unpublished_agent_leaves_the_featured_row(table, state):
    _seed_agent(table, "ast-a", name="Alpha")
    await _publish("ast-a")
    await _unpublish("ast-a", state=state)

    rows, unavailable = await resolve_featured(["ast-a"])

    assert rows == []
    assert unavailable == ["ast-a"]


@pytest.mark.asyncio
async def test_a_deleted_agent_is_reported_not_crashed_on(table):
    rows, unavailable = await resolve_featured(["ast-gone"])

    assert rows == []
    assert unavailable == ["ast-gone"]


@pytest.mark.asyncio
async def test_an_agent_that_was_never_submitted_cannot_be_featured(table):
    _seed_agent(table, "ast-a", name="Alpha")

    rows, unavailable = await resolve_featured(["ast-a"])

    assert rows == []
    assert unavailable == ["ast-a"]


@pytest.mark.asyncio
async def test_a_takedown_empties_the_slot_without_rewriting_the_curation(table):
    """Membership is not self-healing: a reversed takedown restores the slot."""
    _seed_agent(table, "ast-a", name="Alpha")
    await _publish("ast-a")
    await put_featured_ids(["ast-a"], updated_by="admin-1")

    await _unpublish("ast-a")
    featured, _categories = await store_front()
    assert featured == []
    assert await get_featured_ids() == ["ast-a"]

    await _publish("ast-a")
    featured, _categories = await store_front()
    assert [row.name for row in featured] == ["Alpha"]


@pytest.mark.asyncio
async def test_store_front_returns_the_featured_row_with_the_categories(table):
    _seed_agent(table, "ast-a", name="Alpha")
    await _publish("ast-a")
    await put_featured_ids(["ast-a"], updated_by="admin-1")

    featured, categories = await store_front()

    assert [row.name for row in featured] == ["Alpha"]
    assert categories, "the default category set should have been seeded"


# ── the batch read ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_batch_get_skips_missing_ids(table):
    _seed_agent(table, "ast-a", name="Alpha")

    found = await batch_get_agents(["ast-a", "ast-missing"])

    assert set(found) == {"ast-a"}


@pytest.mark.asyncio
async def test_batch_get_tolerates_duplicate_ids(table):
    """BatchGetItem rejects duplicate keys outright, so de-duplication is not optional."""
    _seed_agent(table, "ast-a", name="Alpha")

    found = await batch_get_agents(["ast-a", "ast-a"])

    assert set(found) == {"ast-a"}


@pytest.mark.asyncio
async def test_batch_get_of_nothing_does_not_call_dynamo(table):
    assert await batch_get_agents([]) == {}
