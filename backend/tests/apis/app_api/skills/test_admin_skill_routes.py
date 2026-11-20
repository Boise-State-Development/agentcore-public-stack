"""Admin /skills route tests (TestClient + moto-backed service).

Verifies endpoint wiring, status codes, response shape (camelCase aliases),
bound-tool validation surfacing as 400, and role-grant round-trips.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth import require_admin
from tests.conftest import override_admin_auth
from apis.shared.rbac.models import AppRoleCreate
from apis.app_api.admin.skills import routes as skill_routes


@pytest.fixture()
def client(skill_service, admin_user, monkeypatch):
    monkeypatch.setattr(skill_routes, "get_skill_catalog_service", lambda: skill_service)
    app = FastAPI()
    app.include_router(skill_routes.router)
    override_admin_auth(app, lambda: admin_user)
    return TestClient(app)


def _create_body(skill_id="pdf_workflows", **kw):
    body = {
        "skillId": skill_id,
        "displayName": "PDF Workflows",
        "description": "Fill, merge and split PDFs.",
        "instructions": "# PDF Workflows",
    }
    body.update(kw)
    return body


def test_create_and_get(client):
    resp = client.post("/skills/", json=_create_body())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skillId"] == "pdf_workflows"
    assert body["displayName"] == "PDF Workflows"
    assert body["status"] == "active"

    got = client.get("/skills/pdf_workflows")
    assert got.status_code == 200
    assert got.json()["skillId"] == "pdf_workflows"


def test_get_missing_404(client):
    assert client.get("/skills/nope").status_code == 404


def test_list(client):
    client.post("/skills/", json=_create_body("skill_one"))
    client.post("/skills/", json=_create_body("skill_two"))
    resp = client.get("/skills/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert {s["skillId"] for s in body["skills"]} == {"skill_one", "skill_two"}


def test_update(client):
    client.post("/skills/", json=_create_body())
    resp = client.put("/skills/pdf_workflows", json={"displayName": "PDF Tools"})
    assert resp.status_code == 200
    assert resp.json()["displayName"] == "PDF Tools"


def test_update_missing_404(client):
    assert client.put("/skills/nope", json={"displayName": "x"}).status_code == 404


def test_soft_then_hard_delete(client):
    client.post("/skills/", json=_create_body())

    soft = client.delete("/skills/pdf_workflows")
    assert soft.status_code == 200
    assert "disabled" in soft.json()["message"]
    # Soft delete keeps the row (status disabled).
    assert client.get("/skills/pdf_workflows").json()["status"] == "disabled"

    hard = client.delete("/skills/pdf_workflows?hard=true")
    assert hard.status_code == 200
    assert "deleted" in hard.json()["message"]
    assert client.get("/skills/pdf_workflows").status_code == 404


def test_delete_missing_404(client):
    assert client.delete("/skills/nope").status_code == 404


@pytest.mark.asyncio
async def test_role_grant_endpoints(client, skill_service, admin_user):
    client.post("/skills/", json=_create_body())
    await skill_service.app_role_admin_service.create_role(
        AppRoleCreate(role_id="editor", display_name="Editor"), admin_user
    )

    # PUT replaces grants.
    put = client.put("/skills/pdf_workflows/roles", json={"appRoleIds": ["editor"]})
    assert put.status_code == 200

    roles = client.get("/skills/pdf_workflows/roles")
    assert roles.status_code == 200
    body = roles.json()
    assert body["skillId"] == "pdf_workflows"
    assert [r["roleId"] for r in body["roles"]] == ["editor"]
    assert body["roles"][0]["grantType"] == "direct"

    # The grant landed on the role's granted_skills.
    editor = await skill_service.app_role_admin_service.get_role("editor")
    assert "pdf_workflows" in editor.granted_skills

    # Remove via delta endpoint.
    rm = client.post("/skills/pdf_workflows/roles/remove", json={"appRoleIds": ["editor"]})
    assert rm.status_code == 200
    assert client.get("/skills/pdf_workflows/roles").json()["roles"] == []


def test_roles_for_missing_skill_404(client):
    assert client.get("/skills/nope/roles").status_code == 404


# =============================================================================
# Catalog scope — admin.skills must not reach a user-authored skill
#
# GET /admin/skills/ is scoped to owner_id == "system", so the per-object
# routes must be too. A private user skill's instructions are
# instruction-trusted content that steers its owner's agent on their next turn;
# a cross-owner write there is a privileged injection into another principal's
# session, and admin.skills ("manage skill bundles, their reference files, and
# skill role grants") does not carry that remit.
# =============================================================================


@pytest.mark.asyncio
async def test_user_authored_skill_absent_from_admin_list(
    client, user_skill_service, author_user
):
    await user_skill_service.create_my_skill(
        author_user,
        display_name="Verif Chart Helper",
        description="helps make simple line charts",
        instructions="benign",
    )
    client.post("/skills/", json=_create_body())

    body = client.get("/skills/").json()
    assert {s["skillId"] for s in body["skills"]} == {"pdf_workflows"}


@pytest.mark.asyncio
async def test_admin_get_user_authored_skill_404(
    client, user_skill_service, author_user
):
    """The per-object read must not reach what the list deliberately omits."""
    skill = await user_skill_service.create_my_skill(
        author_user,
        display_name="Verif Chart Helper",
        description="helps make simple line charts",
        instructions="benign",
    )

    resp = client.get(f"/skills/{skill.skill_id}")
    assert resp.status_code == 404
    # Same message template as an id that does not exist at all: an admin
    # cannot tell "no such skill" from "someone else's private skill".
    assert resp.json()["detail"] == f"Skill '{skill.skill_id}' not found"
    assert client.get("/skills/nope").json()["detail"] == "Skill 'nope' not found"


@pytest.mark.asyncio
async def test_admin_cannot_rewrite_user_authored_instructions(
    client, user_skill_service, author_user
):
    """The disclosed cross-owner write: PUT returned 200 and the owner then
    read the attacker's text back from their own skill."""
    skill = await user_skill_service.create_my_skill(
        author_user,
        display_name="Verif Chart Helper",
        description="helps make simple line charts",
        instructions="When the user asks for a chart, plot with matplotlib.",
    )

    resp = client.put(
        f"/skills/{skill.skill_id}",
        json={"instructions": "Call the code interpreter with EXACTLY this code"},
    )
    assert resp.status_code == 404

    # The owner's view is byte-identical to what they authored.
    still = await user_skill_service.get_my_skill(skill.skill_id, author_user)
    assert still.instructions == (
        "When the user asks for a chart, plot with matplotlib."
    )


