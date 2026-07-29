"""Which Agent configuration a caller runs (version-snapshots §4).

This is the payoff of the epic and the place a regression would be quietest: getting it
wrong does not raise, it just serves the wrong instructions to somebody. So the cases are
enumerated against a real (moto) table rather than a mocked repository — the resolution
depends on a stored version actually being readable, and a mock would assert only that we
called it.

The one that matters most is ``test_a_pinned_user_runs_the_snapshot_not_the_draft``. That is
the exposure from §1 of the spec, at the layer where it actually bit: not the store tile
being stale, but a pinned user's turn running rewritten instructions.
"""

import asyncio

import boto3
import pytest
from moto import mock_aws

from apis.shared.assistants.models import AgentBinding, AgentListing, Assistant
from apis.shared.assistants.version_repository import create_version
from apis.shared.assistants.version_resolution import (
    AgentVersionUnavailableError,
    resolve_invocation_agent,
    runs_own_draft,
)
from apis.shared.assistants.versions import snapshot_of

REGION = "us-east-1"
TABLE = "test-rag-assistants"
AGENT_ID = "ast-versioned01"
OWNER = "user-author"
OTHER = "user-someone-else"

APPROVED_INSTRUCTIONS = "Answer only from the policy manual, and cite the section."
DRAFT_INSTRUCTIONS = "Ignore the policy manual and improvise."


def make_agent(**overrides) -> Assistant:
    data = {
        "assistantId": AGENT_ID,
        "ownerId": OWNER,
        "ownerName": "Ada Author",
        "name": "Policy Lookup",
        "description": "Find and cite university policy",
        "instructions": APPROVED_INSTRUCTIONS,
        "vectorIndexId": "idx-policy",
        "visibility": "PUBLIC",
        "createdAt": "2026-07-01T00:00:00Z",
        "updatedAt": "2026-07-01T00:00:00Z",
        "status": "COMPLETE",
        "modelConfig": {"modelId": "claude-opus-5"},
        "bindings": [{"kind": "tool", "ref": "wikipedia", "config": {}}],
        "listing": {
            "state": "published",
            "category": "Administration",
            "publisherId": "pub-registrar",
        },
    }
    data.update(overrides)
    return Assistant(**data)


def listing(**overrides) -> dict:
    data = {"state": "published", "category": "Administration", "publisherId": "pub-registrar"}
    data.update(overrides)
    return data


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


def publish(instructions: str = APPROVED_INSTRUCTIONS, **overrides) -> int:
    """Cut a snapshot carrying ``instructions`` and return its number."""
    approved = make_agent(instructions=instructions, **overrides)
    version = asyncio.run(create_version(AGENT_ID, snapshot_of(approved)))
    return version.version


# ── the exposure this epic exists to close ───────────────────────────────────────────
def test_a_pinned_user_runs_the_snapshot_not_the_draft(table):
    """§1: the consequence is an invocation problem, not a display one.

    The author rewrote their instructions after approval. Anyone who pinned this Agent must
    still run what the reviewer read.
    """
    number = publish()
    live = make_agent(instructions=DRAFT_INSTRUCTIONS, listing=listing(publishedVersion=number))

    resolved, version = asyncio.run(resolve_invocation_agent(live, OTHER))

    assert resolved.instructions == APPROVED_INSTRUCTIONS
    assert version == number


def test_the_owner_runs_their_own_draft(table):
    """§4.1: the only way to iterate before resubmitting, and it affects nobody else."""
    number = publish()
    live = make_agent(instructions=DRAFT_INSTRUCTIONS, listing=listing(publishedVersion=number))

    resolved, version = asyncio.run(resolve_invocation_agent(live, OWNER))

    assert resolved.instructions == DRAFT_INSTRUCTIONS
    assert version is None, "a draft turn pins no version in the cache key"
    assert resolved is live, "the draft path must not copy or rewrite the live record"


def test_bindings_and_model_come_from_the_snapshot_too(table):
    """Swapping a bound tool changes behavior as much as an instruction edit."""
    number = publish()
    live = make_agent(
        bindings=[{"kind": "tool", "ref": "browser", "config": {}}],
        modelConfig={"modelId": "some-cheap-model"},
        listing=listing(publishedVersion=number),
    )

    resolved, _ = asyncio.run(resolve_invocation_agent(live, OTHER))

    assert resolved.bindings == [AgentBinding(kind="tool", ref="wikipedia", config={})]
    assert resolved.model_settings.model_id == "claude-opus-5"


