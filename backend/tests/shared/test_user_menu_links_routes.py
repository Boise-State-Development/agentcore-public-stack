"""Route tests for user-menu links endpoints (admin CRUD + public read)."""

import boto3
import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from apis.shared.auth import get_current_user_from_session, require_admin
from apis.shared.auth.models import User
from apis.shared.user_menu_links import repository as repo_module
from apis.shared.user_menu_links import service as service_module

AWS_REGION = "us-east-1"
TABLE_NAME = "test-user-menu-links-routes"


def _make_user(email: str = "user@example.com", roles=None) -> User:
    return User(
        email=email,
        user_id="user-001",
        name="Test User",
        roles=roles if roles is not None else ["User"],
    )


@pytest.fixture()
def user_menu_links_table(aws, monkeypatch):
    """Moto-backed DynamoDB table + module-singleton reset so the routes pick
    up this fresh table on first call inside each test."""
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
    monkeypatch.setenv("DYNAMODB_USER_MENU_LINKS_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("AWS_REGION", AWS_REGION)
    # The service + repo are module-level singletons; reset them so the
    # next get_*() call constructs a fresh instance against the moto table.
    monkeypatch.setattr(repo_module, "_repository", None)
    monkeypatch.setattr(service_module, "_service", None)
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)


def _build_admin_app() -> FastAPI:
    """Mount the admin router under /admin to mirror the real app."""
    from apis.app_api.admin.user_menu_links.routes import router as admin_router

    app = FastAPI()
    parent = APIRouter(prefix="/admin")
    parent.include_router(admin_router)
    app.include_router(parent)
    return app


def _build_public_app() -> FastAPI:
    from apis.app_api.user_menu_links.routes import router as public_router

    app = FastAPI()
    app.include_router(public_router)
    return app


# ----------------------------------------------------------------------
# Admin routes
# ----------------------------------------------------------------------


class TestAdminRoutes:
    def test_create_returns_201(self, user_menu_links_table):
        app = _build_admin_app()
        admin = _make_user(email="admin@example.com", roles=["system_admin"])
        app.dependency_overrides[require_admin] = lambda: admin

        client = TestClient(app)
        resp = client.post(
            "/admin/user-menu-links/",
            json={"label": "Privacy", "kind": "external", "url": "https://x.example"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["label"] == "Privacy"
        assert body["created_by"] == "admin@example.com"

    def test_create_rejects_non_http_url_with_422(self, user_menu_links_table):
        app = _build_admin_app()
        admin = _make_user(email="admin@example.com", roles=["system_admin"])
        app.dependency_overrides[require_admin] = lambda: admin

        client = TestClient(app)
        resp = client.post(
            "/admin/user-menu-links/",
            json={"label": "Bad", "kind": "external", "url": "javascript:alert(1)"},
        )
        assert resp.status_code == 422

    def test_create_missing_url_for_external_returns_422(self, user_menu_links_table):
        app = _build_admin_app()
        admin = _make_user(email="admin@example.com", roles=["system_admin"])
        app.dependency_overrides[require_admin] = lambda: admin

        client = TestClient(app)
        resp = client.post(
            "/admin/user-menu-links/",
            json={"label": "X", "kind": "external"},
        )
        assert resp.status_code == 422

    def test_non_admin_gets_403(self, user_menu_links_table):
        app = _build_admin_app()

        def _forbid():
            raise HTTPException(status_code=403, detail="Forbidden")

        app.dependency_overrides[require_admin] = _forbid

        client = TestClient(app)
        resp = client.get("/admin/user-menu-links/")
        assert resp.status_code == 403

    def test_list_then_get_round_trips(self, user_menu_links_table):
        app = _build_admin_app()
        admin = _make_user(email="admin@example.com", roles=["system_admin"])
        app.dependency_overrides[require_admin] = lambda: admin

        client = TestClient(app)
        created = client.post(
            "/admin/user-menu-links/",
            json={"label": "About", "kind": "modal", "body_markdown": "# Hi"},
        ).json()

        list_resp = client.get("/admin/user-menu-links/")
        assert list_resp.status_code == 200
        assert list_resp.json()["total"] == 1

        get_resp = client.get(f"/admin/user-menu-links/{created['link_id']}")
        assert get_resp.status_code == 200
        assert get_resp.json()["label"] == "About"

    def test_get_missing_returns_404(self, user_menu_links_table):
        app = _build_admin_app()
        admin = _make_user(email="admin@example.com", roles=["system_admin"])
        app.dependency_overrides[require_admin] = lambda: admin

        client = TestClient(app)
        resp = client.get("/admin/user-menu-links/does-not-exist")
        assert resp.status_code == 404

    def test_update_returns_400_on_invariant_violation(self, user_menu_links_table):
        app = _build_admin_app()
        admin = _make_user(email="admin@example.com", roles=["system_admin"])
        app.dependency_overrides[require_admin] = lambda: admin

        client = TestClient(app)
        created = client.post(
            "/admin/user-menu-links/",
            json={"label": "Privacy", "kind": "external", "url": "https://x.example"},
        ).json()

        # PATCH kind=modal without supplying body_markdown — the merged record
        # fails the kind/body invariant in the repository, which raises
        # ValueError → mapped to 400 by the handler.
        resp = client.patch(
            f"/admin/user-menu-links/{created['link_id']}",
            json={"kind": "modal"},
        )
        assert resp.status_code == 400

    def test_delete_returns_204_then_404(self, user_menu_links_table):
        app = _build_admin_app()
        admin = _make_user(email="admin@example.com", roles=["system_admin"])
        app.dependency_overrides[require_admin] = lambda: admin

        client = TestClient(app)
        created = client.post(
            "/admin/user-menu-links/",
            json={"label": "X", "kind": "external", "url": "https://x.example"},
        ).json()

        del_resp = client.delete(f"/admin/user-menu-links/{created['link_id']}")
        assert del_resp.status_code == 204

        again = client.delete(f"/admin/user-menu-links/{created['link_id']}")
        assert again.status_code == 404


# ----------------------------------------------------------------------
# Public read route
# ----------------------------------------------------------------------


class TestPublicRoute:
    def test_returns_only_enabled_links(self, user_menu_links_table):
        admin_app = _build_admin_app()
        admin = _make_user(email="admin@example.com", roles=["system_admin"])
        admin_app.dependency_overrides[require_admin] = lambda: admin
        admin_client = TestClient(admin_app)

        # Seed one enabled + one disabled link via the admin API.
        admin_client.post(
            "/admin/user-menu-links/",
            json={"label": "Visible", "kind": "external", "url": "https://x.example"},
        )
        admin_client.post(
            "/admin/user-menu-links/",
            json={
                "label": "Hidden",
                "kind": "external",
                "url": "https://y.example",
                "enabled": False,
            },
        )

        public_app = _build_public_app()
        public_app.dependency_overrides[get_current_user_from_session] = (
            lambda: _make_user()
        )
        public_client = TestClient(public_app)
        resp = public_client.get("/user-menu-links/")
        assert resp.status_code == 200
        body = resp.json()
        assert [link["label"] for link in body["links"]] == ["Visible"]
