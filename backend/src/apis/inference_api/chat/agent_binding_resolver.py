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
- ``tool`` bindings → the effective tool allowlist (**replace**, mirroring the model
  override): when an Agent binds tools they *are* its toolset, re-resolved per invoker via
  the same ``AppRoleService.can_access_tool`` gate; a bound tool the invoker lacks blocks the
  turn (D5). An Agent that binds no tools runs on **no** tools of the viewer's — never
  the request's ``enabled_tools`` (below).
  A ref may be *scoped* (``toolId::mcpToolName``) to bind a subset of an MCP server's tools
  rather than all of them; the scoped id rides through to the runtime, where
  ``collect_tool_name_filters`` turns it into that server's ``allowed_tool_names``.
- ``skill`` bindings → the effective skill set (**replace**, same shape as tools): when an
  Agent binds skills they *are* the turn's skills, re-resolved per invoker via the
  invoke-through predicate (§6/D7, ``resolve_invocable_skill_ids``); a bound skill the
  invoker lacks — or the skills feature being disabled in this environment — blocks the
  turn (D5). The caller hands the result to ChatAgent as ``accessible_skill_ids``, which
  is what mounts the ``AgentSkills`` disclosure plugin. Absent skill bindings ⇒ the
  request's ``enabled_skills`` drive the turn as today — except on a project harness,
  which always owns its skills (below).
- ``knowledge_base`` stays with the existing RAG path.

**Degrade with notice — a Shared Project's harness only** (shared-projects §9.6). A project
can have 200 members, and blocking the whole turn because one member lacks one bound tool
would make the project unusable to them. With ``degrade=True`` a missing capability is
dropped instead of raised: unavailable tools and skills are left out, an unavailable model
falls through to the invoker's default, and
each drop is recorded in ``plan.unavailable`` for the route to surface as an
``agent_notice``. Ordinary shared agents keep block-with-message (D5) unchanged. Nothing
about a drop enters the prompt — the notice is for the person, not the model.

