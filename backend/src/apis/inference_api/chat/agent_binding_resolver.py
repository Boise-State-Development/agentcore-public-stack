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
- **PR-A (here):** ``modelConfig`` → ``model_override``, reusing the exact
  ``AppRoleService.can_access_model`` gate the harness already enforces (R2). Absent
  ``modelConfig`` ⇒ no override ⇒ today's model-resolution chain is untouched.
- **Later:** ``memory_space`` bindings → index injection + ``memory_*`` tools (PR-C+).
  ``knowledge_base`` stays with the existing RAG path; ``tool``/``skill`` are inert.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from apis.shared.assistants.models import Assistant
from apis.shared.auth.models import User
from apis.shared.rbac.service import get_app_role_service


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
class AgentInvocationPlan:
    """What the Harness should apply for this turn after resolving the Agent.

    ``model_override`` is ``None`` when the Agent pins no model — the caller then
    resolves the model exactly as today. (Memory resolution lands in a later PR.)
    """

    model_override: Optional[ResolvedModel] = None


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

    return plan
