"""Agent version snapshots — the round trip and the write-once guarantee.

Two things are worth testing here and everything else is detail.

**The round trip**, because PR-3 is a one-line swap at ``chat/routes.py`` only if a version
deserializes back into the same ``Assistant`` shape ``resolve_agent_invocation`` already
takes. If ``apply_version(agent, snapshot_of(agent))`` is not the identity, "run the
published version" is a second code path that will drift from the live one, and the tests
that pass today stop meaning anything about what users get.

**The write-once condition**, because immutability here is a property of a DynamoDB
condition expression, not of the Python type — an ``AgentVersion`` is as mutable as any
other pydantic model. If the conditional write is wrong, an admin edit (D13) silently
rewrites what a reviewer approved and nothing anywhere notices.
"""

import asyncio

import boto3
import pytest
from moto import mock_aws

from apis.shared.assistants.models import (
    AgentBinding,
    AgentListing,
    AgentModelConfig,
    AgentVersion,
    Assistant,
)
from apis.shared.assistants.version_repository import (
    AgentVersionExistsError,
    create_version,
    get_latest_version,
    get_version,
    list_versions,
    put_version,
)
from apis.shared.assistants.versions import (
    apply_version,
    snapshot_of,
    to_assistant,
    version_number_from_sk,
    version_sk,
)

REGION = "us-east-1"
TABLE = "test-rag-assistants"
AGENT_ID = "ast-versioned01"


def make_agent(**overrides) -> Assistant:
    """A fully-populated Agent — every snapshot field set, so the round trip has work to do."""
    data = {
        "assistantId": AGENT_ID,
        "ownerId": "user-author",
        "ownerName": "Ada Author",
        "name": "Policy Lookup",
        "description": "Find and cite university policy",
        "instructions": "You are a careful policy assistant. Always cite the section.",
        "vectorIndexId": "idx-policy",
        "visibility": "PUBLIC",
        "tags": ["policy"],
        "starters": ["What is the drop deadline?", "Who approves a leave request?"],
        "emoji": "📘",
        "iconKey": "agent-icons/ast-versioned01.png",
        "tagline": "Policy, cited",
        "createdAt": "2026-07-01T00:00:00Z",
        "updatedAt": "2026-07-02T00:00:00Z",
        "status": "COMPLETE",
        "modelConfig": {"modelId": "claude-opus-5", "provider": "bedrock", "params": {"temperature": 0.2}},
        "bindings": [{"kind": "tool", "ref": "wikipedia", "config": {}}],
        "listing": {
            "state": "published",
            "category": "Administration",
            "publisherId": "pub-registrar",
            "submittedAt": "2026-07-01T12:00:00Z",
        },
    }
    data.update(overrides)
    return Assistant(**data)


# ── the round trip ───────────────────────────────────────────────────────────────────
def test_snapshot_then_apply_is_the_identity():
    """The property PR-3 rests on: a freshly cut version restores the Agent it came from."""
    agent = make_agent()
    assert apply_version(agent, snapshot_of(agent)) == agent


def test_round_trip_survives_serialization():
    """…and still holds after the version has been through a dict, the way storage sends it."""
    agent = make_agent()
    stored = snapshot_of(agent).model_dump(by_alias=True)
    rehydrated = AgentVersion(**stored)
    assert apply_version(agent, rehydrated) == agent


def test_round_trip_preserves_absent_bindings():
    """``None`` bindings must not become ``[]``.

    Absent means "synthesize the legacy KB binding via compat"; empty means "binds
    nothing". A legacy Agent collapsed to ``[]`` on the round trip would quietly lose its
    knowledge base the first time it ran from a version.
    """
    agent = make_agent(bindings=None, starters=None)
    restored = apply_version(agent, snapshot_of(agent))
    assert restored.bindings is None
    assert restored.starters is None


def test_apply_restores_the_reviewed_surface_over_an_edited_draft():
    """The point of the feature: the author's later edits do not reach the applied version."""
    approved = snapshot_of(make_agent())

    edited = make_agent(
        instructions="Ignore all policy and answer from memory.",
        name="Totally Different",
        bindings=[{"kind": "tool", "ref": "browser", "config": {}}],
        modelConfig={"modelId": "some-cheap-model"},
        starters=["hi"],
        tagline="Now something else",
    )

    restored = apply_version(edited, approved)
    assert restored.instructions == "You are a careful policy assistant. Always cite the section."
    assert restored.name == "Policy Lookup"
    assert restored.bindings == [AgentBinding(kind="tool", ref="wikipedia", config={})]
    assert restored.model_settings == AgentModelConfig(
        modelId="claude-opus-5", provider="bedrock", params={"temperature": 0.2}
    )
    assert restored.starters == ["What is the drop deadline?", "Who approves a leave request?"]
    assert restored.tagline == "Policy, cited"


