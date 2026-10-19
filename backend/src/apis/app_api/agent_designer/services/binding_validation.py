"""Agent Designer Phase 1 — design-time binding + model validation (D4/D5).

Composes the *existing* per-primitive access checks; invents no new RBAC (D4). Run at
write time against the **author** (design-time half of D5); Phase 3 re-resolves each
binding against the invoking user at run time.

Resolution scope:
- ``model``          → must exist + author passes ``ModelAccessService.can_access_model``.
- ``tool``           → author must have the tool in the ``/agents/bindable`` palette
  (``ToolCatalogService.get_user_accessible_tools``, the SAME source the picker fetches, so
  "if the palette offers it, the write accepts it" — cf. the model check). A ref may be
  *scoped* (``toolId::mcpToolName``) to bind a subset of an MCP server's tools: the base
  id is checked against the palette and the tool name against that server's discovered
  ``serverTools`` list. Run-time then re-resolves each bound tool against the *invoker*
  (``AppRoleService.can_access_tool``, D5).
- ``skill``          → feature-flagged; author must have the skill in the ``/agents/bindable``
  palette (``resolve_accessible_skill_ids``, the SAME source the picker fetches). Run-time then
  re-resolves each bound skill against the *invoker* (``resolve_invocable_skill_ids``, D5).
  ``config.version`` pins the binding to a version, which must exist (shared-projects 3.1).
- **Kept bindings.** On an update the route passes the record's own bindings and model
  (``existing_bindings`` / ``existing_model_settings``). A binding already on the record
  (same kind and ref) and an unchanged model skip the *access* check: the palettes only
  list the saver's own capabilities, so a share editor who lacks something the owner bound
  could otherwise never save. Anything newly added is still checked against the saver, and
  shape checks (scoped-ref syntax, pins, param governance, kind rejections) run on
  everything. Same rule as ``projects/harness_settings.py``; the invoker is re-checked at
  run time either way (D5).
- ``knowledge_base`` → **managed implicitly** (the KB is welded to the agent and its index
  is not user-configurable), so it is NOT author-settable; the compat layer synthesizes it
  on read. An explicit knowledge_base binding is rejected.
"""

import logging
from typing import List, Optional, Set, Tuple

from apis.shared.assistants.models import KNOWN_BINDING_KINDS, AgentBinding, AgentModelConfig
from apis.shared.auth.models import User
from apis.shared.feature_flags import skills_enabled
from apis.shared.models.managed_models import list_all_managed_models
from apis.shared.skills.access import resolve_accessible_skill_ids
from apis.shared.skills.pins import PIN_CONFIG_KEY, binding_pin
from apis.shared.skills.repository import get_skill_catalog_repository
from apis.shared.tools.scoped_ids import SCOPE_DELIMITER, parse_scoped_tool_id

from apis.app_api.admin.services.model_access import ModelAccessService
from apis.app_api.tools.service import ToolCatalogService

logger = logging.getLogger(__name__)


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
    existing_bindings: Optional[List[AgentBinding]] = None,
    existing_model_settings: Optional[AgentModelConfig] = None,
    model_access_service: Optional[ModelAccessService] = None,
    tool_service: Optional[ToolCatalogService] = None,
) -> None:
    """Validate an Agent write for ``user``; raise ``BindingValidationError`` on failure.

    Services are injectable for testing. Callers pass only the fields actually present
    on the request — ``None`` means "not provided", so nothing is validated for it.

    ``existing_bindings`` / ``existing_model_settings`` are the record's current values on
    an update. What the write keeps from them skips the access check (see module doc).
    """
    if model_settings is not None:
        model_kept = (
            existing_model_settings is not None
            and existing_model_settings.model_id == model_settings.model_id
        )
        await _validate_model(
            user,
            model_settings,
            model_access_service or ModelAccessService(),
            check_access=not model_kept,
        )

    if bindings is not None:
        kept_keys = _binding_keys(existing_bindings)
        # Resolve the author's accessible tools/skills ONCE (async) when a binding of that
        # kind is present, then validate each binding synchronously against those sets — the
        # same sources the palette uses, so the picker and the write agree (cf. the model
        # check). Each is fetched lazily (only when that kind is actually bound).
        # Tools map catalog id -> the server's discovered tool names (empty for a
        # non-MCP tool, or an MCP server with no discovery snapshot), because a scoped
        # ref has to be checked on both axes: base id in the palette, tool name exposed
        # by that server.
        accessible_tools: Optional[dict] = None
        if any(b.kind == "tool" and _key(b) not in kept_keys for b in bindings):
            svc = tool_service or ToolCatalogService()
            accessible_tools = {
                t.tool_id: {st.name for st in t.server_tools}
                for t in await svc.get_user_accessible_tools(user)
            }
        accessible_skill_ids: Optional[set] = None
        if skills_enabled() and any(
            b.kind == "skill" and _key(b) not in kept_keys for b in bindings
        ):
            accessible_skill_ids = set(await resolve_accessible_skill_ids(user))
        for binding in bindings:
            _validate_binding(
                binding,
                accessible_tools,
                accessible_skill_ids,
                kept=_key(binding) in kept_keys,
            )
        await _validate_skill_pins(bindings)


