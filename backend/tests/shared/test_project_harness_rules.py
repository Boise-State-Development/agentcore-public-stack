"""A project's harness Agent against the real assistants code (shared-projects §3.2).

The harness is an ordinary Agent record marked ``kind="project"``. These tests pin
every place that marking has to change behavior:

  - access resolves through project membership, *before* the record's own owner
    field — so ownership transfer actually moves control of the harness;
  - it never appears in an agent list, a pin, or the store;
  - agent-level delete and sharing refuse it (the project owns both);
  - every member runs the live record.
"""

from __future__ import annotations

import asyncio

import boto3
import pytest
from boto3.dynamodb.conditions import Key

from apis.shared.assistants.service import (
    ProjectHarnessError,
    assert_deletable,
    create_assistant,
    delete_assistant,
    delete_project_harness,
    get_assistant_with_access_check,
    list_user_assistants,
    rename_project_harness,
    resolve_assistant_permission,
    share_assistant,
    update_assistant,
)
from apis.shared.assistants.version_repository import get_latest_version
from apis.shared.assistants.version_resolution import runs_own_draft
from apis.shared.auth.models import User
from apis.shared.projects.service import ProjectService

OWNER = User(user_id="u-owner", email="owner@example.edu", name="Olive", roles=["default"])
EDITOR = User(user_id="u-editor", email="editor@example.edu", name="Ed", roles=["default"])
VIEWER = User(user_id="u-viewer", email="viewer@example.edu", name="Vi", roles=["default"])
STRANGER = User(user_id="u-stranger", email="stranger@example.edu", name="St", roles=["default"])

PROJECTS_TABLE = "test-projects-harness"


@pytest.fixture()
def projects_table(aws, monkeypatch):
    monkeypatch.setenv("DYNAMODB_PROJECTS_TABLE_NAME", PROJECTS_TABLE)
    # Projects are opt-in (CLAUDE.md "Feature flags"); these tests exercise them on.
    monkeypatch.setenv("PROJECTS_ENABLED", "true")
    # create_assistant has no fallback for this; app-api always sets it (CDK).
    monkeypatch.setenv("S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME", "test-index")
    gsi = lambda name, h, r: {  # noqa: E731
        "IndexName": name,
        "KeySchema": [{"AttributeName": h, "KeyType": "HASH"}, {"AttributeName": r, "KeyType": "RANGE"}],
        "Projection": {"ProjectionType": "ALL"},
    }
    boto3.client("dynamodb").create_table(
        TableName=PROJECTS_TABLE,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[gsi("OwnerIndex", "GSI1PK", "GSI1SK"), gsi("MemberIndex", "GSI2PK", "GSI2SK")],
    )


@pytest.fixture()
def project(assistants_table, projects_table):
    """A project with an editor and a viewer, built through the real harness gateway."""
    service = ProjectService()
    created = asyncio.run(service.create_project(OWNER, "Enrollment Sync"))
    service.add_members(created.project_id, OWNER, [EDITOR.email], "editor")
    service.add_members(created.project_id, OWNER, [VIEWER.email], "viewer")
    return service, created


def ordinary_agent(owner: User = OWNER):
    return asyncio.run(
        create_assistant(owner_id=owner.user_id, owner_name=owner.name, name="Plain", description="", instructions="")
    )


def access(agent_id: str, user: User):
    return asyncio.run(get_assistant_with_access_check(agent_id, user.user_id, user.email))


def permission(agent_id: str, user: User):
    return asyncio.run(resolve_assistant_permission(agent_id, user.user_id, user.email))


def test_the_harness_is_marked_with_its_project(project):
    _, created = project
    harness, _ = access(created.harness_agent_id, OWNER)
    assert (harness.kind, harness.project_id, harness.visibility) == ("project", created.project_id, "PRIVATE")


@pytest.mark.parametrize(
    "user,expected", [(OWNER, "owner"), (EDITOR, "editor"), (VIEWER, "viewer"), (STRANGER, None)]
)
def test_harness_access_is_exactly_the_project_role(project, user, expected):
    _, created = project
    agent, role = access(created.harness_agent_id, user)
    assert role == expected
    assert (agent is None) == (expected is None)

    _, resolved = permission(created.harness_agent_id, user)
    assert resolved == expected


