"""Agent Designer Phase 2 — the bindable-primitives catalog (D4).

The read/list dual of ``binding_validation``: where that module *validates* an Agent
write against the author's per-primitive access, this one *lists* what the caller may
bind, composing the **same** existing per-primitive access services. It invents no new
RBAC — it is the palette **and** the design-time enforcement point (D4).

One entry point, ``list_bindable(kind, user)``, fans out to the primitive's existing
list + access service and projects each result into the uniform ``BindableItem`` shape
so every Designer picker consumes one contract. Services are injectable for testing,
mirroring ``validate_agent_write``.

Per-kind sourcing (see the write-side rules in ``binding_validation``):

- ``model``          → ``filter_accessible_models(user, list_all_managed_models())``.
- ``tool``           → ``ToolCatalogService.get_user_accessible_tools(user)`` (already
  RBAC-filtered + MCP-server-tool grouped).
- ``skill``          → ``resolve_accessible_skill_ids(user)`` hydrated via
  ``batch_get_skills`` — **empty when ``skills_enabled()`` is off**.
- ``knowledge_base`` → **empty**. The KB is welded to the agent and its index is not
  user-configurable, so it is never author-settable (``binding_validation`` rejects an
  explicit KB write); the compat layer synthesizes it on read. Nothing to pick.
- ``memory_space``   → ``MemorySpaceService.list_spaces_for_user`` — **empty when
  ``memory_spaces_enabled()`` is off**.
"""

import logging
from typing import List, Optional

from apis.shared.assistants.models import BindableItem
from apis.shared.auth.models import User
from apis.shared.feature_flags import memory_spaces_enabled, skills_enabled
from apis.shared.memory.service import MemorySpaceService
from apis.shared.models.managed_models import list_all_managed_models
from apis.shared.skills.access import resolve_accessible_skill_ids
from apis.shared.skills.repository import get_skill_catalog_repository

from apis.app_api.admin.services.model_access import (
    ModelAccessService,
    get_model_access_service,
)
from apis.app_api.tools.service import ToolCatalogService, get_tool_catalog_service

logger = logging.getLogger(__name__)

# The kinds the catalog can serve. ``model`` is not a binding kind (it is the governed
# single-select ``modelConfig``) but rides the same palette endpoint so the Designer has
# one place to fetch every choice.
BINDABLE_KINDS = ("model", "tool", "skill", "knowledge_base", "memory_space")


async def list_bindable(
    kind: str,
    user: User,
    *,
    model_access_service: Optional[ModelAccessService] = None,
    tool_service: Optional[ToolCatalogService] = None,
    memory_service: Optional[MemorySpaceService] = None,
) -> List[BindableItem]:
    """Return the RBAC-filtered bindable primitives of ``kind`` for ``user``.

    Raises ``ValueError`` for an unknown ``kind`` (the route maps that to 400). Each
    sub-lister is best-effort: a failure in one primitive's service logs and yields an
    empty list rather than failing the whole palette.
    """
    if kind == "model":
        return await _list_models(user, model_access_service or get_model_access_service())
    if kind == "tool":
        return await _list_tools(user, tool_service or get_tool_catalog_service())
    if kind == "skill":
        return await _list_skills(user)
    if kind == "knowledge_base":
        # Welded to the agent, synthesized on read, never author-settable (D2/F4).
        return []
    if kind == "memory_space":
        return _list_memory_spaces(user, memory_service or MemorySpaceService())
    raise ValueError(f"Unknown bindable kind '{kind}'.")


async def _list_models(user: User, svc: ModelAccessService) -> List[BindableItem]:
    try:
        models = await list_all_managed_models()
        accessible = await svc.filter_accessible_models(user, models)
    except Exception:
        logger.warning("Failed to list bindable models", exc_info=True)
        return []
    # ``ref`` is the Bedrock/provider model id — the identifier the runtime resolver,
    # RBAC (``permissions.models``) and invocation all key on. NOT the internal UUID.
    return [
        BindableItem(
            kind="model",
            ref=m.model_id,
            label=m.model_name,
            description=m.provider_name or m.provider or "",
            meta={
                "provider": m.provider,
                "providerName": m.provider_name,
                "isDefault": m.is_default,
                "maxInputTokens": m.max_input_tokens,
                "maxOutputTokens": m.max_output_tokens,
                "supportsCaching": m.supports_caching,
                "inputModalities": m.input_modalities,
                "outputModalities": m.output_modalities,
                "supportedParams": m.supported_params,
            },
        )
        for m in accessible
    ]


async def _list_tools(user: User, svc: ToolCatalogService) -> List[BindableItem]:
    try:
        tools = await svc.get_user_accessible_tools(user)
    except Exception:
        logger.warning("Failed to list bindable tools", exc_info=True)
        return []
    return [
        BindableItem(
            kind="tool",
            ref=t.tool_id,
            label=t.display_name,
            description=t.description or "",
            meta={
                "category": t.category,
                "protocol": t.protocol,
                "requiresOauthProvider": t.requires_oauth_provider,
                "serverTools": [
                    {
                        "name": st.name,
                        "description": st.description,
                        "needsApproval": st.needs_approval,
                        "enabled": st.enabled,
                    }
                    for st in t.server_tools
                ],
            },
        )
        for t in tools
    ]


async def _list_skills(user: User) -> List[BindableItem]:
    if not skills_enabled():
        return []
    try:
        skill_ids = await resolve_accessible_skill_ids(user)
        skills = await get_skill_catalog_repository().batch_get_skills(skill_ids)
    except Exception:
        logger.warning("Failed to list bindable skills", exc_info=True)
        return []
    return [
        BindableItem(
            kind="skill",
            ref=s.skill_id,
            label=s.display_name,
            description=s.description or "",
            meta={"boundToolIds": s.bound_tool_ids, "compose": s.compose},
        )
        for s in skills
    ]


def _list_memory_spaces(user: User, svc: MemorySpaceService) -> List[BindableItem]:
    if not memory_spaces_enabled():
        return []
    try:
        spaces = svc.list_spaces_for_user(user.user_id, user.email)
    except Exception:
        logger.warning("Failed to list bindable memory spaces", exc_info=True)
        return []
    return [
        BindableItem(
            kind="memory_space",
            ref=space.space_id,
            label=space.name,
            description=space.template or "",
            meta={"role": role, "ownerId": space.owner_id, "template": space.template},
        )
        for space, role in spaces
    ]
