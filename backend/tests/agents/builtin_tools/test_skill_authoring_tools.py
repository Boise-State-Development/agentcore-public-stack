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

import hashlib
import json

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

    def test_the_specs_are_pinned(self):
        """The seven specs sit in the cached ``toolConfig`` of every turn that has
        them, so a change is one deliberate prefix rewrite per deployment. Field-set
        support rides on the results, not the specs, and left this unchanged. If a
        spec change is intended, re-measure with scripts/measure_tool_spec_tokens.py,
        state the growth in the PR, and update the digest."""
        specs = [t.tool_spec for t in build_skill_authoring_tools("s", "u", None)]
        digest = hashlib.sha256(
            json.dumps(specs, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert digest == "ac839bf403e28f653fe6dfd311fabc8077937782c0db37a2d6089cf733df3cf8"


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


FIELD_SET = """---
title: Award notice
version: 2
---

| name | title | type | find | enum | optional |
|---|---|---|---|---|---|
| sponsor | Sponsor | string | The agency making the award | | no |
| award_amount | Award amount | currency | The total amount awarded | | no |
| award_type | Award type | enum | The kind of award | grant, contract | yes |
"""


class TestFieldSets:
    """A field set is a reference file the write path already parses
    (field-set-authoring §4.2); the tools read it back into their results."""

    def test_writing_one_lists_its_fields_in_order(self, service, monkeypatch):
        monkeypatch.setenv("DOCUMENT_EXTRACT_ENABLED", "true")
        _create()
        result = _tools()["my_skill_write_file"](
            skill_id="grant_review", filename="field-set.award-notice.md", content=FIELD_SET
        )
        assert result["status"] == "success", result
        text = _text(result)
        assert 'field set "award-notice" (Award notice, version 2) with 3 fields' in text
        assert "sponsor (string), award_amount (currency), award_type (enum)" in text
        assert "document_extract" in text
        assert "turned off" not in text

    @pytest.mark.parametrize("value", [None, "", "false"])
    def test_it_says_when_extraction_is_off(self, service, monkeypatch, value):
        """In development, only "true" enables; unset arrives as ""."""
        if value is None:
            monkeypatch.delenv("DOCUMENT_EXTRACT_ENABLED", raising=False)
        else:
            monkeypatch.setenv("DOCUMENT_EXTRACT_ENABLED", value)
        _create()
        result = _tools()["my_skill_write_file"](
            skill_id="grant_review", filename="field-set.award-notice.md", content=FIELD_SET
        )
        assert result["status"] == "success"
        assert "Structured extraction is turned off in this deployment" in _text(result)

    def test_a_malformed_one_is_refused_with_the_parsers_message(self, service):
        _create()
        broken = FIELD_SET.replace("| enum | The kind of award | grant, contract |", "| enum | The kind of award | |")
        result = _tools()["my_skill_write_file"](
            skill_id="grant_review", filename="field-set.award-notice.md", content=broken
        )
        assert result["status"] == "error"
        assert "type 'enum' needs allowed values" in _text(result)

    def test_a_bad_set_name_is_refused(self, service):
        _create()
        result = _tools()["my_skill_write_file"](
            skill_id="grant_review", filename="field-set.Award_Notice.md", content=FIELD_SET
        )
        assert result["status"] == "error"
        assert "lowercase words joined by hyphens" in _text(result)

    def test_one_saved_as_another_kind_is_flagged(self, service):
        """Only references/ is read for field sets, so a script is not one."""
        _create()
        result = _tools()["my_skill_write_file"](
            skill_id="grant_review", filename="field-set.award-notice.md", content=FIELD_SET, kind="script"
        )
        assert result["status"] == "success"
        assert "not read as a field set, because it was saved as a script" in _text(result)

    def test_an_ordinary_file_gets_no_field_set_note(self, service):
        _create()
        result = _tools()["my_skill_write_file"](skill_id="grant_review", filename="a.md", content="x")
        assert "field set" not in _text(result)

    def test_read_marks_field_set_files_only(self, service):
        _create()
        tools = _tools()
        tools["my_skill_write_file"](skill_id="grant_review", filename="field-set.award-notice.md", content=FIELD_SET)
        tools["my_skill_write_file"](skill_id="grant_review", filename="notes.md", content="x")
        files = {f["filename"]: f for f in _text(tools["my_skill_read"](skill_id="grant_review"))["files"]}
        assert files["field-set.award-notice.md"]["field_set"] == "award-notice"
        assert "field_set" not in files["notes.md"]

    def test_deleting_one_says_to_update_the_instructions(self, service):
        _create()
        tools = _tools()
        tools["my_skill_write_file"](skill_id="grant_review", filename="field-set.award-notice.md", content=FIELD_SET)
        result = tools["my_skill_delete_file"](skill_id="grant_review", filename="field-set.award-notice.md")
        assert result["status"] == "success"
        assert 'name the field set "award-notice", update them too' in _text(result)


class TestRunsFromAnEventLoop:
    @pytest.mark.asyncio
    async def test_a_tool_called_on_a_running_loop_still_completes(self, service):
        """Strands calls these from a worker thread; a caller already on a loop
        must not hit a nested asyncio.run."""
        result = _tools()["my_skill_list"]()
        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# my_skill_write_file(from_attachment=…) (spec, "Writing a file from an attachment")
# ---------------------------------------------------------------------------

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class _FakeFiles:
    """The conversation file index, keyed by conversation like the real GSI."""

    def __init__(self, rows, bodies):
        self.rows = rows
        self.bodies = bodies
        self.listed = []

    def list_session_files_sync(self, session_id, status=None):
        self.listed.append(session_id)
        return [r for r in self.rows if r.session_id == session_id]

    async def get_file(self, user_id, upload_id):
        return next((r for r in self.rows if r.user_id == user_id and r.upload_id == upload_id), None)


def _row(upload_id, filename, mime, body, *, session_id="sess-1", user_id=OWNER, **kw):
    from apis.shared.files.models import FileMetadata, FileStatus

    fields = {
        "upload_id": upload_id,
        "user_id": user_id,
        "session_id": session_id,
        "filename": filename,
        "mime_type": mime,
        "size_bytes": len(body),
        "s3_key": f"k/{upload_id}",
        "s3_bucket": "files",
        "status": FileStatus.READY,
        **kw,
    }
    return FileMetadata(**fields)


@pytest.fixture()
def attachments(monkeypatch):
    """Install a fake file index and S3 read; returns ``add(upload_id, name, mime, body)``."""
    from agents.builtin_tools.skill_authoring import attachment_source
    from apis.shared.files import repository

    files = _FakeFiles([], {})
    monkeypatch.setattr(repository, "get_file_upload_repository", lambda: files)
    monkeypatch.setattr(attachment_source, "_fetch", lambda meta: files.bodies[meta.s3_key])

    def add(upload_id, filename, mime, body, **kw):
        row = _row(upload_id, filename, mime, body, **kw)
        files.rows.append(row)
        files.bodies.setdefault(row.s3_key, body)
        return row

    add.files = files
    return add


def _playbook_docx():
    from tests.shared.docx_fixtures import build_docx, deleted, inserted, paragraph, run

    return build_docx(
        paragraph(run("Indemnification", bold=True)),
        paragraph(run("The Sponsor shall indemnify the University "), deleted("fully"), inserted("to the extent permitted by law"), run(".")),
        paragraph(run("Governing law: Idaho.")),
    )


def _write(**kw):
    return _tools()["my_skill_write_file"](skill_id="grant_review", **kw)


def _stored(service, filename):
    import asyncio

    _, raw = asyncio.run(
        service.read_resource("grant_review", filename, User(email="", user_id=OWNER, name="", roles=[]))
    )
    return raw


class TestFromAttachment:
    def test_a_markdown_attachment_is_copied_byte_for_byte(self, service, attachments):
        _create()
        body = "# Playbook\n\nCláusula 4 — keep “exactly” this.\r\n\n- one\n- two\n".encode()
        attachments("up-1", "playbook.md", "text/markdown", body)
        result = _write(filename="playbook.md", from_attachment="playbook.md")
        assert result["status"] == "success", result
        assert _stored(service, "playbook.md") == body
        text = _text(result)
        assert "from the attachment 'playbook.md' (copied unchanged" in text
        assert hashlib.sha256(body).hexdigest() in text

    def test_a_docx_is_stored_as_docx_markdowns_output(self, service, attachments):
        from apis.shared.files.docx_markdown import docx_to_markdown

        _create()
        raw = _playbook_docx()
        attachments("up-2", "Playbook.docx", _DOCX_MIME, raw)
        result = _write(filename="playbook.md", from_attachment="up-2")
        assert result["status"] == "success", result
        expected = docx_to_markdown(raw)
        assert _stored(service, "playbook.md").decode() == expected
        assert "[deleted: fully]" in expected and "[inserted: to the extent permitted by law]" in expected
        assert "converted from Word to Markdown" in _text(result)

    def test_an_upload_id_wins_over_a_filename_and_a_name_matches_case_insensitively(self, service, attachments):
        _create()
        attachments("up-1", "Notes.TXT", "text/plain", b"by name")
        assert _write(filename="a.md", from_attachment="notes.txt")["status"] == "success"
        assert _stored(service, "a.md") == b"by name"

    def test_a_name_shared_by_two_uploads_is_refused_with_both_ids(self, service, attachments):
        _create()
        attachments("up-old", "playbook.md", "text/markdown", b"v1")
        attachments("up-new", "playbook.md", "text/markdown", b"v2")
        result = _write(filename="playbook.md", from_attachment="playbook.md")
        assert result["status"] == "error"
        assert "up-old" in _text(result) and "up-new" in _text(result)
        assert _write(filename="playbook.md", from_attachment="up-new")["status"] == "success"
        assert _stored(service, "playbook.md") == b"v2"

    def test_only_the_callers_files_in_this_conversation_resolve(self, service, attachments):
        """(session, user), never the session alone."""
        _create()
        attachments("up-theirs", "theirs.md", "text/markdown", b"x", user_id=OTHER)
        attachments("up-elsewhere", "elsewhere.md", "text/markdown", b"x", session_id="sess-2")
        for ref in ("up-theirs", "theirs.md", "up-elsewhere", "elsewhere.md", "missing.md"):
            result = _write(filename="a.md", from_attachment=ref)
            assert result["status"] == "error", ref
            assert "is attached to this conversation" in _text(result)
        assert set(attachments.files.listed) == {"sess-1"}

    def test_a_shared_file_reads_through_to_its_original_and_says_when_it_is_gone(self, service, attachments):
        from apis.shared.files.models import LinkedFrom

        _create()
        attachments("orig", "playbook.md", "text/markdown", b"original text", session_id="source-sess")
        link = LinkedFrom(upload_id="orig", session_id="source-sess", from_title="Source chat")
        # A reference row is the original's s3Key under its own id (handoffs §4.9).
        attachments("ref-1", "playbook.md", "text/markdown", b"", linked_from=link, s3_key="k/orig")
        assert _write(filename="a.md", from_attachment="ref-1")["status"] == "success"
        assert _stored(service, "a.md") == b"original text"
        attachments.files.rows.remove(next(r for r in attachments.files.rows if r.upload_id == "orig"))
        result = _write(filename="b.md", from_attachment="ref-1")
        assert result["status"] == "error"
        assert "No longer available (deleted from Source chat)" in _text(result)

    def test_a_pdf_is_refused_with_the_formats_that_work(self, service, attachments):
        _create()
        attachments("up-1", "playbook.pdf", "application/pdf", b"%PDF-1.7")
        result = _write(filename="a.md", from_attachment="up-1")
        assert result["status"] == "error"
        assert ".docx" in _text(result) and ".md" in _text(result)

    def test_text_that_is_not_utf8_is_refused(self, service, attachments):
        _create()
        attachments("up-1", "a.txt", "text/plain", b"\xff\xfe\x00bad")
        result = _write(filename="a.md", from_attachment="up-1")
        assert result["status"] == "error"
        assert "not valid UTF-8" in _text(result)

    def test_the_file_cap_applies_to_the_copied_text(self, service, attachments):
        _create()
        attachments("up-1", "big.md", "text/markdown", b"x" * 1_048_577)
        result = _write(filename="big.md", from_attachment="up-1")
        assert result["status"] == "error"
        assert "1,048,576" in _text(result)

    def test_exactly_one_of_content_and_from_attachment(self, service, attachments):
        _create()
        attachments("up-1", "a.md", "text/markdown", b"x")
        both = _write(filename="a.md", content="typed", from_attachment="up-1")
        assert both["status"] == "error" and "not both" in _text(both)
        neither = _write(filename="a.md")
        assert neither["status"] == "error" and "from_attachment" in _text(neither)
        # A model that sends an empty string for the unused argument is not refused.
        assert _write(filename="a.md", content="", from_attachment="up-1")["status"] == "success"
        assert _write(filename="b.md", content="typed", from_attachment="")["status"] == "success"

    def test_the_target_type_rule_still_applies(self, service, attachments):
        _create()
        attachments("up-1", "a.md", "text/markdown", b"x")
        result = _write(filename="chart.png", from_attachment="up-1")
        assert result["status"] == "error"
        assert "cannot be written as text" in _text(result)


class TestFromAttachmentCard:
    @pytest.fixture(autouse=True)
    def _no_reach(self, monkeypatch):
        from agents.builtin_tools.skill_authoring import approval
        from apis.shared.skills.reach import SkillReach

        monkeypatch.setattr(approval, "lookup_skill_reach", lambda s, o: SkillReach(s, []))

    async def _card(self, tool_input):
        from agents.builtin_tools.skill_authoring.approval import describe_for_approval

        details = await describe_for_approval(OWNER, "my_skill_write_file", tool_input, "sess-1")
        return details.tool_input

    @pytest.mark.asyncio
    async def test_a_new_file_shows_source_size_and_hash_of_what_is_written(self, service, attachments):
        from apis.shared.files.docx_markdown import docx_to_markdown

        _create()
        raw = _playbook_docx()
        attachments("up-2", "Playbook.docx", _DOCX_MIME, raw)
        card = await self._card({"skill_id": "grant_review", "filename": "playbook.md", "from_attachment": "Playbook.docx"})
        expected = docx_to_markdown(raw).encode()
        assert list(card)[0] == "reach"
        assert "from_attachment" not in card and "content" not in card
        assert card["source"] == {
            "attachment": "Playbook.docx",
            "upload_id": "up-2",
            "size_bytes": len(raw),
            "converted": "from Word to Markdown",
        }
        assert card["written"]["bytes"] == len(expected)
        assert card["written"]["sha256"] == hashlib.sha256(expected).hexdigest()
        assert card["change"] == "new file"
        # The card's hash is the hash the write reports.
        result = _write(filename="playbook.md", from_attachment="Playbook.docx")
        assert card["written"]["sha256"] in _text(result)

    @pytest.mark.asyncio
    async def test_an_edit_shows_lines_added_and_removed_and_the_whole_small_diff(self, service, attachments):
        _create()
        old = "".join(f"Clause {i}: keep this wording.\n" for i in range(400))
        _write(filename="playbook.md", content=old)
        new = old.replace("Clause 200: keep this wording.", "Clause 200: keep this exact wording.")
        attachments("up-3", "playbook.md", "text/markdown", new.encode())
        card = await self._card({"skill_id": "grant_review", "filename": "playbook.md", "from_attachment": "up-3"})
        change = card["change"]
        assert (change["lines_added"], change["lines_removed"]) == (1, 1)
        assert "-Clause 200: keep this wording." in change["diff"]
        assert "+Clause 200: keep this exact wording." in change["diff"]
        assert "more characters not shown" not in change["diff"]

    @pytest.mark.asyncio
    async def test_an_identical_file_says_so(self, service, attachments):
        _create()
        _write(filename="a.md", content="same\n")
        attachments("up-1", "a.md", "text/markdown", b"same\n")
        card = await self._card({"skill_id": "grant_review", "filename": "a.md", "from_attachment": "up-1"})
        assert card["change"].startswith("no change")

    @pytest.mark.asyncio
    async def test_an_unresolvable_attachment_still_renders_and_says_why(self, service, attachments):
        _create()
        card = await self._card({"skill_id": "grant_review", "filename": "a.md", "from_attachment": "nope.md"})
        assert card["source"] == "nope.md"
        assert "The write will fail if approved" in card["problem"]

    @pytest.mark.asyncio
    async def test_an_inline_content_write_is_unchanged(self, service, attachments):
        card = await self._card({"skill_id": "grant_review", "filename": "a.md", "content": "hello"})
        assert card["content"] == "hello"
        assert "source" not in card and "change" not in card
        assert attachments.files.listed == []


class TestHardening:
    """Unsafe characters in names and descriptions, and the instructions cap."""

    def test_create_refuses_a_bidi_override_in_the_description(self, service):
        result = _tools()["my_skill_create"](
            display_name="Fine", description="Reviews ‮grants.", instructions=""
        )
        assert result["status"] == "error"
        assert "U+202E" in _text(result)

    def test_create_refuses_a_zero_width_character_in_the_name(self, service):
        result = _tools()["my_skill_create"](
            display_name="Gr​ant", description="Reviews grants.", instructions=""
        )
        assert result["status"] == "error"
        assert "U+200B" in _text(result)

    def test_update_refuses_a_bidi_override_in_the_description(self, service):
        _create()
        result = _tools()["my_skill_update"](
            skill_id="grant_review", description="Reviews ‮grants."
        )
        assert result["status"] == "error"
        assert "U+202E" in _text(result)

    def test_create_and_update_cap_the_instructions(self, service):
        from apis.shared.skills.models import SKILL_INSTRUCTIONS_MAX_BYTES

        over = "x" * (SKILL_INSTRUCTIONS_MAX_BYTES + 1)
        result = _tools()["my_skill_create"](
            display_name="Big", description="Reviews grants.", instructions=over
        )
        assert result["status"] == "error"
        assert "200 KiB" in _text(result)
        _create()
        result = _tools()["my_skill_update"](skill_id="grant_review", instructions=over)
        assert result["status"] == "error"
        assert "200 KiB" in _text(result)
        at_cap = "x" * SKILL_INSTRUCTIONS_MAX_BYTES
        result = _tools()["my_skill_update"](skill_id="grant_review", instructions=at_cap)
        assert result["status"] == "success", result


class TestSkillMetadataValidation:
    """skill_metadata goes through the lenient agentskills.io check: errors refuse, warnings pass."""

    @pytest.mark.parametrize(
        "metadata,needle",
        [
            ({"compatibility": "c" * 501}, "compatibility"),
            ({"compatibility": 3}, "compatibility"),
            ({"metadata": "flat"}, "metadata"),
        ],
    )
    def test_create_refuses_an_error(self, service, metadata, needle):
        result = _tools()["my_skill_create"](
            display_name="Meta", description="d", instructions="", skill_metadata=metadata
        )
        assert result["status"] == "error"
        assert "skill_metadata" in _text(result) and needle in _text(result)

    def test_a_non_string_metadata_value_is_only_a_warning(self, service):
        _create(skill_metadata={"metadata": {"version": 2}, "license": "MIT"})
        result = _tools()["my_skill_update"](
            skill_id="grant_review",
            description="New.",
            skill_metadata={"metadata": {"version": 2}, "license": "MIT"},
        )
        assert result["status"] == "success", result

    def test_create_passes_a_warning(self, service):
        _create(skill_metadata={"version": "1.0", "license": "MIT"})
        import asyncio

        row = asyncio.run(service.repository.get_skill("grant_review"))
        assert row.skill_metadata == {"version": "1.0", "license": "MIT"}

    def test_update_refuses_an_error_and_leaves_the_row(self, service):
        _create(skill_metadata={"license": "MIT"})
        result = _tools()["my_skill_update"](
            skill_id="grant_review", skill_metadata={"compatibility": ""}
        )
        assert result["status"] == "error"
        assert "compatibility" in _text(result)
        import asyncio

        row = asyncio.run(service.repository.get_skill("grant_review"))
        assert row.skill_metadata == {"license": "MIT"}


# =============================================================================
# Audit (agentskills-conformance D-6): the four writes are recorded once each
# =============================================================================


class TestAuditRecords:
    @pytest.fixture()
    def audit_items(self, monkeypatch):
        from apis.shared.audit.service import AuditService
        from apis.shared.skills import audit as skill_audit

        items = []

        class _Repo:
            def put(self, record):
                items.append(record.to_item())

        service = AuditService(repository=_Repo())
        monkeypatch.setattr(skill_audit, "get_audit_service", lambda: service)
        return items

    def test_each_write_tool_records_once_after_it_succeeds(self, service, audit_items):
        tools = _tools()
        _create()
        sid = _skill_id(service)
        assert tools["my_skill_update"](skill_id=sid, description="Reviews grants.")["status"] == "success"
        assert tools["my_skill_write_file"](skill_id=sid, filename="notes.md", content="hi")["status"] == "success"
        assert tools["my_skill_delete_file"](skill_id=sid, filename="notes.md")["status"] == "success"

        assert [i["action"] for i in audit_items] == [
            "skill.created",
            "skill.updated",
            "skill.updated",
            "skill.updated",
        ]
        assert {i["actorUserId"] for i in audit_items} == {OWNER}
        assert "Step 1." not in json.dumps(audit_items, default=str)

    def test_a_refused_write_records_nothing(self, service, audit_items):
        _create()
        result = _tools(OTHER)["my_skill_update"](skill_id=_skill_id(service), description="x")
        assert result["status"] == "error"
        assert [i["action"] for i in audit_items] == ["skill.created"]

    def test_a_failing_audit_write_does_not_fail_the_tool(self, service, monkeypatch):
        from apis.shared.audit.service import AuditService
        from apis.shared.skills import audit as skill_audit

        class _Down:
            def put(self, record):
                raise RuntimeError("DynamoDB is down")

        monkeypatch.setattr(skill_audit, "get_audit_service", lambda: AuditService(repository=_Down()))
        _create()
        sid = _skill_id(service)
        assert _tools()["my_skill_update"](skill_id=sid, description="Reviews grants.")["status"] == "success"
