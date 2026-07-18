"""Agent Designer Phase 3 — run-time binding resolution (the Harness side).

At invocation the Harness re-resolves an Agent's ``modelConfig`` + ``bindings`` against
the **invoking** user (D5), not the author — an Agent is shared but its capabilities are
gated per user. v1 policy is **block-with-message**: if the invoker lacks a required
capability the turn raises ``AgentBindingBlockedError`` and the route streams a
conversational error (SSE ``stream_error``) rather than silently downgrading.

This module lives in inference-api and may import only ``apis.shared`` (never
``apis.app_api`` — the design-time ``binding_validation`` there is a different concern and
would break the import boundary). It reuses the harness's existing per-primitive access
checks; it invents no new RBAC (D4).

Phase 3 lands incrementally:
- ``modelConfig`` → ``model_override``, reusing the exact ``AppRoleService.can_access_model``
  gate the harness already enforces (R2). Absent ``modelConfig`` ⇒ no override ⇒ today's
  model-resolution chain is untouched.
- ``memory_space`` bindings → index injection + ``memory_*`` tools.
- ``tool`` bindings → the effective tool allowlist (**replace**, mirroring the model
  override): when an Agent binds tools they *are* its toolset, re-resolved per invoker via
  the same ``AppRoleService.can_access_tool`` gate; a bound tool the invoker lacks blocks the
  turn (D5). Absent tool bindings ⇒ the request's ``enabled_tools`` drive the turn as today.
- ``skill`` bindings → the effective skill set (**replace**, same shape as tools): when an
  Agent binds skills they *are* the turn's skills, re-resolved per invoker via
  ``AppRoleService.can_access_skill``; a bound skill the invoker lacks — or the skills feature
  being disabled in this environment — blocks the turn (D5). The caller then forces skill-mode
  (``agent_type="skill"``) for the turn so the SkillAgent actually discloses them. Absent skill
  bindings ⇒ the request's ``agent_type``/``enabled_skills`` drive the turn as today.
- ``knowledge_base`` stays with the existing RAG path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List, Optional

from apis.shared.assistants.models import Assistant
from apis.shared.auth.models import User
from apis.shared.feature_flags import memory_spaces_enabled, skills_enabled
from apis.shared.memory.service import MemorySpaceService
from apis.shared.rbac.service import get_app_role_service

_ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}


class AgentBindingBlockedError(Exception):
    """The invoking user lacks a capability the Agent requires (D5 block-with-message).

    ``message`` is markdown shown to the user as the assistant turn.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class ResolvedModel:
    """A governed model selection resolved for the invoking user."""

    model_id: str
    provider: Optional[str] = None
    params: Optional[dict] = None


@dataclass
class ResolvedMemoryBinding:
    """A ``memory_space`` binding resolved for the invoking user.

    ``role``/``access`` decide what the Harness may do: ``access`` is the *authored*
    intent (``read``|``readwrite``), ``role`` is the invoker's actual grant. A
    ``readwrite`` binding requires the invoker to be ``editor+`` (else the resolver
    blocks — no silent read-only downgrade, D5). ``always_load`` drives prompt injection.
    """

    space_id: str
    space_name: str
    role: str
    access: str
    always_load: Optional[List[str]] = None


@dataclass
class ResolvedTools:
    """The Agent's ``tool`` bindings resolved to an effective allowlist for the invoker.

    ``tool_ids`` **replaces** the request's ``enabled_tools`` for this turn (an Agent that
    binds tools owns its toolset, like ``modelConfig`` owns the model). Every id has already
    passed the invoker's ``AppRoleService.can_access_tool`` gate; a bound tool the invoker
    could not access blocks the turn before this is constructed (D5), so the list is safe to
    hand straight to the tool filter. An empty ``tool_ids`` is meaningful — the Agent
    deliberately runs with *no* tools — and is distinct from ``plan.tools is None`` (no tool
    binding, fall through to the request).
    """

    tool_ids: List[str]


@dataclass
class ResolvedSkills:
    """The Agent's ``skill`` bindings resolved to an effective skill set for the invoker.

    ``skill_ids`` **replaces** the request's ``enabled_skills`` for this turn so ChatAgent's
    AgentSkills plugin discloses exactly these (an Agent that binds skills owns its skills,
    like tools own the toolset). Every id has already passed the
    invoker's ``AppRoleService.can_access_skill`` gate; a bound skill the invoker could not
    access blocks the turn before this is constructed (D5). Always non-empty — the resolver
    returns ``None`` (no skill binding) rather than an empty ``ResolvedSkills``.
    """

    skill_ids: List[str]


@dataclass
class AgentInvocationPlan:
    """What the Harness should apply for this turn after resolving the Agent.

    ``model_override`` is ``None`` when the Agent pins no model — the caller then
    resolves the model exactly as today. ``memory`` is ``None`` when the Agent binds no
    Memory Space. ``tools`` is ``None`` when the Agent binds no tools — the caller then
    uses the request's ``enabled_tools`` unchanged. ``skills`` is ``None`` when the Agent
    binds no skills — the caller then uses the request's ``agent_type``/``enabled_skills``.
    """

    model_override: Optional[ResolvedModel] = None
    memory: Optional[ResolvedMemoryBinding] = None
    tools: Optional[ResolvedTools] = None
    skills: Optional[ResolvedSkills] = None