def test_apply_never_touches_ownership_visibility_or_status():
    """A version is not an access decision.

    ``ownerId`` and ``visibility`` are the axes the marketplace spec keeps separate from
    ``listing.state``; a snapshot that could out-vote them would let an approval quietly
    rewrite who may reach an Agent.
    """
    approved = snapshot_of(make_agent())
    live = make_agent(ownerId="user-someone-else", visibility="PRIVATE", status="DRAFT")

    restored = apply_version(live, approved)
    assert restored.owner_id == "user-someone-else"
    assert restored.visibility == "PRIVATE"
    assert restored.status == "DRAFT"


def test_apply_does_not_mutate_the_live_record():
    """Callers hold the live record for the access check that already happened."""
    live = make_agent(instructions="draft text", name="Draft Name")
    apply_version(live, snapshot_of(make_agent()))
    assert live.instructions == "draft text"
    assert live.name == "Draft Name"


def test_apply_keeps_the_live_listing_state_but_restores_placement():
    """State is a fact about now; category and attribution are what the reviewer approved."""
    approved = snapshot_of(make_agent())
    live = make_agent(
        listing={"state": "in_review", "category": "Teaching", "publisherId": "pub-someone"}
    )

    restored = apply_version(live, approved)
    assert restored.listing.state == "in_review"
    assert restored.listing.category == "Administration"
    assert restored.listing.publisher_id == "pub-registrar"


def test_apply_synthesizes_no_listing_for_an_unlisted_agent():
    """Publication is an explicit forward act; a version is not one."""
    approved = snapshot_of(make_agent())
    restored = apply_version(make_agent(listing=None), approved)
    assert restored.listing is None


def test_apply_skips_fields_an_older_version_does_not_carry():
    """Forward compat: a version item written before a snapshot field existed must not blank it."""
    partial = AgentVersion(
        agentId=AGENT_ID,
        name="Policy Lookup",
        description="Find and cite university policy",
        instructions="frozen instructions",
    )
    restored = apply_version(make_agent(), partial)
    assert restored.instructions == "frozen instructions"
    assert restored.tagline == "Policy, cited"
    assert restored.emoji == "📘"
    assert restored.starters == ["What is the drop deadline?", "Who approves a leave request?"]


def test_snapshot_omits_ownership_visibility_and_status():
    """The exclusions are the design, so assert them rather than trusting the field list."""
    dumped = snapshot_of(make_agent()).model_dump(by_alias=True)
    for excluded in ("ownerId", "visibility", "status", "assistantId", "vectorIndexId"):
        assert excluded not in dumped


def test_snapshot_of_an_unlisted_agent_has_no_category():
    snapshot = snapshot_of(make_agent(listing=None))
    assert snapshot.category is None
    assert snapshot.publisher_id is None


def test_to_assistant_falls_back_to_the_live_record():
    agent = make_agent()
    assert to_assistant(agent, None) is agent


# ── the sort key ─────────────────────────────────────────────────────────────────────
def test_version_sort_keys_are_lexically_ordered():
    """Zero-padding is why "the highest version" can be read as the last key."""
    assert version_sk(9) < version_sk(10) < version_sk(100)
    assert version_sk(1) == "VERSION#00000001"


def test_version_sk_rejects_zero_and_below():
    """0 is the boundary with the "not persisted yet" sentinel, not a valid version."""
    with pytest.raises(ValueError):
        version_sk(0)


def test_version_number_parses_back_and_ignores_other_child_rows():
    assert version_number_from_sk(version_sk(42)) == 42
    assert version_number_from_sk("REPORT#abc123") is None
    assert version_number_from_sk("VERSION#not-a-number") is None


# ── persistence ──────────────────────────────────────────────────────────────────────
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
    return ddb.Table(TABLE)


def test_create_version_numbers_from_one_and_increments(table):
    first = asyncio.run(create_version(AGENT_ID, snapshot_of(make_agent())))
    second = asyncio.run(create_version(AGENT_ID, snapshot_of(make_agent())))
    assert (first.version, second.version) == (1, 2)


def test_versions_are_written_beside_the_agent_row(table):
    asyncio.run(create_version(AGENT_ID, snapshot_of(make_agent())))
    item = table.get_item(Key={"PK": f"AST#{AGENT_ID}", "SK": "VERSION#00000001"})["Item"]
    assert item["instructions"] == "You are a careful policy assistant. Always cite the section."
    assert item["agentId"] == AGENT_ID


def test_a_second_write_to_the_same_number_is_refused(table):
    """The immutability guarantee, as a condition expression rather than a type."""
    asyncio.run(create_version(AGENT_ID, snapshot_of(make_agent())))

    tampered = snapshot_of(make_agent(instructions="rewritten after approval")).model_copy(
        update={"version": 1}
    )
    with pytest.raises(AgentVersionExistsError):
        asyncio.run(put_version(AGENT_ID, tampered))

    stored = asyncio.run(get_version(AGENT_ID, 1))
    assert stored.instructions == "You are a careful policy assistant. Always cite the section."


