"""Duplicate an Agent for its owner or an editor.

The copy is built from the raw record ``resolve_assistant_permission`` returns, which is
the editable draft an owner/editor sees in the Agent Designer (not the approved
marketplace snapshot). It belongs to the caller, is PRIVATE, and gets a new id.

Copied: name (prefixed), description, instructions, tags, starters, emoji, model
settings, bindings (minus knowledge-base bindings), citation/download flags, and the
icon. Not copied: the knowledge base (documents, vectors, sync policies, crawl jobs,
managed KB), shares, listing state, pins, versions, reports and usage count. Pins are
pointers by design, so a viewer who merely added a public agent cannot duplicate it.
"""

import asyncio
import logging

from apis.app_api.agent_designer.services.binding_validation import (
    BindingValidationError,
    validate_agent_write,
)
from apis.shared.assistants.icons import get_icon_store
from apis.shared.assistants.listing_repository import write_icon_key
from apis.shared.assistants.models import Assistant
from apis.shared.assistants.service import (
    create_assistant,
    is_project_harness,
    resolve_assistant_permission,
)
from apis.shared.auth.models import User
from apis.shared.timestamps import utc_now_iso

logger = logging.getLogger(__name__)

_DUPLICATE_PERMISSIONS = ("owner", "editor")


class AgentDuplicationError(Exception):
    """A duplicate was refused; ``status_code`` is the HTTP status the route returns."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def duplicate_agent(agent_id: str, user: User) -> Assistant:
    """Create a PRIVATE copy of ``agent_id`` owned by ``user``."""
    source, permission = await resolve_assistant_permission(
        assistant_id=agent_id, user_id=user.user_id, user_email=user.email
    )
    if source is None:
        raise AgentDuplicationError(f"Agent not found: {agent_id}", 404)
    if is_project_harness(source):
        raise AgentDuplicationError("This agent belongs to a project and can't be duplicated.", 409)
    if permission not in _DUPLICATE_PERMISSIONS:
        raise AgentDuplicationError("You do not have permission to duplicate this agent", 403)

    # The knowledge base is not copied; the new agent gets its own empty one.
    bindings = None if source.bindings is None else [b for b in source.bindings if b.kind != "knowledge_base"]

    try:
        await validate_agent_write(user, bindings=bindings, model_settings=source.model_settings)
    except BindingValidationError as e:
        raise AgentDuplicationError(f"This agent can't be duplicated: {e.message}", e.status_code) from e

    new = await create_assistant(
        owner_id=user.user_id,
        owner_name=user.name,
        name=f"Copy of {source.name}",
        description=source.description,
        instructions=source.instructions,
        visibility="PRIVATE",
        tags=list(source.tags or []),
        starters=list(source.starters or []),
        emoji=source.emoji,
        bindings=bindings,
        model_settings=source.model_settings,
        show_citations=source.show_citations,
        allow_document_download=source.allow_document_download,
    )
    await _copy_icon(source, new)
    return new


async def _copy_icon(source: Assistant, new: Assistant) -> None:
    """Copy the source's current icon to the new agent. Best effort: never raises."""
    if not source.icon_key:
        return
    try:
        store = get_icon_store()
        data, content_type = await asyncio.to_thread(store.get, source.icon_key)
        ext = source.icon_key.rsplit(".", 1)[-1]
        key = await asyncio.to_thread(
            lambda: store.put(agent_id=new.assistant_id, content=data, ext=ext, content_type=content_type)
        )
        await write_icon_key(new.assistant_id, key, updated_at=utc_now_iso())
        new.icon_key = key
    except Exception:
        logger.warning(f"Failed to copy icon from agent {source.assistant_id} to {new.assistant_id}", exc_info=True)
