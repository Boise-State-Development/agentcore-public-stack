"""Agent Marketplace Phase 1 — publisher profiles and eligibility (D12).

Two properties matter more than the CRUD: an individual profile is auto-created on first
submission and is **never verified**, and eligibility is a *proposal* allowlist that never
reaches an access decision.
"""

import boto3
import pytest
from moto import mock_aws

from apis.shared.assistants.publishers import (
    delete_publisher,
    ensure_individual_profile,
    get_publisher,
    list_eligibility,
    list_publishers,
    list_publishers_for_user,
    put_publisher,
    set_eligibility,
)
from apis.shared.assistants.models import PublisherProfile

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
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb.Table(TABLE)


# ── auto-created individual profiles ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_individual_profile_is_created_on_first_submission(table):
    profile = await ensure_individual_profile("user-001", "Ada Author")

    assert profile.kind == "individual"
    assert profile.label == "Ada Author"
    assert await get_publisher(profile.id) is not None


@pytest.mark.asyncio
async def test_individual_profiles_are_never_verified(table):
    """``verified`` means 'a university team stands behind this' — never a single person."""
    profile = await ensure_individual_profile("user-001", "Ada Author")
    assert profile.verified is False


@pytest.mark.asyncio
async def test_auto_creation_is_idempotent(table):
    """A second submission must find the profile, not mint a duplicate under a new id."""
    first = await ensure_individual_profile("user-001", "Ada Author")
    second = await ensure_individual_profile("user-001", "Ada Renamed")

    assert first.id == second.id
    assert second.label == "Ada Author"  # the existing record wins
    assert len([p for p in await list_publishers() if p.kind == "individual"]) == 1


@pytest.mark.asyncio
async def test_author_may_propose_their_own_profile_and_only_that(table):
    await ensure_individual_profile("user-001", "Ada Author")
    await ensure_individual_profile("user-002", "Bo Builder")

    eligible = await list_publishers_for_user("user-001")
    assert eligible == [(await ensure_individual_profile("user-001", "Ada Author")).id]


@pytest.mark.asyncio
async def test_user_ids_with_awkward_characters_produce_a_usable_id(table):
    """The id lands in a sort key, so it is slugified."""
    profile = await ensure_individual_profile("AzureAD\\ada.author@u.edu", "Ada Author")
    assert "\\" not in profile.id
    assert await get_publisher(profile.id) is not None


# ── admin-managed profiles ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_profiles_sort_by_order_then_label(table):
    await put_publisher(PublisherProfile(id="p-c", label="Comms", kind="department", order=2))
    await put_publisher(PublisherProfile(id="p-a", label="Advising", kind="department", order=1))
    await put_publisher(PublisherProfile(id="p-b", label="Bursar", kind="department", order=1))

    assert [p.id for p in await list_publishers()] == ["p-a", "p-b", "p-c"]


@pytest.mark.asyncio
async def test_enabled_only_filters_disabled_profiles(table):
    await put_publisher(PublisherProfile(id="p-a", label="Advising", kind="department"))
    await put_publisher(
        PublisherProfile(id="p-x", label="Retired", kind="department", enabled=False)
    )

    assert [p.id for p in await list_publishers(enabled_only=True)] == ["p-a"]


@pytest.mark.asyncio
async def test_verified_institution_profile_round_trips(table):
    await put_publisher(
        PublisherProfile(id="p-bsu", label="Boise State", kind="institution", verified=True)
    )
    profile = await get_publisher("p-bsu")

    assert profile.verified is True
    assert profile.kind == "institution"


# ── eligibility is a proposal allowlist ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_set_eligibility_replaces_the_set(table):
    await put_publisher(PublisherProfile(id="p-a", label="Advising", kind="department"))

    await set_eligibility("p-a", ["user-001", "user-002"])
    assert await list_eligibility("p-a") == ["user-001", "user-002"]

    await set_eligibility("p-a", ["user-002", "user-003"])
    assert await list_eligibility("p-a") == ["user-002", "user-003"]


@pytest.mark.asyncio
async def test_eligibility_is_scoped_to_its_own_publisher(table):
    await put_publisher(PublisherProfile(id="p-a", label="Advising", kind="department"))
    await put_publisher(PublisherProfile(id="p-b", label="Bursar", kind="department"))

    await set_eligibility("p-a", ["user-001"])
    await set_eligibility("p-b", ["user-002"])

    assert await list_eligibility("p-a") == ["user-001"]
    assert await list_publishers_for_user("user-001") == ["p-a"]


@pytest.mark.asyncio
async def test_deleting_a_publisher_removes_its_eligibility_items(table):
    await put_publisher(PublisherProfile(id="p-a", label="Advising", kind="department"))
    await set_eligibility("p-a", ["user-001", "user-002"])

    await delete_publisher("p-a")

    assert await get_publisher("p-a") is None
    assert await list_eligibility("p-a") == []
    assert await list_publishers_for_user("user-001") == []


@pytest.mark.asyncio
async def test_eligibility_records_carry_no_permission_semantics(table):
    """Guard on the shape: an eligibility item is a pointer, not a grant.

    If a role, scope or permission field ever appears here, someone has started treating
    the proposal allowlist as an access control — which D12 explicitly forbids.
    """
    await put_publisher(PublisherProfile(id="p-a", label="Advising", kind="department"))
    await set_eligibility("p-a", ["user-001"])

    item = table.get_item(Key={"PK": "AGENT_PUBLISHERS", "SK": "ELIG#p-a#user-001"})["Item"]
    assert set(item) == {"PK", "SK", "publisherId", "userId", "createdAt"}
