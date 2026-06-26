"""Tests for the Google Drive export-target adapter (mocked Drive API v3)."""

from __future__ import annotations

import json
from typing import Dict, List, Tuple

import httpx
import pytest

from apis.app_api.export_targets.adapters.google_drive import GoogleDriveExportAdapter
from apis.app_api.export_targets.models import (
    ExportFormat,
    ExportTargetAuthError,
)


def _adapter(handler) -> GoogleDriveExportAdapter:
    return GoogleDriveExportAdapter(transport=httpx.MockTransport(handler))


def _parse_multipart(body: bytes) -> Tuple[Dict, str, bytes]:
    """Split a Drive multipart/related body into (metadata, media_mime, media)."""
    text = body.decode("utf-8", errors="replace")
    # Parts are separated by the boundary; the metadata is JSON, the media
    # follows its own Content-Type header.
    meta_start = text.index("{")
    meta_end = text.index("}", meta_start) + 1
    metadata = json.loads(text[meta_start:meta_end])
    media_marker = "Content-Type: "
    second = text.index(media_marker, meta_end)
    media_mime = text[second + len(media_marker):].split("\r\n", 1)[0]
    media = text.split("\r\n\r\n", 2)[-1].rsplit("\r\n--", 1)[0]
    return metadata, media_mime, media.encode("utf-8")


class TestCreateDocument:
    @pytest.mark.asyncio
    async def test_google_doc_into_existing_folder(self):
        captured: Dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/drive/v3/files" and request.method == "GET":
                captured["folder_query"] = request.url.params.get("q")
                return httpx.Response(200, json={"files": [{"id": "folder-1", "name": "AI Conversations"}]})
            if request.url.path == "/upload/drive/v3/files":
                captured["upload_body"] = request.content
                captured["upload_type"] = request.url.params.get("uploadType")
                return httpx.Response(
                    200,
                    json={"id": "doc-1", "name": "My Chat", "webViewLink": "https://docs/doc-1"},
                )
            return httpx.Response(500, text=f"unexpected {request.method} {request.url}")

        result = await _adapter(handler).create_document(
            "tok",
            content=b"<h1>hi</h1>",
            name="My Chat",
            source_mime_type="text/html",
            target_format=ExportFormat.GOOGLE_DOC,
        )

        assert result.file_id == "doc-1"
        assert result.web_view_link == "https://docs/doc-1"
        assert captured["upload_type"] == "multipart"
        assert "AI Conversations" in captured["folder_query"]

        metadata, media_mime, media = _parse_multipart(captured["upload_body"])
        assert metadata["mimeType"] == "application/vnd.google-apps.document"
        assert metadata["parents"] == ["folder-1"]
        assert metadata["name"] == "My Chat"
        assert media_mime == "text/html"
        assert b"<h1>hi</h1>" in media

    @pytest.mark.asyncio
    async def test_creates_folder_when_absent(self):
        calls: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            if request.url.path == "/drive/v3/files" and request.method == "GET":
                return httpx.Response(200, json={"files": []})
            if request.url.path == "/drive/v3/files" and request.method == "POST":
                body = json.loads(request.content)
                assert body["mimeType"] == "application/vnd.google-apps.folder"
                return httpx.Response(200, json={"id": "folder-new"})
            if request.url.path == "/upload/drive/v3/files":
                metadata, _, _ = _parse_multipart(request.content)
                assert metadata["parents"] == ["folder-new"]
                return httpx.Response(200, json={"id": "doc-2", "name": "C"})
            return httpx.Response(500, text="unexpected")

        result = await _adapter(handler).create_document(
            "tok",
            content=b"# c",
            name="C",
            source_mime_type="text/markdown",
            target_format=ExportFormat.MARKDOWN,
        )

        assert result.file_id == "doc-2"
        assert result.web_view_link is None
        assert "POST /drive/v3/files" in calls  # folder was created

    @pytest.mark.asyncio
    async def test_explicit_parent_skips_folder_lookup(self):
        calls: List[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(f"{request.method} {request.url.path}")
            if request.url.path == "/upload/drive/v3/files":
                metadata, media_mime, _ = _parse_multipart(request.content)
                assert metadata["parents"] == ["given-folder"]
                assert metadata["mimeType"] == "text/markdown"
                assert media_mime == "text/markdown"
                return httpx.Response(200, json={"id": "doc-3", "name": "C"})
            return httpx.Response(500, text="unexpected")

        result = await _adapter(handler).create_document(
            "tok",
            content=b"# c",
            name="C",
            source_mime_type="text/markdown",
            target_format=ExportFormat.MARKDOWN,
            parent_id="given-folder",
        )

        assert result.file_id == "doc-3"
        assert calls == ["POST /upload/drive/v3/files"]  # no folder search/create

    @pytest.mark.asyncio
    async def test_auth_error_maps_to_export_target_auth_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="forbidden")

        with pytest.raises(ExportTargetAuthError):
            await _adapter(handler).create_document(
                "tok",
                content=b"x",
                name="C",
                source_mime_type="text/html",
                target_format=ExportFormat.GOOGLE_DOC,
                parent_id="f1",
            )


class TestListDestinations:
    @pytest.mark.asyncio
    async def test_lists_my_drive_and_shared_drives(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"drives": [{"id": "sd1", "name": "Team"}]})

        dests = await _adapter(handler).list_destinations("tok")
        names = [d.name for d in dests]
        assert "My Drive" in names
        assert "Team" in names

    @pytest.mark.asyncio
    async def test_shared_drive_failure_is_tolerated(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="no shared drives")

        dests = await _adapter(handler).list_destinations("tok")
        assert [d.name for d in dests] == ["My Drive"]
