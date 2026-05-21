"""Tests for the file-source adapter registry and Document provenance fields."""

import pytest

from apis.shared.oauth.models import OAuthProviderType

from apis.app_api.documents.models import Document
from apis.app_api.file_sources.adapter import AdapterMetadata, FileSourceAdapter
from apis.app_api.file_sources.models import BrowseResult, DownloadedFile, SourceRoot
from apis.app_api.file_sources.registry import AdapterRegistry, registry


class _StubAdapter(FileSourceAdapter):
    """Minimal adapter used to exercise the registry in isolation."""

    def __init__(self, key: str, provider_type: OAuthProviderType) -> None:
        self._key = key
        self._provider_type = provider_type

    @property
    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            key=self._key,
            display_name=self._key,
            icon="stub",
            compatible_provider_types=(self._provider_type,),
            required_scopes=(),
        )

    async def list_roots(self, access_token):  # type: ignore[no-untyped-def]
        return [SourceRoot(id="root", name="Root")]

    async def browse(self, access_token, folder_id, cursor=None):  # type: ignore[no-untyped-def]
        return BrowseResult()

    async def search(self, access_token, query, cursor=None):  # type: ignore[no-untyped-def]
        return BrowseResult()

    async def download(self, access_token, file_id):  # type: ignore[no-untyped-def]
        return DownloadedFile(content=b"", filename="f", content_type="text/plain")


class TestAdapterRegistry:
    def test_register_and_get(self):
        reg = AdapterRegistry()
        adapter = _StubAdapter("box", OAuthProviderType.CUSTOM)
        reg.register(adapter)
        assert reg.get("box") is adapter
        assert reg.get("missing") is None

    def test_register_rejects_duplicate_key(self):
        reg = AdapterRegistry()
        reg.register(_StubAdapter("box", OAuthProviderType.CUSTOM))
        with pytest.raises(ValueError, match="Duplicate"):
            reg.register(_StubAdapter("box", OAuthProviderType.CUSTOM))

    def test_adapters_for_provider_type_filters(self):
        reg = AdapterRegistry()
        google = _StubAdapter("google-drive", OAuthProviderType.GOOGLE)
        microsoft = _StubAdapter("onedrive", OAuthProviderType.MICROSOFT)
        reg.register(google)
        reg.register(microsoft)
        assert reg.adapters_for_provider_type(OAuthProviderType.GOOGLE) == [google]
        assert reg.adapters_for_provider_type(OAuthProviderType.SLACK) == []

    def test_default_registry_ships_google_drive(self):
        adapter = registry.get("google-drive")
        assert adapter is not None
        assert adapter.metadata.compatible_provider_types == (OAuthProviderType.GOOGLE,)


class TestDocumentProvenance:
    def _base_fields(self):
        return {
            "documentId": "DOC-abc123",
            "assistantId": "AST-1",
            "filename": "report.pdf",
            "contentType": "application/pdf",
            "sizeBytes": 1024,
            "s3Key": "uploads/report.pdf",
            "status": "uploading",
            "createdAt": "2026-05-21T00:00:00Z",
            "updatedAt": "2026-05-21T00:00:00Z",
        }

    def test_provenance_defaults_to_none_for_device_uploads(self):
        doc = Document.model_validate(self._base_fields())
        assert doc.source_connector_id is None
        assert doc.source_adapter_key is None
        assert doc.source_file_id is None
        assert doc.source_etag is None
        assert doc.imported_by_user_id is None
        # Device uploads must not write empty provenance keys to DynamoDB.
        assert "sourceConnectorId" not in doc.model_dump(by_alias=True, exclude_none=True)

    def test_provenance_round_trips_through_aliases(self):
        fields = self._base_fields()
        fields.update(
            {
                "sourceConnectorId": "google",
                "sourceAdapterKey": "google-drive",
                "sourceFileId": "1AbC",
                "sourceEtag": "42",
                "importedByUserId": "user-9",
            }
        )
        doc = Document.model_validate(fields)
        assert doc.source_connector_id == "google"
        assert doc.source_adapter_key == "google-drive"
        assert doc.source_file_id == "1AbC"
        assert doc.source_etag == "42"
        assert doc.imported_by_user_id == "user-9"

        dumped = doc.model_dump(by_alias=True, exclude_none=True)
        assert dumped["sourceConnectorId"] == "google"
        assert dumped["sourceAdapterKey"] == "google-drive"
        assert dumped["sourceFileId"] == "1AbC"
        assert dumped["importedByUserId"] == "user-9"
