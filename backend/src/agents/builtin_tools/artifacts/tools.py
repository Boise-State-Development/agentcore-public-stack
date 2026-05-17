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
        an interactive widget, a formatted report, or a written document.

        Two authoring modes:

        - HTML (default): `content` MUST be a complete standalone HTML
          document (include `<!doctype html>` and a full `<html>` …
          `</html>`). It renders in a sandboxed iframe with a strict CSP:
          inline `<style>`/`<script>` are allowed, as are scripts from
          `https://cdn.tailwindcss.com`, `https://esm.sh`,
          `https://cdn.jsdelivr.net`, and `https://unpkg.com` — no
          other origin loads. The page cannot make `fetch`/XHR calls
          (CSP `connect-src` is blocked), so inline any data.

          Load JS libraries from one of those CDNs and pin a version.
          Chart.js note: use an auto-registering build or charts
          render blank — e.g.
          `import Chart from "https://esm.sh/chart.js@4/auto"` inside a
          `<script type="module">`, or the UMD bundle
          `<script src="https://cdn.jsdelivr.net/npm/chart.js@4">`. A
          bare `https://esm.sh/chart.js` import (no `/auto`) silently
          fails to draw.

        - Markdown: pass `content_type="text/markdown"` and provide raw
          GitHub-flavored Markdown as `content`. Do NOT add an HTML
          shell — the system renders the Markdown for the user. Prefer
          this for prose, reports, and documentation.

        Either way, do NOT wrap `content` in markdown code fences.

        Args:
            title: Short human-readable name shown in the artifacts list.
            content: The full HTML document, or raw Markdown when
                content_type is text/markdown.
            content_type: Defaults to text/html. Pass "text/markdown"
                to author a Markdown document instead.

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
        same rules as create_artifact: a complete standalone HTML
        document, or raw Markdown when content_type is text/markdown; no
        markdown code fences either way. If content_type is omitted the
        artifact's existing type (HTML or Markdown) is kept.

        Args:
            artifact_id: The artifact to update.
            content: The full replacement HTML document, or raw Markdown
                for a Markdown artifact.
            title: Optional new title; unchanged if omitted.
            content_type: Optional; unchanged if omitted. Pass
                "text/markdown" to switch an artifact to Markdown.

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
