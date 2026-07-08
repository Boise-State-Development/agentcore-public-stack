"""Agent Designer Phase 1 — design-time binding + model validation (D4/D5).

Composes the *existing* per-primitive access checks; invents no new RBAC (D4). Run at
write time against the **author** (design-time half of D5); Phase 3 re-resolves each
binding against the invoking user at run time.

Phase 1 resolution scope:
- ``model``          → must exist + author passes ``ModelAccessService.can_access_model``.
- ``memory_space``   → feature-flagged; author needs viewer+ (read) / editor+ (readwrite).
- ``knowledge_base`` → **managed implicitly** (the KB is welded to the agent and its index
  is not user-configurable), so it is NOT author-settable in Phase 1; the compat layer
  synthesizes it on read. An explicit knowledge_base binding is rejected.
- ``tool`` / ``skill`` → **inert**: shape-checked only, stored verbatim, no catalog lookup
  and no RBAC (that lands with the Phase 2 catalog / Phase 3 resolution).
"""

import logging
from typing import List, Optional

from apis.shared.assistants.models import KNOWN_BINDING_KINDS, AgentBinding, AgentModelConfig
from apis.shared.auth.models import User
from apis.shared.feature_flags import memory_spaces_enabled
from apis.shared.memory.service import MemorySpaceService
from apis.shared.models.managed_models import get_managed_model

from apis.app_api.admin.services.model_access import ModelAccessService

logger = logging.getLogger(__name__)

_INERT_KINDS = ("tool", "skill")
_ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}


class BindingValidationError(Exception):
    """A binding/model failed design-time validation.

    ``status_code`` maps to the HTTP response the route should return: 403 for an
    access denial (the author lacks the capability), 400 for a malformed request.
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def validate_agent_write(
    user: User,
    *,
    bindings: Optional[List[AgentBinding]] = None,
    model_settings: Optional[AgentModelConfig] = None,
    model_access_service: Optional[ModelAccessService] = None,
    memory_service: Optional[MemorySpaceService] = None,
) -> None:
    """Validate an Agent write for ``user``; raise ``BindingValidationError`` on failure.

    Services are injectable for testing. Callers pass only the fields actually present
    on the request — ``None`` means "not provided", so nothing is validated for it.
    """
    if model_settings is not None:
        await _validate_model(user, model_settings, model_access_service or ModelAccessService())

    if bindings is not None:
        mem = memory_service or MemorySpaceService()
        for binding in bindings:
            _validate_binding(user, binding, mem)


async def _validate_model(user: User, cfg: AgentModelConfig, svc: ModelAccessService) -> None:
    model = await get_managed_model(cfg.model_id)
    if model is None:
        raise BindingValidationError(f"Model '{cfg.model_id}' is not available.", status_code=400)
    if not await svc.can_access_model(user, model):
        raise BindingValidationError(
            f"You do not have access to model '{cfg.model_id}'.", status_code=403
        )


def _validate_binding(user: User, binding: AgentBinding, mem: MemorySpaceService) -> None:
    kind = binding.kind
    if kind not in KNOWN_BINDING_KINDS:
        raise BindingValidationError(f"Unsupported binding kind '{kind}'.", status_code=400)

    if kind == "knowledge_base":
        raise BindingValidationError(
            "knowledge_base bindings are managed automatically and cannot be set directly.",
            status_code=400,
        )

    if kind in _INERT_KINDS:
        # Inert in Phase 1: shape only, no RBAC, no catalog lookup.
        if not binding.ref or not binding.ref.strip():
            raise BindingValidationError(f"{kind} binding requires a non-empty 'ref'.", status_code=400)
        return

    if kind == "memory_space":
        _validate_memory_space(user, binding, mem)


def _validate_memory_space(user: User, binding: AgentBinding, mem: MemorySpaceService) -> None:
    if not memory_spaces_enabled():
        raise BindingValidationError("Memory Spaces are not enabled.", status_code=400)

    access = (binding.config or {}).get("access", "read")
    if access not in ("read", "readwrite"):
        raise BindingValidationError(
            f"memory_space binding 'access' must be 'read' or 'readwrite', got '{access}'.",
            status_code=400,
        )

    always_load = (binding.config or {}).get("alwaysLoad")
    if always_load is not None and not (
        isinstance(always_load, list) and all(isinstance(x, str) for x in always_load)
    ):
        raise BindingValidationError("memory_space 'alwaysLoad' must be a list of strings.", status_code=400)

    space, role = mem.resolve_permission(binding.ref, user.user_id, user.email)
    if space is None:
        raise BindingValidationError(f"Memory space '{binding.ref}' not found.", status_code=400)

    required = "editor" if access == "readwrite" else "viewer"
    if role is None or _ROLE_RANK[role] < _ROLE_RANK[required]:
        raise BindingValidationError(
            f"'{required}' access required on memory space '{binding.ref}'.", status_code=403
        )
