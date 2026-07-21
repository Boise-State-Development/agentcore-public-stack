"""Workspace tools (list / read / write) over the user-files store.

A generic file surface for the agent: enumerate the user's files, read text
content on demand (bounded), and write text deliverables the user can
download. All storage flows through ``apis.shared.files.workspace`` — the
DynamoDB user-files table is the source of truth, so workspace files appear
in the chat's Files panel alongside uploads.

Design notes
------------
* Identity (``user_id`` / ``session_id``) is captured by closure via the
  ``make_*`` factories — the same pattern as the artifact, spreadsheet, and
  word-document tools (the Strands runtime here does NOT populate
  ``ToolContext.invocation_state`` with identity). The tools are injected
  per-request through ``extra_tools`` (see ``_build_workspace_tools`` in
  ``apis/inference_api/chat/routes.py``); they are deliberately NOT registered
  in ``builtin_tools/__init__`` because they need request-scoped identity.
* Binary files are returned by reference (presigned URL), never base64 — file
  bytes do not flow through the model (token-cost tenet, CLAUDE.md).
* ``workspace_write`` returns the same inline download-card contract as the
  word tool (``ui_type: "workspace_file"``) so the SPA renders a first-class
  download card.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from strands import tool

from apis.shared.files.workspace import (
    WorkspaceError,
    WorkspaceStorageNotConfiguredError,
    list_workspace_files,
    read_workspace_file,
    write_workspace_file,
)

logger = logging.getLogger(__name__)

_NO_STORAGE_MESSAGE = (
    "❌ File workspace storage is not configured "
    "(S3_USER_FILES_BUCKET_NAME is not set on the runtime)."
)


def _error(text: str) -> Dict[str, Any]:
    return {"content": [{"text": text}], "status": "error"}


def _success(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"content": [{"json": payload}], "status": "success"}


def make_workspace_list_tool(session_id: str, user_id: str):
    """Create a ``workspace_list`` tool bound to the given identity."""

    @tool
    async def workspace_list(scope: str = "session") -> Any:
        """List the files available in the user's workspace.

        Covers files the user attached and files tools have produced (Word
        documents, workspace writes, …). Entries with ``readable: true`` can
        be opened with ``workspace_read``; others are binary and move by
        reference.

        Args:
            scope: "session" (default) lists files from this conversation;
                "user" lists the user's files across all conversations
                (newest first) — use it when the user refers to a file from
                an earlier conversation.

        Returns:
            Files with upload_id, filename, mime_type, size_bytes, source,
            and readable. Capped at the newest ~100 entries
            (``truncated: true`` when more exist).
        """
        try:
            result = await list_workspace_files(user_id, session_id, scope=scope)
        except WorkspaceStorageNotConfiguredError:
            return _error(_NO_STORAGE_MESSAGE)
        except WorkspaceError as exc:
            return _error(f"❌ {exc}")
        except Exception as exc:  # noqa: BLE001 - surface conversationally
            logger.error(f"workspace_list error: {exc}")
            return _error(f"❌ Failed to list workspace files: {exc}")
        return _success(result)

    return workspace_list


def make_workspace_read_tool(session_id: str, user_id: str):
    """Create a ``workspace_read`` tool bound to the given identity."""

    @tool
    async def workspace_read(upload_id: str, offset: int = 0) -> Any:
        """Read a file from the user's workspace.

        Text files (plain, markdown, CSV, JSON, HTML) return their content
        inline, up to ~48KB per call — when ``truncated`` is true, call again
        with ``offset`` set to ``next_offset`` to continue. Binary files
        (PDF, Office, images) return metadata plus a download URL instead of
        content; hand tabular files to ``analyze_spreadsheet``.

        Args:
            upload_id: The file's id, as returned by ``workspace_list``.
            offset: Byte offset to continue a previous truncated read
                (default 0).

        Returns:
            For text: content, truncated flag, and next_offset. For binary:
            metadata and a short-lived download URL.
        """
        try:
            result = await read_workspace_file(user_id, upload_id, offset=offset)
        except WorkspaceStorageNotConfiguredError:
            return _error(_NO_STORAGE_MESSAGE)
        except WorkspaceError as exc:
            return _error(f"❌ {exc}")
        except Exception as exc:  # noqa: BLE001 - surface conversationally
            logger.error(f"workspace_read error: {exc}")
            return _error(f"❌ Failed to read file '{upload_id}': {exc}")
        return _success(result)

    return workspace_read


def make_workspace_write_tool(session_id: str, user_id: str):
    """Create a ``workspace_write`` tool bound to the given identity."""

    @tool
    async def workspace_write(
        filename: str,
        content: str,
        mime_type: str = "text/plain",
    ) -> Any:
        """Save a text file to the user's workspace with a download card.

        Use this for text deliverables the user should keep: markdown
        reports, CSV exports, JSON data, code listings. The file is saved to
        this chat's Files and presented with a download button. For Word
        documents use ``create_word_document``; for interactive documents
        use ``create_artifact``.

        Args:
            filename: File name, a single path segment (letters, numbers,
                dots, hyphens, underscores, spaces). The extension must match
                ``mime_type`` and is added automatically if omitted.
            content: The file content as plain text (max 1MB).
            mime_type: One of text/plain (default), text/markdown, text/csv,
                text/html, application/json.

        Returns:
            An inline download card. The file is also saved to this chat's
            Files, and each write creates a new file version (no overwrite).
        """
        try:
            result = await write_workspace_file(
                user_id, session_id, filename, content, mime_type=mime_type
            )
        except WorkspaceStorageNotConfiguredError:
            return _error(_NO_STORAGE_MESSAGE)
        except WorkspaceError as exc:
            return _error(f"❌ {exc}")
        except Exception as exc:  # noqa: BLE001 - surface conversationally
            logger.error(f"workspace_write error: {exc}")
            return _error(f"❌ Failed to save '{filename}': {exc}")

        # Same promoted download-card contract as word_document_tool — the
        # SPA routes ui_type "workspace_file" to the inline download card.
        return json.dumps(
            {
                "success": True,
                "ui_type": "workspace_file",
                "ui_display": "inline",
                "payload": {
                    "filename": result["filename"],
                    "download_url": result["download_url"],
                    "size_kb": result["size_kb"],
                },
                "summary": (
                    f"Saved {result['filename']} ({result['size_kb']}). "
                    "Also saved to this chat's Files."
                ),
            }
        )

    return workspace_write
