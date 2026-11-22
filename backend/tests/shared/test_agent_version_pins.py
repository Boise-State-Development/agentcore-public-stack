"""Which Agent versions a saved workflow's step may pin (workflow-designer §23.2).

Two layers: the pure rule (``version_pins.pin_refusal``), and ``resolve_invocation_agent`` with
``pinned_version`` against a real (moto) table, where every row of §23.2's table is played the way
production gets there: versions cut by ``create_version``, published through ``set_version_index``
(which stamps ``publishedAt``), and the listing's pointer moved as approval, rollback and takedown
move it.
"""

import asyncio

import boto3
import pytest
from moto import mock_aws

from apis.shared.assistants.listing import gsi5_keys
from apis.shared.assistants.models import AgentVersion, Assistant
from apis.shared.assistants.version_pins import (
    PIN_REFUSAL_REASONS,
    AgentPinRefusedError,
    pin_refusal,
    pin_refusal_message,
    pin_row_needed,
)
from apis.shared.assistants.version_repository import create_version, set_version_index
from apis.shared.assistants.version_resolution import (
    AgentVersionUnavailableError,
    resolve_invocation_agent,
)
from apis.shared.assistants.versions import snapshot_of

REGION = "us-east-1"
TABLE = "test-rag-assistants"
AGENT_ID = "ast-pinned01"
OWNER = "user-author"
OTHER = "user-starter"
CREATED = "2026-07-01T00:00:00Z"
DRAFT = "The author's unreviewed draft."


def _row(number: int, published_at=None) -> AgentVersion:
    return AgentVersion(
        agent_id=AGENT_ID,
        version=number,
        name="Reviewer",
        description="d",
        instructions=f"v{number}",
        published_at=published_at,
    )


# ── the pure rule ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "number, published, row, expected",
    [
        (3, 3, _row(3), None),  # the current published version, stamped or not
        (3, 5, _row(3, "2026-11-01T00:00:00Z"), None),  # superseded, but was published
        (3, 5, _row(3), "never_published"),  # a rejected / withdrawn submission's number
        (5, 3, None, "above_published"),  # above a rollback
        (3, None, None, "unpublished"),  # takedown, withdrawal to private, never published
        (3, 3, None, "missing"),
        (2, 5, None, "missing"),
    ],
)
def test_the_rule(number, published, row, expected):
    assert pin_refusal(number, published, row) == expected


def test_the_row_is_needed_only_at_or_below_the_published_version():
    assert pin_row_needed(3, 5) and pin_row_needed(5, 5)
    assert not pin_row_needed(6, 5)
    assert not pin_row_needed(3, None)


@pytest.mark.parametrize("reason", PIN_REFUSAL_REASONS)
def test_every_reason_has_copy_naming_the_version(reason):
    message = pin_refusal_message(reason, 3, 5 if reason != "unpublished" else None)
    assert "version 3" in message.lower()
    if reason in ("above_published", "never_published"):
        assert "version 5" in message


def test_a_refusal_is_not_an_outage():
    """``AgentVersionUnavailableError`` is a 503; a refused pin is answered in the conversation."""
    assert not issubclass(AgentPinRefusedError, AgentVersionUnavailableError)
    error = AgentPinRefusedError(AGENT_ID, 4, "above_published", published=3)
    assert (error.agent_id, error.number, error.reason, error.published) == (AGENT_ID, 4, "above_published", 3)


# ── resolve_invocation_agent with a pin, against a real table ──────────────────────


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


def agent(published, *, instructions: str = DRAFT, state: str = "published", **overrides) -> Assistant:
    data = {
        "assistantId": AGENT_ID,
        "ownerId": OWNER,
        "ownerName": "An Author",
        "name": "Reviewer",
        "description": "Reviews a proposal",
        "instructions": instructions,
        "vectorIndexId": "idx",
        "visibility": "PUBLIC",
        "createdAt": CREATED,
        "updatedAt": CREATED,
        "status": "COMPLETE",
        "listing": {
            "state": state,
            "category": "Research",
            "publisherId": "pub-research",
            "publishedVersion": published,
        },
    }
    data.update(overrides)
    return Assistant(**data)


def cut(n: int) -> int:
    """Cut version ``n`` (a submission) without publishing it."""
    version = asyncio.run(create_version(AGENT_ID, snapshot_of(agent(None, instructions=f"v{n}"))))
    assert version.version == n
    return n


def publish(n: int) -> None:
    """Make ``n`` the published version the way ``_publish_version`` does (stamps ``publishedAt``)."""
    asyncio.run(set_version_index(AGENT_ID, n, gsi5_keys("published", "Research", CREATED)))


def resolve(live: Assistant, user: str, pin: int):
    return asyncio.run(resolve_invocation_agent(live, user, pinned_version=pin))


def refused(live: Assistant, user: str, pin: int) -> AgentPinRefusedError:
    with pytest.raises(AgentPinRefusedError) as caught:
        resolve(live, user, pin)
    return caught.value


