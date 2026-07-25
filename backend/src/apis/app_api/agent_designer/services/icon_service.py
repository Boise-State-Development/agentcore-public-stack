"""Agent Marketplace Phase 4 — the icon upload / read / remove paths (D5).

Authorization for an Agent's square icon, and the one place the three storage concerns
meet: validation (``apis.shared.assistants.icons``), the object write (S3), and the
``iconKey`` attribute write (``listing_repository.write_icon_key``).

Two authorization rules, and they are deliberately different from each other:

* **Writing an icon is editing the agent.** Owner *or* editor, matching ``PUT /agents/{id}``
  — an icon is presentation, not behavior, and an editor who may rewrite the instructions
  is not someone to stop at the avatar. (Admins reach the same field through D13's
  ``PATCH /admin/agents/{id}/listing``, which is a separate path with its own audit trail.)
* **Reading an icon follows the shelf, not the record.** A published agent's icon is
  readable by any authenticated user, because the store read already hands that agent's
  name, tagline and emoji to every browsing user — the icon is no new disclosure, and
  gating it on the record's own access check would render a broken image on the shelf of
  any published agent whose ``visibility`` is still PRIVATE.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Tuple

from apis.shared.assistants.icons import (
    IconError,
    IconStoreError,
    get_icon_store,
    icon_url,
    icon_version,
    normalize_icon,
)
from apis.shared.assistants.listing_repository import write_icon_key
from apis.shared.assistants.models import AgentIconResponse, Assistant
from apis.shared.assistants.service import (
    _get_assistant_cloud_without_ownership_check,
    get_assistant_with_access_check,
    resolve_assistant_permission,
)
from apis.shared.auth.models import User

logger = logging.getLogger(__name__)


class AgentIconError(Exception):
    """An icon operation the caller may not perform, or an image we cannot store.

    ``status_code`` maps to the HTTP response: 403 authorization, 404 missing agent or
    missing icon, 400 an image that fails the D5 limits, 503 storage unconfigured.
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


async def _load_for_write(agent_id: str, user: User) -> Assistant:
    """Load an Agent the caller may edit (owner or editor), or raise."""
    assistant, permission = await resolve_assistant_permission(
        assistant_id=agent_id, user_id=user.user_id, user_email=user.email
    )
    if not assistant:
        raise AgentIconError(f"Agent not found: {agent_id}", status_code=404)
    if permission not in ("owner", "editor"):
        raise AgentIconError(
            "You do not have permission to change this agent's icon.", status_code=403
        )
    return assistant


async def upload_icon(agent_id: str, content: bytes, user: User) -> AgentIconResponse:
    """Validate, store and record a new icon; return the key and its URL.

    The old object is deleted only *after* the record points at the new one, so a failure
    anywhere in the middle leaves the agent with its previous icon rather than none. The
    key is content-addressed, which makes re-uploading the same image idempotent — and
    makes the delete a no-op in exactly that case, which is why it is skipped when the
    key is unchanged.
    """
    assistant = await _load_for_write(agent_id, user)

    try:
        data, ext, content_type = normalize_icon(content)
    except IconError as e:
        raise AgentIconError(str(e), status_code=400) from e

    store = get_icon_store()
    try:
        key = store.put(agent_id=agent_id, content=data, ext=ext, content_type=content_type)
    except IconStoreError as e:
        logger.error(f"Icon storage unavailable for agent {agent_id}: {e}")
        raise AgentIconError("Icon storage is unavailable.", status_code=503) from e

    previous = assistant.icon_key
    await write_icon_key(agent_id, key, updated_at=_now())
    if previous and previous != key:
        store.delete(previous)

    logger.info(f"🖼️ User {user.user_id} set the icon for agent {agent_id}")
    return AgentIconResponse(agent_id=agent_id, icon_key=key, icon_url=icon_url(agent_id, key))


async def remove_icon(agent_id: str, user: User) -> AgentIconResponse:
    """Clear the icon, returning the agent to the generated gradient fallback (D5).

    Not in the spec's API table, and it has to exist: ``PATCH /admin/.../listing`` can only
    *replace* ``iconKey``, so without this an author who uploads an off-brand icon has no
    way back to the default the store was designed around.
    """
    assistant = await _load_for_write(agent_id, user)
    previous = assistant.icon_key

    await write_icon_key(agent_id, None, updated_at=_now())
    if previous:
        get_icon_store().delete(previous)

    logger.info(f"🖼️ User {user.user_id} removed the icon for agent {agent_id}")
    return AgentIconResponse(agent_id=agent_id, icon_key=None, icon_url=None)


async def read_icon(agent_id: str, user: User) -> Tuple[bytes, str, str]:
    """Return ``(bytes, content_type, version)`` for an agent's icon.

    Access follows the shelf: published → any authenticated user; otherwise the record's
    own access check (see the module docstring). The ``version`` is the key's digest,
    which the route serves as the ETag.
    """
    assistant = await _resolve_readable(agent_id, user)
    if not assistant.icon_key:
        raise AgentIconError("This agent has no icon.", status_code=404)

    try:
        data, content_type = get_icon_store().get(assistant.icon_key)
    except IconStoreError as e:
        # A key that outlived its object: 404 rather than 500, so the SPA's <img> error
        # path drops to the generated fallback instead of showing a broken tile.
        logger.warning(f"Icon object missing for agent {agent_id}: {e}")
        raise AgentIconError("This agent has no icon.", status_code=404) from e

    return data, content_type, icon_version(assistant.icon_key) or ""


async def _resolve_readable(agent_id: str, user: User) -> Assistant:
    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        raise RuntimeError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable is required")

    record = await _get_assistant_cloud_without_ownership_check(agent_id, table_name)
    if not record:
        raise AgentIconError(f"Agent not found: {agent_id}", status_code=404)
    if record.listing and record.listing.state == "published":
        return record

    assistant, _permission = await get_assistant_with_access_check(
        assistant_id=agent_id, user_id=user.user_id, user_email=user.email
    )
    if not assistant:
        raise AgentIconError(
            "Access denied: you do not have permission to access this agent", status_code=403
        )
    return assistant