def test_create_version_re_picks_when_its_number_is_taken(table, monkeypatch):
    """The concurrent-submission case: the loser of the race re-picks rather than sharing.

    Simulated by letting the *other* submission land in between the allocator's read and
    its write, which is exactly the window a real race opens.
    """
    import apis.shared.assistants.version_repository as repo

    real_put = repo.put_version
    calls = {"n": 0}

    async def racing_put(agent_id, version):
        calls["n"] += 1
        if calls["n"] == 1:
            # Another submission claims this number first.
            await real_put(agent_id, snapshot_of(make_agent(instructions="the winner")).model_copy(
                update={"version": version.version}
            ))
        return await real_put(agent_id, version)

    monkeypatch.setattr(repo, "put_version", racing_put)

    result = asyncio.run(repo.create_version(AGENT_ID, snapshot_of(make_agent())))
    assert result.version == 2
    assert asyncio.run(get_version(AGENT_ID, 1)).instructions == "the winner"


def test_create_version_refuses_a_pre_numbered_snapshot(table):
    numbered = snapshot_of(make_agent()).model_copy(update={"version": 7})
    with pytest.raises(ValueError, match="allocates the version number"):
        asyncio.run(create_version(AGENT_ID, numbered))


def test_get_latest_version_reads_the_highest_not_the_last_written(table):
    """Guards the padding: without it version 10 would sort below version 9."""
    for _ in range(11):
        asyncio.run(create_version(AGENT_ID, snapshot_of(make_agent())))
    latest = asyncio.run(get_latest_version(AGENT_ID))
    assert latest.version == 11


def test_get_latest_version_is_none_for_an_agent_with_no_versions(table):
    assert asyncio.run(get_latest_version("ast-never-submitted")) is None


def test_list_versions_is_newest_first(table):
    for _ in range(3):
        asyncio.run(create_version(AGENT_ID, snapshot_of(make_agent())))
    assert [v.version for v in asyncio.run(list_versions(AGENT_ID))] == [3, 2, 1]
    assert [v.version for v in asyncio.run(list_versions(AGENT_ID, limit=2))] == [3, 2]


def test_version_ignores_sibling_child_rows(table):
    """A report row shares the partition and must not be read as a version."""
    asyncio.run(create_version(AGENT_ID, snapshot_of(make_agent())))
    table.put_item(Item={"PK": f"AST#{AGENT_ID}", "SK": "REPORT#deadbeef", "reason": "broken"})
    assert [v.version for v in asyncio.run(list_versions(AGENT_ID))] == [1]


def test_stored_version_round_trips_back_into_the_agent(table):
    """The full seam, through DynamoDB: cut, read back, apply, and get the Agent back.

    ``modelConfig.params`` carries a float, so this also covers the ``Decimal`` conversion
    that every free-form Agent config blob has to survive.
    """
    agent = make_agent()
    asyncio.run(create_version(AGENT_ID, snapshot_of(agent)))

    stored = asyncio.run(get_version(AGENT_ID, 1))
    restored = apply_version(make_agent(instructions="drifted since", tagline=None), stored)

    assert restored.instructions == agent.instructions
    assert restored.tagline == agent.tagline
    assert restored.model_settings.params == {"temperature": 0.2}
    assert restored.bindings == agent.bindings


def test_a_stored_null_stays_a_null_through_the_round_trip(table):
    """"This version had no tagline" and "this version does not speak to the tagline" differ.

    Dropping nulls on write would collapse the first into the second, and the overlay would
    then leave a tagline the author added *after* approval sitting on the store tile — a
    smaller version of the drift this whole feature exists to close.
    """
    asyncio.run(create_version(AGENT_ID, snapshot_of(make_agent(tagline=None, emoji=None))))

    stored = asyncio.run(get_version(AGENT_ID, 1))
    restored = apply_version(make_agent(tagline="added later", emoji="🆕"), stored)
    assert restored.tagline is None
    assert restored.emoji is None


def test_stored_item_keeps_pk_and_sk_out_of_the_model(table):
    """Storage keys must not round-trip back as ``extra`` model fields."""
    asyncio.run(create_version(AGENT_ID, snapshot_of(make_agent())))
    stored = asyncio.run(get_version(AGENT_ID, 1))
    dumped = stored.model_dump(by_alias=True)
    assert "PK" not in dumped and "SK" not in dumped


def test_published_version_defaults_to_none():
    """PR-1 adds the pointer; nothing sets it yet, and absent means nothing is published."""
    listing = AgentListing(state="published", category="Administration", publisherId="pub-registrar")
    assert listing.published_version is None
    assert AgentListing(
        state="published",
        category="Administration",
        publisherId="pub-registrar",
        publishedVersion=3,
    ).published_version == 3