def test_an_approved_update_leaves_the_pinned_step_on_its_version(table):
    """v3 → v5 approved: a step pinned to v3 still runs v3."""
    for n in (1, 2, 3):
        cut(n)
    publish(3)
    cut(4)
    cut(5)
    publish(5)

    resolved, version = resolve(agent(5), OTHER, 3)
    assert (resolved.instructions, version) == ("v3", 3)


def test_the_owner_runs_the_pinned_version_too(table):
    """§23.2 Q2.3: a step whose result depends on who starts it is not a test."""
    cut(1)
    publish(1)
    cut(2)
    publish(2)

    resolved, version = resolve(agent(2, instructions=DRAFT), OWNER, 1)
    assert (resolved.instructions, version) == ("v1", 1)
    current, _ = resolve(agent(2, instructions=DRAFT), OWNER, 2)
    assert current.instructions == "v2"


def test_a_rollback_blocks_steps_pinned_above_it_until_it_rolls_forward(table):
    for n in (1, 2, 3, 4, 5):
        cut(n)
        publish(n)

    # An admin rolls back v5 → v3: the pointer moves, v4 and v5 keep their stamps.
    for pin in (4, 5):
        error = refused(agent(3), OTHER, pin)
        assert (error.reason, error.published) == ("above_published", 3)
    assert resolve(agent(3), OTHER, 3)[0].instructions == "v3"

    # Rolled forward to v5 again: both run.
    assert resolve(agent(5), OTHER, 4)[0].instructions == "v4"
    assert resolve(agent(5), OTHER, 5)[0].instructions == "v5"


@pytest.mark.parametrize("state", ["taken_down", "private"])
def test_a_takedown_or_withdrawal_blocks_every_pinned_step(table, state):
    """The pointer is cleared; falling forward would run the author's unreviewed draft."""
    cut(1)
    publish(1)
    for pin in (1, 2):
        error = refused(agent(None, state=state), OTHER, pin)
        assert error.reason == "unpublished"
    assert refused(agent(None, state=state), OWNER, 1).reason == "unpublished"


def test_a_rejected_submission_can_never_be_pinned(table):
    cut(1)
    publish(1)
    cut(2)  # submitted, then rejected: a number, never published
    cut(3)
    publish(3)

    assert refused(agent(3), OTHER, 2).reason == "never_published"


def test_a_version_superseded_before_the_stamp_cannot_be_pinned(table):
    """Dev only: a version published before ``publishedAt`` existed carries no stamp."""
    cut(1)  # published once, before the stamp existed: no publishedAt on its row
    cut(2)
    publish(2)

    assert refused(agent(2), OTHER, 1).reason == "never_published"
    assert resolve(agent(2), OTHER, 2)[0].instructions == "v2", "the current version runs, stamp or not"


def test_a_missing_row_is_refused_not_served_as_the_draft(table):
    cut(1)
    publish(1)

    assert refused(agent(3), OTHER, 2).reason == "missing"


def test_a_project_harness_has_nothing_to_pin(table):
    harness = agent(None, kind="project")
    assert refused(harness, OTHER, 1).reason == "unpublished"


# ── no pin: exactly today ──────────────────────────────────────────────────────────


def test_without_a_pin_the_owner_still_runs_the_draft(table):
    cut(1)
    publish(1)
    live = agent(1)

    resolved, version = asyncio.run(resolve_invocation_agent(live, OWNER))
    assert resolved is live and version is None
    assert asyncio.run(resolve_invocation_agent(live, OWNER, pinned_version=None)) == (live, None)


def test_without_a_pin_others_run_the_published_version(table):
    cut(1)
    publish(1)
    cut(2)

    resolved, version = asyncio.run(resolve_invocation_agent(agent(1), OTHER))
    assert (resolved.instructions, version) == ("v1", 1)


def test_without_a_pin_a_taken_down_agent_still_runs_the_live_record(table):
    """Unchanged by D.8 (the open question in §23.2 is separate)."""
    live = agent(None, state="taken_down")
    assert asyncio.run(resolve_invocation_agent(live, OTHER)) == (live, None)


def test_get_versions_reads_several_numbers_of_one_agent_in_one_call(table):
    """The sheet's one read: keyed by ``(agent, number)``, a missing number simply absent."""
    from apis.shared.assistants.version_repository import get_versions

    for n in (1, 2, 3):
        cut(n)
    publish(2)

    rows = asyncio.run(get_versions([(AGENT_ID, 1), (AGENT_ID, 2), (AGENT_ID, 9), (AGENT_ID, 2)]))

    assert sorted(rows) == [(AGENT_ID, 1), (AGENT_ID, 2)]
    assert rows[(AGENT_ID, 2)].published_at and rows[(AGENT_ID, 1)].published_at is None
    assert asyncio.run(get_versions([])) == {}