**An Agent's tools are the Agent's.** An Agent that binds no tools runs on none of the
viewer's picker tools: an author who selected no tools gets an Agent that uses none,
not one whose toolset changes with whoever is chatting (a knowledge-base course
assistant once paused on a student-facing turn for the viewer's own Canvas sign-in).
One floor stays: such an Agent still gets the admin's always-on tools
(``ResolvedTools.owns_toolset`` is False), because otherwise anyone could shed them by
creating a trivial Agent (admin-always-on-tools §7 D4). System tools and the
attachment / knowledge-base spreadsheet auto-enable are applied by the route, as on
every turn.

**A project's skills are the project's.** A harness that binds no skills, or whose bound
skills were all dropped for this member, runs with *no* skills — never the member's own.
One project conversation must not change with whoever is typing, and a member's own
picks are not part of what the project's editors chose. Tools already follow that rule
for every Agent (above). Ordinary shared Agents that bind no skills still take the
request's skills.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from apis.shared.assistants.models import Assistant
from apis.shared.auth.models import User
from apis.shared.feature_flags import skills_enabled
from apis.shared.models.retirement import (
    EffectiveModel,
    resolve_effective_model,
    retired_model_message,
)
from apis.shared.rbac.model_access import model_access_denied_message
from apis.shared.rbac.service import get_app_role_service
from apis.shared.skills.access import resolve_invocable_skill_ids
from apis.shared.skills.pins import binding_pin, pinned_ref
from apis.shared.tools.scoped_ids import base_tool_id


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
class ResolvedTools:
    """The Agent's ``tool`` bindings resolved to an effective allowlist for the invoker.

    ``tool_ids`` **replaces** the request's ``enabled_tools`` for this turn (an Agent that
    binds tools owns its toolset, like ``modelConfig`` owns the model). Every id has already
    passed the invoker's ``AppRoleService.can_access_tool`` gate; a bound tool the invoker
    could not access blocks the turn before this is constructed (D5), so the list is safe to
    hand straight to the tool filter. An empty ``tool_ids`` is meaningful — the Agent runs
    with *no* tools of the request's. ``resolve_agent_invocation`` never leaves
    ``plan.tools`` as ``None``: an Agent that binds no tools gets an empty list with
    ``owns_toolset`` False.

    Ids are carried **verbatim**, scoping included: a scoped ``base::tool`` id must survive
    into ``enabled_tools`` for ``collect_tool_name_filters`` to fold it into that server's
    ``allowed_tool_names`` and build a filtered MCP client. Collapsing one to its base here
    would silently restore the whole server — the exact bug the scoping exists to prevent,
    and invisible from the outside because the turn would still work.
    """

    tool_ids: List[str]
    # False only for an Agent (project harness included) that binds no tools: its toolset
    # is empty, but it expressed no intent about tools, so the admin's always-on floor
    # still applies (D4). True when bound tools were all dropped while degrading.
    owns_toolset: bool = True


@dataclass
class ResolvedSkills:
    """The Agent's ``skill`` bindings resolved to an effective skill set for the invoker.

    ``skill_ids`` **replaces** the request's ``enabled_skills`` for this turn so ChatAgent's
    AgentSkills plugin discloses exactly these (an Agent that binds skills owns its skills,
    like tools own the toolset). Every id has already passed the invoker's invoke-through
    predicate (§6); a bound skill the invoker could not access blocks the turn before this
    is constructed (D5). Empty only for a project harness, where it means "this project
    runs with no skills" and is distinct from ``plan.skills is None`` (no skill binding,
    fall through to the request) — the same split ``ResolvedTools`` makes.
    """

    # A pinned binding's id carries its version (``skill_id@3``, ``skills.pins``).
    skill_ids: List[str]


@dataclass
class UnavailableCapabilities:
    """What a degraded resolution dropped (``degrade=True`` only)."""

    model_id: Optional[str] = None
    tools: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.model_id or self.tools or self.skills)

    def notice(self) -> str:
        """One plain-language sentence naming everything that was left out."""
        parts: List[str] = []
        if self.model_id:
            parts.append(f"the model {self.model_id} (your default model is used instead)")
        if self.tools:
            parts.append(("the tool " if len(self.tools) == 1 else "the tools ") + ", ".join(self.tools))
        if self.skills:
            parts.append(("the skill " if len(self.skills) == 1 else "the skills ") + ", ".join(self.skills))
        return (
            "Some of this project's setup isn't available to your account, so this "
            f"conversation runs without {'; '.join(parts)}. Ask an administrator for access."
        )


