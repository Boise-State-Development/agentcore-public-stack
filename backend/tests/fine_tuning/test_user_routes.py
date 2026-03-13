"""Route tests for user-facing fine-tuning access endpoint."""

import pytest
from unittest.mock import MagicMock
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apis.shared.auth.models import User
from apis.shared.auth.dependencies import get_current_user
from apis.app_api.fine_tuning.routes import router
from apis.app_api.fine_tuning.repository import get_fine_tuning_access_repository


def _create_app():
    app = FastAPI()
    app.include_router(router)
    return app


def _override_auth(app: FastAPI, user: User):
    app.dependency_overrides[get_current_user] = lambda: user


def _override_repo(app: FastAPI, repo: MagicMock):
    app.dependency_overrides[get_fine_tuning_access_repository] = lambda: repo


class TestCheckAccess:

    def test_returns_access_info_for_whitelisted_user(self, make_user):
        app = _create_app()
        user = make_user(email="allowed@example.com")
        _override_auth(app, user)

        mock_repo = MagicMock()
        mock_repo.check_and_reset_quota.return_value = {
            "email": "allowed@example.com",
            "granted_by": "admin@example.com",
            "granted_at": "2026-01-01T00:00:00Z",
            "monthly_quota_hours": 10.0,
            "current_month_usage_hours": 3.5,
            "quota_period": "2026-03",
        }
        _override_repo(app, mock_repo)

        client = TestClient(app)
        resp = client.get("/fine-tuning/access")

        assert resp.status_code == 200
        body = resp.json()
        assert body["has_access"] is True
        assert body["monthly_quota_hours"] == 10.0
        assert body["current_month_usage_hours"] == 3.5
        assert body["quota_period"] == "2026-03"

    def test_returns_no_access_for_non_whitelisted_user(self, make_user):
        app = _create_app()
        user = make_user(email="denied@example.com")
        _override_auth(app, user)

        mock_repo = MagicMock()
        mock_repo.check_and_reset_quota.return_value = None
        _override_repo(app, mock_repo)

        client = TestClient(app)
        resp = client.get("/fine-tuning/access")

        assert resp.status_code == 200
        body = resp.json()
        assert body["has_access"] is False
        assert body["monthly_quota_hours"] is None

    def test_returns_401_when_unauthenticated(self):
        app = _create_app()

        def _raise_401():
            raise HTTPException(status_code=401, detail="Not authenticated")
        app.dependency_overrides[get_current_user] = _raise_401

        client = TestClient(app)
        resp = client.get("/fine-tuning/access")

        assert resp.status_code == 401
