"""Route tests for system-prompts endpoints (admin CRUD + public read).

Wire format is snake_case throughout to match the user_menu_links convention.
"""

import boto3
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth import get_current_user_from_session, require_admin
from apis.shared.auth.models import User
from apis.shared.system_prompts import repository as repo_module
from apis.shared.system_prompts import service as service_module

AWS_REGION = "us-east-1"
TABLE_NAME = "test-system-prompts-routes"


def _make_user(email: str = "user@example.com", roles=None) -> User:
    return User(
        email=email,
        user_id="user-001",
        name="Test User",
        roles=roles if roles is not None else ["User"],
    )


@pytest.fixture()
def system_prompts_table(aws, monkeypatch):
    ddb = boto3.client("dynamodb", region_name=AWS_REGION)
    ddb.create_table(
        TableName=TABLE_NAME,
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
    monkeypatch.setenv("DYNAMODB_SYSTEM_PROMPTS_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("AWS_REGION", AWS_REGION)
    # Reset module-level singletons so each test gets a fresh repo+service
    # bound to the table created by this fixture.
    monkeypatch.setattr(repo_module, "_repository", None)
    monkeypatch.setattr(service_module, "_service", None)
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)


def _build_admin_app() -> FastAPI:
    from apis.app_api.admin.system_prompts.routes import router as admin_router

    app = FastAPI()
    parent = APIRouter(prefix="/admin")
    parent.include_router(admin_router)
    app.include_router(parent)
    return app


def _build_user_app() -> FastAPI:
    from apis.app_api.system_prompts.routes import router as user_router

    app = FastAPI()
    app.include_router(user_router)
    return app


_PAYLOAD = {
    "name": "Guided Learning",
    "description": "Uses the Socratic method.",
    "prompt_text": "Do not give direct answers. Ask guiding questions.",
    "status": "enabled",
}


@pytest.fixture()
def admin_client(system_prompts_table):
    app = _build_admin_app()
    admin = _make_user(roles=["system_admin"])
    app.dependency_overrides[require_admin] = lambda: admin
    return TestClient(app)


@pytest.fixture()
def user_client(system_prompts_table):
    app = _build_user_app()
    user = _make_user()
    app.dependency_overrides[get_current_user_from_session] = lambda: user
    return TestClient(app)


class TestAdminRoutes:
    def test_create_returns_201_snake_case(self, admin_client):
        resp = admin_client.post("/admin/system-prompts/", json=_PAYLOAD)
        assert resp.status_code == 201
        body = resp.json()
        # Wire format is snake_case end-to-end
        assert body["prompt_id"]
        assert body["name"] == "Guided Learning"
        assert body["prompt_text"] == _PAYLOAD["prompt_text"]
        assert body["status"] == "enabled"
        assert "created_at" in body
        assert "updated_at" in body

    def test_create_rejects_bad_status(self, admin_client):
        resp = admin_client.post(
            "/admin/system-prompts/",
            json={**_PAYLOAD, "status": "draft"},
        )
        # Pydantic Literal rejects unknown statuses with 422
        assert resp.status_code == 422

    def test_list_returns_all_including_disabled(self, admin_client):
        admin_client.post("/admin/system-prompts/", json=_PAYLOAD)
        admin_client.post("/admin/system-prompts/", json={**_PAYLOAD, "name": "Disabled", "status": "disabled"})

        resp = admin_client.get("/admin/system-prompts/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2

    def test_list_enabled_only_filter(self, admin_client):
        admin_client.post("/admin/system-prompts/", json=_PAYLOAD)
        admin_client.post("/admin/system-prompts/", json={**_PAYLOAD, "name": "Hidden", "status": "disabled"})

        resp = admin_client.get("/admin/system-prompts/?enabled_only=true")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["prompts"][0]["status"] == "enabled"

    def test_get_single(self, admin_client):
        created = admin_client.post("/admin/system-prompts/", json=_PAYLOAD).json()
        prompt_id = created["prompt_id"]

        resp = admin_client.get(f"/admin/system-prompts/{prompt_id}")
        assert resp.status_code == 200
        assert resp.json()["prompt_id"] == prompt_id

    def test_get_missing_returns_404(self, admin_client):
        resp = admin_client.get("/admin/system-prompts/does-not-exist")
        assert resp.status_code == 404

    def test_update_status(self, admin_client):
        created = admin_client.post("/admin/system-prompts/", json=_PAYLOAD).json()
        prompt_id = created["prompt_id"]

        resp = admin_client.patch(f"/admin/system-prompts/{prompt_id}", json={"status": "disabled"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "disabled"

    def test_update_missing_returns_404(self, admin_client):
        resp = admin_client.patch("/admin/system-prompts/no-such-id", json={"status": "disabled"})
        assert resp.status_code == 404

    def test_delete(self, admin_client):
        created = admin_client.post("/admin/system-prompts/", json=_PAYLOAD).json()
        prompt_id = created["prompt_id"]

        resp = admin_client.delete(f"/admin/system-prompts/{prompt_id}")
        assert resp.status_code == 204

        resp = admin_client.get(f"/admin/system-prompts/{prompt_id}")
        assert resp.status_code == 404

    def test_requires_admin(self, system_prompts_table):
        """Non-admin dependency raises 403 — verify route doesn't bypass it."""
        from fastapi import HTTPException
        app = _build_admin_app()
        app.dependency_overrides[require_admin] = lambda: (_ for _ in ()).throw(
            HTTPException(status_code=403, detail="Forbidden")
        )
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/admin/system-prompts/")
        assert resp.status_code == 403


class TestUserRoutes:
    def test_list_returns_enabled_only_without_prompt_text(self, system_prompts_table):
        """Users see only enabled prompts and never see prompt_text."""
        # Seed via admin
        admin_app = _build_admin_app()
        admin = _make_user(roles=["system_admin"])
        admin_app.dependency_overrides[require_admin] = lambda: admin
        admin_client = TestClient(admin_app)
        admin_client.post("/admin/system-prompts/", json=_PAYLOAD)
        admin_client.post("/admin/system-prompts/", json={**_PAYLOAD, "name": "Hidden", "status": "disabled"})

        # Read as user
        user_app = _build_user_app()
        user = _make_user()
        user_app.dependency_overrides[get_current_user_from_session] = lambda: user
        user_client = TestClient(user_app)

        resp = user_client.get("/system-prompts/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["prompts"][0]["name"] == "Guided Learning"
        # prompt_text must NEVER appear in user response, in any casing
        assert "prompt_text" not in body["prompts"][0]
        assert "promptText" not in body["prompts"][0]
        # User-facing response carries prompt_id (not promptId)
        assert "prompt_id" in body["prompts"][0]

    def test_user_list_empty_when_no_prompts(self, user_client):
        resp = user_client.get("/system-prompts/")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
