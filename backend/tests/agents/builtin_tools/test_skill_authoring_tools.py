"""The seven ``my_skill_*`` tools (docs/specs/skill-creator.md, Tools).

Driven against the real ``UserSkillService`` on moto (DynamoDB + S3), so the
caps, the ownership check and the type allowlist are the service's own, not
stand-ins. What these pin:

- every tool acts as the closed-over user, and someone else's skill reads as
  not found (D4);
- every failure is a legible error result, never a raised exception;
- writes are text only and capped (D5);
- there is no whole-skill delete;
- the create and update results carry the D6 / D11 instructions.
"""

import boto3
import pytest
from moto import mock_aws

from agents.builtin_tools.skill_authoring import tools as skill_tools
from agents.builtin_tools.skill_authoring.tools import (
    CUSTOMIZE_SKILLS_PATH,
    build_skill_authoring_tools,
)
from apis.shared.auth.models import User
from apis.shared.skills.models import SKILL_DESCRIPTION_MAX_LENGTH, SkillVisibility

REGION = "us-east-1"
TABLE = "test-app-roles"
BUCKET = "test-skill-resources"

OWNER = "user-author"
OTHER = "user-other"


def _gsi(name, hash_key, range_key):
    return {
        "IndexName": name,
        "KeySchema": [
            {"AttributeName": hash_key, "KeyType": "HASH"},
            {"AttributeName": range_key, "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    }


@pytest.fixture(autouse=True)
def _reset_skill_freshness():
    from apis.shared.skills import freshness

    freshness._reset_for_tests()
    yield
    freshness._reset_for_tests()


@pytest.fixture()
def service(monkeypatch):
    """A moto-backed UserSkillService, installed where the tools look for it."""
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "DYNAMODB_APP_ROLES_TABLE_NAME": TABLE,
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        attrs = ["PK", "SK"] + [f"GSI{i}{k}" for i in range(1, 5) for k in ("PK", "SK")]
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in attrs],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                _gsi("JwtRoleMappingIndex", "GSI1PK", "GSI1SK"),
                _gsi("ToolRoleMappingIndex", "GSI2PK", "GSI2SK"),
                _gsi("ModelRoleMappingIndex", "GSI3PK", "GSI3SK"),
                _gsi("SkillOwnerIndex", "GSI4PK", "GSI4SK"),
            ],
        )
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)

        from apis.shared.skills.bundle_files import SkillBundleFiles
        from apis.shared.skills.repository import SkillCatalogRepository
        from apis.shared.skills.resource_store import SkillResourceStore
        from apis.shared.skills.user_service import UserSkillService

        repo = SkillCatalogRepository(table_name=TABLE)
        svc = UserSkillService(
            repository=repo,
            bundle_files=SkillBundleFiles(
                repository=repo,
                resource_store=SkillResourceStore(bucket_name=BUCKET, s3_client=s3),
            ),
        )
        monkeypatch.setattr(skill_tools, "get_user_skill_service", lambda: svc)
        yield svc


def _tools(user_id=OWNER):
    return {t.tool_name: t for t in build_skill_authoring_tools("sess-1", user_id, f"{user_id}@example.com")}


def _text(result):
    block = result["content"][0]
    return block.get("text") if "text" in block else block["json"]


def _create(name="Grant Review", description="Reviews grant proposals.", user_id=OWNER, **kw):
    result = _tools(user_id)["my_skill_create"](
        display_name=name, description=description, instructions="Step 1.", **kw
    )
    assert result["status"] == "success", result
    return result


def _skill_id(service, user_id=OWNER):
    import asyncio

    skills = asyncio.run(
        service.list_my_skills(User(email="", user_id=user_id, name="", roles=[]))
    )
    return skills[0].skill_id


class TestToolset:
    def test_exactly_seven_tools_in_a_fixed_order(self):
        names = [t.tool_name for t in build_skill_authoring_tools("s", "u", None)]
        assert names == [
            "my_skill_list",
            "my_skill_read",
            "my_skill_read_file",
            "my_skill_create",
            "my_skill_update",
            "my_skill_write_file",
            "my_skill_delete_file",
        ]

    def test_a_session_reuses_its_tools_and_never_another_users(self):
        """Building costs ~8 ms on the turn path, so a (session, user, email)
        builds once. The memo key is the whole closure, so it can never hand
        one user's tools to another."""
        first = build_skill_authoring_tools("s1", "u1", "u1@x.edu")
        assert [id(t) for t in build_skill_authoring_tools("s1", "u1", "u1@x.edu")] == [id(t) for t in first]
        for other in [("s1", "u2", "u2@x.edu"), ("s2", "u1", "u1@x.edu")]:
            assert not {id(t) for t in build_skill_authoring_tools(*other)} & {id(t) for t in first}

    def test_there_is_no_whole_skill_delete(self):
        """Hard delete with an S3 purge stays a UI action (spec, Tools)."""
        for t in build_skill_authoring_tools("s", "u", None):
            assert t.tool_name == "my_skill_delete_file" or "delete" not in t.tool_name
            assert "delete_my_skill" not in t.tool_name

    def test_no_tool_takes_a_confirm_flag(self):
        """D10: a model-supplied confirmation is what injected content forges."""
        for t in build_skill_authoring_tools("s", "u", None):
            props = t.tool_spec["inputSchema"]["json"].get("properties", {})
            assert "confirm" not in props, t.tool_name


