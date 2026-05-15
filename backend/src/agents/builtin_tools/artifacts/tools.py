"""Context-bound factories for the artifact authoring tools.

Identity is captured by closure (the codebase has no tool-execution
contextvar) — same pattern as the spreadsheet_analysis tools. Blocking
boto3 work is offloaded with ``asyncio.to_thread`` so the chat event
loop stays responsive under concurrent load.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from strands import tool

from . import service

logger = logging.getLogger(__name__)


def make_create_artifact_tool(session_id: str, user_id: str):
    @tool
    async def create_artifact(
        title: str,
        content: str,
        content_type: str = "text/html; charset=utf-8",
    ) -> dict[str, Any]:
        """Save a standalone document as a versioned artifact the user can open.

        Use this when you produce a self-contained deliverable the user
        will want to view, keep, or iterate on — an HTML page, a chart,
        an interactive widget, a formatted report.

        `content` MUST be a complete standalone HTML document (include
        `<!doctype html>` and a full `<html>` … `</html>`). It renders in
        a sandboxed iframe with a strict CSP: inline `<style>`/`<script>`
        are allowed, as are scripts from `https://cdn.tailwindcss.com`
        and `https://esm.sh`. It cannot make network calls. Do NOT wrap
        the document in markdown fences.

        Args:
            title: Short human-readable name shown in the artifacts list.
            content: The full HTML document.
            content_type: Defaults to text/html; leave as-is for HTML.

        Returns the new artifact id and version — reference the id if the
        user later asks you to change it (via update_artifact).
        """
        try:
            artifact_id, version = await asyncio.to_thread(
                service.create_artifact_record,
                user_id, session_id, title, content, content_type,
            )
        except service.ArtifactConfigError as exc:
            return {"content": [{"text": f"❌ Artifacts are not available: {exc}"}], "status": "error"}
        except service.ArtifactError as exc:
            return {"content": [{"text": f"❌ Failed to create artifact: {exc}"}], "status": "error"}
        return {
            "content": [{"text": (
                f'Created artifact "{title}" '
                f"(id: {artifact_id}, version {version})."
            )}],
            "status": "success",
        }

    return create_artifact


def make_update_artifact_tool(session_id: str, user_id: str):
    @tool
    async def update_artifact(
        artifact_id: str,
        content: str,
        title: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> dict[str, Any]:
        """Replace an existing artifact's content, creating a new version.

        Prior versions are kept immutably. Pass the `artifact_id`
        returned by an earlier create_artifact. `content` follows the
        same rules as create_artifact (a complete standalone HTML
        document, no markdown fences).

        Args:
            artifact_id: The artifact to update.
            content: The full replacement HTML document.
            title: Optional new title; unchanged if omitted.
            content_type: Optional; unchanged if omitted.

        Returns the new version number.
        """
        try:
            version = await asyncio.to_thread(
                service.update_artifact_record,
                user_id, artifact_id, content, title, content_type,
            )
        except service.ArtifactNotFoundError:
            return {"content": [{"text": (
                f"❌ Artifact {artifact_id} was not found. Use the id "
                f"returned by create_artifact."
            )}], "status": "error"}
        except service.ArtifactConfigError as exc:
            return {"content": [{"text": f"❌ Artifacts are not available: {exc}"}], "status": "error"}
        except service.ArtifactError as exc:
            return {"content": [{"text": f"❌ Failed to update artifact: {exc}"}], "status": "error"}
        return {
            "content": [{"text": (
                f"Updated artifact {artifact_id} to version {version}."
            )}],
            "status": "success",
        }

    return update_artifact
