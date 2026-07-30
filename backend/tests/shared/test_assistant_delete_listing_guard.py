"""Delete is refused while a marketplace listing exists (version-snapshots §5.2).

Ownership alone used to be enough, so an author could hard-delete an approved Agent
straight out of the store — the same unilateral removal `withdrawal_requested` exists to
stop, except irreversible and taking the review history with it.

The case worth the most attention is
``test_the_guard_runs_before_any_destructive_cleanup``. The `/assistants/{id}` delete path
soft-deletes documents and removes sync policies *before* it deletes the record, so a
refusal discovered at the record write would leave the Agent gutted **and** still in the
store — strictly worse than either outcome alone.
"""

import asyncio

import boto3
import pytest
from moto import mock_aws

from apis.shared.assistants.service import (
    AssistantListedError,
    assert_deletable,
    delete_assistant,
)

REGION = "us-east-1"
TABLE = "test-rag-assistants"
AGENT_ID = "ast-deletable01"
OWNER = "user-author"


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


def seed(table, listing=None):
    item = {
        "PK": f"AST#{AGENT_ID}",
        "SK": "METADATA",
        "assistantId": AGENT_ID,
        "ownerId": OWNER,
        "ownerName": "Ada Author",
        "name": "Policy Lookup",
        "description": "Find and cite university policy",
        "instructions": "Answer from the policy manual.",
        "vectorIndexId": "idx",
        "visibility": "PUBLIC",
        "createdAt": "2026-07-01T00:00:00Z",
        "updatedAt": "2026-07-01T00:00:00Z",
        "status": "COMPLETE",
    }
    if listing:
        item["listing"] = listing
    table.put_item(Item=item)


def listing_of(state: str, **extra) -> dict:
    return {
        "state": state,
        "category": "Administration",
        "publisherId": "pub-registrar",
        **extra,
    }


def exists(table) -> bool:
    return "Item" in table.get_item(Key={"PK": f"AST#{AGENT_ID}", "SK": "METADATA"})


# ── what may be deleted ──────────────────────────────────────────────────────────────
def test_an_agent_with_no_listing_deletes(table):
    """The common case, and it must not change: most Agents were never submitted."""
    seed(table)
    assert asyncio.run(delete_assistant(AGENT_ID, OWNER)) is True
    assert not exists(table)


def test_a_private_listing_deletes(table):
    """``private`` is reachable from every other state, so this is the way out."""
    seed(table, listing_of("private"))
    assert asyncio.run(delete_assistant(AGENT_ID, OWNER)) is True
    assert not exists(table)


# ── what may not ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "state", ["published", "in_review", "changes_requested", "taken_down", "withdrawal_requested"]
)
def test_a_listed_agent_is_refused_and_survives(table, state):
    seed(table, listing_of(state))

    with pytest.raises(AssistantListedError):
        asyncio.run(delete_assistant(AGENT_ID, OWNER))

    assert exists(table), "a refused delete must not have deleted anything"


def test_taken_down_is_refused_deliberately(table):
    """An author must not be able to delete their way out of a takedown record."""
    seed(table, listing_of("taken_down"))
    with pytest.raises(AssistantListedError):
        asyncio.run(delete_assistant(AGENT_ID, OWNER))


def test_a_pending_withdrawal_is_refused(table):
    """A requested withdrawal is not a granted one — deleting would pre-empt the admin."""
    seed(table, listing_of("withdrawal_requested"))
    with pytest.raises(AssistantListedError):
        asyncio.run(delete_assistant(AGENT_ID, OWNER))


def test_the_refusal_names_the_path_forward(table):
    """An author told "no" without being told "do this instead" files a ticket."""
    seed(table, listing_of("published"))
    with pytest.raises(AssistantListedError) as raised:
        asyncio.run(delete_assistant(AGENT_ID, OWNER))
    assert "Request withdrawal first" in raised.value.message

    seed(table, listing_of("changes_requested"))
    with pytest.raises(AssistantListedError) as raised:
        asyncio.run(delete_assistant(AGENT_ID, OWNER))
    assert "back to private" in raised.value.message


