"""Agent Marketplace Phase 1 — the sparse directory index against a real table.

The state machine tests assert what the keys *should* be; these assert what actually
lands in DynamoDB, because the failure this index exists to prevent — a delisted agent
still answerable by the store query — is a persistence failure, not a logic one.

The guard in ``test_routine_agent_edit_does_not_resurrect_the_directory_key`` is the one
to keep: ``Assistant`` is ``extra="allow"`` and reads hydrate from the raw item, so the
GSI keys round-trip as extra model fields and the generic update path would rewrite them
if they were not listed immutable.
"""

import os

import boto3
import pytest
from moto import mock_aws

from apis.shared.assistants.listing_repository import (
    clear_listing,
    list_by_state,
    write_listing,
)
from apis.shared.assistants.models import AgentListing, Assistant

REGION = "us-east-1"
TABLE = "test-rag-assistants"
AGENT_ID = "ast-marketplace01"
CREATED = "2026-07-01T00:00:00Z"


@pytest.fixture()
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
    with mock_aws():
        yield


@pytest.fixture()
def table(aws):
    """The assistants table, carrying the same GSI5 the CDK construct declares."""
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
    t = ddb.Table(TABLE)
    t.put_item(
        Item={
            "PK": f"AST#{AGENT_ID}",
            "SK": "METADATA",
            "assistantId": AGENT_ID,
            "ownerId": "user-author",
            "ownerName": "Ada Author",
            "name": "Policy Lookup",
            "description": "Find and cite university policy",
            "instructions": "Answer from the policy manual.",
            "vectorIndexId": "assistants-index",
            "visibility": "PRIVATE",
            "usageCount": 0,
            "createdAt": CREATED,
            "updatedAt": CREATED,
            "status": "COMPLETE",
            "GSI_PK": "OWNER#user-author",
            "GSI_SK": f"STATUS#COMPLETE#CREATED#{CREATED}",
            "GSI2_PK": "VISIBILITY#PRIVATE",
            "GSI2_SK": f"STATUS#COMPLETE#CREATED#{CREATED}",
        }
    )
    return t


def _listing(state: str, category: str = "Administration") -> AgentListing:
    return AgentListing(
        state=state, category=category, publisher_id="pub-registrar", submitted_at=CREATED
    )


def _item(table):
    return table.get_item(Key={"PK": f"AST#{AGENT_ID}", "SK": "METADATA"})["Item"]


def _query_store(table, category: str):
    return table.query(
        IndexName="AgentDirectoryIndex",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("GSI5_PK").eq(f"LISTED#{category}"),
    )["Items"]


# ── write / clear ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_publishing_writes_the_directory_key(table):
    await write_listing(AGENT_ID, _listing("published"), CREATED)

    item = _item(table)
    assert item["GSI5_PK"] == "LISTED#Administration"
    assert item["GSI5_SK"] == f"CREATED#{CREATED}"
    assert item["listing"]["state"] == "published"
    assert len(_query_store(table, "Administration")) == 1


@pytest.mark.asyncio
async def test_submission_does_not_write_a_directory_key(table):
    """in_review must never be reachable from the store."""
    await write_listing(AGENT_ID, _listing("in_review"), CREATED)

    item = _item(table)
    assert "GSI5_PK" not in item
    assert "GSI5_SK" not in item
    assert item["listing"]["state"] == "in_review"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["taken_down", "private", "changes_requested"])
async def test_leaving_published_clears_the_directory_key(table, state):
    """The delisting path. A stale key here would keep a pulled agent in the store."""
    await write_listing(AGENT_ID, _listing("published"), CREATED)
    assert _query_store(table, "Administration")

    await write_listing(AGENT_ID, _listing(state), CREATED)

    item = _item(table)
    assert "GSI5_PK" not in item
    assert "GSI5_SK" not in item
    assert item["listing"]["state"] == state
    assert _query_store(table, "Administration") == []


@pytest.mark.asyncio
async def test_recategorizing_a_published_listing_moves_its_partition(table):
    await write_listing(AGENT_ID, _listing("published", "Administration"), CREATED)
    await write_listing(AGENT_ID, _listing("published", "Teaching"), CREATED)

    assert _query_store(table, "Administration") == []
    assert len(_query_store(table, "Teaching")) == 1


@pytest.mark.asyncio
async def test_write_to_a_missing_agent_raises(table):
    with pytest.raises(ValueError, match="not found"):
        await write_listing("ast-nope", _listing("published"), CREATED)


@pytest.mark.asyncio
async def test_clear_listing_removes_block_and_keys(table):
    await write_listing(AGENT_ID, _listing("published"), CREATED)
    await clear_listing(AGENT_ID)

    item = _item(table)
    assert "listing" not in item
    assert "GSI5_PK" not in item