class TestCreate:
    def test_creates_a_private_skill_owned_by_the_closed_over_user(self, service):
        _create()
        skill_id = _skill_id(service)
        assert skill_id == "grant_review"
        import asyncio

        row = asyncio.run(service.repository.get_skill(skill_id))
        assert row.owner_id == OWNER
        assert row.visibility == SkillVisibility.PRIVATE
        assert row.instructions == "Step 1."

    def test_result_says_how_to_turn_it_on(self, service):
        """D6: skills are opt-in per turn and a new one starts off; the slash
        command only appears once it is on, so the result names the place."""
        text = _text(_create())
        assert "grant_review" in text
        assert "Customize → Skills" in text
        assert CUSTOMIZE_SKILLS_PATH in text
        assert "new conversation" in text
        assert "slash" not in text

    @pytest.mark.parametrize(
        "name,description,needle",
        [
            ("   ", "fine", "name cannot be blank"),
            ("Fine", "", "description cannot be blank"),
            ("x" * 201, "fine", "over 200 characters"),
            ("Fine", "d" * (SKILL_DESCRIPTION_MAX_LENGTH + 1), "the limit is"),
        ],
    )
    def test_bad_fields_are_legible_errors(self, service, name, description, needle):
        result = _tools()["my_skill_create"](
            display_name=name, description=description, instructions=""
        )
        assert result["status"] == "error"
        assert needle in _text(result)

    def test_the_skill_cap_is_a_legible_error(self, service, monkeypatch):
        import apis.shared.skills.user_service as user_service

        monkeypatch.setattr(user_service, "MAX_SKILLS_PER_USER", 1)
        _create()
        result = _tools()["my_skill_create"](
            display_name="Second", description="Another.", instructions=""
        )
        assert result["status"] == "error"
        assert "maximum of 1 skills" in _text(result)
        assert "Customize → Skills" in _text(result)

    def test_an_unexpected_failure_is_a_result_not_an_exception(self, service, monkeypatch):
        async def boom(*args, **kwargs):
            raise RuntimeError("socket closed")

        monkeypatch.setattr(service, "create_my_skill", boom)
        result = _tools()["my_skill_create"](
            display_name="X skill", description="Does x.", instructions=""
        )
        assert result["status"] == "error"
        assert "RuntimeError" in _text(result)
        assert "Nothing was changed" in _text(result)


class TestReads:
    def test_list_shows_only_the_callers_skills(self, service):
        _create("Mine", "One of mine.")
        _create("Theirs", "Someone else's.", user_id=OTHER)
        listed = _text(_tools()["my_skill_list"]())["skills"]
        assert [s["skill_id"] for s in listed] == ["mine"]
        assert set(listed[0]) == {"skill_id", "display_name", "description", "status"}

    def test_read_returns_the_manifest_without_file_bodies(self, service):
        """D11: file bodies go through my_skill_read_file, one at a time."""
        _create()
        tools = _tools()
        tools["my_skill_write_file"](
            skill_id="grant_review", filename="forms.md", content="SECRET BODY"
        )
        read = _text(tools["my_skill_read"](skill_id="grant_review"))
        assert read["instructions"] == "Step 1."
        assert read["files"] == [
            {"filename": "forms.md", "kind": "reference", "size": 11, "content_type": "text/markdown"}
        ]
        assert "SECRET BODY" not in str(read)

    def test_someone_elses_skill_reads_as_not_found(self, service):
        _create("Theirs", "Someone else's.", user_id=OTHER)
        for name, kwargs in [
            ("my_skill_read", {"skill_id": "theirs"}),
            ("my_skill_read_file", {"skill_id": "theirs", "filename": "a.md"}),
            ("my_skill_update", {"skill_id": "theirs", "instructions": "pwned"}),
            ("my_skill_write_file", {"skill_id": "theirs", "filename": "a.md", "content": "x"}),
            ("my_skill_delete_file", {"skill_id": "theirs", "filename": "a.md"}),
        ]:
            result = _tools()[name](**kwargs)
            assert result["status"] == "error", name
            assert "You have no skill with id 'theirs'" in _text(result), name

    def test_read_file_returns_the_text(self, service):
        _create()
        tools = _tools()
        tools["my_skill_write_file"](skill_id="grant_review", filename="notes.txt", content="héllo")
        result = tools["my_skill_read_file"](skill_id="grant_review", filename="notes.txt")
        assert result == {"content": [{"text": "héllo"}], "status": "success"}

    def test_read_file_refuses_a_binary_file(self, service):
        _create()
        import asyncio

        asyncio.run(
            service.add_resource(
                "grant_review", "logo.png", b"\x89PNG\r\n",
                "image/png", User(email="", user_id=OWNER, name="", roles=[]), kind="asset",
            )
        )
        result = _tools()["my_skill_read_file"](skill_id="grant_review", filename="logo.png")
        assert result["status"] == "error"
        assert "cannot be read as text" in _text(result)

    def test_read_file_of_a_missing_file_is_legible(self, service):
        _create()
        result = _tools()["my_skill_read_file"](skill_id="grant_review", filename="nope.md")
        assert result["status"] == "error"
        assert "not found" in _text(result)


