"""Google Drive file-source adapter (Drive API v3).

Maps Drive's API onto the normalized file-source contract:
- roots: My Drive, Shared with me, and each shared drive
- Google-native docs (Docs/Sheets/Slides) have no downloadable bytes, so
  `download` exports them to an Office/PDF format Docling can ingest
- shared-drive content is included everywhere via `supportsAllDrives`
"""

import logging
from typing import Any, Dict, List, Optional

import httpx

from apis.shared.oauth.models import OAuthProviderType

from apis.app_api.file_sources.adapter import AdapterMetadata, FileSourceAdapter
from apis.app_api.file_sources.models import (
    BrowseResult,
    DownloadedFile,
    FileEntry,
    FileEntryType,
    FileSourceAuthError,
    FileSourceError,
    FileSourceNotFoundError,
    SourceRoot,
)

logger = logging.getLogger(__name__)

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

_FOLDER_MIME = "application/vnd.google-apps.folder"
_NATIVE_PREFIX = "application/vnd.google-apps."

# Synthetic root identifiers (Drive uses "root" for My Drive natively).
ROOT_MY_DRIVE = "root"
ROOT_SHARED_WITH_ME = "sharedWithMe"

# Google-native MIME type -> (export MIME type, file extension). Native docs
# without an entry here (Forms, Sites, shortcuts) cannot be ingested.
_EXPORT_MAP: Dict[str, tuple] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}

_PAGE_SIZE = 100
_LIST_FIELDS = "nextPageToken,files(id,name,mimeType,size,modifiedTime,version)"
_TIMEOUT = httpx.Timeout(30.0)


