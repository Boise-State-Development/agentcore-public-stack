"""Tests for connector ↔ export-target-adapter mapping.

Covers the OAuthProvider model round-trip for `export_target_adapter_id`, the
admin-route validation helper, the export-target registry, and the read-only
GET /admin/export-target-adapters endpoint.

The export-target registry ships empty in this PR (the Google Drive export
adapter lands in the next one), so the validation/endpoint tests register a
stub adapter under a test-only key — distinct from any real adapter key so it
never collides with a future shipped adapter.
"""

from __future__ import annotations

from typing import List, Optional

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apis.shared.auth import require_admin
from apis.shared.auth.models import User
from apis.shared.oauth.models import OAuthProvider, OAuthProviderType

from apis.app_api.admin.export_targets import routes as adapter_routes
from apis.app_api.admin.oauth.routes import _validate_export_target_adapter
from apis.app_api.export_targets.adapter import (
    ExportTargetAdapter,
    ExportTargetMetadata,
)
from apis.app_api.export_targets.models import (
    CreatedFile,
    ExportDestination,
    ExportFormat,
)
from apis.app_api.export_targets.registry import ExportTargetRegistry, registry

_STUB_KEY = "test-export-target"
_STUB_SCOPE = "https://www.googleapis.com/auth/drive.file"


def _admin() -> User:
    return User(
        user_id="admin-1",
        email="admin@example.com",
        name="Admin",
        roles=["admin"],
        raw_token="test-token",
    )


class _StubExportAdapter(ExportTargetAdapter):
    """Minimal in-test adapter so the registry/endpoint have something to list."""

    @property
    def metadata(self) -> ExportTargetMetadata:
        return ExportTargetMetadata(
            key=_STUB_KEY,
            display_name="Test Export Target",
            icon="test-icon",
            compatible_provider_types=(OAuthProviderType.GOOGLE,),
            required_scopes=(_STUB_SCOPE,),
            supported_formats=(ExportFormat.GOOGLE_DOC, ExportFormat.MARKDOWN),
        )

    async def list_destinations(self, access_token: str) -> List[ExportDestination]:
        return [ExportDestination(id="root", name="My Drive")]

    async def create_document(
        self,
        access_token: str,
        *,
        content: bytes,
        name: str,
        source_mime_type: str,
        target_format: ExportFormat,
        parent_id: Optional[str] = None,
    ) -> CreatedFile:
        return CreatedFile(file_id="file-1", name=name, web_view_link=None)


@pytest.fixture
def registered_stub():
    """Register the stub into the process-wide registry for the test, then remove it."""
    adapter = _StubExportAdapter()
    registry.register(adapter)
    try:
        yield adapter
    finally:
        # No public unregister — the registry is immutable at runtime in prod.
        registry._adapters.pop(_STUB_KEY, None)


class TestOAuthProviderExportMapping:
    def test_export_target_adapter_id_round_trips_through_dynamo(self):
        provider = OAuthProvider(
            provider_id="google",
            display_name="Google",
            provider_type=OAuthProviderType.GOOGLE,
            scopes=["openid"],
            allowed_roles=[],
            export_target_adapter_id="google-drive",
        )
        restored = OAuthProvider.from_dynamo_item(provider.to_dynamo_item())
        assert restored.export_target_adapter_id == "google-drive"

    def test_connector_can_be_both_source_and_target(self):
        provider = OAuthProvider(
            provider_id="google",
            display_name="Google",
            provider_type=OAuthProviderType.GOOGLE,
            scopes=["openid"],
            allowed_roles=[],
            file_source_adapter_id="google-drive",
            export_target_adapter_id="google-drive",
        )
        restored = OAuthProvider.from_dynamo_item(provider.to_dynamo_item())
        assert restored.file_source_adapter_id == "google-drive"
        assert restored.export_target_adapter_id == "google-drive"

    def test_unmapped_provider_round_trips_as_none(self):
        provider = OAuthProvider(
            provider_id="slack",
            display_name="Slack",
            provider_type=OAuthProviderType.SLACK,
            scopes=[],
            allowed_roles=[],
        )
        assert provider.export_target_adapter_id is None
        restored = OAuthProvider.from_dynamo_item(provider.to_dynamo_item())
        assert restored.export_target_adapter_id is None

    def test_legacy_dynamo_item_without_field_defaults_to_none(self):
        # Records written before this field existed have no exportTargetAdapterId.
        item = OAuthProvider(
            provider_id="github",
            display_name="GitHub",
            provider_type=OAuthProviderType.GITHUB,
            scopes=[],
            allowed_roles=[],
        ).to_dynamo_item()
        del item["exportTargetAdapterId"]
        assert OAuthProvider.from_dynamo_item(item).export_target_adapter_id is None