class TestUpdate:
    def test_updates_and_warns_that_earlier_activations_are_stale(self, service):
        """D11: the old instructions are still in history from an earlier activation."""
        _create()
        result = _tools()["my_skill_update"](skill_id="grant_review", instructions="Step 2.")
        assert result["status"] == "success"
        assert "activate the skill again" in _text(result)
        import asyncio

        assert asyncio.run(service.repository.get_skill("grant_review")).instructions == "Step 2."

    def test_status_is_validated(self, service):
        _create()
        result = _tools()["my_skill_update"](skill_id="grant_review", status="archived")
        assert result["status"] == "error"
        assert "active, draft, disabled" in _text(result)

    def test_a_blank_name_is_refused(self, service):
        _create()
        result = _tools()["my_skill_update"](skill_id="grant_review", display_name=" ")
        assert result["status"] == "error"
        assert "name cannot be blank" in _text(result)

    def test_nothing_to_update_is_refused(self, service):
        _create()
        result = _tools()["my_skill_update"](skill_id="grant_review")
        assert result["status"] == "error"
        assert "Nothing to update" in _text(result)


class TestFiles:
    def test_write_then_overwrite_then_delete(self, service):
        _create()
        tools = _tools()
        first = tools["my_skill_write_file"](skill_id="grant_review", filename="a.md", content="one")
        assert "1 of 50 files" in _text(first)
        again = tools["my_skill_write_file"](skill_id="grant_review", filename="a.md", content="two")
        assert "1 of 50 files" in _text(again)
        assert tools["my_skill_read_file"](skill_id="grant_review", filename="a.md")["content"][0]["text"] == "two"
        deleted = tools["my_skill_delete_file"](skill_id="grant_review", filename="a.md")
        assert deleted["status"] == "success"
        assert "now has 0 files" in _text(deleted)

    def test_a_binary_filename_is_refused(self, service):
        """D5: text written under a .png name is a corrupt binary."""
        _create()
        result = _tools()["my_skill_write_file"](
            skill_id="grant_review", filename="chart.png", content="not really a png"
        )
        assert result["status"] == "error"
        assert "cannot be written as text" in _text(result)

    def test_a_disallowed_type_is_refused(self, service):
        _create()
        result = _tools()["my_skill_write_file"](
            skill_id="grant_review", filename="page.html", content="<script>"
        )
        assert result["status"] == "error"
        assert result["content"][0]["text"].startswith("❌ Could not write 'page.html'")

    def test_an_oversized_file_is_refused_before_any_io(self, service, monkeypatch):
        _create()

        async def must_not_run(*a, **k):  # pragma: no cover - asserted by absence
            raise AssertionError("service reached")

        monkeypatch.setattr(service, "write_text_file", must_not_run)
        result = _tools()["my_skill_write_file"](
            skill_id="grant_review", filename="big.md", content="x" * 1_048_577
        )
        assert result["status"] == "error"
        assert "the limit is 1,048,576" in _text(result)

    def test_a_bad_kind_is_refused(self, service):
        _create()
        result = _tools()["my_skill_write_file"](
            skill_id="grant_review", filename="a.md", content="x", kind="bin"
        )
        assert result["status"] == "error"
        assert "reference, script, asset" in _text(result)

    def test_the_per_skill_file_cap_is_legible(self, service, monkeypatch):
        import apis.shared.skills.bundle_files as bundle_files

        monkeypatch.setattr(bundle_files, "MAX_RESOURCES_PER_SKILL", 1)
        _create()
        tools = _tools()
        tools["my_skill_write_file"](skill_id="grant_review", filename="a.md", content="x")
        result = tools["my_skill_write_file"](skill_id="grant_review", filename="b.md", content="y")
        assert result["status"] == "error"
        assert "maximum of 1 reference files" in _text(result)

    def test_deleting_a_missing_file_is_legible(self, service):
        _create()
        result = _tools()["my_skill_delete_file"](skill_id="grant_review", filename="nope.md")
        assert result["status"] == "error"
        assert "not found" in _text(result)


class TestRunsFromAnEventLoop:
    @pytest.mark.asyncio
    async def test_a_tool_called_on_a_running_loop_still_completes(self, service):
        """Strands calls these from a worker thread; a caller already on a loop
        must not hit a nested asyncio.run."""
        result = _tools()["my_skill_list"]()
        assert result["status"] == "success"
