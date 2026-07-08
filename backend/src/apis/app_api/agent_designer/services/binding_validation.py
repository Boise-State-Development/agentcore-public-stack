"""Agent Designer Phase 1 — design-time binding + model validation (D4/D5).

Composes the *existing* per-primitive access checks; invents no new RBAC (D4). Run at
write time against the **author** (design-time half of D5); Phase 3 re-resolves each
binding against the invoking user at run time.

Resolution scope:
- ``model``          → must exist + author passes ``ModelAccessService.can_access_model``.
- ``tool``           → author must have the tool in the ``/agents/bindable`` palette
  (``ToolCatalogService.get_user_accessible_tools``, the SAME source the picker fetches, so
  "if the palette offers it, the write accepts it" — cf. the model check). Run-time then
  re-resolves each bound tool against the *invoker* (``AppRoleService.can_access_tool``, D5).
- ``skill``          → feature-flagged; author must have the skill in the ``/agents/bindable``
  palette (``resolve_accessible_skill_ids``, the SAME source the picker fetches). Run-time then
  re-resolves each bound skill against the *invoker* (``AppRoleService.can_access_skill``, D5).
- ``memory_space``   → feature-flagged; author needs viewer+ (read) / editor+ (readwrite).
- ``knowledge_base`` → **managed implicitly** (the KB is welded to the agent and its index
  is not user-configurable), so it is NOT author-settable; the compat layer synthesizes it
  on read. An explicit knowledge_base binding is rejected.
"""

import logging
from typing import List, Optional

from apis.shared.assistants.models import KNOWN_BINDING_KINDS, AgentBinding, AgentModelConfig
from apis.shared.auth.models import User
from apis.shared.feature_flags import memory_spaces_enabled, skills_enabled
from apis.shared.memory.service import MemorySpaceService
from apis.shared.models.managed_models import list_all_managed_models
from apis.shared.skills.access import resolve_accessible_skill_ids

from apis.app_api.admin.services.model_access import ModelAccessService
from apis.app_api.tools.service import ToolCatalogService

logger = logging.getLogger(__name__)

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
    tool_service: Optional[ToolCatalogService] = None,
) -> None:
    """Validate an Agent write for ``user``; raise ``BindingValidationError`` on failure.

    Services are injectable for testing. Callers pass only the fields actually present
    on the request — ``None`` means "not provided", so nothing is validated for it.
    """
    if model_settings is not None:
        await _validate_model(user, model_settings, model_access_service or ModelAccessService())

    if bindings is not None:
        mem = memory_service or MemorySpaceService()
        # Resolve the author's accessible tools/skills ONCE (async) when a binding of that
        # kind is present, then validate each binding synchronously against those sets — the
        # same sources the palette uses, so the picker and the write agree (cf. the model
        # check). Each is fetched lazily (only when that kind is actually bound).
        accessible_tool_ids: Optional[set] = None
        if any(b.kind == "tool" for b in bindings):
            svc = tool_service or ToolCatalogService()
            accessible_tool_ids = {t.tool_id for t in await svc.get_user_accessible_tools(user)}
        accessible_skill_ids: Optional[set] = None
        if skills_enabled() and any(b.kind == "skill" for b in bindings):
            accessible_skill_ids = set(await resolve_accessible_skill_ids(user))
        for binding in bindings:
            _validate_binding(user, binding, mem, accessible_tool_ids, accessible_skill_ids)


async def _validate_model(user: User, cfg: AgentModelConfig, svc: ModelAccessService) -> None:
    # ``modelConfig.modelId`` is the Bedrock/provider model id — the identifier the
    # runtime resolver, RBAC (``permissions.models``) and invocation all key on. Look
    # the record up by ``model_id`` (NOT ``get_managed_model``, which keys on the
    # internal UUID PK and would reject a valid Bedrock id with a 400).
    model = next((m for m in await list_all_managed_models() if m.model_id == cfg.model_id), None)
    if model is None:
        raise BindingValidationError(f"Model '{cfg.model_id}' is not available.", status_code=400)
    # Use the SAME predicate the ``/agents/bindable`` catalog uses (``filter_accessible_models``),
    # not ``can_access_model``: the latter only honors an AppRole model grant when the model
    # record also carries a non-empty ``allowed_app_roles``, so a model granted purely via the
    # user's AppRole ``permissions.models`` (empty ``allowed_app_roles``) is *listed* by the
    # palette but would be *rejected* here — the picker shows it, saving 403s. Filtering the
    # single model guarantees "if the palette offers it, the write accepts it", and matches the
    # runtime's membership grant.
    if not await svc.filter_accessible_models(user, [model]):
        raise BindingValidationError(
            f"You do not have access to model '{cfg.model_id}'.", status_code=403
        )


def _validate_binding(
    user: User,
    binding: AgentBinding,
    mem: MemorySpaceService,
    accessible_tool_ids: Optional[set] = None,
    accessible_skill_ids: Optional[set] = None,
) -> None:
    kind = binding.kind
    if kind not in KNOWN_BINDING_KINDS:
        raise BindingValidationError(f"Unsupported binding kind '{kind}'.", status_code=400)

    if kind == "knowledge_base":
        raise BindingValidationError(
            "knowledge_base bindings are managed automatically and cannot be set directly.",
            status_code=400,
        )

    if kind == "tool":
        _validate_tool(binding, accessible_tool_ids or set())
        return

    if kind == "skill":
        _validate_skill(binding, accessible_skill_ids or set())
        return

    if kind == "memory_space":
        _validate_memory_space(user, binding, mem)


def _validate_tool(binding: AgentBinding, accessible_tool_ids: set) -> None:
    ref = (binding.ref or "").strip()
    if not ref:
        raise BindingValidationError("tool binding requires a non-empty 'ref'.", status_code=400)
    # The tool id must be in the author's palette (get_user_accessible_tools) — the exact
    # source the Designer picker fetches, so a bindable tool is always writable. Run-time
    # re-resolves against the invoker via AppRoleService.can_access_tool (D5).
    if ref not in accessible_tool_ids:
        raise BindingValidationError(
            f"You do not have access to tool '{ref}'.", status_code=403
        )


def _validate_skill(binding: AgentBinding, accessible_skill_ids: set) -> None:
    if not skills_enabled():
        raise BindingValidationError("Skills are not enabled.", status_code=400)
    ref = (binding.ref or "").strip()
    if not ref:
        raise BindingValidationError("skill binding requires a non-empty 'ref'.", status_code=400)
    # The skill id must be in the author's palette (resolve_accessible_skill_ids) — the exact
    # source the Designer picker fetches. Run-time re-resolves against the invoker via
    # AppRoleService.can_access_skill (D5).
    if ref not in accessible_skill_ids:
        raise BindingValidationError(
            f"You do not have access to skill '{ref}'.", status_code=403
        )


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
