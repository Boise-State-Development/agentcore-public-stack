"""Tests for connector ↔ file-source-adapter mapping.

Covers the OAuthProvider model round-trip, the admin-route validation
helper, and the read-only GET /admin/file-source-adapters endpoint.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apis.shared.auth import require_admin
from apis.shared.auth.models import User
from apis.shared.oauth.models import OAuthProvider, OAuthProviderType

from apis.app_api.admin.file_sources import routes as adapter_routes
from apis.app_api.admin.oauth.routes import _validate_file_source_adapter


def _admin() -> User:
    return User(
        user_id="admin-1",
        email="admin@example.com",
        name="Admin",
        roles=["admin"],
        raw_token="test-token",
    )


class TestOAuthProviderMapping:
    def test_file_source_adapter_id_round_trips_through_dynamo(self):
        provider = OAuthProvider(
            provider_id="google",
            display_name="Google",
            provider_type=OAuthProviderType.GOOGLE,
            scopes=["openid"],
            allowed_roles=[],
            file_source_adapter_id="google-drive",
        )
        restored = OAuthProvider.from_dynamo_item(provider.to_dynamo_item())
        assert restored.file_source_adapter_id == "google-drive"

    def test_unmapped_provider_round_trips_as_none(self):
        provider = OAuthProvider(
            provider_id="slack",
            display_name="Slack",
            provider_type=OAuthProviderType.SLACK,
            scopes=[],
            allowed_roles=[],
        )
        assert provider.file_source_adapter_id is None
        restored = OAuthProvider.from_dynamo_item(provider.to_dynamo_item())
        assert restored.file_source_adapter_id is None

    def test_legacy_dynamo_item_without_field_defaults_to_none(self):
        # Records written before this field existed have no fileSourceAdapterId.
        item = OAuthProvider(
            provider_id="github",
            display_name="GitHub",
            provider_type=OAuthProviderType.GITHUB,
            scopes=[],
            allowed_roles=[],
        ).to_dynamo_item()
        del item["fileSourceAdapterId"]
        assert OAuthProvider.from_dynamo_item(item).file_source_adapter_id is None


class TestValidateFileSourceAdapter:
    def test_empty_or_none_is_a_noop(self):
        _validate_file_source_adapter(None, OAuthProviderType.GOOGLE)
        _validate_file_source_adapter("", OAuthProviderType.GOOGLE)

    def test_valid_mapping_passes(self):
        _validate_file_source_adapter("google-drive", OAuthProviderType.GOOGLE)

    def test_unknown_adapter_is_rejected(self):
        with pytest.raises(HTTPException) as exc:
            _validate_file_source_adapter("dropbox", OAuthProviderType.GOOGLE)
        assert exc.value.status_code == 400
        assert "Unknown file-source adapter" in exc.value.detail

    def test_incompatible_provider_type_is_rejected(self):
        with pytest.raises(HTTPException) as exc:
            _validate_file_source_adapter("google-drive", OAuthProviderType.SLACK)
        assert exc.value.status_code == 400
        assert "not compatible" in exc.value.detail


class TestListFileSourceAdaptersEndpoint:
    @pytest.fixture
    def client(self) -> TestClient:
        app = FastAPI()
        app.include_router(adapter_routes.router)
        app.dependency_overrides[require_admin] = _admin
        return TestClient(app)

    def test_lists_shipped_adapters(self, client: TestClient):
        response = client.get("/file-source-adapters/")
        assert response.status_code == 200
        adapters = {a["key"]: a for a in response.json()["adapters"]}
        assert "google-drive" in adapters
        drive = adapters["google-drive"]
        assert drive["displayName"] == "Google Drive"
        assert drive["compatibleProviderTypes"] == ["google"]
        assert drive["requiredScopes"] == [
            "https://www.googleapis.com/auth/drive.readonly"
        ]
