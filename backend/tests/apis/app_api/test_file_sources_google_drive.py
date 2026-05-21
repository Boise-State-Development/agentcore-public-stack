"""Tests for the Google Drive file-source adapter (mocked Drive API v3)."""

import httpx
import pytest

from apis.app_api.file_sources.adapters.google_drive import (
    ROOT_MY_DRIVE,
    GoogleDriveAdapter,
)
from apis.app_api.file_sources.models import (
    FileEntryType,
    FileSourceAuthError,
)


def _adapter(handler) -> GoogleDriveAdapter:
    """Build an adapter whose HTTP calls are served by `handler`."""
    return GoogleDriveAdapter(transport=httpx.MockTransport(handler))


class TestBrowse:
    @pytest.mark.asyncio
    async def test_browse_parses_entries_and_selectability(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["q"] = request.url.params.get("q")
            return httpx.Response(
                200,
                json={
                    "nextPageToken": "PAGE2",
                    "files": [
                        {"id": "f1", "name": "Folder", "mimeType": "application/vnd.google-apps.folder"},
                        {
                            "id": "f2",
                            "name": "Notes",
                            "mimeType": "application/vnd.google-apps.document",
                            "modifiedTime": "2026-05-01T00:00:00Z",
                            "version": "7",
                        },
                        {
                            "id": "f3",
                            "name": "report.pdf",
                            "mimeType": "application/pdf",
                            "size": "2048",
                        },
                        {
                            "id": "f4",
                            "name": "Survey",
                            "mimeType": "application/vnd.google-apps.form",
                        },
                    ],
                },
            )

        result = await _adapter(handler).browse("tok", ROOT_MY_DRIVE)

        assert captured["q"] == "'root' in parents and trashed = false"
        assert result.next_cursor == "PAGE2"
        by_id = {e.id: e for e in result.entries}
        # Folders navigate but are not selectable.
        assert by_id["f1"].type is FileEntryType.FOLDER
        assert by_id["f1"].selectable is False
        # Google-native doc is exportable, so it is selectable.
        assert by_id["f2"].selectable is True
        assert by_id["f2"].etag == "7"
        # Binary file is selectable with a parsed size.
        assert by_id["f3"].selectable is True
        assert by_id["f3"].size_bytes == 2048
        # Google Form has no export mapping, so it is not selectable.
        assert by_id["f4"].selectable is False

    @pytest.mark.asyncio
    async def test_search_escapes_quotes_in_query(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["q"] = request.url.params.get("q")
            return httpx.Response(200, json={"files": []})

        await _adapter(handler).search("tok", "O'Brien")

        assert captured["q"] == "name contains 'O\\'Brien' and trashed = false"


class TestDownload:
    @pytest.mark.asyncio
    async def test_native_doc_is_exported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/export"):
                assert request.url.params.get("mimeType") == (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )
                return httpx.Response(200, content=b"docx-bytes")
            return httpx.Response(
                200,
                json={"name": "Quarterly Notes", "mimeType": "application/vnd.google-apps.document"},
            )

        result = await _adapter(handler).download("tok", "doc1")

        assert result.content == b"docx-bytes"
        assert result.filename == "Quarterly Notes.docx"
        assert result.content_type.endswith("wordprocessingml.document")

    @pytest.mark.asyncio
    async def test_binary_file_is_downloaded_directly(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("alt") == "media":
                return httpx.Response(200, content=b"%PDF-bytes")
            return httpx.Response(
                200, json={"name": "report.pdf", "mimeType": "application/pdf"}
            )

        result = await _adapter(handler).download("tok", "file1")

        assert result.content == b"%PDF-bytes"
        assert result.filename == "report.pdf"
        assert result.content_type == "application/pdf"


class TestErrors:
    @pytest.mark.asyncio
    async def test_unauthorized_raises_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="invalid credentials")

        with pytest.raises(FileSourceAuthError):
            await _adapter(handler).browse("bad-token", ROOT_MY_DRIVE)


class TestListRoots:
    @pytest.mark.asyncio
    async def test_includes_synthetic_roots_and_shared_drives(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"drives": [{"id": "sd1", "name": "Marketing"}]}
            )

        roots = await _adapter(handler).list_roots("tok")

        names = [r.name for r in roots]
        assert names == ["My Drive", "Shared with me", "Marketing"]

    @pytest.mark.asyncio
    async def test_shared_drive_failure_is_tolerated(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="shared drives disabled")

        roots = await _adapter(handler).list_roots("tok")

        # The two synthetic roots still come back even when /drives fails.
        assert [r.id for r in roots] == ["root", "sharedWithMe"]
