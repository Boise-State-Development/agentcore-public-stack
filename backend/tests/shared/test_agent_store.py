"""Agent Marketplace Phase 2 — the browse read and the category records (D4, D10).

The point of these is that the store's safety property is *structural*: browse cannot
return an unpublished agent because an unpublished agent has no key in GSI5. So the tests
go through a real (moto) table and index rather than mocking the query — a mock would
happily assert whatever filtering logic we wrote, which is exactly the thing that must not
be load-bearing.
"""

import boto3
import pytest
from moto import mock_aws

from apis.app_api.agent_designer.services.store_service import (
    browse_all,
    browse_category,
    store_front,
)
from apis.shared.assistants.categories import (
    category_in_use,
    delete_category,
    ensure_seeded,
    get_category,
    list_categories,
    put_category,
)
from apis.shared.assistants.listing import DEFAULT_CATEGORIES
from apis.shared.assistants.listing_repository import query_store

from .conftest import publish_agent_version, unpublish_agent_version
from apis.shared.assistants.models import AgentCategory, AgentListing, PublisherProfile
from apis.shared.assistants.publishers import put_publisher

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


def _seed_agent(table, agent_id: str, *, created_at: str, name: str = "An Agent", **extra):
    item = {
        "PK": f"AST#{agent_id}",
        "SK": "METADATA",
        "assistantId": agent_id,
        "ownerId": "user-author",
        "ownerName": "Ada Author",
        "name": name,
        "description": "A description the shelf must not show",
        "instructions": "SECRET SYSTEM PROMPT",
        "vectorIndexId": "assistants-index",
        "visibility": "PRIVATE",
        "usageCount": 7,
        "createdAt": created_at,
        "updatedAt": created_at,
        "status": "COMPLETE",
        "emoji": "📋",
        "tagline": "Find and cite university policy",
    }
    item.update(extra)
    table.put_item(Item=item)
    return item


async def _publish(agent_id: str, category: str, created_at: str, publisher_id="pub-registrar"):
    """Cut a snapshot and shelve it — see ``conftest.publish_agent_version``.

    Note what the shelf now renders from: the version, not the Agent row. The seeded row's
    ``instructions`` ("SECRET SYSTEM PROMPT") is captured into the snapshot too, which is
    why ``test_the_shelf_never_carries_behavior`` still means something — the projection has
    to drop it, not merely fail to fetch it.
    """
    await publish_agent_version(agent_id, category, created_at, publisher_id=publisher_id)


# ── the structural guarantee ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_browse_returns_published_agents(table):
    _seed_agent(table, "ast-001", created_at="2026-07-01T00:00:00Z", name="Policy Lookup")
    await _publish("ast-001", "Administration", "2026-07-01T00:00:00Z")

    listings, cursor = await browse_category("Administration")

    assert [row.name for row in listings] == ["Policy Lookup"]
    assert cursor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["in_review", "changes_requested", "taken_down", "private"])
async def test_browse_cannot_see_an_unpublished_agent(table, state):
    """Not filtered out — never indexed. The safety property is structural."""
    _seed_agent(table, "ast-001", created_at="2026-07-01T00:00:00Z")
    await _publish("ast-001", "Administration", "2026-07-01T00:00:00Z")
    await unpublish_agent_version(
        "ast-001", "Administration", "2026-07-01T00:00:00Z", state=state
    )

    listings, _ = await browse_category("Administration")
    assert listings == []


@pytest.mark.asyncio
async def test_an_agent_that_was_never_submitted_is_invisible(table):
    _seed_agent(table, "ast-002", created_at="2026-07-01T00:00:00Z")

    listings, _ = await browse_category("Administration")
    assert listings == []


# ── the read shape ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_shelf_never_carries_behavior(table):
    """D4: a store row is icon, name and one line — not a spec sheet, and not a leak.

    ``instructions`` in particular is returned by ``GET /agents/{id}`` today; the store is
    seen by every browsing user, so it gets the narrowest projection that renders.
    """
    _seed_agent(table, "ast-001", created_at="2026-07-01T00:00:00Z")
    await _publish("ast-001", "Administration", "2026-07-01T00:00:00Z")

    listings, _ = await browse_category("Administration")
    payload = listings[0].model_dump(by_alias=True)

    for leaked in ("instructions", "description", "bindings", "modelConfig", "ownerId", "ownerName"):
        assert leaked not in payload
    assert set(payload) == {"agentId", "name", "tagline", "emoji", "iconUrl", "publisher", "category"}


@pytest.mark.asyncio
async def test_publisher_is_projected_to_label_kind_and_verified(table):
    await put_publisher(
        PublisherProfile(
            id="pub-registrar", label="Office of the Registrar", kind="department", verified=True
        )
    )
    _seed_agent(table, "ast-001", created_at="2026-07-01T00:00:00Z")
    await _publish("ast-001", "Administration", "2026-07-01T00:00:00Z")

    listings, _ = await browse_category("Administration")

    assert listings[0].publisher.label == "Office of the Registrar"
    assert listings[0].publisher.verified is True
    # No publisher id on the shelf — it is an internal reference, not display content.
    assert "id" not in listings[0].publisher.model_dump(by_alias=True)


@pytest.mark.asyncio
async def test_a_listing_whose_publisher_was_deleted_still_renders(table):
    _seed_agent(table, "ast-001", created_at="2026-07-01T00:00:00Z")
    await _publish("ast-001", "Administration", "2026-07-01T00:00:00Z", publisher_id="pub-gone")

    listings, _ = await browse_category("Administration")
    assert listings[0].publisher is None


