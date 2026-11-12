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

**A model id with no catalog row is refused** — for everyone, ``*`` holders
included, exactly like a disabled row. Pricing lives on the row, so a call on
such an id prices to ``None``: it is stored at no cost, counted against no quota
and fires ``UnmeteredModelCall``. Before this rule, an id with no row was
decided by role grants alone, so a ``*`` holder — and any Agent bound to a model
whose row was later deleted or re-registered under another id — ran free. The
catalog's own rows are all cataloged, so ``/models`` and the turn path still
agree; the turn just stops running what the catalog never lists.

**A catalog that cannot be read fails open** to the role grants, exactly as
``resolve_effective_model`` fails open to "run as requested": an outage must not
refuse every model call. Callers say which case they hold with
``catalog_read`` — ``record=None`` with ``catalog_read=True`` (the default) is
"no row"; ``catalog_read=False`` is "unknown".

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
    *,
    catalog_read: bool = True,
) -> bool:
    """Decide whether a user may run ``model_id``. The single access rule.

    Args:
        model_id: The provider model id the call will invoke.
        record: Its catalog row, or ``None`` when it has none (or the catalog
            could not be read — say so with ``catalog_read``).
        model_permissions: Model ids the user's AppRoles grant (may hold ``*``).
        user_roles: The user's JWT roles, for the legacy ``availableToRoles`` list.
        catalog_read: ``False`` when ``record`` is ``None`` because the catalog
            could not be read, which falls back to role grants. ``True`` means a
            ``None`` record is the catalog's answer: no row, so refused.
    """
    if record is None and catalog_read:
        return False

    if record is not None and not record.enabled:
        return False

    if "*" in model_permissions or model_id in model_permissions:
        return True

    if record is not None and record.available_to_roles:
        return bool(set(user_roles).intersection(record.available_to_roles))

    return False


def model_access_denied_message(
    model_id: str,
    record: Optional[ManagedModel],
    *,
    agent: bool = False,
    uncataloged: bool = False,
) -> str:
    """The conversational message for a turn refused by :func:`grants_model_access`.

    Names the model by its catalog display name when the row is known. A row
    with ``enabled: false`` reads as switched off by an administrator, because
    that is true for everyone and no grant would change it; anything else reads
    as not available to this account. An ``uncataloged`` id (the catalog was
    read and has no row for it) reads as not offered here, because no grant
    would change that either: only an administrator adding the model, or the
    user (or the agent's owner) choosing another. Errors stream as assistant
    messages (CLAUDE.md), so this is markdown the user reads in the thread.
    """
    name = (record.model_name if record is not None else None) or model_id
    disabled = record is not None and not record.enabled
    if uncataloged and record is None:
        if agent:
            return (
                f"This agent runs on **{name}**, which isn't offered on this platform. "
                "Ask the agent's owner to choose another model, or use a different agent."
            )
        return f"**{name}** isn't offered on this platform. Choose another model to continue."
    if agent:
        if disabled:
            return (
                f"This agent runs on **{name}**, which has been turned off by an administrator. "
                "Ask the agent's owner to choose another model, or use a different agent."
            )
        return (
            f"This agent runs on **{name}**, which isn't available to your account. "
            "Ask an administrator for access, or use a different agent."
        )
    if disabled:
        return (
            f"**{name}** has been turned off by an administrator. "
            "Choose another model to continue."
        )
    return (
        f"**{name}** isn't available to your account. "
        "Choose another model to continue, or ask an administrator for access."
    )
