"""Google Drive export-target adapter (Drive API v3).

Writes a rendered document into the user's Drive. Uses the least-privilege
`drive.file` scope: the app can create files and only ever sees the files it
created — it cannot read the rest of the user's Drive. A `GOOGLE_DOC` export
uploads an HTML body against the Google Doc MIME type so Drive converts it to
a native Doc; a `MARKDOWN` export uploads the bytes as a plain `.md` file.

When no destination folder is given, files land in a single app-owned folder
(find-or-create by name) so exports don't clutter the user's Drive root. With
`drive.file` the folder search only matches files the app itself created,
which is exactly the folder we made on a previous export.

`transport` is injectable so tests can drive the adapter with an
`httpx.MockTransport`; production constructs it with no arguments. Mirrors the
file-source Drive adapter's HTTP conventions.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from apis.shared.oauth.models import OAuthProviderType

from apis.app_api.export_targets.adapter import (
    ExportTargetAdapter,
    ExportTargetMetadata,
)
from apis.app_api.export_targets.models import (
    CreatedFile,
    ExportDestination,
    ExportFormat,
    ExportTargetAuthError,
    ExportTargetError,
    ExportTargetNotFoundError,
)

logger = logging.getLogger(__name__)

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"

_FOLDER_MIME = "application/vnd.google-apps.folder"
_GOOGLE_DOC_MIME = "application/vnd.google-apps.document"

# Single app-owned folder exports land in when no destination is chosen.
# Overridable per-deployment; the default is intentionally generic.
_DEFAULT_FOLDER_ENV = "EXPORT_DRIVE_FOLDER_NAME"
_DEFAULT_FOLDER_NAME = "AI Conversations"

# Fixed multipart boundary — the body never contains it, and a constant keeps
# uploads deterministic (and easy to assert in tests).
_MULTIPART_BOUNDARY = "agentcore-export-boundary"

_RETURN_FIELDS = "id,name,webViewLink"
_TIMEOUT = httpx.Timeout(30.0)


def _escape_query_value(value: str) -> str:
    """Escape a value for safe interpolation into a Drive `q` parameter."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveExportAdapter(ExportTargetAdapter):
    """Drive API v3 export adapter."""

    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._transport = transport

    @property
    def metadata(self) -> ExportTargetMetadata:
        return ExportTargetMetadata(
            key="google-drive",
            display_name="Google Drive",
            icon="google-drive",
            compatible_provider_types=(OAuthProviderType.GOOGLE,),
            required_scopes=(DRIVE_FILE_SCOPE,),
            # PDF is intentionally omitted until the render step can produce it
            # (needs a renderer dependency we don't ship yet).
            supported_formats=(ExportFormat.GOOGLE_DOC, ExportFormat.MARKDOWN),
        )

    async def list_destinations(self, access_token: str) -> List[ExportDestination]:
        # `drive.file` cannot enumerate the user's folders, so the real folder
        # picker is driven by the combined-scope connector's file-source
        # `browse` (see the spec). This returns just the implicit roots.
        roots = [ExportDestination(id="root", name="My Drive")]
        try:
            data = await self._get_json(
                access_token,
                "/drives",
                params={"pageSize": 100, "fields": "drives(id,name)"},
            )
            for drive in data.get("drives", []):
                roots.append(ExportDestination(id=drive["id"], name=drive["name"]))
        except ExportTargetError as err:
            logger.info("Skipping shared drives for export destinations: %s", err)
        return roots

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
        if target_format == ExportFormat.GOOGLE_DOC:
            file_mime = _GOOGLE_DOC_MIME
        elif target_format == ExportFormat.MARKDOWN:
            file_mime = source_mime_type or "text/markdown"
        else:
            raise ExportTargetError(
                f"Google Drive export does not support format '{target_format}'"
            )

        if parent_id is None:
            parent_id = await self._ensure_default_folder(access_token)

        metadata: Dict[str, Any] = {
            "name": name,
            "mimeType": file_mime,
            "parents": [parent_id],
        }
        body, content_type = _multipart_related(metadata, content, source_mime_type)

        data = await self._request_json(
            access_token,
            "POST",
            f"{DRIVE_UPLOAD_BASE}/files",
            params={
                "uploadType": "multipart",
                "supportsAllDrives": "true",
                "fields": _RETURN_FIELDS,
            },
            content=body,
            content_type=content_type,
        )
        return CreatedFile(
            file_id=data.get("id", ""),
            name=data.get("name", name),
            web_view_link=data.get("webViewLink"),
        )

    # ── internals ───────────────────────────────────────────────────────────

    async def _ensure_default_folder(self, access_token: str) -> str:
        """Return the id of the app's export folder, creating it if needed."""
        folder_name = os.environ.get(_DEFAULT_FOLDER_ENV, _DEFAULT_FOLDER_NAME)
        escaped = _escape_query_value(folder_name)
        data = await self._get_json(
            access_token,
            "/files",
            params={
                "q": (
                    f"name = '{escaped}' and mimeType = '{_FOLDER_MIME}' "
                    "and trashed = false"
                ),
                "spaces": "drive",
                "fields": "files(id,name)",
                "pageSize": 1,
            },
        )
        files = data.get("files", [])
        if files:
            return files[0]["id"]

        created = await self._request_json(
            access_token,
            "POST",
            f"{DRIVE_API_BASE}/files",
            params={"fields": "id"},
            content=json.dumps(
                {"name": folder_name, "mimeType": _FOLDER_MIME}
            ).encode("utf-8"),
            content_type="application/json; charset=UTF-8",
        )
        return created["id"]

    @staticmethod
    def _auth_headers(access_token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        status = response.status_code
        snippet = response.text[:300]
        if status in (401, 403):
            raise ExportTargetAuthError(
                f"Google Drive rejected the request ({status}): {snippet}"
            )
        if status == 404:
            raise ExportTargetNotFoundError(
                f"Google Drive resource not found: {snippet}"
            )
        raise ExportTargetError(f"Google Drive request failed ({status}): {snippet}")

    async def _get_json(
        self, access_token: str, path: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        async with httpx.AsyncClient(
            transport=self._transport, timeout=_TIMEOUT
        ) as client:
            try:
                response = await client.get(
                    f"{DRIVE_API_BASE}{path}",
                    params=params,
                    headers=self._auth_headers(access_token),
                )
            except httpx.HTTPError as err:
                raise ExportTargetError(f"Google Drive request error: {err}") from err
        self._raise_for_status(response)
        data: Dict[str, Any] = response.json()
        return data

    async def _request_json(
        self,
        access_token: str,
        method: str,
        url: str,
        *,
        params: Dict[str, Any],
        content: bytes,
        content_type: str,
    ) -> Dict[str, Any]:
        headers = self._auth_headers(access_token)
        headers["Content-Type"] = content_type
        async with httpx.AsyncClient(
            transport=self._transport, timeout=_TIMEOUT
        ) as client:
            try:
                response = await client.request(
                    method, url, params=params, headers=headers, content=content
                )
            except httpx.HTTPError as err:
                raise ExportTargetError(f"Google Drive upload error: {err}") from err
        self._raise_for_status(response)
        data: Dict[str, Any] = response.json()
        return data


def _multipart_related(
    metadata: Dict[str, Any], media: bytes, media_mime: str
) -> tuple[bytes, str]:
    """Build a Drive `multipart/related` upload body (metadata + media)."""
    boundary = _MULTIPART_BOUNDARY
    preamble = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {media_mime}\r\n\r\n"
    ).encode("utf-8")
    closing = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = preamble + media + closing
    return body, f"multipart/related; boundary={boundary}"