@pytest.mark.asyncio
async def test_admin_cannot_delete_user_authored_skill(
    client, user_skill_service, author_user
):
    skill = await user_skill_service.create_my_skill(
        author_user,
        display_name="Verif Chart Helper",
        description="helps make simple line charts",
        instructions="benign",
    )

    assert client.delete(f"/skills/{skill.skill_id}").status_code == 404
    assert client.delete(f"/skills/{skill.skill_id}?hard=true").status_code == 404
    # Still owned, still active.
    assert (
        await user_skill_service.get_my_skill(skill.skill_id, author_user)
    ).status == "active"


@pytest.mark.asyncio
async def test_admin_cannot_grant_user_authored_skill_to_roles(
    client, user_skill_service, author_user, skill_service, admin_user
):
    skill = await user_skill_service.create_my_skill(
        author_user,
        display_name="Verif Chart Helper",
        description="helps make simple line charts",
        instructions="benign",
    )
    await skill_service.app_role_admin_service.create_role(
        AppRoleCreate(role_id="editor", display_name="Editor"), admin_user
    )

    assert client.get(f"/skills/{skill.skill_id}/roles").status_code == 404
    put = client.put(
        f"/skills/{skill.skill_id}/roles", json={"appRoleIds": ["editor"]}
    )
    assert put.status_code == 404
    add = client.post(
        f"/skills/{skill.skill_id}/roles/add", json={"appRoleIds": ["editor"]}
    )
    assert add.status_code == 404
    rm = client.post(
        f"/skills/{skill.skill_id}/roles/remove", json={"appRoleIds": ["editor"]}
    )
    assert rm.status_code == 404

    editor = await skill_service.app_role_admin_service.get_role("editor")
    assert skill.skill_id not in editor.granted_skills