def _escape_query_value(value: str) -> str:
    """Escape a value for safe interpolation into a Drive `q` parameter."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveAdapter(FileSourceAdapter):
    """Drive API v3 adapter.

    `transport` is injectable so tests can drive the adapter with an
    `httpx.MockTransport`; production code constructs it with no arguments.
    """

    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self._transport = transport

    @property
    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            key="google-drive",
            display_name="Google Drive",
            icon="google-drive",
            compatible_provider_types=(OAuthProviderType.GOOGLE,),
            required_scopes=(DRIVE_READONLY_SCOPE,),
        )

    async def list_roots(self, access_token: str) -> List[SourceRoot]:
        roots = [
            SourceRoot(id=ROOT_MY_DRIVE, name="My Drive"),
            SourceRoot(id=ROOT_SHARED_WITH_ME, name="Shared with me"),
        ]
        # Shared drives are optional — a user may have none, or the org may
        # not use them. A failure here must not break the common case.
        try:
            data = await self._get_json(
                access_token,
                "/drives",
                params={"pageSize": _PAGE_SIZE, "fields": "drives(id,name)"},
            )
            for drive in data.get("drives", []):
                roots.append(SourceRoot(id=drive["id"], name=drive["name"]))
        except FileSourceError as err:
            logger.info("Skipping shared drives for Drive roots: %s", err)
        return roots

    async def browse(
        self, access_token: str, folder_id: str, cursor: Optional[str] = None
    ) -> BrowseResult:
        params: Dict[str, Any] = {
            "pageSize": _PAGE_SIZE,
            "fields": _LIST_FIELDS,
            "orderBy": "folder,name",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if cursor:
            params["pageToken"] = cursor
        if folder_id == ROOT_SHARED_WITH_ME:
            params["q"] = "sharedWithMe and trashed = false"
            params["corpora"] = "user"
        else:
            params["q"] = f"'{_escape_query_value(folder_id)}' in parents and trashed = false"
            params["corpora"] = "allDrives"
        data = await self._get_json(access_token, "/files", params=params)
        return self._to_browse_result(data)

    async def search(
        self, access_token: str, query: str, cursor: Optional[str] = None
    ) -> BrowseResult:
        escaped = _escape_query_value(query)
        params: Dict[str, Any] = {
            "pageSize": _PAGE_SIZE,
            "fields": _LIST_FIELDS,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "corpora": "allDrives",
            "q": f"name contains '{escaped}' and trashed = false",
        }
        if cursor:
            params["pageToken"] = cursor
        data = await self._get_json(access_token, "/files", params=params)
        return self._to_browse_result(data)

    async def download(self, access_token: str, file_id: str) -> DownloadedFile:
        meta = await self._get_json(
            access_token,
            f"/files/{file_id}",
            params={"fields": "name,mimeType", "supportsAllDrives": "true"},
        )
        name = meta.get("name", file_id)
        mime = meta.get("mimeType", "")

        if mime in _EXPORT_MAP:
            export_mime, ext = _EXPORT_MAP[mime]
            content = await self._get_bytes(
                access_token,
                f"/files/{file_id}/export",
                params={"mimeType": export_mime},
            )
            filename = name if name.endswith(ext) else f"{name}{ext}"
            return DownloadedFile(content=content, filename=filename, content_type=export_mime)

        if mime.startswith(_NATIVE_PREFIX):
            raise FileSourceError(
                f"Google '{mime}' files cannot be exported for indexing"
            )

        content = await self._get_bytes(
            access_token,
            f"/files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
        return DownloadedFile(
            content=content,
            filename=name,
            content_type=mime or "application/octet-stream",
        )

    # ── internals ───────────────────────────────────────────────────────────

    def _to_browse_result(self, data: Dict[str, Any]) -> BrowseResult:
        entries = [self._to_entry(f) for f in data.get("files", [])]
        # Breadcrumbs are tracked client-side as the user navigates; the
        # adapter returns a flat page.
        return BrowseResult(entries=entries, next_cursor=data.get("nextPageToken"))

    @staticmethod
    def _to_entry(item: Dict[str, Any]) -> FileEntry:
        mime = item.get("mimeType", "")
        if mime == _FOLDER_MIME:
            return FileEntry(
                id=item["id"],
                name=item.get("name", ""),
                type=FileEntryType.FOLDER,
                mime_type=mime,
                selectable=False,
            )
        is_native = mime.startswith(_NATIVE_PREFIX)
        selectable = (not is_native) or (mime in _EXPORT_MAP)
        raw_size = item.get("size")
        return FileEntry(
            id=item["id"],
            name=item.get("name", ""),
            type=FileEntryType.FILE,
            mime_type=mime,
            size_bytes=int(raw_size) if raw_size is not None else None,
            modified_at=item.get("modifiedTime"),
            etag=item.get("version"),
            selectable=selectable,
        )

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
            raise FileSourceAuthError(
                f"Google Drive rejected the request ({status}): {snippet}"
            )
        if status == 404:
            raise FileSourceNotFoundError(f"Google Drive resource not found: {snippet}")
        raise FileSourceError(f"Google Drive request failed ({status}): {snippet}")

    async def _get_json(
        self, access_token: str, path: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        async with httpx.AsyncClient(transport=self._transport, timeout=_TIMEOUT) as client:
            try:
                response = await client.get(
                    f"{DRIVE_API_BASE}{path}",
                    params=params,
                    headers=self._auth_headers(access_token),
                )
            except httpx.HTTPError as err:
                raise FileSourceError(f"Google Drive request error: {err}") from err
        self._raise_for_status(response)
        data: Dict[str, Any] = response.json()
        return data

    async def _get_bytes(
        self, access_token: str, path: str, params: Dict[str, Any]
    ) -> bytes:
        async with httpx.AsyncClient(transport=self._transport, timeout=_TIMEOUT) as client:
            try:
                response = await client.get(
                    f"{DRIVE_API_BASE}{path}",
                    params=params,
                    headers=self._auth_headers(access_token),
                )
            except httpx.HTTPError as err:
                raise FileSourceError(f"Google Drive download error: {err}") from err
        self._raise_for_status(response)
        return response.content
