"""Route tests for GET /admin/auth-providers/cognito-redirect-uri."""

import pytest
from typing import List, Optional
from unittest.mock import patch
from fastapi import FastAPI, APIRouter
from fastapi.testclient import TestClient

from apis.shared.auth.models import User
from apis.shared.auth import require_admin


@pytest.fixture
def make_user():
    def _make_user(
        email: str = "admin@example.com",
        user_id: str = "user-001",
        name: str = "Test Admin",
        roles: Optional[List[str]] = None,
    ) -> User:
        return User(
            email=email,
            user_id=user_id,
            name=name,
            roles=roles if roles is not None else ["Admin"],
        )

    return _make_user


def _create_app(admin_user: User) -> FastAPI:
    from apis.app_api.admin.auth_providers.routes import router

    app = FastAPI()
    admin_router = APIRouter(prefix="/admin")
    admin_router.include_router(router)
    app.include_router(admin_router)
    app.dependency_overrides[require_admin] = lambda: admin_user
    return app


class TestCognitoRedirectUri:
    def test_returns_composed_redirect_uri(self, make_user):
        app = _create_app(make_user())
        with patch.dict(
            "os.environ",
            {"COGNITO_DOMAIN_URL": "https://prefix.auth.us-west-2.amazoncognito.com"},
        ):
            client = TestClient(app)
            resp = client.get("/admin/auth-providers/cognito-redirect-uri")

        assert resp.status_code == 200
        assert resp.json() == {
            "redirect_uri": "https://prefix.auth.us-west-2.amazoncognito.com/oauth2/idpresponse"
        }

    def test_strips_trailing_slash(self, make_user):
        app = _create_app(make_user())
        with patch.dict(
            "os.environ",
            {"COGNITO_DOMAIN_URL": "https://prefix.auth.us-west-2.amazoncognito.com/"},
        ):
            client = TestClient(app)
            resp = client.get("/admin/auth-providers/cognito-redirect-uri")

        assert resp.status_code == 200
        assert resp.json()["redirect_uri"].endswith(
            ".amazoncognito.com/oauth2/idpresponse"
        )

    def test_503_when_env_var_missing(self, make_user):
        app = _create_app(make_user())
        with patch.dict("os.environ", {"COGNITO_DOMAIN_URL": ""}, clear=False):
            client = TestClient(app)
            resp = client.get("/admin/auth-providers/cognito-redirect-uri")

        assert resp.status_code == 503
        assert "COGNITO_DOMAIN_URL" in resp.json()["detail"]