def _key(binding: AgentBinding) -> Tuple[str, str]:
    return binding.kind, (binding.ref or "").strip()


def _binding_keys(bindings: Optional[List[AgentBinding]]) -> Set[Tuple[str, str]]:
    return {_key(b) for b in bindings or []}


async def _validate_model(
    user: User, cfg: AgentModelConfig, svc: ModelAccessService, *, check_access: bool = True
) -> None:
    # ``modelConfig.modelId`` is the Bedrock/provider model id — the identifier the
    # runtime resolver, RBAC (``permissions.models``) and invocation all key on. Look
    # the record up by ``model_id`` (NOT ``get_managed_model``, which keys on the
    # internal UUID PK and would reject a valid Bedrock id with a 400).
    model = next((m for m in await list_all_managed_models() if m.model_id == cfg.model_id), None)
    if model is None:
        raise BindingValidationError(f"Model '{cfg.model_id}' is not available.", status_code=400)
    # Use the SAME predicate the ``/agents/bindable`` catalog uses, so "if the palette
    # offers it, the write accepts it". Every model check — this one, the palette and
    # the run-time ``AppRoleService.can_access_model`` the invoker is later held to —
    # reads ``apis.shared.rbac.model_access.grants_model_access``, so the author cannot
    # compose a model the run-time check would refuse on the same grants (#798).
    # An unchanged model was accepted when it was bound; the saver need not hold it.
    if check_access and not await svc.filter_accessible_models(user, [model]):
        raise BindingValidationError(
            f"You do not have access to model '{cfg.model_id}'.", status_code=403
        )

    _validate_model_params(cfg, model)


def _validate_model_params(cfg: AgentModelConfig, model) -> None:
    """Design-time governance of ``modelConfig.params`` against the model's admin spec.

    Each param the author sets must be one the model's ``supported_params`` declares
    supported, unlocked, and within bounds — the SAME per-model rules the invocation
    path enforces (``chat/routes.py`` merge), but surfaced as an author-facing reject
    rather than a silent runtime clamp so the Designer stores only clean values. The
    Designer picker constrains inputs from the same ``supportedParams`` meta, so this
    is the belt-and-suspenders backstop.
    """
    params = cfg.params or {}
    if not params:
        return
    spec_map = {}
    supported = getattr(model, "supported_params", None)
    if supported is not None:
        spec_map = supported.params or {}

    for key, value in params.items():
        spec = spec_map.get(key)
        if spec is None or not spec.supported:
            raise BindingValidationError(
                f"Model '{cfg.model_id}' does not support the '{key}' parameter.",
                status_code=400,
            )
        if spec.locked:
            raise BindingValidationError(
                f"The '{key}' parameter is locked by the administrator and cannot be set.",
                status_code=400,
            )
        if spec.allowed is not None:
            if value not in spec.allowed:
                raise BindingValidationError(
                    f"'{key}' must be one of {spec.allowed}; got '{value}'.",
                    status_code=400,
                )
            continue
        # Numeric param. bool is an int subclass — reject it explicitly so a JSON
        # ``true`` can't masquerade as 1.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BindingValidationError(f"'{key}' must be a number.", status_code=400)
        if spec.min is not None and value < spec.min:
            raise BindingValidationError(f"'{key}' must be >= {spec.min}.", status_code=400)
        if spec.max is not None and value > spec.max:
            raise BindingValidationError(f"'{key}' must be <= {spec.max}.", status_code=400)


