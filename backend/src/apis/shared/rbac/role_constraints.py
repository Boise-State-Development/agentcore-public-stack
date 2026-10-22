"""Constraints applied to ``AppRole`` mutations.

These guards live at the service layer (rather than only at the API layer)
so they apply uniformly whether a mutation comes from the admin REST API,
a CLI script, or future automation. They protect against two classes of
mistake:

1. Adding ubiquitous JWT group names (e.g. ``"default"``, ``"*"``) to a
   protected role's ``jwt_role_mappings``, which would silently grant that
   role's permissions to every authenticated user.
2. Storing free-form text in ``jwt_role_mappings`` that doesn't look like
   a real group identifier (HTML, control characters, invisible Unicode,
   etc.) — typically indicates a mis-typed or attacker-shaped payload
   rather than a legitimate IdP claim value.

   Internal spaces *are* legitimate: Entra security groups are routinely
   named as display names (``"PSEmeriti Entra Sync"``), and we do not
   control those names. What stays rejected is anything that cannot round
   trip through the claim: a comma (the ``custom:roles`` claim is
   comma-separated, so a comma-bearing group name is unrepresentable),
   and leading/trailing whitespace (both claim parsers ``.strip()`` every
   entry, so an edge-padded mapping could never match an incoming claim —
   it would look granted and grant nothing).
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
#
# Compared after :func:`_normalize_for_forbidden_check`, which folds case
# and treats space/hyphen/underscore as the same separator. That matters
# now that spaces are accepted: ``"Authenticated Users"`` and
# ``"All Users"`` are real Entra/AD display names for exactly the
# populations this set exists to keep off a protected role, and before
# normalization only the hyphenated spelling was caught.
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
        "all-users",
        "domain-users",
    }
)

# Conservative pattern for a JWT group identifier: alphanumerics, underscore,
# and hyphen, plus *single internal* ASCII spaces. Entra security groups are
# commonly named as display names ("PSEmeriti Entra Sync"), and the tenant
# owner — not this platform — chooses those names, so a space has to be
# accepted verbatim.
#
# The alternation (rather than adding " " to the character class) is what
# forbids leading, trailing, and doubled spaces. Both shapes end the same
# way -- a mapping that looks granted and matches nothing. A stored value
# has to match the incoming claim byte for byte, and the claim parsers
# ``.strip()`` every entry, so an edge-padded value can never match; a
# doubled space is indistinguishable from a single one in the admin's
# comma-separated field and is far likelier to be a typo than a real group.
#
# Deliberately still rejected: commas (the delimiter in a ``custom:roles``
# claim and in the admin form, so a comma-bearing name is unrepresentable),
# and every non-space whitespace or invisible character — tabs, newlines,
# NBSP (U+00A0), zero-width space (U+200B). JavaScript ``trim()`` does not
# strip U+200B, so a name pasted out of Entra or Teams can carry one into
# the payload; it must fail loudly rather than be stored as a mapping that
# looks granted and matches nothing.
_JWT_MAPPING_PATTERN = re.compile(r"^[A-Za-z0-9_-]+(?: [A-Za-z0-9_-]+)*$")

# Length bounds, checked separately from the pattern so an out-of-range value
# gets an error that says so.
_JWT_MAPPING_MIN_LENGTH = 2
_JWT_MAPPING_MAX_LENGTH = 64

# Characters reproduced literally by :func:`_describe_mapping`. Everything
# else — control characters, ``<``, NBSP, zero-width characters, any
# non-ASCII — is escaped to a visible ``<U+XXXX>`` token before it reaches an
# error body or a log line. The set is the accepted charset plus the three
# punctuation marks an admin plausibly typed by mistake (``,``, ``.``,
# ``/``); none of them carry an injection shape.
_ECHO_SAFE_CHAR = re.compile(r"[A-Za-z0-9 _,./-]")

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


def _normalize_for_forbidden_check(value: str) -> str:
    """Fold a mapping to the form compared against the forbidden set.

    Case-insensitive, and space/hyphen/underscore are treated as the same
    separator, so ``"Authenticated Users"``, ``"authenticated_users"`` and
    ``"authenticated-users"`` all collapse to one entry.
    """
    return re.sub(r"[ _-]+", "-", value.strip().lower())


def _is_forbidden_for_protected(value: str) -> bool:
    normalized = _normalize_for_forbidden_check(value)
    return (
        value.strip().lower() in _FORBIDDEN_PROTECTED_MAPPINGS
        or normalized in _FORBIDDEN_PROTECTED_MAPPINGS
    )


def _describe_mapping(entry: str) -> str:
    """Render ``entry`` for an error message and a log line.

    Two jobs. First, make the invisible visible: every character outside
    :data:`_ECHO_SAFE_CHAR` is replaced by a ``<U+XXXX>`` token, so an admin
    who pasted a group name out of Entra or Teams can see *that* there is a
    zero-width space or an NBSP in it — echoing the raw value would render
    identically to a correct one and tell them nothing.

    Second, bound the output. The result is length-capped, so a malformed
    payload cannot ride an arbitrarily long string into the 400 body or the
    ``Role update failed:`` log line.
    """
    truncated = entry[:_JWT_MAPPING_MAX_LENGTH]
    rendered = "".join(
        ch if _ECHO_SAFE_CHAR.fullmatch(ch) else f"<U+{ord(ch):04X}>"
        for ch in truncated
    )
    ellipsis = "..." if len(entry) > _JWT_MAPPING_MAX_LENGTH else ""
    return f"'{rendered}{ellipsis}'"


def validate_jwt_role_mappings(role_id: str, mappings: Iterable[str]) -> None:
    """Validate ``jwt_role_mappings`` content for ``role_id``.

    Entries may contain single internal spaces (Entra security groups are
    routinely named as display names) but must otherwise be alphanumerics,
    underscore and hyphen, 2-64 characters. See :data:`_JWT_MAPPING_PATTERN`
    for why each excluded shape is excluded.

    Like :func:`validate_admin_scopes`, the errors here name the offending
    entry. The same reasoning applies verbatim: only a ``system_admin`` can
    reach this code path (the roles admin is non-delegable), so there is no
    untrusted caller to withhold detail from — and a bare "Invalid role
    configuration." on a comma-separated field turns a one-character typo
    into a CloudWatch expedition. The value is echoed only after
    :func:`_describe_mapping` has bounded its length and escaped anything
    outside a conservative charset.

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
        if not isinstance(entry, str):
            raise RoleConstraintError("Each JWT role mapping must be a string.")

        described = _describe_mapping(entry)

        if not (
            _JWT_MAPPING_MIN_LENGTH <= len(entry) <= _JWT_MAPPING_MAX_LENGTH
        ):
            raise RoleConstraintError(
                f"JWT role mapping {described} must be between "
                f"{_JWT_MAPPING_MIN_LENGTH} and {_JWT_MAPPING_MAX_LENGTH} "
                "characters."
            )

        if "," in entry:
            raise RoleConstraintError(
                f"JWT role mapping {described} must not contain a comma. "
                "Commas separate one mapping from the next."
            )

        if entry != entry.strip(" "):
            raise RoleConstraintError(
                f"JWT role mapping {described} must not start or end with a "
                "space. The claim is read with leading and trailing "
                "whitespace stripped, so a padded mapping would never match."
            )

        if "  " in entry:
            raise RoleConstraintError(
                f"JWT role mapping {described} must not contain consecutive "
                "spaces."
            )

        if not _JWT_MAPPING_PATTERN.fullmatch(entry):
            raise RoleConstraintError(
                f"JWT role mapping {described} contains unsupported "
                "characters. Allowed: letters, digits, underscore, hyphen, "
                "and single spaces between words."
            )

    if role_id in PROTECTED_ROLE_IDS:
        for entry in mappings:
            if _is_forbidden_for_protected(entry):
                raise RoleConstraintError(
                    f"JWT role mapping {_describe_mapping(entry)} is held by "
                    "every authenticated user and cannot be mapped to the "
                    f"protected role '{role_id}'."
                )


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