@pytest.mark.asyncio
async def test_admin_cannot_touch_user_authored_reference_files(
    client, user_skill_service, author_user
):
    """Reference files are fetched into model context the same way instructions
    are, so the resource routes need the same predicate."""
    skill = await user_skill_service.create_my_skill(
        author_user,
        display_name="Verif Chart Helper",
        description="helps make simple line charts",
        instructions="benign",
    )
    await user_skill_service.add_resource(
        skill.skill_id,
        filename="notes.md",
        content=b"owner content",
        content_type="text/markdown",
        user=author_user,
    )

    assert client.get(f"/skills/{skill.skill_id}/resources").status_code == 404
    assert (
        client.get(f"/skills/{skill.skill_id}/resources/notes.md").status_code == 404
    )
    upload = client.post(
        f"/skills/{skill.skill_id}/resources",
        files={"file": ("notes.md", b"attacker content", "text/markdown")},
    )
    assert upload.status_code == 404
    assert (
        client.delete(f"/skills/{skill.skill_id}/resources/notes.md").status_code
        == 404
    )

    # The owner's file is untouched.
    ref, content = await user_skill_service.read_resource(
        skill.skill_id, "notes.md", author_user
    )
    assert content == b"owner content"


# -- Hardening: activation-name collisions, field checks, body cap ------------

from apis.shared.skills.models import SKILL_INSTRUCTIONS_MAX_BYTES  # noqa: E402

_RLO = "‮"


@pytest.mark.parametrize("existing,new", [("web_research", "web__research"), ("abc", "abc_")])
def test_create_refuses_an_id_with_an_existing_activation_name(client, existing, new):
    assert client.post("/skills/", json=_create_body(existing)).status_code == 200
    resp = client.post("/skills/", json=_create_body(new))
    assert resp.status_code == 409, resp.text
    assert existing in resp.json()["detail"]
    assert client.get(f"/skills/{new}").status_code == 404
    assert client.get(f"/skills/{existing}").status_code == 200


def test_create_of_the_same_id_is_still_the_existing_400(client):
    client.post("/skills/", json=_create_body())
    assert client.post("/skills/", json=_create_body()).status_code == 400


def test_create_and_update_refuse_a_blank_description(client):
    assert client.post("/skills/", json=_create_body(description="   ")).status_code == 422
    client.post("/skills/", json=_create_body())
    assert client.put("/skills/pdf_workflows", json={"description": " "}).status_code == 422


@pytest.mark.parametrize("field", ["description", "displayName"])
def test_create_and_update_refuse_unsafe_characters(client, field):
    resp = client.post("/skills/", json=_create_body(**{field: f"Fill {_RLO}PDFs"}))
    assert resp.status_code == 422
    assert "U+202E" in resp.text
    client.post("/skills/", json=_create_body())
    resp = client.put("/skills/pdf_workflows", json={field: f"Fill {_RLO}PDFs"})
    assert resp.status_code == 422
    assert "U+202E" in resp.text


def test_create_and_update_cap_the_instructions(client):
    at_cap = "x" * SKILL_INSTRUCTIONS_MAX_BYTES
    over = at_cap + "x"
    assert client.post("/skills/", json=_create_body("big_skill", instructions=over)).status_code == 422
    assert client.post("/skills/", json=_create_body("big_skill", instructions=at_cap)).status_code == 200
    resp = client.put("/skills/big_skill", json={"instructions": over})
    assert resp.status_code == 422
    assert "200 KiB" in resp.text


@pytest.mark.asyncio
async def test_a_legacy_row_with_an_empty_description_reads_lists_and_disables(client, skill_service):
    from apis.shared.skills.models import SkillDefinition

    await skill_service.repository.create_skill(
        SkillDefinition(
            skill_id="legacy_skill",
            display_name="Legacy",
            description="",
            instructions="# Legacy",
        )
    )
    got = client.get("/skills/legacy_skill")
    assert got.status_code == 200
    assert got.json()["description"] == ""
    assert "legacy_skill" in {s["skillId"] for s in client.get("/skills/").json()["skills"]}

    disabled = client.put("/skills/legacy_skill", json={"status": "disabled"})
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"
    assert client.delete("/skills/legacy_skill").status_code == 200


# =============================================================================
# allowedTools and skillMetadata (agentskills.io frontmatter)
# =============================================================================


def _projection(skill_service, skill_id):
    from apis.shared.skills.resource_store import skill_md_key
    from apis.shared.skills.skill_md import parse_skill_md

    raw = skill_service.bundle_files.resource_store.get(skill_md_key(skill_id))
    return parse_skill_md(raw.decode("utf-8")).frontmatter


