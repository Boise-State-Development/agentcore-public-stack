"""The closed registry of delegated admin scopes.

An admin scope names one admin *feature area* — one router package under
``apis/app_api/admin/`` — that a system admin may delegate to another role via
``AppRole.granted_admin_scopes``.

Two properties of this module matter more than its contents:

**It is closed.** Scopes are defined here in code, never derived from a catalog
and never free text. The roles admin UI builds its ``grantedTools`` control from
the *tool catalog* with no free-text entry, so a capability id that is not a
catalog tool cannot be granted from the UI at all — only by hand-writing
DynamoDB items. That is precisely what made the short-lived ``skills`` and
``scheduled-runs`` capability gates inoperable (see the docstring on
``AppRoleService.resolve_user_permissions`` and the comment block at
``sidenav.html``). Admin scopes therefore get their own grant axis fed by this
registry, and PR-3 serves it to the SPA so the role form can render real
checkboxes.

**Some scopes are permanently non-delegable.** ``admin.roles`` is self-evident:
whoever can edit a role can grant themselves anything. ``admin.auth_providers``
is the non-obvious one — role resolution starts from JWT claims, so whoever
controls IdP attribute mapping controls which groups arrive and therefore which
AppRoles resolve. It is role administration by another route. Both stay
``system_admin``-only forever, enforced at the service layer by
``role_constraints.validate_admin_scopes`` so the rule holds for the REST API,
seed scripts, and future automation alike.

There is deliberately **no wildcard**. ``"*"`` is the idiom for the tool, model,
and skill axes; on this axis it is the shorthand that would quietly turn a role
into a superuser. Full admin is spelled ``system_admin``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class AdminScope:
    """One delegable (or explicitly non-delegable) admin feature area."""

    id: str
    label: str
    group: str
    description: str
    delegable: bool = True


# Scope groups mirror the SPA's admin nav groups (``admin.layout.ts``) so the
# role form can render the picker in the same shape an admin already navigates.
GROUP_USAGE = "Usage & Spend"
GROUP_AI_CONFIG = "AI Configuration"
GROUP_MARKETPLACE = "Agent Marketplace"
GROUP_IDENTITY = "Identity & Access"
GROUP_CUSTOMIZATION = "Customization"


ADMIN_SCOPES: tuple[AdminScope, ...] = (
    AdminScope(
        id="admin.costs",
        label="Cost Analytics",
        group=GROUP_USAGE,
        description="View spend dashboards, per-session call anatomy, and cost exports.",
    ),
    AdminScope(
        id="admin.quota",
        label="Quotas",
        group=GROUP_USAGE,
        description="Manage quota tiers, assignments, overrides, and quota events.",
    ),
    AdminScope(
        id="admin.fine_tuning",
        label="Fine-Tuning",
        group=GROUP_USAGE,
        description="Manage fine-tuning access, jobs, and fine-tuning costs.",
    ),
    AdminScope(
        id="admin.models",
        label="Models",
        group=GROUP_AI_CONFIG,
        description="Manage the managed-model catalog and provider model listings.",
    ),
    AdminScope(
        id="admin.tools",
        label="Tools",
        group=GROUP_AI_CONFIG,
        description="Manage the tool catalog, discovery, and which roles may use each tool.",
    ),
    AdminScope(
        id="admin.skills",
        label="Skills",
        group=GROUP_AI_CONFIG,
        description="Manage skill bundles, their reference files, and skill role grants.",
    ),
    AdminScope(
        id="admin.connectors",
        label="Connectors",
        group=GROUP_AI_CONFIG,
        description="Manage OAuth providers used by external MCP tools.",
    ),
    AdminScope(
        id="admin.file_sources",
        label="File Sources",
        group=GROUP_AI_CONFIG,
        description="View the registered file-source adapters.",
    ),
    AdminScope(
        id="admin.export_targets",
        label="Export Targets",
        group=GROUP_AI_CONFIG,
        description="View the registered conversation export-target adapters.",
    ),
    AdminScope(
        id="admin.marketplace",
        label="Agent Marketplace",
        group=GROUP_MARKETPLACE,
        description=(
            "Review agent submissions, handle reports and takedowns, and curate the "
            "storefront, categories, and default pins."
        ),
    ),
    AdminScope(
        id="admin.users",
        label="Users",
        group=GROUP_IDENTITY,
        description="Browse and search the user directory.",
    ),
    AdminScope(
        id="admin.system_prompts",
        label="System Prompts",
        group=GROUP_CUSTOMIZATION,
        description="Manage the admin-authored system prompts offered to users.",
    ),
    AdminScope(
        id="admin.user_menu_links",
        label="User Menu Links",
        group=GROUP_CUSTOMIZATION,
        description="Manage the custom links shown in the user menu.",
    ),
    # ── Non-delegable ────────────────────────────────────────────────────────
    # Present in the registry so they are named, documented, and covered by the
    # route-coverage test — but rejected by validation if anyone tries to grant
    # them. See the module docstring for why auth_providers belongs here.
    AdminScope(
        id="admin.roles",
        label="Roles",
        group=GROUP_IDENTITY,
        description=(
            "Manage AppRoles and their grants. Never delegable — editing a role is "
            "how admin power is handed out."
        ),
        delegable=False,
    ),
    AdminScope(
        id="admin.auth_providers",
        label="Auth Providers",
        group=GROUP_IDENTITY,
        description=(
            "Manage identity providers and claim mapping. Never delegable — "
            "controlling which claims arrive controls which roles resolve."
        ),
        delegable=False,
    ),
)


ADMIN_SCOPES_BY_ID: dict[str, AdminScope] = {s.id: s for s in ADMIN_SCOPES}

ALL_SCOPE_IDS: frozenset[str] = frozenset(ADMIN_SCOPES_BY_ID)

NON_DELEGABLE_SCOPES: frozenset[str] = frozenset(
    s.id for s in ADMIN_SCOPES if not s.delegable
)

DELEGABLE_SCOPES: frozenset[str] = ALL_SCOPE_IDS - NON_DELEGABLE_SCOPES


def get_scope(scope_id: str) -> Optional[AdminScope]:
    """Return the registry entry for ``scope_id``, or None if unknown."""
    return ADMIN_SCOPES_BY_ID.get(scope_id)


def is_known_scope(scope_id: str) -> bool:
    """Return True if ``scope_id`` exists in the registry."""
    return scope_id in ALL_SCOPE_IDS


def is_delegable(scope_id: str) -> bool:
    """Return True if ``scope_id`` may appear in a role's ``granted_admin_scopes``."""
    return scope_id in DELEGABLE_SCOPES


def normalize_scopes(scopes: Optional[Iterable[str]]) -> list[str]:
    """De-duplicate and sort a scope list.

    Sorted because these lists are merged into ``UserEffectivePermissions``
    alongside tools/models/skills, which are sorted deliberately — set iteration
    order varies with hash randomization, and an order flip between turns
    rewrites a cached prompt prefix. Keeping the whole struct deterministic is
    cheaper than reasoning about which fields escape into the prompt.
    """
    if not scopes:
        return []
    return sorted(set(scopes))
