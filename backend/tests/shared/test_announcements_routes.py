"""Route tests for the admin announcements endpoints.

PR-1 ships no user-facing surface, so there is only one router to cover. The
two checks worth naming: ``ctaUrl`` is rejected at the **API**, not only in the
admin form (anyone can curl this), and the whole package disappears when the
kill switch is thrown.
"""

import importlib
import os

import boto3
import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

import apis.app_api.admin.routes as admin_routes_module
from apis.shared.announcements import repository as repo_module
from apis.shared.announcements import service as service_module
from apis.shared.auth.models import User
from tests.conftest import override_admin_auth

AWS_REGION = "us-east-1"
TABLE_NAME = "test-announcements-routes"

FUTURE = "2099-01-01T00:00:00Z"


def _make_user(email: str = "admin@example.com", roles=None) -> User:
    return User(
        email=email,
        user_id="admin-001",
        name="Test Admin",
        roles=roles if roles is not None else ["system_admin"],
    )


@pytest.fixture()
def announcements_table(aws, monkeypatch):
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
    monkeypatch.setenv("DYNAMODB_ANNOUNCEMENTS_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("AWS_REGION", AWS_REGION)
    # Module-level singletons; reset so the next get_*() builds against moto.
    monkeypatch.setattr(repo_module, "_repository", None)
    monkeypatch.setattr(service_module, "_service", None)
    return boto3.resource("dynamodb", region_name=AWS_REGION).Table(TABLE_NAME)


def _build_app(user: User = None) -> FastAPI:
    from apis.app_api.admin.announcements.routes import router as admin_router

    app = FastAPI()
    parent = APIRouter(prefix="/admin")
    parent.include_router(admin_router)
    app.include_router(parent)
    override_admin_auth(app, (lambda: user) if user else (lambda: _make_user()))
    return app


def _client(user: User = None) -> TestClient:
    return TestClient(_build_app(user))


def _payload(**kw) -> dict:
    body = {
        "title": "Skills are here",
        "body_markdown": "# Skills",
        "publish_at": "2020-01-01T00:00:00Z",
    }
    body.update(kw)
    return body


class TestAdminCrud:
    def test_create_returns_201_as_draft(self, announcements_table):
        resp = _client().post("/admin/announcements/", json=_payload())
        assert resp.status_code == 201
        body = resp.json()
        assert body["state"] == "draft"
        assert body["revision"] == 1
        assert body["surfaces"] == ["panel"]
        assert body["created_by"] == "admin@example.com"

    def test_list_then_get_round_trips(self, announcements_table):
        client = _client()
        created = client.post("/admin/announcements/", json=_payload()).json()

        listed = client.get("/admin/announcements/")
        assert listed.status_code == 200
        assert listed.json()["total"] == 1

        got = client.get(f"/admin/announcements/{created['announcement_id']}")
        assert got.status_code == 200
        assert got.json()["title"] == "Skills are here"

    def test_list_filters_by_state(self, announcements_table):
        client = _client()
        created = client.post("/admin/announcements/", json=_payload()).json()
        client.post("/admin/announcements/", json=_payload(title="Second"))
        client.post(f"/admin/announcements/{created['announcement_id']}/publish")

        published = client.get("/admin/announcements/?state=published").json()
        assert [a["title"] for a in published["announcements"]] == ["Skills are here"]

    def test_get_missing_returns_404(self, announcements_table):
        assert _client().get("/admin/announcements/nope").status_code == 404

    def test_publish_archive_revise(self, announcements_table):
        client = _client()
        created = client.post("/admin/announcements/", json=_payload()).json()
        aid = created["announcement_id"]

        assert client.post(f"/admin/announcements/{aid}/publish").json()["state"] == (
            "published"
        )
        assert client.post(f"/admin/announcements/{aid}/revise").json()["revision"] == 2
        assert client.post(f"/admin/announcements/{aid}/archive").json()["state"] == (
            "archived"
        )

    def test_publishing_an_archived_announcement_returns_400(self, announcements_table):
        client = _client()
        aid = client.post("/admin/announcements/", json=_payload()).json()[
            "announcement_id"
        ]
        client.post(f"/admin/announcements/{aid}/archive")
        assert client.post(f"/admin/announcements/{aid}/publish").status_code == 400

    def test_patch_leaves_revision_alone(self, announcements_table):
        client = _client()
        aid = client.post("/admin/announcements/", json=_payload()).json()[
            "announcement_id"
        ]
        patched = client.patch(
            f"/admin/announcements/{aid}", json={"title": "Skills are here!"}
        ).json()
        assert patched["title"] == "Skills are here!"
        assert patched["revision"] == 1

    def test_patch_to_an_invalid_merged_record_returns_400(self, announcements_table):
        client = _client()
        aid = client.post("/admin/announcements/", json=_payload()).json()[
            "announcement_id"
        ]
        resp = client.patch(
            f"/admin/announcements/{aid}", json={"surfaces": ["panel", "banner"]}
        )
        assert resp.status_code == 400
        assert "expiresAt" in resp.json()["detail"]

    def test_patch_cannot_change_state(self, announcements_table):
        """The publish guard must not be reachable around.

        If PATCH accepted `state`, an archived announcement could be put back
        in front of every user by a request that looks like a body edit — and
        the /publish state machine would be decorative.
        """
        client = _client()
        aid = client.post("/admin/announcements/", json=_payload()).json()[
            "announcement_id"
        ]
        client.post(f"/admin/announcements/{aid}/archive")

        resp = client.patch(f"/admin/announcements/{aid}", json={"state": "published"})

        # Unknown field: pydantic ignores it rather than 422-ing, so assert on
        # the outcome that matters — the state did not move.
        assert resp.status_code == 200
        assert resp.json()["state"] == "archived"

    def test_create_cannot_start_published(self, announcements_table):
        """Going live is its own call, never a side effect of authoring."""
        resp = _client().post("/admin/announcements/", json=_payload(state="published"))
        assert resp.status_code == 422

    def test_create_may_start_scheduled(self, announcements_table):
        resp = _client().post("/admin/announcements/", json=_payload(state="scheduled"))
        assert resp.status_code == 201
        assert resp.json()["state"] == "scheduled"

    def test_unknown_state_filter_is_rejected(self, announcements_table):
        assert _client().get("/admin/announcements/?state=nonsense").status_code == 422

    def test_delete_returns_204_then_404(self, announcements_table):
        client = _client()
        aid = client.post("/admin/announcements/", json=_payload()).json()[
            "announcement_id"
        ]
        assert client.delete(f"/admin/announcements/{aid}").status_code == 204
        assert client.delete(f"/admin/announcements/{aid}").status_code == 404


class TestApiLevelValidation:
    def test_cta_url_rejects_javascript_scheme(self, announcements_table):
        """Rejected at the API, not only in the form.

        Angular's DomSanitizer strips `javascript:` from `[href]`, but the SPA
        form is not the only client this endpoint has — and the announcement
        scope is delegable, so the author may not be a platform admin (§D10).
        """
        resp = _client().post(
            "/admin/announcements/",
            json=_payload(cta_label="Learn more", cta_url="javascript:alert(1)"),
        )
        assert resp.status_code == 422

    def test_patch_cta_url_rejects_javascript_scheme(self, announcements_table):
        client = _client()
        aid = client.post("/admin/announcements/", json=_payload()).json()[
            "announcement_id"
        ]
        resp = client.patch(
            f"/admin/announcements/{aid}",
            json={"cta_label": "Learn more", "cta_url": "javascript:alert(1)"},
        )
        assert resp.status_code == 422

    def test_banner_without_expiry_returns_422(self, announcements_table):
        resp = _client().post(
            "/admin/announcements/", json=_payload(surfaces=["panel", "banner"])
        )
        assert resp.status_code == 422

    def test_oversized_body_returns_422(self, announcements_table):
        resp = _client().post(
            "/admin/announcements/",
            json=_payload(body_markdown="x" * (16 * 1024 + 1)),
        )
        assert resp.status_code == 422

    def test_target_roles_are_stored_verbatim_and_grant_nothing(
        self, announcements_table
    ):
        """§D9 — a display filter, never an RBAC grant.

        The role list lands on the announcement item and nowhere else; the
        role records are not touched. Pinned here so a future "fix" that writes
        it through to `AppRole.granted*` has to delete a test that says why.
        """
        created = _client().post(
            "/admin/announcements/",
            json=_payload(target_roles=["faculty", "staff"]),
        ).json()

        assert created["target_roles"] == ["faculty", "staff"]
        item = announcements_table.get_item(
            Key={
                "PK": "ANNOUNCEMENTS",
                "SK": f"ANNOUNCEMENT#{created['announcement_id']}",
            }
        )["Item"]
        assert item["targetRoles"] == ["faculty", "staff"]


class TestAuthorization:
    def test_non_admin_gets_403(self, announcements_table):
        app = _build_app()

        def _forbid():
            raise HTTPException(status_code=403, detail="Forbidden")

        override_admin_auth(app, _forbid)
        assert TestClient(app).get("/admin/announcements/").status_code == 403


# ---------------------------------------------------------------------------
# ANNOUNCEMENTS_ENABLED kill switch
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_router_paths():
    """Reload the admin router under a chosen ANNOUNCEMENTS_ENABLED value.

    Restores the module (flag unset → enabled) on teardown so a reload here
    cannot leak an unmounted router into later tests.
    """

    def _load(*, enabled: bool) -> set[str]:
        # Default-ON, so "disabled" must be set EXPLICITLY.
        os.environ["ANNOUNCEMENTS_ENABLED"] = "true" if enabled else "false"
        importlib.reload(admin_routes_module)
        return {
            getattr(route, "path", "") for route in admin_routes_module.router.routes
        }

    yield _load

    os.environ.pop("ANNOUNCEMENTS_ENABLED", None)
    importlib.reload(admin_routes_module)


class TestFeatureFlag:
    def test_defaults_on_when_unset(self, monkeypatch):
        from apis.shared.feature_flags import announcements_enabled

        monkeypatch.delenv("ANNOUNCEMENTS_ENABLED", raising=False)
        assert announcements_enabled() is True

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("false", False),
            ("False", False),
            (" false ", False),
            ("true", True),
            ("0", True),
            # An unset GitHub Actions variable forwards as the empty string; it
            # must resolve ENABLED, or the kill switch dark-ships a live feature.
            ("", True),
            ("   ", True),
        ],
    )
    def test_only_literal_false_disables(self, monkeypatch, value, expected):
        from apis.shared.feature_flags import announcements_enabled

        monkeypatch.setenv("ANNOUNCEMENTS_ENABLED", value)
        assert announcements_enabled() is expected

    def test_admin_router_unmounted_when_disabled(self, admin_router_paths):
        paths = admin_router_paths(enabled=False)
        assert not any("/announcements" in p for p in paths)

    def test_admin_router_mounted_when_enabled(self, admin_router_paths):
        paths = admin_router_paths(enabled=True)
        assert any("/announcements" in p for p in paths)

    def test_disabled_router_404s_the_surface(self, announcements_table):
        """The end a caller actually sees: the path is gone, not 403."""
        os.environ["ANNOUNCEMENTS_ENABLED"] = "false"
        try:
            importlib.reload(admin_routes_module)
            app = FastAPI()
            app.include_router(admin_routes_module.router)
            override_admin_auth(app, lambda: _make_user())
            assert TestClient(app).get("/admin/announcements/").status_code == 404
        finally:
            os.environ.pop("ANNOUNCEMENTS_ENABLED", None)
            importlib.reload(admin_routes_module)
