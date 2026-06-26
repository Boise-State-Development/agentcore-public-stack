"""Context-bound factory for the `save_conversation` tool.

Identity + destination are captured by closure (the codebase has no
tool-execution contextvar) — same pattern as the artifact / spreadsheet tools.
The tool reuses the shared export core: it pages the transcript, renders it,
resolves the user's OAuth token, and creates the document via the connector's
export-target adapter.

OAuth consent is handled *before* the tool runs by the agent's
`OAuthConsentHook` (the route wires `save_conversation -> provider_id` into the
hook's `tool_use_provider_lookup`). By the time this tool executes the vault
holds a usable token; the token resolution here is the same call the gate made.
If consent is somehow still missing (tool enabled without the gate wiring), the
tool returns an actionable error rather than failing opaquely.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from strands import tool

from apis.shared.export_targets.adapter import ExportTargetAdapter
from apis.shared.export_targets.models import (
    ExportFormat,
    ExportInclude,
    ExportTargetError,
)
from apis.shared.export_targets.render import render_transcript
from apis.shared.export_targets.service import resolve_export_target_token
from apis.shared.export_targets.transcript import collect_transcript
from apis.shared.oauth.models import OAuthProvider
from apis.shared.sessions.metadata import add_export_receipt, get_session_metadata
from apis.shared.sessions.models import ExportReceipt

logger = logging.getLogger(__name__)


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"text": f"❌ {text}"}], "status": "error"}


def make_save_conversation_tool(
    session_id: str,
    user_id: str,
    provider: OAuthProvider,
    adapter: ExportTargetAdapter,
):
    """Build a `save_conversation` tool bound to one session + export target.

    Args:
        session_id: The conversation to export.
        user_id: The exporting user (token owner + ownership key).
        provider: The OAuth connector mapped as the export target.
        adapter: The export-target adapter resolved from `provider`.
    """
    supported = adapter.metadata.supported_formats
    destination = provider.display_name
    # Enumerated in error messages so the model can correct an invalid choice.
    format_values = ", ".join(f"'{f.value}'" for f in supported)

    @tool
    async def save_conversation(format: str = "google_doc") -> dict[str, Any]:
        """Save this conversation to the user's connected app as a document.

        Use when the user asks to save, export, or send this chat to their
        connected destination (e.g. "save this conversation to my Google
        Drive"). Saves the full transcript so far and returns a link to open
        it. The user only needs to authorize the connection once; if they
        haven't yet, they'll be prompted before the save runs.

        Args:
            format: Output format. Defaults to 'google_doc' (a native, styled
                document); 'markdown' produces a portable Markdown file. If you
                pass an unsupported value the error lists the valid choices.

        Returns a confirmation with a link to open the saved document.
        """
        try:
            export_format = ExportFormat(format)
        except ValueError:
            return _error(
                f"'{format}' is not a supported format. Choose one of: "
                f"{format_values}."
            )
        if export_format not in supported:
            return _error(
                f"{destination} cannot export to '{format}'. Choose one of: "
                f"{format_values}."
            )

        # Resolve the user's token. The consent gate runs first, so this is a
        # vault hit; requires_consent here means the gate wasn't wired in.
        try:
            token_result = await resolve_export_target_token(provider, user_id)
        except Exception as exc:  # workload/callback context unavailable
            logger.warning("save_conversation token resolution failed: %s", exc)
            return _error(
                "Couldn't reach the connection service to save this "
                "conversation. Try again shortly."
            )
        if token_result.requires_consent or not token_result.access_token:
            return _error(
                f"Connect {destination} first, then ask me to save again."
            )

        metadata = await get_session_metadata(session_id, user_id)
        title = (metadata.title if metadata else None) or "Conversation"

        messages = await collect_transcript(session_id, user_id)
        try:
            rendered = render_transcript(
                title, messages, export_format, ExportInclude()
            )
        except ValueError as exc:
            return _error(str(exc))

        try:
            created = await adapter.create_document(
                token_result.access_token,
                content=rendered.content,
                name=rendered.suggested_name,
                source_mime_type=rendered.mime_type,
                target_format=export_format,
                parent_id=None,
            )
        except ExportTargetError as exc:
            logger.warning("save_conversation create_document failed: %s", exc)
            return _error(
                f"{destination} rejected the request. Reconnect the app and "
                "try again."
            )

        receipt = ExportReceipt(
            connector_id=provider.provider_id,
            adapter_key=adapter.metadata.key,
            format=export_format.value,
            file_id=created.file_id,
            file_name=created.name,
            web_view_link=created.web_view_link,
            exported_at=datetime.now(timezone.utc).isoformat(),
        )
        # Best-effort persistence (mirrors the export endpoint): a metadata
        # write hiccup must not fail a save that already succeeded.
        await add_export_receipt(session_id, user_id, receipt)

        link = (
            f" Open it here: {created.web_view_link}"
            if created.web_view_link
            else ""
        )
        return {
            "content": [
                {
                    "text": (
                        f'Saved "{created.name}" to {destination}.{link}'
                    )
                }
            ],
            "status": "success",
        }

    return save_conversation