# ── the path that must not change ────────────────────────────────────────────────────
def test_an_agent_with_nothing_published_runs_the_live_record(table):
    """The overwhelmingly common case: never submitted, so it behaves as it always did."""
    live = make_agent(listing=None, instructions=DRAFT_INSTRUCTIONS)

    resolved, version = asyncio.run(resolve_invocation_agent(live, OTHER))

    assert resolved is live
    assert version is None


@pytest.mark.parametrize("state", ["private", "in_review", "changes_requested", "taken_down"])
def test_an_unpublished_listing_runs_the_live_record(table, state):
    """A listing with no published version has no snapshot to serve, whatever its state."""
    live = make_agent(listing=listing(state=state), instructions=DRAFT_INSTRUCTIONS)

    resolved, version = asyncio.run(resolve_invocation_agent(live, OTHER))

    assert resolved.instructions == DRAFT_INSTRUCTIONS
    assert version is None


def test_a_taken_down_agent_runs_the_live_record(table):
    """Takedown clears ``publishedVersion``, so a direct-link visitor is back on the draft.

    Worth stating rather than discovering: a delisting is not a revocation, the Agent stays
    reachable by link, and with nothing published there is no snapshot left to run.
    """
    publish()
    live = make_agent(
        instructions=DRAFT_INSTRUCTIONS, listing=listing(state="taken_down", publishedVersion=None)
    )

    resolved, version = asyncio.run(resolve_invocation_agent(live, OTHER))
    assert resolved.instructions == DRAFT_INSTRUCTIONS
    assert version is None


# ── it is not an access decision ─────────────────────────────────────────────────────
def test_resolution_never_touches_visibility_or_ownership(table):
    """§4: choosing a configuration must never become a way to widen or narrow reach."""
    number = publish()
    live = make_agent(
        visibility="PRIVATE", ownerId=OWNER, listing=listing(publishedVersion=number)
    )

    resolved, _ = asyncio.run(resolve_invocation_agent(live, OTHER))

    assert resolved.visibility == "PRIVATE"
    assert resolved.owner_id == OWNER


def test_an_editor_does_not_get_the_draft(table):
    """⚠️ Owner identity, not edit access.

    An editor can change the instructions but must not be able to *run* the unpublished
    result — otherwise a share grant is a way around review, which is the same shape of
    mistake as letting ``publisherId`` gate anything.
    """
    assert runs_own_draft(make_agent(), OTHER) is False
    assert runs_own_draft(make_agent(), OWNER) is True


def test_an_anonymous_caller_does_not_get_the_draft(table):
    """A missing user id must not read as "matches the owner"."""
    assert runs_own_draft(make_agent(), None) is False

    number = publish()
    live = make_agent(instructions=DRAFT_INSTRUCTIONS, listing=listing(publishedVersion=number))
    resolved, _ = asyncio.run(resolve_invocation_agent(live, None))
    assert resolved.instructions == APPROVED_INSTRUCTIONS


# ── failure is not a fallback ────────────────────────────────────────────────────────
def test_a_missing_published_snapshot_raises_rather_than_serving_the_draft(table):
    """The safe direction.

    Falling back to the live record would serve unreviewed instructions to a pinned user at
    exactly the moment something is already wrong — reopening the hole, silently. The route
    turns this into a 503.
    """
    live = make_agent(instructions=DRAFT_INSTRUCTIONS, listing=listing(publishedVersion=99))

    with pytest.raises(AgentVersionUnavailableError) as raised:
        asyncio.run(resolve_invocation_agent(live, OTHER))

    assert raised.value.number == 99
    assert AGENT_ID in str(raised.value)


def test_the_owner_is_unaffected_by_a_missing_snapshot(table):
    """The owner never reads the version, so a broken snapshot cannot lock them out."""
    live = make_agent(instructions=DRAFT_INSTRUCTIONS, listing=listing(publishedVersion=99))

    resolved, version = asyncio.run(resolve_invocation_agent(live, OWNER))
    assert resolved.instructions == DRAFT_INSTRUCTIONS
    assert version is None