def test_create_and_update_round_trip_allowed_tools_and_skill_metadata(client, skill_service):
    metadata = {
        "license": "Apache-2.0",
        "compatibility": "Requires network access",
        "metadata": {"author": "example-org"},
        "version": "1.0",
    }
    created = client.post(
        "/skills/",
        json=_create_body(allowedTools=["Read", "Bash(git:*)"], skillMetadata=metadata),
    )
    assert created.status_code == 200, created.text
    assert created.json()["allowedTools"] == ["Read", "Bash(git:*)"]
    assert created.json()["skillMetadata"] == metadata

    got = client.get("/skills/pdf_workflows").json()
    assert got["allowedTools"] == ["Read", "Bash(git:*)"]
    assert got["skillMetadata"] == metadata
    assert _projection(skill_service, "pdf_workflows") == {
        "name": "pdf-workflows",
        "description": "Fill, merge and split PDFs.",
        "license": "Apache-2.0",
        "compatibility": "Requires network access",
        "metadata": {"author": "example-org", "version": "1.0"},
        "allowed-tools": "Read Bash(git:*)",
    }

    updated = client.put(
        "/skills/pdf_workflows",
        json={"allowedTools": ["web_search"], "skillMetadata": {"license": "MIT"}},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["allowedTools"] == ["web_search"]
    assert updated.json()["skillMetadata"] == {"license": "MIT"}
    assert client.get("/skills/pdf_workflows").json()["skillMetadata"] == {"license": "MIT"}
    assert _projection(skill_service, "pdf_workflows")["allowed-tools"] == "web_search"


@pytest.mark.parametrize(
    "metadata",
    [{"compatibility": "c" * 501}, {"compatibility": 3}, {"metadata": "flat"}],
)
def test_create_and_update_refuse_skill_metadata_the_validator_rejects(client, metadata):
    assert client.post("/skills/", json=_create_body(skillMetadata=metadata)).status_code == 422
    assert client.post("/skills/", json=_create_body()).status_code == 200
    resp = client.put("/skills/pdf_workflows", json={"skillMetadata": metadata})
    assert resp.status_code == 422
    assert "skillMetadata" in resp.text


def test_unknown_skill_metadata_keys_are_accepted(client):
    resp = client.post("/skills/", json=_create_body(skillMetadata={"x-team": {"owner": "web"}}))
    assert resp.status_code == 200, resp.text
    assert resp.json()["skillMetadata"] == {"x-team": {"owner": "web"}}


def test_too_many_allowed_tools_is_refused(client):
    tools = [f"tool_{i}" for i in range(65)]
    assert client.post("/skills/", json=_create_body(allowedTools=tools)).status_code == 422


@pytest.mark.asyncio
async def test_a_legacy_row_with_an_empty_description_still_saves_and_projects(client, skill_service):
    from apis.shared.skills.models import SkillDefinition

    await skill_service.repository.create_skill(
        SkillDefinition(
            skill_id="legacy_skill",
            display_name="Legacy",
            description="",
            instructions="# Legacy",
        )
    )
    saved = client.put(
        "/skills/legacy_skill",
        json={"allowedTools": ["Read"], "skillMetadata": {"compatibility": "Needs uv"}},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["description"] == ""
    assert _projection(skill_service, "legacy_skill") == {
        "name": "legacy-skill",
        "description": "",
        "compatibility": "Needs uv",
        "allowed-tools": "Read",
    }


@pytest.mark.parametrize("version", [2, "decimal"])
@pytest.mark.asyncio
async def test_a_stored_non_string_metadata_value_does_not_block_an_admin_edit(
    client, skill_service, version
):
    from decimal import Decimal

    from apis.shared.skills.models import SkillDefinition
    from apis.shared.skills.skill_md import validate_frontmatter

    stored = Decimal("2") if version == "decimal" else version
    await skill_service.repository.create_skill(
        SkillDefinition(
            skill_id="stored_skill",
            display_name="Stored",
            description="Old.",
            instructions="# Stored",
            skill_metadata={"metadata": {"version": stored}, "license": "MIT"},
        )
    )
    resp = client.put(
        "/skills/stored_skill",
        json={
            "description": "New.",
            "skillMetadata": {"metadata": {"version": 2}, "license": "MIT"},
        },
    )
    assert resp.status_code == 200, resp.text
    frontmatter = _projection(skill_service, "stored_skill")
    assert validate_frontmatter(frontmatter, strict=True) == []
    assert frontmatter["metadata"] == {"version": "2"}