def test_transfer_moves_control_of_the_harness_too(project):
    service, created = project
    service.get_project(created.project_id, EDITOR)  # back-fill userId
    service.transfer_ownership(created.project_id, OWNER, EDITOR.email)

    # The record's ownerId still names the creator; membership decides.
    assert access(created.harness_agent_id, OWNER)[1] == "editor"
    assert access(created.harness_agent_id, EDITOR)[1] == "owner"


def test_removed_member_loses_the_harness(project):
    service, created = project
    service.remove_member(created.project_id, OWNER, VIEWER.email)
    assert access(created.harness_agent_id, VIEWER) == (None, None)


def test_harness_never_appears_in_its_creators_agent_list(project):
    _, created = project
    plain = ordinary_agent()
    listed, _ = asyncio.run(list_user_assistants(OWNER.user_id))
    assert [a.assistant_id for a in listed] == [plain.assistant_id]


def test_agent_level_delete_refuses_the_harness(project):
    _, created = project
    with pytest.raises(ProjectHarnessError, match="belongs to a project"):
        asyncio.run(delete_assistant(created.harness_agent_id, OWNER.user_id))
    assert access(created.harness_agent_id, OWNER)[0] is not None


def test_the_delete_guard_refuses_the_harness_before_the_route_cleans_anything_up(project):
    """``DELETE /assistants`` soft-deletes documents and queues the knowledge base for
    teardown after this guard and before ``delete_assistant``'s own refusal, so the
    guard is what keeps the project's files intact."""
    _, created = project
    with pytest.raises(ProjectHarnessError, match="belongs to a project"):
        asyncio.run(assert_deletable(created.harness_agent_id, OWNER.user_id))


def test_the_delete_guard_hands_back_the_callers_own_agent(assistants_table, projects_table):
    plain = ordinary_agent()
    assert asyncio.run(assert_deletable(plain.assistant_id, OWNER.user_id)).assistant_id == plain.assistant_id
    assert asyncio.run(assert_deletable(plain.assistant_id, STRANGER.user_id)) is None


def test_renaming_the_project_renames_its_harness(project):
    """The chat breadcrumb reads the harness's name, which used to keep the name the
    project was created with."""
    service, created = project
    asyncio.run(service.update_project(created.project_id, EDITOR, name="Enrollment Sync FY27",
                                       description="Canvas enrollment, next year"))

    harness, _ = access(created.harness_agent_id, VIEWER)
    assert (harness.name, harness.description) == ("Enrollment Sync FY27", "Canvas enrollment, next year")
    assert (harness.kind, harness.project_id) == ("project", created.project_id)


def test_a_rename_cuts_no_version(project):
    """Versions are the project's settings history: instructions, model, tools, skills."""
    service, created = project
    asyncio.run(service.update_project(created.project_id, OWNER, name="Renamed"))
    assert asyncio.run(get_latest_version(created.harness_agent_id)) is None


def test_a_change_that_is_not_a_rename_leaves_the_harness_alone(project):
    service, created = project
    before, _ = access(created.harness_agent_id, OWNER)
    asyncio.run(service.update_project(created.project_id, OWNER, editors_manage_members=False))
    after, _ = access(created.harness_agent_id, OWNER)
    assert after.updated_at == before.updated_at


def test_harness_rename_refuses_an_ordinary_agent(assistants_table, projects_table):
    plain = ordinary_agent()
    with pytest.raises(ValueError, match="not a project harness"):
        asyncio.run(rename_project_harness(plain.assistant_id, name="Hijacked"))
    assert asyncio.run(rename_project_harness("ast-gone", name="Nobody")) is False