async def resolve_agent_invocation(assistant: Assistant, invoker: User) -> AgentInvocationPlan:
    """Resolve an Agent's governed capabilities for ``invoker``; raise on a block (D5).

    PR-A resolves only ``modelConfig``. The model is access-checked against the invoker
    with the same ``AppRoleService.can_access_model`` the harness uses elsewhere (R2), so
    an author cannot compose a model the invoker is later blocked on at model-resolution
    time.
    """
    plan = AgentInvocationPlan()

    model_settings = assistant.model_settings
    if model_settings is not None:
        app_role_service = get_app_role_service()
        if not await app_role_service.can_access_model(invoker, model_settings.model_id):
            raise AgentBindingBlockedError(
                f"This agent runs on **{model_settings.model_id}**, which isn't available "
                "to your account. Ask an administrator for access, or use a different agent."
            )
        plan.model_override = ResolvedModel(
            model_id=model_settings.model_id,
            provider=model_settings.provider,
            params=model_settings.params,
        )

    plan.memory = await _resolve_memory(assistant, invoker)
    plan.tools = await _resolve_tools(assistant, invoker)
    plan.skills = await _resolve_skills(assistant, invoker)
    return plan


async def _resolve_skills(assistant: Assistant, invoker: User) -> Optional[ResolvedSkills]:
    """Resolve the Agent's ``skill`` bindings to an effective skill set for ``invoker`` (D5).

    Each bound skill is re-checked against the invoker with ``AppRoleService.can_access_skill``
    (the same AppRole layer the harness's model/tool gates use); a single missing skill blocks
    the turn (block-with-message, no silent drop — D5). The skills feature being disabled in
    this environment also blocks — design-time refuses to create these while the flag is off,
    so reaching here with the flag off is environment drift (mirror ``_resolve_memory``).
    Returns ``None`` when the Agent binds no skills, leaving the request's ``agent_type`` /
    ``enabled_skills`` in force.
    """
    skill_bindings = [b for b in (assistant.bindings or []) if b.kind == "skill"]
    if not skill_bindings:
        return None

    if not skills_enabled():
        raise AgentBindingBlockedError(
            "This agent uses Skills, which aren't enabled in this environment."
        )

    app_role_service = get_app_role_service()
    resolved: List[str] = []
    for binding in skill_bindings:
        ref = binding.ref
        if not await app_role_service.can_access_skill(invoker, ref):
            raise AgentBindingBlockedError(
                f"This agent uses the skill **{ref}**, which isn't available to your account. "
                "Ask an administrator for access, or use a different agent."
            )
        if ref not in resolved:
            resolved.append(ref)

    return ResolvedSkills(skill_ids=resolved)


async def _resolve_tools(assistant: Assistant, invoker: User) -> Optional[ResolvedTools]:
    """Resolve the Agent's ``tool`` bindings to an effective allowlist for ``invoker`` (D5).

    Each bound tool is re-checked against the invoker with the same
    ``AppRoleService.can_access_tool`` gate the harness already enforces (R2), so an author
    cannot compose a tool the invoker is later denied. A single missing tool blocks the turn
    (block-with-message, no silent drop — D5). Returns ``None`` when the Agent binds no tools,
    leaving the request's ``enabled_tools`` in force. The RBAC service is fetched lazily (only
    when the Agent actually binds tools) — mirroring how ``_resolve_memory`` builds its own.
    """
    tool_bindings = [b for b in (assistant.bindings or []) if b.kind == "tool"]
    if not tool_bindings:
        return None

    app_role_service = get_app_role_service()
    resolved: List[str] = []
    for binding in tool_bindings:
        ref = binding.ref
        if not await app_role_service.can_access_tool(invoker, ref):
            raise AgentBindingBlockedError(
                f"This agent uses the tool **{ref}**, which isn't available to your account. "
                "Ask an administrator for access, or use a different agent."
            )
        if ref not in resolved:
            resolved.append(ref)

    return ResolvedTools(tool_ids=resolved)


async def _resolve_memory(assistant: Assistant, invoker: User) -> Optional[ResolvedMemoryBinding]:
    """Resolve the Agent's ``memory_space`` binding for ``invoker`` (D5); raise on block.

    v1 supports one Memory Space per Agent (Phase-1 UI writes at most one); any extras are
    ignored. Reads permission via ``MemorySpaceService`` (the same identity-based
    ``resolve_permission`` the app-api surface uses — D4), wrapped in a thread since it's
    sync boto3.
    """
    memory_bindings = [b for b in (assistant.bindings or []) if b.kind == "memory_space"]
    if not memory_bindings:
        return None

    if not memory_spaces_enabled():
        # Design-time validation refuses to create these while the flag is off, so
        # hitting this means environment drift — block rather than silently drop (D5).
        raise AgentBindingBlockedError(
            "This agent uses Memory, which isn't enabled in this environment."
        )

    binding = memory_bindings[0]
    access = (binding.config or {}).get("access", "read")

    service = MemorySpaceService()
    space, role = await asyncio.to_thread(
        service.resolve_permission, binding.ref, invoker.user_id, invoker.email
    )
    if space is None:
        raise AgentBindingBlockedError(
            "This agent's Memory Space no longer exists. Ask its owner to reconnect it."
        )

    required = "editor" if access == "readwrite" else "viewer"
    if role is None or _ROLE_RANK[role] < _ROLE_RANK[required]:
        raise AgentBindingBlockedError(
            f"This agent needs **{required}** access to its Memory Space "
            f'"{space.name}", which your account doesn\'t have.'
        )

    return ResolvedMemoryBinding(
        space_id=binding.ref,
        space_name=space.name,
        role=role,
        access=access,
        always_load=(binding.config or {}).get("alwaysLoad"),
    )
