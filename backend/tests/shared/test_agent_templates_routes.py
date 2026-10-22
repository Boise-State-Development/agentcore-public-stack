"""Route tests for agent-templates endpoints (admin CRUD + public read).

Admin wire is snake_case; the public endpoint returns the client
``TemplateCatalogEntry`` shape (camelCase ``draft`` + ``pitch``), so the
existing picker consumes it unchanged.
"""

import boto3
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from apis.shared.auth import get_current_user_from_session
from tests.conftest import override_admin_auth
from apis.shared.auth.models import User
from apis.shared.agent_templates import repository as repo_module
from apis.shared.agent_templates import service as service_module
from apis.shared.caching import config_cache

AWS_REGION = "us-east-1"
TABLE_NAME = "test-agent-templates-routes"


def _make_user(email: str = "user@example.com", roles=None) -> User:
    return User(
        email=email,
        user_id="user-001",
        name="Test User",
        roles=roles if roles is not None else ["User"],
    )


@pytest.fixture()
def templates_table(aws, monkeypatch):
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
    monkeypatch.setenv("DYNAMODB_AGENT_TEMPLATES_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("AWS_REGION", AWS_REGION)
    monkeypatch.setattr(repo_module, "_repository", None)
    monkeypatch.setattr(service_module, "_service", None)
    config_cache.invalidate(config_cache.AGENT_TEMPLATES)
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)


def _build_admin_app() -> FastAPI:
    from apis.app_api.admin.agent_templates.routes import router as admin_router

    app = FastAPI()
    parent = APIRouter(prefix="/admin")
    parent.include_router(admin_router)
    app.include_router(parent)
    return app


def _build_user_app() -> FastAPI:
    from apis.app_api.agent_templates.routes import router as user_router

    app = FastAPI()
    app.include_router(user_router)
    return app


_PAYLOAD = {
    "template_id": "course-helper",
    "name": "Course Helper",
    "description": "A study assistant for one course.",
    "emoji": "🎓",
    "instructions": "You are a course helper.",
    "tags": [],
    "starters": ["What topics does this cover?"],
    "modelConfig": {"modelId": "us.anthropic.claude-sonnet-5", "params": {}},
    "bindings": [{"kind": "tool", "ref": "some_tool", "config": {}}],
    "pitch": "Answers from your course materials.",
    "status": "enabled",
    "sort_order": 10,
}


@pytest.fixture()
def admin_client(templates_table):
    app = _build_admin_app()
    admin = _make_user(roles=["system_admin"])
    override_admin_auth(app, lambda: admin)
    return TestClient(app)


@pytest.fixture()
def user_client(templates_table):
    app = _build_user_app()
    user = _make_user()
    app.dependency_overrides[get_current_user_from_session] = lambda: user
    return TestClient(app)


class TestAdminRoutes:
    def test_create_returns_201(self, admin_client):
        resp = admin_client.post("/admin/agent-templates/", json=_PAYLOAD)
        assert resp.status_code == 201
        body = resp.json()
        assert body["template_id"] == "course-helper"
        assert body["name"] == "Course Helper"
        # Admin wire exposes modelConfig as the camel alias.
        assert body["modelConfig"]["modelId"] == "us.anthropic.claude-sonnet-5"
        assert body["created_at"]

    def test_duplicate_id_conflicts(self, admin_client):
        admin_client.post("/admin/agent-templates/", json=_PAYLOAD)
        resp = admin_client.post("/admin/agent-templates/", json=_PAYLOAD)
        assert resp.status_code == 409

    def test_list_all_includes_disabled(self, admin_client):
        admin_client.post("/admin/agent-templates/", json=_PAYLOAD)
        admin_client.post(
            "/admin/agent-templates/",
            json={**_PAYLOAD, "template_id": "off", "name": "Off", "status": "disabled"},
        )
        resp = admin_client.get("/admin/agent-templates/")
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    def test_update_and_delete(self, admin_client):
        admin_client.post("/admin/agent-templates/", json=_PAYLOAD)
        patched = admin_client.patch(
            "/admin/agent-templates/course-helper", json={"name": "Renamed"}
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "Renamed"

        deleted = admin_client.delete("/admin/agent-templates/course-helper")
        assert deleted.status_code == 204
        assert admin_client.get("/admin/agent-templates/course-helper").status_code == 404


class TestPublicRoute:
    def test_empty_catalog_returns_empty_list(self, user_client):
        resp = user_client.get("/templates/")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"templates": [], "total": 0}

    def test_returns_enabled_only_in_client_shape(self, admin_client, user_client):
        admin_client.post("/admin/agent-templates/", json=_PAYLOAD)
        admin_client.post(
            "/admin/agent-templates/",
            json={**_PAYLOAD, "template_id": "off", "name": "Off", "status": "disabled"},
        )
        resp = user_client.get("/templates/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        entry = body["templates"][0]
        # Public shape mirrors the client TemplateCatalogEntry { draft, pitch }.
        assert set(entry.keys()) == {"draft", "pitch"}
        draft = entry["draft"]
        assert draft["templateId"] == "course-helper"
        assert draft["modelConfig"]["modelId"] == "us.anthropic.claude-sonnet-5"
        assert draft["bindings"][0]["ref"] == "some_tool"
        # Catalog/audit fields must NOT leak into the public draft.
        assert "status" not in draft
        assert "sort_order" not in draft
        assert "createdBy" not in draft