def test_deleting_an_agent_removes_its_share_rows(assistants_table, projects_table):
    """Share rows carry the SharedWithIndex key and used to outlive the Agent forever."""
    import os

    plain = ordinary_agent()
    assert asyncio.run(share_assistant(plain.assistant_id, OWNER.user_id, [EDITOR.email, VIEWER.email])) is True

    assert asyncio.run(delete_assistant(plain.assistant_id, OWNER.user_id)) is True

    table = boto3.resource("dynamodb").Table(os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"])
    left = table.query(KeyConditionExpression=Key("PK").eq(f"AST#{plain.assistant_id}"))["Items"]
    assert left == []


def test_agent_level_sharing_refuses_the_harness(project):
    _, created = project
    assert asyncio.run(share_assistant(created.harness_agent_id, OWNER.user_id, [STRANGER.email])) is False
    assert access(created.harness_agent_id, STRANGER) == (None, None)


def test_project_harness_delete_refuses_an_ordinary_agent(assistants_table, projects_table):
    plain = ordinary_agent()
    with pytest.raises(ValueError, match="not a project harness"):
        asyncio.run(delete_project_harness(plain.assistant_id))


def test_project_harness_delete_removes_the_harness_and_tolerates_a_retry(project):
    _, created = project
    assert asyncio.run(delete_project_harness(created.harness_agent_id)) is True
    assert asyncio.run(delete_project_harness(created.harness_agent_id)) is False


def test_kind_and_project_survive_an_update(project):
    _, created = project
    asyncio.run(update_assistant(created.harness_agent_id, OWNER.user_id, instructions="Answer from the SIS docs."))
    harness, _ = access(created.harness_agent_id, VIEWER)
    assert (harness.kind, harness.project_id) == ("project", created.project_id)
    assert harness.instructions == "Answer from the SIS docs."


def test_every_member_runs_the_live_record(project):
    _, created = project
    harness, _ = access(created.harness_agent_id, VIEWER)
    assert runs_own_draft(harness, VIEWER.user_id) is True

    plain = ordinary_agent()
    assert runs_own_draft(plain, VIEWER.user_id) is False
    assert runs_own_draft(plain, OWNER.user_id) is True


def test_the_harness_cannot_be_pinned(project):
    from apis.app_api.agent_designer.services.pin_service import PinError, pin_agent

    _, created = project
    with pytest.raises(PinError) as refused:
        asyncio.run(pin_agent(VIEWER, created.harness_agent_id))
    assert refused.value.status_code == 400


def test_the_harness_cannot_be_submitted_to_the_store(project):
    from apis.app_api.agent_designer.services.listing_service import (
        PROJECT_HARNESS_LISTING_MESSAGE,
        ListingError,
        preflight_listing,
        submit_listing,
    )
    from apis.shared.assistants.models import SubmitListingRequest

    _, created = project
    _, block_reason, _, _ = asyncio.run(preflight_listing(created.harness_agent_id, OWNER))
    assert block_reason == PROJECT_HARNESS_LISTING_MESSAGE

    with pytest.raises(ListingError, match="belongs to a project"):
        asyncio.run(
            submit_listing(created.harness_agent_id, OWNER, SubmitListingRequest(category="productivity"))
        )


def test_an_archived_projects_harness_is_read_only_for_every_member(project):
    service, created = project
    asyncio.run(service.update_project(created.project_id, OWNER, status="archived"))
    for user in (OWNER, EDITOR, VIEWER):
        agent, role = access(created.harness_agent_id, user)
        assert (agent is not None, role) == (True, "viewer")
        assert permission(created.harness_agent_id, user)[1] == "viewer"
    assert access(created.harness_agent_id, STRANGER) == (None, None)

    asyncio.run(service.update_project(created.project_id, OWNER, status="active"))
    assert permission(created.harness_agent_id, EDITOR)[1] == "editor"


def test_the_kill_switch_shuts_the_harness_for_everyone(project, monkeypatch):
    """PROJECTS_ENABLED=false must stop the harness wherever it is reachable, not
    only the /projects routes: no member (nor its creator) gets a role, so chat
    turns and the agent document routes refuse alike. Nothing is deleted."""
    from apis.shared.assistants.service import is_disabled_project_harness

    _, created = project
    plain = ordinary_agent()
    assert not asyncio.run(is_disabled_project_harness(created.harness_agent_id))

    monkeypatch.setenv("PROJECTS_ENABLED", "false")
    for user in (OWNER, EDITOR, VIEWER):
        assert access(created.harness_agent_id, user) == (None, None)
        # What the agent document and sync-policy routes ask.
        assert permission(created.harness_agent_id, user)[1] is None
    assert asyncio.run(is_disabled_project_harness(created.harness_agent_id))
    # An ordinary agent is untouched by the switch.
    assert access(plain.assistant_id, OWNER)[1] == "owner"
    assert not asyncio.run(is_disabled_project_harness(plain.assistant_id))

    monkeypatch.setenv("PROJECTS_ENABLED", "true")
    assert access(created.harness_agent_id, EDITOR)[1] == "editor"

    # Opt-in: unset is off, like "false".
    monkeypatch.delenv("PROJECTS_ENABLED")
    assert access(created.harness_agent_id, EDITOR) == (None, None)