def _validate_binding(
    binding: AgentBinding,
    accessible_tools: Optional[dict] = None,
    accessible_skill_ids: Optional[set] = None,
    *,
    kept: bool = False,
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
        _validate_tool(binding, accessible_tools or {}, kept=kept)
        return

    if kind == "skill":
        _validate_skill(binding, accessible_skill_ids or set(), kept=kept)


def _validate_tool(binding: AgentBinding, accessible_tools: dict, *, kept: bool = False) -> None:
    """Validate one ``tool`` binding, bare (whole server) or scoped (one tool of it).

    Mirrors ``ToolCatalogService.save_user_preferences``, which validates the same
    ``base::tool`` shape on the user-preference axis: the **base** id must be in the
    author's palette (``get_user_accessible_tools`` — the exact source the Designer
    picker fetches, so a bindable tool is always writable), and the tool name must be
    one the server actually exposes.

    A server with an empty ``serverTools`` list has simply never been discovered
    ("Discover from server" on the admin tool page populates it); the name check is
    skipped there rather than rejecting every scoped ref, exactly as the preference
    path does. That cached list is a UI/validation aid only — it is never consulted at
    run time, so it can gate what an author may *write* but is not the enforcement
    point. Enforcement is the scoped id itself reaching ``collect_tool_name_filters``.
    """
    ref = (binding.ref or "").strip()
    if not ref:
        raise BindingValidationError("tool binding requires a non-empty 'ref'.", status_code=400)

    base, tool_name = parse_scoped_tool_id(ref)
    if tool_name is None and SCOPE_DELIMITER in ref:
        # "canvas_faculty::" parses to a bare ref, so it would silently store as a
        # whole-server binding — the opposite of what the author meant to narrow.
        raise BindingValidationError(
            f"tool binding ref '{ref}' is missing a tool name after "
            f"'{SCOPE_DELIMITER}'.",
            status_code=400,
        )
    # A kept binding was validated when it was added; the saver's palette (and so the
    # server's discovered names) may not include it, so only the shape is re-checked.
    if kept:
        return
    # Run-time re-resolves against the invoker via AppRoleService.can_access_tool (D5),
    # which likewise admits a scoped ref whose base server is granted.
    if base not in accessible_tools:
        raise BindingValidationError(
            f"You do not have access to tool '{base}'.", status_code=403
        )

    if tool_name is not None:
        server_names = accessible_tools[base]
        if server_names and tool_name not in server_names:
            raise BindingValidationError(
                f"Tool '{base}' does not expose a tool named '{tool_name}'.",
                status_code=400,
            )


def _validate_skill(binding: AgentBinding, accessible_skill_ids: set, *, kept: bool = False) -> None:
    if not skills_enabled():
        raise BindingValidationError("Skills are not enabled.", status_code=400)
    ref = (binding.ref or "").strip()
    if not ref:
        raise BindingValidationError("skill binding requires a non-empty 'ref'.", status_code=400)
    # The skill id must be in the author's palette (resolve_accessible_skill_ids) — the exact
    # source the Designer picker fetches. Run-time re-resolves against the invoker via
    # resolve_invocable_skill_ids (D5), which adds the Agent-owner invoke-through clause.
    if not kept and ref not in accessible_skill_ids:
        raise BindingValidationError(
            f"You do not have access to skill '{ref}'.", status_code=403
        )
    pin = (binding.config or {}).get(PIN_CONFIG_KEY)
    if pin is not None and binding_pin(binding.config) is None:
        raise BindingValidationError(
            f"skill binding '{ref}' has an invalid version; it must be a whole number of 1 or more.",
            status_code=400,
        )


async def _validate_skill_pins(bindings: List[AgentBinding]) -> None:
    """A pinned skill binding must name a version that exists (shared-projects 3.1).

    The runtime leaves out a pin whose version is missing rather than running the
    live skill, so a pin to a version that was never cut would bind a skill that
    never runs.
    """
    pinned = [
        (b.ref, binding_pin(b.config))
        for b in bindings
        if b.kind == "skill" and binding_pin(b.config) is not None
    ]
    if not pinned:
        return
    found = await get_skill_catalog_repository().batch_get_skill_versions(pinned)
    for ref, number in pinned:
        if (ref, number) not in found:
            raise BindingValidationError(
                f"Version {number} of skill '{ref}' does not exist.", status_code=400
            )
