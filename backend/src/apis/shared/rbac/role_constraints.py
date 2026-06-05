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
"""

from __future__ import annotations

import re
from typing import Iterable

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


class RoleConstraintError(ValueError):
    """Raised when a role mutation violates a security constraint."""


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
