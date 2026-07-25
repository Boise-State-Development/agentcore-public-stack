"""Agent Marketplace Phase 3 — the detail read and the runnability preview (D4, D6).

Two reads over one Agent record, with one thing in common: **they speak in names.**

* ``resolve_capabilities`` answers *what does this Agent reach?* — the bound primitives
  projected to ``{label, kind}``. Labels come from the **unfiltered** catalog, on
  purpose: D6 has to be able to name a capability the viewer lacks, and a viewer-filtered
  lookup would silently drop exactly the entries that matter most.
* ``resolve_runnability`` answers *will it run for me?* — the Agent's ``modelConfig`` +
  ``bindings`` diffed against the viewer's own ``bindable_catalog.list_bindable``
  results. It **composes the five existing per-primitive access services** and adds no
  sixth (Agent Designer D4); every ``list_bindable`` call here is the same call the
  Designer's picker makes, so "what you may bind" and "what will run for you" can never
  drift apart.

Two rules in here are load-bearing and easy to erode:

**1. ``knowledge_base`` is never gated.** ``compat.effective_bindings`` synthesizes a
``knowledge_base`` binding on *every* legacy Agent, while ``list_bindable`` returns an
empty list for that kind (the KB is welded to the Agent and is not user-configurable).
A naive diff therefore marks every legacy Agent *blocked*. The KB is excluded from both
reads instead — it is not a capability the viewer can lack.

**2. ``limits`` requires an explicitly optional binding.** ``agent_binding_resolver`` is
block-with-message for *every* kind it resolves — a missing model, tool, skill or memory
space raises rather than degrading. So a gap only downgrades to "Runs with limits for
you" when the binding declares ``config.optional == true``; anything else missing is
``blocked``, because telling a user an Agent "runs with limits" when the next turn will
raise would be a preview that lies. No surface writes ``optional`` yet — the flag is the
seam for when the resolver grows a degrade path, not a live feature.
"""

import logging
from typing import Dict, List, Optional, Tuple

from apis.shared.assistants.categories import get_category
from apis.shared.assistants.compat import effective_bindings
from apis.shared.assistants.models import (
    AgentCapability,
    AgentRunnabilityResponse,
    Assistant,
    ListingPublisher,
    MissingCapability,
)
from apis.shared.assistants.publishers import get_publisher
from apis.shared.auth.models import User
from apis.shared.models.managed_models import list_all_managed_models
from apis.shared.skills.repository import get_skill_catalog_repository
from apis.shared.tools.scoped_ids import base_tool_id
from apis.shared.memory.service import MemorySpaceService

from apis.app_api.agent_designer.services.bindable_catalog import list_bindable
from apis.app_api.tools.service import ToolCatalogService, get_tool_catalog_service

logger = logging.getLogger(__name__)

# Kinds a viewer can be denied, in the order the detail page lists them. ``knowledge_base``
# is deliberately absent (rule 1 above) and so is any kind this deployment does not know —
# the run-time resolver ignores unknown kinds, so previewing a block on one would be wrong.
GATED_KINDS = ("tool", "skill", "memory_space")

# Fallbacks for a ref whose primitive no longer resolves. Never fall back to the ref
# itself: a capability label is rendered to every viewer, and refs are not display content.
_KIND_FALLBACK_LABELS = {
    "tool": "A tool",
    "skill": "A skill",
    "memory_space": "A memory space",
    "model": "A model",
}


def _fallback(kind: str) -> str:
    return _KIND_FALLBACK_LABELS.get(kind, "A capability")


def _is_optional(binding) -> bool:
    """Whether this binding degrades rather than blocks when the viewer lacks it."""
    return (binding.config or {}).get("optional") is True


def _binding_key(binding) -> str:
    """The identity a binding is looked up and diffed by.

    Tool refs may be *scoped* (``toolId::mcpToolName``) to bind a subset of an MCP
    server's tools, while both the catalog and the RBAC gate key on the bare catalog id
    — so a scoped ref must collapse to its base before either lookup.
    """
    return base_tool_id(binding.ref) if binding.kind == "tool" else binding.ref


def _gated_bindings(assistant: Assistant) -> List:
    """The Agent's bindings that a viewer can actually be denied, KB excluded."""
    return [b for b in effective_bindings(assistant) if b.kind in GATED_KINDS]