@pytest.mark.asyncio
async def test_presentation_fields_ride_the_same_write(table):
    """D13 edits land atomically with the listing rather than racing a second update."""
    await write_listing(
        AGENT_ID,
        _listing("published"),
        CREATED,
        name="Policy Finder",
        tagline="Cite the policy manual",
        icon_key="icons/policy.png",
    )

    item = _item(table)
    assert item["name"] == "Policy Finder"
    assert item["tagline"] == "Cite the policy manual"
    assert item["iconKey"] == "icons/policy.png"


# ── the immutable-fields guard ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_directory_keys_round_trip_onto_the_model(table):
    """The premise of the two guards below, asserted directly.

    ``Assistant`` is ``extra="allow"`` and ``_get_assistant_cloud`` validates the raw
    DynamoDB item, so the index keys and the listing block come back as model fields and
    are therefore candidates for rewrite by the generic update path. If this ever stops
    being true the guards below become vacuous, so assert it rather than assume it.
    """
    from apis.shared.assistants.service import get_assistant

    await write_listing(AGENT_ID, _listing("published"), CREATED)
    stale = await get_assistant(AGENT_ID, "user-author")

    dumped = stale.model_dump(by_alias=True, exclude_none=True)
    assert dumped["GSI5_PK"] == "LISTED#Administration"
    assert dumped["listing"]["state"] == "published"


@pytest.mark.asyncio
async def test_stale_edit_racing_a_takedown_does_not_resurrect_the_directory_key(table):
    """A takedown must survive an in-flight author edit that began before it.

    The real exposure, reproduced deterministically: the author's request reads the agent
    while it is still published (so its in-memory copy carries GSI5_*), an admin takes it
    down, and then the author's write lands. Without GSI5_* in ``immutable_fields`` that
    write puts the directory key straight back and the pulled agent is in the store again.
    """
    from apis.shared.assistants.service import _update_assistant_cloud, get_assistant

    await write_listing(AGENT_ID, _listing("published"), CREATED)
    stale = await get_assistant(AGENT_ID, "user-author")  # read while published

    await write_listing(AGENT_ID, _listing("taken_down"), CREATED)  # admin pulls it
    assert "GSI5_PK" not in _item(table)

    stale.description = "An unrelated tweak"
    await _update_assistant_cloud(stale, TABLE)  # the in-flight write lands

    item = _item(table)
    assert item["description"] == "An unrelated tweak"
    assert "GSI5_PK" not in item, "a stale edit re-published a taken-down agent"
    assert _query_store(table, "Administration") == []


@pytest.mark.asyncio
async def test_stale_edit_racing_a_review_does_not_clobber_the_decision(table):
    """The same race in the other direction — the listing block itself.

    An author editing their agent while a reviewer approves it must not roll the listing
    back to the state their request happened to read.
    """
    from apis.shared.assistants.service import _update_assistant_cloud, get_assistant

    await write_listing(AGENT_ID, _listing("in_review"), CREATED)
    stale = await get_assistant(AGENT_ID, "user-author")  # read while in review

    await write_listing(AGENT_ID, _listing("published"), CREATED)  # admin approves

    stale.name = "Renamed"
    await _update_assistant_cloud(stale, TABLE)

    item = _item(table)
    assert item["name"] == "Renamed"
    assert item["listing"]["state"] == "published", "a stale edit reverted an approval"
    assert len(_query_store(table, "Administration")) == 1


# ── the D3 backfill default ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_existing_records_carry_no_listing_block(table):
    """The whole backfill: an untouched record has no listing, and PUBLIC never implies one."""
    item = _item(table)
    assert "listing" not in item
    assert "GSI5_PK" not in item

    assistant = Assistant.model_validate(item)
    assert assistant.listing is None


@pytest.mark.asyncio
async def test_public_visibility_never_produces_a_listing(table):
    """D3's data-safety consequence: shipping this must not publish existing PUBLIC agents."""
    from apis.shared.assistants.service import update_assistant

    await update_assistant(assistant_id=AGENT_ID, owner_id="user-author", visibility="PUBLIC")

    item = _item(table)
    assert item["visibility"] == "PUBLIC"
    assert "listing" not in item
    assert "GSI5_PK" not in item
    assert _query_store(table, "Administration") == []


# ── admin reads ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_list_by_state_filters_and_ignores_unsubmitted_records(table):
    assert await list_by_state() == []

    await write_listing(AGENT_ID, _listing("in_review"), CREATED)

    assert len(await list_by_state()) == 1
    assert len(await list_by_state("in_review")) == 1
    assert await list_by_state("published") == []