# ── the ordering guard ───────────────────────────────────────────────────────────────
def test_the_guard_runs_before_any_destructive_cleanup(table):
    """``assert_deletable`` exists so a caller can refuse *before* doing damage.

    ``/assistants/{id}`` soft-deletes documents and removes sync policies before the record
    delete. Discovering the refusal down there would leave a live listing pointing at a
    gutted Agent — the failure this refusal exists to prevent, made worse.
    """
    seed(table, listing_of("published"))
    with pytest.raises(AssistantListedError):
        asyncio.run(assert_deletable(AGENT_ID, OWNER))


def test_the_guard_and_the_delete_agree(table):
    """Two call sites, one rule. If these diverge, one path silently permits the other's no."""
    for state in ("published", "in_review", "taken_down", "withdrawal_requested"):
        seed(table, listing_of(state))
        with pytest.raises(AssistantListedError):
            asyncio.run(assert_deletable(AGENT_ID, OWNER))
        with pytest.raises(AssistantListedError):
            asyncio.run(delete_assistant(AGENT_ID, OWNER))

    for state in (None, "private"):
        seed(table, listing_of(state) if state else None)
        asyncio.run(assert_deletable(AGENT_ID, OWNER))  # does not raise
        assert asyncio.run(delete_assistant(AGENT_ID, OWNER)) is True


def test_the_guard_is_silent_on_a_missing_agent(table):
    """"Not found" is the delete path's own 404; pre-empting it would mislabel the error."""
    asyncio.run(assert_deletable("ast-nope", OWNER))


def test_the_guard_is_silent_for_a_non_owner(table):
    """Same reason — ownership is the delete path's decision, not this guard's."""
    seed(table, listing_of("published"))
    asyncio.run(assert_deletable(AGENT_ID, "user-someone-else"))
    assert asyncio.run(delete_assistant(AGENT_ID, "user-someone-else")) is False


# ── child rows must not outlive the Agent ────────────────────────────────────────────
def test_deleting_an_agent_takes_its_version_snapshots_with_it(table):
    """Versions are child rows, and child rows must never outlive what they concern.

    Missed when snapshots shipped: nothing deleted ``VERSION#`` rows, so a deleted Agent
    left its whole history behind. Invisible rather than harmful — a deletable Agent's
    listing is ``private``, so its versions carry no store key — which is precisely why the
    leak would never have surfaced on its own.
    """
    from apis.shared.assistants.version_repository import create_version, list_versions
    from apis.shared.assistants.versions import snapshot_of
    from apis.shared.assistants.service import get_assistant

    seed(table, listing_of("private"))
    agent = asyncio.run(get_assistant(AGENT_ID, OWNER))
    for _ in range(3):
        asyncio.run(create_version(AGENT_ID, snapshot_of(agent)))
    assert len(asyncio.run(list_versions(AGENT_ID))) == 3

    assert asyncio.run(delete_assistant(AGENT_ID, OWNER)) is True
    assert asyncio.run(list_versions(AGENT_ID)) == []


def test_a_refused_delete_leaves_the_versions_alone(table):
    """The cleanup rides the delete, so a refusal must not take the history with it."""
    from apis.shared.assistants.version_repository import create_version, list_versions
    from apis.shared.assistants.versions import snapshot_of
    from apis.shared.assistants.service import get_assistant

    seed(table, listing_of("published", publishedVersion=1))
    agent = asyncio.run(get_assistant(AGENT_ID, OWNER))
    asyncio.run(create_version(AGENT_ID, snapshot_of(agent)))

    with pytest.raises(AssistantListedError):
        asyncio.run(delete_assistant(AGENT_ID, OWNER))

    assert len(asyncio.run(list_versions(AGENT_ID))) == 1