# ------------------------------------------------------------------ label resolution
async def _tool_labels(refs: List[str], svc: ToolCatalogService) -> Dict[str, str]:
    labels: Dict[str, str] = {}
    for ref in refs:
        try:
            tool = await svc.get_tool(ref)
        except Exception:
            logger.warning(f"Failed to resolve tool label for '{ref}'", exc_info=True)
            tool = None
        if tool is not None:
            labels[ref] = tool.display_name
    return labels


async def _skill_labels(refs: List[str]) -> Dict[str, str]:
    if not refs:
        return {}
    try:
        skills = await get_skill_catalog_repository().batch_get_skills(refs)
    except Exception:
        logger.warning("Failed to resolve skill labels", exc_info=True)
        return {}
    return {s.skill_id: s.display_name for s in skills}


def _memory_labels(refs: List[str], user: User, svc: MemorySpaceService) -> Dict[str, str]:
    """Memory-space names, resolved through the viewer.

    Unlike tools and skills there is no unfiltered name lookup for a space, and there
    should not be — a space is personal data. That is tolerable precisely because D7
    blocks submission on a ``memory_space`` binding, so a *published* Agent never has
    one; this path is reached by an author looking at their own Agent, where the lookup
    resolves. Anything unresolved falls back to the generic kind label.
    """
    labels: Dict[str, str] = {}
    for ref in refs:
        try:
            space, _role = svc.resolve_permission(ref, user.user_id, user.email)
        except Exception:
            logger.warning(f"Failed to resolve memory space label for '{ref}'", exc_info=True)
            space = None
        if space is not None:
            labels[ref] = space.name
    return labels


async def _model_label(model_id: str) -> Optional[str]:
    """The managed model's display name, unfiltered by the viewer's access."""
    try:
        model = next((m for m in await list_all_managed_models() if m.model_id == model_id), None)
    except Exception:
        logger.warning(f"Failed to resolve model label for '{model_id}'", exc_info=True)
        return None
    return model.model_name if model else None


async def _labels_by_kind(
    assistant: Assistant,
    user: User,
    *,
    tool_service: Optional[ToolCatalogService] = None,
    memory_service: Optional[MemorySpaceService] = None,
) -> Dict[str, Dict[str, str]]:
    """``{kind: {ref: label}}`` for every gated binding on the Agent.

    Each per-kind lookup runs only when that kind is actually bound, mirroring the lazy
    fetching in ``binding_validation.validate_agent_write``.
    """
    by_kind: Dict[str, List[str]] = {kind: [] for kind in GATED_KINDS}
    for binding in _gated_bindings(assistant):
        key = _binding_key(binding)
        if key not in by_kind[binding.kind]:
            by_kind[binding.kind].append(key)

    labels: Dict[str, Dict[str, str]] = {kind: {} for kind in GATED_KINDS}
    if by_kind["tool"]:
        labels["tool"] = await _tool_labels(by_kind["tool"], tool_service or get_tool_catalog_service())
    if by_kind["skill"]:
        labels["skill"] = await _skill_labels(by_kind["skill"])
    if by_kind["memory_space"]:
        labels["memory_space"] = _memory_labels(
            by_kind["memory_space"], user, memory_service or MemorySpaceService()
        )
    return labels


# --------------------------------------------------------------------- capabilities
async def resolve_capabilities(
    assistant: Assistant,
    user: User,
    *,
    tool_service: Optional[ToolCatalogService] = None,
    memory_service: Optional[MemorySpaceService] = None,
) -> Tuple[List[AgentCapability], Optional[str]]:
    """Return ``(capabilities, model_label)`` for the detail read.

    Capabilities are de-duplicated by ``(kind, label)``: two scoped bindings into the
    same MCP server are one line on the page, not two identical ones. ``model_label`` is
    ``None`` when the Agent pins no model, which means "resolve the model as today" —
    the detail page renders an em dash rather than inventing a name.
    """
    labels = await _labels_by_kind(
        assistant, user, tool_service=tool_service, memory_service=memory_service
    )

    capabilities: List[AgentCapability] = []
    seen = set()
    for binding in _gated_bindings(assistant):
        label = labels.get(binding.kind, {}).get(_binding_key(binding)) or _fallback(binding.kind)
        if (binding.kind, label) in seen:
            continue
        seen.add((binding.kind, label))
        capabilities.append(AgentCapability(label=label, kind=binding.kind))

    model_label = None
    if assistant.model_settings is not None:
        model_label = await _model_label(assistant.model_settings.model_id)

    return capabilities, model_label