class TestExportTargetRegistry:
    def test_register_get_and_all(self):
        reg = ExportTargetRegistry()
        adapter = _StubExportAdapter()
        reg.register(adapter)
        assert reg.get(_STUB_KEY) is adapter
        assert adapter in reg.all()

    def test_duplicate_key_raises(self):
        reg = ExportTargetRegistry()
        reg.register(_StubExportAdapter())
        with pytest.raises(ValueError):
            reg.register(_StubExportAdapter())

    def test_adapters_for_provider_type_filters(self):
        reg = ExportTargetRegistry()
        reg.register(_StubExportAdapter())
        assert reg.adapters_for_provider_type(OAuthProviderType.GOOGLE)
        assert reg.adapters_for_provider_type(OAuthProviderType.SLACK) == []

    def test_default_registry_includes_google_drive(self):
        # The shipped registry now carries the Google Drive export adapter.
        drive = registry.get("google-drive")
        assert drive is not None
        assert drive.metadata.compatible_provider_types == (OAuthProviderType.GOOGLE,)


class TestValidateExportTargetAdapter:
    def test_empty_or_none_is_a_noop(self):
        _validate_export_target_adapter(None, OAuthProviderType.GOOGLE)
        _validate_export_target_adapter("", OAuthProviderType.GOOGLE)

    def test_valid_mapping_passes(self, registered_stub):
        _validate_export_target_adapter(_STUB_KEY, OAuthProviderType.GOOGLE)

    def test_unknown_adapter_is_rejected(self):
        with pytest.raises(HTTPException) as exc:
            _validate_export_target_adapter("nope", OAuthProviderType.GOOGLE)
        assert exc.value.status_code == 400
        assert "Unknown export-target adapter" in exc.value.detail

    def test_incompatible_provider_type_is_rejected(self, registered_stub):
        with pytest.raises(HTTPException) as exc:
            _validate_export_target_adapter(_STUB_KEY, OAuthProviderType.SLACK)
        assert exc.value.status_code == 400
        assert "not compatible" in exc.value.detail


class TestListExportTargetAdaptersEndpoint:
    @pytest.fixture
    def client(self) -> TestClient:
        app = FastAPI()
        app.include_router(adapter_routes.router)
        app.dependency_overrides[require_admin] = _admin
        return TestClient(app)

    def test_lists_shipped_adapters(self, client: TestClient, registered_stub):
        response = client.get("/export-target-adapters/")
        assert response.status_code == 200
        adapters = {a["key"]: a for a in response.json()["adapters"]}
        assert _STUB_KEY in adapters
        stub = adapters[_STUB_KEY]
        assert stub["displayName"] == "Test Export Target"
        assert stub["compatibleProviderTypes"] == ["google"]
        assert stub["requiredScopes"] == [_STUB_SCOPE]
        assert stub["supportedFormats"] == ["google_doc", "markdown"]

    def test_lists_shipped_google_drive_adapter(self, client: TestClient):
        response = client.get("/export-target-adapters/")
        assert response.status_code == 200
        adapters = {a["key"]: a for a in response.json()["adapters"]}
        assert "google-drive" in adapters
        drive = adapters["google-drive"]
        assert drive["displayName"] == "Google Drive"
        assert drive["requiredScopes"] == [
            "https://www.googleapis.com/auth/drive.file"
        ]
        assert drive["supportedFormats"] == ["google_doc", "markdown"]
