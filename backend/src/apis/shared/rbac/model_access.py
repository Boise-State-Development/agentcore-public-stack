"""The one rule for "may this user run this model?".

Every model-access decision in the platform reads this predicate: the chat turn
(``/invocations``), api-converse, an Agent's ``modelConfig`` override, the
``/models`` catalog, the Agent Designer's binding validation and the
``set_default_model`` tool. It lives in ``apis.shared`` because ``app_api``,
``inference_api`` and ``agents/`` may only meet here (see
``tests/architecture/test_import_boundaries.py``).

There used to be three copies. ``AppRoleService.can_access_model`` read role
grants only, while ``ModelAccessService`` also honoured the legacy
``availableToRoles`` list and the ``enabled`` flag, and ``account_tools`` kept a
hand-written mirror of the latter. So a model granted the legacy way was
*listed* by ``/models`` and *denied* by chat, and a disabled model was hidden by
the catalog yet still ran (#798).

The rule, in order:

1. A catalog row with ``enabled: false`` is refused — for everyone, ``*``
   holders included. Disabling a model is how an admin takes it out of service.
2. A role grant (``grantedModels`` holds the model id, or ``*``) allows it.
3. The legacy JWT-role list on the row (``availableToRoles``) allows it.

**A model id with no catalog row** is decided by role grants alone — an explicit
id grant or ``*``. There is no ``enabled`` flag or ``availableToRoles`` list to
read, so rules 1 and 3 have nothing to say. This is what the chat path did
before the rules converged, and it is also what happens when the catalog cannot
be read: that fails open to the role grants, exactly as
``resolve_effective_model`` fails open to "run as requested". The catalog
cannot list a row it does not have, so the two surfaces cannot disagree about
such an id — one just never shows it. (The chat path's saved-default fallback
separately refuses a default with no row, because it has no pricing.)

**Retirement runs first.** Callers resolve the requested id through
``apis.shared.models.retirement.resolve_effective_model`` and check the
*effective* id. The admin write path requires a successor to be active and
enabled, so a redirect lands on a row this predicate admits; a retired row's own
``enabled`` flag is never consulted, because the check never sees the retired id.

``ManagedModel.allowed_app_roles`` is not an input: it is a display projection of
the role records (see ``app_api/admin/services/model_roles.py``).
"""

from typing import AbstractSet, Optional

from apis.shared.models.models import ManagedModel


def grants_model_access(
    model_id: str,
    record: Optional[ManagedModel],
    model_permissions: AbstractSet[str],
    user_roles: AbstractSet[str],
) -> bool:
    """Decide whether a user may run ``model_id``. The single access rule.

    Args:
        model_id: The provider model id the call will invoke.
        record: Its catalog row, or ``None`` when it has none (or the catalog
            could not be read) — see the module docstring for that case.
        model_permissions: Model ids the user's AppRoles grant (may hold ``*``).
        user_roles: The user's JWT roles, for the legacy ``availableToRoles`` list.
    """
    if record is not None and not record.enabled:
        return False

    if "*" in model_permissions or model_id in model_permissions:
        return True

    if record is not None and record.available_to_roles:
        return bool(set(user_roles).intersection(record.available_to_roles))

    return False