# ── ordering + pagination ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_browse_is_newest_first(table):
    for idx, created in enumerate(["2026-01-01Z", "2026-04-01Z", "2026-07-01Z"]):
        _seed_agent(table, f"ast-{idx}", created_at=created, name=f"Agent {idx}")
        await _publish(f"ast-{idx}", "Research", created)

    listings, _ = await browse_category("Research")
    assert [row.name for row in listings] == ["Agent 2", "Agent 1", "Agent 0"]


@pytest.mark.asyncio
async def test_cursor_pages_through_a_category(table):
    for idx, created in enumerate(["2026-01-01Z", "2026-04-01Z", "2026-07-01Z"]):
        _seed_agent(table, f"ast-{idx}", created_at=created, name=f"Agent {idx}")
        await _publish(f"ast-{idx}", "Research", created)

    first, cursor = await browse_category("Research", limit=2)
    assert [row.name for row in first] == ["Agent 2", "Agent 1"]
    assert cursor

    second, next_cursor = await browse_category("Research", limit=2, cursor=cursor)
    assert [row.name for row in second] == ["Agent 0"]
    assert next_cursor is None


@pytest.mark.asyncio
async def test_a_malformed_cursor_degrades_to_the_first_page(table):
    """A hand-edited URL is a client bug, not a 500."""
    _seed_agent(table, "ast-001", created_at="2026-07-01T00:00:00Z")
    await _publish("ast-001", "Administration", "2026-07-01T00:00:00Z")

    items, _ = await query_store("Administration", cursor="not-a-real-cursor")
    assert len(items) == 1


@pytest.mark.asyncio
async def test_browse_all_merges_categories_newest_first(table):
    await ensure_seeded()
    _seed_agent(table, "ast-a", created_at="2026-01-01Z", name="Old Admin")
    await _publish("ast-a", "Administration", "2026-01-01Z")
    _seed_agent(table, "ast-b", created_at="2026-07-01Z", name="New Research")
    await _publish("ast-b", "Research", "2026-07-01Z")

    listings = await browse_all()
    assert [row.name for row in listings] == ["New Research", "Old Admin"]


# ── categories (D10) ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_seeding_is_idempotent_and_uses_the_phase_1_ids(table):
    """Seeded ids must equal the Phase 1 constants — they are GSI5 partition suffixes."""
    first = await ensure_seeded()
    second = await ensure_seeded()

    assert [c.id for c in first] == list(DEFAULT_CATEGORIES)
    assert [c.id for c in second] == list(DEFAULT_CATEGORIES)
    assert len(await list_categories()) == len(DEFAULT_CATEGORIES)


@pytest.mark.asyncio
async def test_categories_sort_by_order_then_label(table):
    await put_category(AgentCategory(id="c", label="Ceramics", order=2))
    await put_category(AgentCategory(id="a", label="Astronomy", order=1))
    await put_category(AgentCategory(id="b", label="Botany", order=1))

    assert [c.id for c in await list_categories()] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_renaming_changes_the_label_and_never_the_id(table):
    """The id is half of GSI5_PK, so a rename must not move a listing's partition."""
    await put_category(AgentCategory(id="Administration", label="Administration"))
    _seed_agent(table, "ast-001", created_at="2026-07-01Z")
    await _publish("ast-001", "Administration", "2026-07-01Z")

    existing = await get_category("Administration")
    await put_category(existing.model_copy(update={"label": "University Operations"}))

    renamed = await get_category("Administration")
    assert renamed.id == "Administration"
    assert renamed.label == "University Operations"
    # The shelf still resolves under the original partition.
    listings, _ = await browse_category("Administration")
    assert len(listings) == 1


@pytest.mark.asyncio
async def test_disabled_categories_drop_out_of_the_browse_header(table):
    await ensure_seeded()
    existing = await get_category("Teaching")
    await put_category(existing.model_copy(update={"enabled": False}))

    _featured, categories = await store_front()
    assert "Teaching" not in [c.id for c in categories]


@pytest.mark.asyncio
async def test_disabling_a_category_leaves_its_listings_queryable(table):
    """Disable is the soft alternative to delete; existing listings keep working."""
    await ensure_seeded()
    _seed_agent(table, "ast-001", created_at="2026-07-01Z")
    await _publish("ast-001", "Teaching", "2026-07-01Z")

    existing = await get_category("Teaching")
    await put_category(existing.model_copy(update={"enabled": False}))

    listings, _ = await browse_category("Teaching")
    assert len(listings) == 1


@pytest.mark.asyncio
async def test_category_in_use_detects_a_referencing_listing(table):
    await ensure_seeded()
    _seed_agent(table, "ast-001", created_at="2026-07-01Z")
    await _publish("ast-001", "Teaching", "2026-07-01Z")

    assert await category_in_use("Teaching") is True
    assert await category_in_use("Research") is False


@pytest.mark.asyncio
async def test_delete_removes_an_unreferenced_category(table):
    await ensure_seeded()
    await delete_category("Research")
    assert await get_category("Research") is None


@pytest.mark.asyncio
async def test_store_front_seeds_and_returns_no_featured_row_yet(table):
    """``featured`` is present but empty until Phase 5 — a stable contract, not a stub."""
    featured, categories = await store_front()

    assert featured == []
    assert [c.id for c in categories] == list(DEFAULT_CATEGORIES)