class AgentNoticeEvent(BaseModel):
    """SSE ``agent_notice``: the turn runs, but without some of the agent's setup.

    Emitted before ``message_start`` (beside ``quota_session_notice``) when a degraded
    resolution dropped something. Travels on the SSE channel only — never into the
    prompt, so the cacheable prefix is untouched. Not persisted: it describes this turn's
    resolution, which the next turn re-derives.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: str = "agent_notice"
    session_id: str = Field(..., alias="sessionId")
    agent_id: str = Field(..., alias="agentId")
    project_id: Optional[str] = Field(None, alias="projectId")
    message: str
    unavailable_model_id: Optional[str] = Field(None, alias="unavailableModelId")
    unavailable_tools: List[str] = Field(default_factory=list, alias="unavailableTools")
    unavailable_skills: List[str] = Field(default_factory=list, alias="unavailableSkills")

    @classmethod
    def from_unavailable(
        cls, dropped: "UnavailableCapabilities", *, session_id: str, agent_id: str, project_id: Optional[str]
    ) -> "AgentNoticeEvent":
        return cls(
            session_id=session_id,
            agent_id=agent_id,
            project_id=project_id,
            message=dropped.notice(),
            unavailable_model_id=dropped.model_id,
            unavailable_tools=list(dropped.tools),
            unavailable_skills=list(dropped.skills),
        )

    def to_sse_format(self) -> str:
        return f"event: agent_notice\ndata: {json.dumps(self.model_dump(by_alias=True, exclude_none=True))}\n\n"


@dataclass
class AgentInvocationPlan:
    """What the Harness should apply for this turn after resolving the Agent.

    ``model_override`` is ``None`` when the Agent pins no model — the caller then
    resolves the model exactly as today. ``tools`` is never ``None`` once resolved: an
    Agent that binds no tools gets an empty toolset (``owns_toolset`` False), so the
    viewer's picker never reaches an Agent turn. ``skills`` is ``None`` when the Agent
    binds no skills — the caller then uses the request's ``agent_type``/``enabled_skills``
    — except on a project harness, where it is empty.
    """

    model_override: Optional[ResolvedModel] = None
    tools: Optional[ResolvedTools] = None
    skills: Optional[ResolvedSkills] = None
    unavailable: UnavailableCapabilities = field(default_factory=UnavailableCapabilities)


async def resolve_agent_invocation(
    assistant: Assistant, invoker: User, *, degrade: bool = False
) -> AgentInvocationPlan:
    """Resolve an Agent's governed capabilities for ``invoker``.

    Raises ``AgentBindingBlockedError`` on the first missing capability (D5), unless
    ``degrade`` is set — a project harness — in which case the capability is dropped and
    recorded in ``plan.unavailable`` instead (see the module docstring).

    The model is access-checked against the invoker with the same
    ``AppRoleService.can_access_model`` the harness uses elsewhere (R2), so an author
    cannot compose a model the invoker is later blocked on at model-resolution time.
    """
    plan = AgentInvocationPlan()

    model_settings = assistant.model_settings
    if model_settings is not None:
        # A retired model runs as its successor — including from a published
        # snapshot, which still names the model it was reviewed on — and the
        # access check below is on the model that will actually run
        # (docs/specs/model-retirement.md §5 Stage 3). The Agent's params ride
        # along; the successor's own spec drops what it does not support.
        effective = await resolve_effective_model(model_settings.model_id) or EffectiveModel(
            requested_id=model_settings.model_id, model_id=model_settings.model_id
        )
        model_id = effective.model_id
        provider = effective.provider if effective.redirected else model_settings.provider
        app_role_service = get_app_role_service()
        if effective.denied:
            if degrade:
                plan.unavailable.model_id = model_settings.model_id
            else:
                raise AgentBindingBlockedError(retired_model_message(effective.retired, agent=True))
        elif await app_role_service.can_access_model(invoker, model_id, record=effective.record):
            plan.model_override = ResolvedModel(
                model_id=model_id,
                provider=provider,
                params=model_settings.params,
            )
        elif degrade:
            # No override: the route's normal chain picks the invoker's default model.
            plan.unavailable.model_id = model_id
        else:
            raise AgentBindingBlockedError(
                model_access_denied_message(model_id, effective.record, agent=True)
            )

    plan.tools = await _resolve_tools(assistant, invoker, plan.unavailable if degrade else None)
    plan.skills = await _resolve_skills(assistant, invoker, plan.unavailable if degrade else None)
    if plan.tools is None:
        # An Agent that binds no tools runs on none of the viewer's picks. It chose no
        # tools, so the admin's always-on floor still applies (module docstring).
        plan.tools = ResolvedTools(tool_ids=[], owns_toolset=False)
    if degrade and plan.skills is None:
        # A project harness owns its skills: none bound, or none left after degrading,
        # means none — not the member's own (see the module docstring).
        plan.skills = ResolvedSkills(skill_ids=[])
    return plan


async def _resolve_skills(
    assistant: Assistant, invoker: User, dropped: Optional[UnavailableCapabilities] = None
) -> Optional[ResolvedSkills]:
    """Resolve the Agent's ``skill`` bindings to an effective skill set for ``invoker`` (D5).

    Each bound skill is re-checked against the invoker with the **invoke-through**
    predicate (§6/D7, ``resolve_invocable_skill_ids``): catalog grant ∪ ownership ∪
    "the Agent's owner authored it". A single missing skill blocks the turn
    (block-with-message, no silent drop — D5). The skills feature being disabled in
    this environment also blocks — design-time refuses to create these while the flag is off,
    so reaching here with the flag off is environment drift (mirror ``_resolve_tools``).
    Returns ``None`` when the Agent binds no skills, leaving the request's ``agent_type`` /
    ``enabled_skills`` in force; ``resolve_agent_invocation`` turns that into an empty set
    for a project harness.

    This is the **only** place invoke-through applies. It is AGENT-scoped, not
    user-scoped: widening the shared ``resolve_accessible_skill_ids`` instead would
    leak an Agent owner's private skills into every invoker's plain-chat picker.

    Clause 3 of the predicate also requires the invoker to have share-access to the
    Agent. That half is already satisfied here by construction — the caller reaches
    this resolver only after ``get_assistant_with_access_check`` has admitted the
    invoker — so the predicate only re-tests the owner-match half.

    Replaced the previous ``AppRoleService.can_access_skill`` check (since removed),
    which was wrong on two axes: it had no ownership clause (an author binding their
    *own* authored skill was blocked on their own invocation), and its ``"*"`` wildcard
    matched any id at all, including another user's private authored skill.
    """
    skill_bindings = [b for b in (assistant.bindings or []) if b.kind == "skill"]
    if not skill_bindings:
        return None

    refs: List[str] = []
    pins: dict = {}
    for binding in skill_bindings:
        if binding.ref not in refs:
            refs.append(binding.ref)
            pins[binding.ref] = binding_pin(binding.config)

    if not skills_enabled():
        if dropped is not None:
            dropped.skills.extend(refs)
            return None
        raise AgentBindingBlockedError(
            "This agent uses Skills, which aren't enabled in this environment."
        )

    invocable = await resolve_invocable_skill_ids(
        invoker, refs, getattr(assistant, "owner_id", None)
    )
    missing = [ref for ref in refs if ref not in invocable]
    if missing and dropped is None:
        raise AgentBindingBlockedError(
            f"This agent uses the skill **{missing[0]}**, which isn't available to your account. "
            "Ask an administrator for access, or use a different agent."
        )
    if missing:
        dropped.skills.extend(missing)
    # A pinned binding (shared-projects 3.1) runs as ``skill_id@n``; access was judged on
    # the catalog id above, and the pin only chooses which content the skill runs.
    kept = [pinned_ref(ref, pins[ref]) for ref in refs if ref in invocable]
    # None when every skill was dropped; the caller makes that "no skills" for a project.
    return ResolvedSkills(skill_ids=kept) if kept else None


async def _resolve_tools(
    assistant: Assistant, invoker: User, dropped: Optional[UnavailableCapabilities] = None
) -> Optional[ResolvedTools]:
    """Resolve the Agent's ``tool`` bindings to an effective allowlist for ``invoker`` (D5).

    Each bound tool is re-checked against the invoker with the same
    ``AppRoleService.can_access_tool`` gate the harness already enforces (R2), so an author
    cannot compose a tool the invoker is later denied. A single missing tool blocks the turn
    (block-with-message, no silent drop — D5). Returns ``None`` when the Agent binds no tools;
    ``resolve_agent_invocation`` turns that into an empty toolset that keeps the always-on
    floor. The RBAC service is fetched lazily (only when the Agent actually binds tools).

    A ref may be **scoped** (``toolId::mcpToolName``) to bind a subset of an MCP server's
    tools. Access is a property of the *server*, so the gate is keyed on the base id — which
    is also what an administrator would grant, and so what the block message names. The
    scoped id itself is preserved in the result; that is what narrows the turn.
    """
    tool_bindings = [b for b in (assistant.bindings or []) if b.kind == "tool"]
    if not tool_bindings:
        return None

    app_role_service = get_app_role_service()
    resolved: List[str] = []
    # Access is per server, so check each base once: an Agent binding seven tools of one
    # MCP server is the normal shape here, and it should cost one gate call, not seven.
    checked_bases: set = set()
    # Degrading, a denied base drops *every* ref of that server: the gate is per base, so
    # skipping only the ref that was checked would let a later scoped ref of the same
    # server through unchecked.
    denied_bases: set = set()
    for binding in tool_bindings:
        ref = binding.ref
        base = base_tool_id(ref)
        if base in denied_bases:
            continue
        if base not in checked_bases:
            if not await app_role_service.can_access_tool(invoker, ref):
                if dropped is None:
                    raise AgentBindingBlockedError(
                        f"This agent uses the tool **{base}**, which isn't available to your "
                        "account. Ask an administrator for access, or use a different agent."
                    )
                denied_bases.add(base)
                dropped.tools.append(base)
                continue
            checked_bases.add(base)
        if ref not in resolved:
            resolved.append(ref)

    # Possibly empty after degrading, which still means "this agent's toolset is these
    # tools" — never a fall-through to the request's enabled_tools.
    return ResolvedTools(tool_ids=resolved)
