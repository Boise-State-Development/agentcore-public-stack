"""Constraints applied to ``AppRole`` mutations.

These guards live at the service layer (rather than only at the API layer)
so they apply uniformly whether a mutation comes from the admin REST API,
a CLI script, or future automation. They protect against two classes of
mistake:

1. Adding ubiquitous JWT group names (e.g. ``"default"``, ``"*"``) to a
   protected role's ``jwt_role_mappings``, which would silently grant that
   role's permissions to every authenticated user.
2. Storing free-form text in ``jwt_role_mappings`` that doesn't look like
   a real group identifier (whitespace, HTML, control characters, etc.) —
   typically indicates a mis-typed or attacker-shaped payload rather than
   a legitimate IdP claim value.
3. Granting an admin scope that must never be delegated, or one that does
   not exist — see :func:`validate_admin_scopes`.
"""

from __future__ import annotations

import re
from typing import Iterable

from .admin_scopes import is_delegable, is_known_scope

# Roles that must never have their JWT mappings broadened. Adding to this
# set is the recommended way to protect new role names introduced later.
PROTECTED_ROLE_IDS: frozenset[str] = frozenset({"system_admin"})

# JWT group names that, if mapped to a protected role, would grant that
# role's permissions to a population the platform considers non-empty
# (``"default"`` is the universal group every authenticated user holds in
# the standard Cognito setup; the rest are common synonyms or wildcards
# we never want to accept on a protected role).
_FORBIDDEN_PROTECTED_MAPPINGS: frozenset[str] = frozenset(
    {
        "default",
        "*",
        "user",
        "users",
        "everyone",
        "anyone",
        "authenticated",
        "authenticated-users",
        "all",
        "any",
        "public",
    }
)

# Conservative pattern for a JWT group identifier: alphanumerics, underscore,
# and hyphen, 2–64 characters. Real-world IdP groups (Entra, Okta, Cognito
# Cognito groups, custom claims) all conform to this shape; values that
# don't are almost certainly malformed.
_JWT_MAPPING_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,64}$")

# Shape of an admin scope id (``admin.tools``). Deliberately length-bounded:
# this pattern is the gate that decides whether a rejected value is safe to
# echo back in an error message and a log line, so it must not admit an
# arbitrarily long string.
_ADMIN_SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){1,3}$")


class RoleConstraintError(ValueError):
    """Raised when a role mutation violates a security constraint."""


class RoleMutationForbidden(Exception):
    """Raised when the *actor* is not permitted to mutate a particular role.

    Distinct from :class:`RoleConstraintError` (which is about the *content* of
    a mutation) because this one is an authorization failure and must surface
    as a 403, not a 400. Deliberately not a ``ValueError``: the resource admin
    routes that can trigger a write-through catch ``ValueError`` and map it to
    400, which would misreport a denied privilege escalation as a bad request.
    ``app_api.main`` registers a handler that maps this to 403.
    """


def _is_forbidden_for_protected(value: str) -> bool:
    return value.strip().lower() in _FORBIDDEN_PROTECTED_MAPPINGS


def validate_jwt_role_mappings(role_id: str, mappings: Iterable[str]) -> None:
    """Validate ``jwt_role_mappings`` content for ``role_id``.

    Args:
        role_id: The role being mutated.
        mappings: The proposed list of JWT group names.

    Raises:
        RoleConstraintError: when any entry fails format validation, or when
            ``role_id`` is in :data:`PROTECTED_ROLE_IDS` and the mapping
            includes a forbidden ubiquitous value.
    """
    if mappings is None:
        return

    for entry in mappings:
        if not isinstance(entry, str) or not _JWT_MAPPING_PATTERN.fullmatch(entry):
            raise RoleConstraintError("Invalid role configuration.")

    if role_id in PROTECTED_ROLE_IDS:
        for entry in mappings:
            if _is_forbidden_for_protected(entry):
                raise RoleConstraintError("Invalid role configuration.")


def is_protected_role(role_id: str) -> bool:
    """Return True if ``role_id`` is in the protected set."""
    return role_id in PROTECTED_ROLE_IDS


def validate_admin_scopes(scopes: Iterable[str] | None) -> None:
    """Validate a role's proposed ``granted_admin_scopes``.

    Rejects two things:

    * **Unknown scopes.** The registry is closed, so an id that isn't in it is
      either a typo or a stale grant left behind by a deleted feature area.
      Either way it would sit in the role record looking like a permission
      while granting nothing — the silent-no-op failure mode this axis exists
      to avoid.
    * **Non-delegable scopes** (``admin.roles``, ``admin.auth_providers``).
      These are the escalation paths back to full admin: editing a role grants
      arbitrary permissions, and editing IdP claim mapping decides which roles
      resolve at all. Enforced here, at the service layer, so the rule applies
      to the REST API, seed scripts, and any future automation equally.

    Unlike :func:`validate_jwt_role_mappings`, the errors here name the
    offending value. Only a ``system_admin`` can reach this code path (writing
    admin scopes requires the roles admin, which is itself non-delegable), so
    there is no untrusted caller to withhold detail from, and a silent
    "Invalid role configuration." on a 15-checkbox form is hostile to the one
    person allowed to use it.

    Raises:
        RoleConstraintError: on an unknown or non-delegable scope.
    """
    if not scopes:
        return

    for scope in scopes:
        if not isinstance(scope, str) or not _ADMIN_SCOPE_PATTERN.fullmatch(scope):
            # Echo only after the value has passed a conservative charset/length
            # check, so a malformed payload can't ride into the error message.
            raise RoleConstraintError("Invalid admin scope.")

        if not is_known_scope(scope):
            raise RoleConstraintError(f"Unknown admin scope: '{scope}'.")

        if not is_delegable(scope):
            raise RoleConstraintError(
                f"Admin scope '{scope}' cannot be delegated. "
                "Managing roles and auth providers requires the system_admin role."
            )
