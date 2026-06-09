"""Admin /skills route tests (TestClient + moto-backed service).

Verifies endpoint wiring, status codes, response shape (camelCase aliases),
bound-tool validation surfacing as 400, and role-grant round-trips.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth import require_admin
from apis.shared.rbac.models import AppRoleCreate
from apis.shared.tools.models import ToolDefinition, ToolProtocol, ToolStatus
from apis.app_api.admin.skills import routes as skill_routes


@pytest.fixture()
def client(skill_service, admin_user, monkeypatch):
    monkeypatch.setattr(skill_routes, "get_skill_catalog_service", lambda: skill_service)
    app = FastAPI()
    app.include_router(skill_routes.router)
    app.dependency_overrides[require_admin] = lambda: admin_user
    return TestClient(app)


def _create_body(skill_id="pdf_workflows", **kw):
    body = {
        "skillId": skill_id,
        "displayName": "PDF Workflows",
        "description": "Fill, merge and split PDFs.",
        "instructions": "# PDF Workflows",
        "boundToolIds": [],
    }
    body.update(kw)
    return body


async def _seed_tool(tool_repo, tool_id, status=ToolStatus.ACTIVE):
    await tool_repo.create_tool(
        ToolDefinition(
            tool_id=tool_id,
            display_name=tool_id,
            description="x",
            protocol=ToolProtocol.LOCAL,
            status=status,
        )
    )


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


def test_create_rejects_unknown_bound_tool(client):
    resp = client.post(
        "/skills/", json=_create_body(boundToolIds=["ghost_tool"])
    )
    assert resp.status_code == 400
    assert "unknown tool" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_with_active_bound_tool(client, tool_repo):
    await _seed_tool(tool_repo, "fill_pdf_form")
    resp = client.post(
        "/skills/", json=_create_body(boundToolIds=["fill_pdf_form"])
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["boundToolIds"] == ["fill_pdf_form"]


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
