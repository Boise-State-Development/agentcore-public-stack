"""Agent Marketplace Phase 6 — the admin side of default pins (D9, D10).

Projects a role's stored pin ids into rows an admin can read, and answers the question
D9.5 asks before a save: **will this Agent actually work for this role's members?**

Two checks, deliberately separate, because they fail for different reasons and have
different remedies:

* **Reachability** — a ``PRIVATE`` Agent is visible to its owner alone and a ``SHARED``
  one to its share list, so seeding either to a role hands most members a pin that
  silently resolves to nothing (``list_pins`` access-checks every row on every read). The
  remedy is the *author's*: publish it, or set it PUBLIC. This is not gated on the listing
  being *published* — an Agent can be PUBLIC without ever having been submitted to the
  store, and a role seed is a pin, not a shelf slot.

* **Runnability against the role** — the Agent's pinned model and its tool/skill bindings
  diffed against that role's own ``effective_permissions``, which
  ``_compute_effective_permissions`` already denormalizes onto the AppRole record. Seeding
  410 researchers an Agent that fails on their first message is the exact failure D9.5
  exists to prevent. The remedy is the *admin's*: grant the capability, or seed something
  else.

Both **warn**; neither refuses. An admin can legitimately seed an Agent whose author is
about to publish it, or whose missing tool is about to be granted, and a save that blocks
on a condition the admin is mid-way through fixing teaches people to work around the
console rather than through it.

⚠️ The role-level diff is narrower than the per-user one on purpose. ``memory_space``
resolves per person and cannot be decided for a role at all, so it is reported as a *note*
rather than counted as present or missing — "ready" here must never claim to have checked
something it could not.
"""

import logging
from typing import List, Sequence, Set, Tuple

from apis.shared.assistants.icons import icon_url
from apis.shared.assistants.listing_repository import batch_get_agents
from apis.shared.assistants.models import (
    Assistant,
    ListingPublisher,
    MissingCapability,
    RoleAgentPin,
    RoleAgentPinRow,
)
from apis.shared.assistants.publishers import list_publishers
from apis.shared.rbac.models import AppRole

# Reused rather than re-derived: these are the same helpers the per-invoker runnability
# preview (D6) uses, so the admin's assignment-time warning and the user's "will it run
# for me?" line can never drift into disagreeing about what an Agent binds.
from apis.app_api.agent_designer.services.agent_detail import (
    _binding_key,
    _fallback,
    _gated_bindings,
    _is_optional,
    _labels_by_kind,
    _model_label,
)

logger = logging.getLogger(__name__)

# What a role's ``effective_permissions`` can actually decide. ``memory_space`` is absent
# because a role does not grant memory spaces — people do, per space.
_ROLE_GATED_KINDS = ("tool", "skill")

_MEMORY_NOTE = (
    "Binds a memory space, which is granted per person — this check cannot decide it for "
    "a whole role."
)


def _grants(role: AppRole, axis: str) -> Set[str]:
    perms = role.effective_permissions
    return set(getattr(perms, axis, []) or [])


def _granted(grants: Set[str], key: str) -> bool:
    return "*" in grants or key in grants


async def _diff_against_role(
    assistant: Assistant, role: AppRole, admin_user
) -> Tuple[str, List[MissingCapability], List[str]]:
    """``(state, missing, notes)`` for one Agent against one role (D9.5)."""
    missing: List[MissingCapability] = []
    notes: List[str] = []

    if assistant.model_settings is not None:
        model_id = assistant.model_settings.model_id
        if not _granted(_grants(role, "models"), model_id):
            missing.append(
                MissingCapability(
                    label=await _model_label(model_id) or _fallback("model"),
                    kind="model",
                    # The pinned model is never optional: the binding resolver blocks the
                    # turn outright when the invoker cannot reach it.
                    optional=False,
                )
            )

    bindings = _gated_bindings(assistant)
    if any(binding.kind == "memory_space" for binding in bindings):
        notes.append(_MEMORY_NOTE)

    labels = await _labels_by_kind(assistant, admin_user)
    for kind, axis in (("tool", "tools"), ("skill", "skills")):
        of_kind = [b for b in bindings if b.kind == kind]
        if not of_kind:
            continue
        grants = _grants(role, axis)
        for binding in of_kind:
            key = _binding_key(binding)
            if _granted(grants, key):
                continue
            label = labels.get(kind, {}).get(key) or _fallback(kind)
            if any(m.kind == kind and m.label == label for m in missing):
                continue
            missing.append(
                MissingCapability(label=label, kind=kind, optional=_is_optional(binding))
            )

    if not missing:
        state = "ready"
    elif all(item.optional for item in missing):
        state = "limits"
    else:
        state = "blocked"
    return state, missing, notes


async def resolve_role_pins(
    role: AppRole, pins: Sequence[RoleAgentPin], admin_user
) -> Tuple[List[RoleAgentPinRow], List[str]]:
    """Project a role's stored pins into admin rows, in seed order.

    Returns ``(rows, unavailable_ids)``. An id whose Agent no longer exists is *reported*,
    never pruned on read: a GET that rewrote the seed list would make an accidental delete
    of the wrong Agent look like the admin's own edit.
    """
    if not pins:
        return [], []

    agent_ids = [pin.agent_id for pin in pins]
    records = await batch_get_agents(agent_ids)
    publishers = {p.id: p for p in await list_publishers()}

    rows: List[RoleAgentPinRow] = []
    unavailable: List[str] = []
    for pin in pins:
        item = records.get(pin.agent_id)
        if not item:
            unavailable.append(pin.agent_id)
            continue
        try:
            assistant = Assistant.model_validate(item)
        except Exception:
            logger.warning(f"Skipping unparseable pinned agent {pin.agent_id}", exc_info=True)
            unavailable.append(pin.agent_id)
            continue

        listing = assistant.listing
        profile = publishers.get(listing.publisher_id) if listing else None
        state, missing, notes = await _diff_against_role(assistant, role, admin_user)
        visibility = assistant.visibility or "PRIVATE"

        rows.append(
            RoleAgentPinRow(
                agent_id=assistant.assistant_id,
                name=assistant.name,
                tagline=assistant.tagline,
                emoji=assistant.emoji,
                icon_url=icon_url(assistant.assistant_id, assistant.icon_key),
                publisher=(
                    ListingPublisher(
                        label=profile.label, kind=profile.kind, verified=profile.verified
                    )
                    if profile
                    else None
                ),
                category=(listing.category if listing else ""),
                order=pin.order,
                locked=pin.locked,
                listing_state=(listing.state if listing else None),
                reachable=visibility == "PUBLIC",
                visibility=visibility,
                state=state,
                missing=missing,
                notes=notes,
            )
        )

    return rows, unavailable