# ------------------------------------------------------------------ listing display
async def resolve_listing_display(
    assistant: Assistant,
) -> Tuple[Optional[ListingPublisher], Optional[str]]:
    """Return ``(publisher, category_label)`` for the detail page's Details panel.

    Both halves exist because **the stored listing holds ids, and ids are not display
    content**: ``listing.publisherId`` references a ``PublisherProfile`` (D12) and
    ``listing.category`` is a category id that an admin may since have renamed. Rendering
    either raw would put an internal reference on a page every browsing user sees, and in
    the publisher's case would also skip the ``verified`` mark that the whole attribution
    decision hangs on.

    ⚠️ This is the *only* thing ``publisherId`` is ever used for. It is display-only and
    must never reach an access check — ``ownerId`` governs edit rights and Skills v2
    invoke-through resolution, and re-attributing a listing changes neither.

    A deleted publisher or category yields ``None`` rather than an error: a listing whose
    attribution was removed still renders, exactly as the shelf does.
    """
    listing = assistant.listing
    if listing is None:
        return None, None

    publisher = None
    try:
        profile = await get_publisher(listing.publisher_id)
        if profile is not None:
            publisher = ListingPublisher(
                label=profile.label, kind=profile.kind, verified=profile.verified
            )
    except Exception:
        logger.warning(
            f"Failed to resolve publisher '{listing.publisher_id}' for detail read", exc_info=True
        )

    category_label = listing.category
    try:
        category = await get_category(listing.category)
        if category is not None:
            category_label = category.label
    except Exception:
        logger.warning(
            f"Failed to resolve category '{listing.category}' for detail read", exc_info=True
        )

    return publisher, category_label


# ---------------------------------------------------------------------- runnability
async def resolve_runnability(
    assistant: Assistant,
    user: User,
    *,
    tool_service: Optional[ToolCatalogService] = None,
    memory_service: Optional[MemorySpaceService] = None,
) -> AgentRunnabilityResponse:
    """Diff the Agent's model + bindings against the viewer's own catalog (D6).

    ``ready`` when nothing is missing, ``limits`` when only optional bindings are, and
    ``blocked`` the moment something the run-time resolver would raise on is absent.
    """
    missing: List[MissingCapability] = []
    labels = await _labels_by_kind(
        assistant, user, tool_service=tool_service, memory_service=memory_service
    )

    # The pinned model. Not a binding and never optional — ``agent_binding_resolver``
    # blocks the turn outright when the invoker cannot access it.
    if assistant.model_settings is not None:
        model_id = assistant.model_settings.model_id
        available_models = {item.ref for item in await list_bindable("model", user)}
        if model_id not in available_models:
            missing.append(
                MissingCapability(
                    label=await _model_label(model_id) or _fallback("model"),
                    kind="model",
                    optional=False,
                )
            )

    bindings = _gated_bindings(assistant)
    for kind in GATED_KINDS:
        of_kind = [b for b in bindings if b.kind == kind]
        if not of_kind:
            continue
        # One catalog fetch per bound kind — the same RBAC-filtered list the Designer
        # picker shows the viewer. An empty list is a legitimate answer (the primitive's
        # feature flag is off in this environment), and it correctly blocks, matching
        # the run-time resolver's behaviour in the same situation.
        available = {item.ref for item in await list_bindable(kind, user)}
        for binding in of_kind:
            key = _binding_key(binding)
            if key in available:
                continue
            label = labels.get(kind, {}).get(key) or _fallback(kind)
            if any(m.kind == kind and m.label == label for m in missing):
                continue
            missing.append(
                MissingCapability(label=label, kind=kind, optional=_is_optional(binding))
            )

    if not missing:
        state = "ready"
    elif all(item.optional for item in missing):
        state = "limits"
    else:
        state = "blocked"

    return AgentRunnabilityResponse(
        agent_id=assistant.assistant_id, state=state, missing=missing
    )
