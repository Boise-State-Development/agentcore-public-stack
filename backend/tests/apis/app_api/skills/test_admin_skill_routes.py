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
