"""Where one user-authored skill is bound, and so who an edit to it reaches.

skill-creator spec D9. A user's own skill runs for people other than its author
in exactly one way: a binding on an Agent whose *owner* authored the skill
(invoke-through, clause 3 of ``resolve_invocable_skill_ids``). A project harness
is such an Agent (``kind == "project"``), so Shared Projects are covered by the
same rule. Clauses 1 and 2 never widen a private skill: no role grants one, and
ownership only admits the author.

That makes the reverse lookup exact, not a heuristic: every binding that can run
skill X for someone else lives on an Agent in X's owner's partition of the
assistants table (``OwnerStatusIndex``, ``GSI_PK = OWNER#{owner}``), harnesses
included, because a harness keeps the ``ownerId`` it was created with and the
resolver judges that same field. A binding on someone else's Agent can only ever
run the skill for its author.

Runs inside a tool call's approval step, never on the turn path. Synchronous
boto3; async callers wrap it in ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from apis.shared.skills.pins import binding_pin, split_pinned_ref

logger = logging.getLogger(__name__)

# A user with more Agents than this many pages of projected rows is not someone
# this lookup should stall an approval prompt for. Hitting it is reported as an
# incomplete result rather than silently truncated.
MAX_PAGES = 10

# How many names each line of the summary spells out before "and N more".
_MAX_NAMES = 5

_SHARED_VISIBILITIES = frozenset({"SHARED", "PUBLIC"})


@dataclass(frozen=True)
class SkillBinding:
    """One Agent or project harness that binds the skill."""

    name: str
    is_project: bool
    # Whether anyone but the owner can invoke it. Always True for a project:
    # its harness runs for every member.
    shared: bool
    pinned_version: Optional[int]


@dataclass
class SkillReach:
    """Every binding of one skill in its owner's Agents and projects."""

    skill_id: str
    bindings: List[SkillBinding] = field(default_factory=list)
    # False when the lookup failed or stopped at MAX_PAGES. The summary then
    # says so instead of claiming the skill is bound nowhere.
    complete: bool = True
    error: Optional[str] = None

    @property
    def unpinned(self) -> List[SkillBinding]:
        return [b for b in self.bindings if b.pinned_version is None]

    @property
    def pinned(self) -> List[SkillBinding]:
        return [b for b in self.bindings if b.pinned_version is not None]

    def summary(self) -> str:
        """One paragraph for the approval card: who the edit reaches."""
        if self.error is not None:
            return (
                f"Could not check where this skill is used ({self.error}). Any shared "
                "agent or project that uses it unpinned will run the new version on "
                "its next turn."
            )

        live_shared = [b for b in self.unpinned if b.shared]
        live_private = [b for b in self.unpinned if not b.shared]
        parts: List[str] = []

        if live_shared:
            agents = [b for b in live_shared if not b.is_project]
            projects = [b for b in live_shared if b.is_project]
            groups = []
            if agents:
                groups.append(_count(len(agents), "shared agent") + f" ({_names(agents)})")
            if projects:
                groups.append(_count(len(projects), "project") + f" ({_names(projects)})")
            parts.append(
                f"This skill runs unpinned in {' and '.join(groups)}. They will use the "
                "new version on their next turn, for everyone who uses them."
            )
        if live_private:
            noun = "agent" if len(live_private) == 1 else "agents"
            parts.append(
                f"It is also used unpinned by {len(live_private)} of your private {noun}"
                f" ({_names(live_private)})."
            )
        if self.pinned:
            pinned = ", ".join(
                f"{_label(b)} (v{b.pinned_version})" for b in self.pinned[:_MAX_NAMES]
            )
            extra = len(self.pinned) - _MAX_NAMES
            if extra > 0:
                pinned += f" and {extra} more"
            parts.append(f"Pinned, so unaffected: {pinned}.")
        if not parts:
            parts.append(
                "This skill is not used by any of your agents or projects, so the change "
                "reaches only your own conversations that turn it on."
            )
        if not self.complete:
            parts.append(
                f"Only the first {MAX_PAGES} pages of your agents were checked."
            )
        return " ".join(parts)


def _count(n: int, singular: str, plural: Optional[str] = None) -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def _label(binding: SkillBinding) -> str:
    return f'project "{binding.name}"' if binding.is_project else f'"{binding.name}"'


def _names(bindings: List[SkillBinding]) -> str:
    shown = ", ".join(f'"{b.name}"' for b in bindings[:_MAX_NAMES])
    extra = len(bindings) - _MAX_NAMES
    return f"{shown} and {extra} more" if extra > 0 else shown


def bindings_from_items(skill_id: str, items: Iterable[Dict[str, Any]]) -> List[SkillBinding]:
    """The bindings of ``skill_id`` among projected assistant items.

    Pure, so the classification is testable without DynamoDB. Ordered by name so
    the approval card reads the same on every call.
    """
    found: List[SkillBinding] = []
    for item in items:
        if not item.get("assistantId"):
            continue
        for binding in item.get("bindings") or []:
            if not isinstance(binding, dict) or binding.get("kind") != "skill":
                continue
            # A pin lives in config; a ref that already carries ``@n`` is
            # tolerated the same way the turn path splits run-time ids.
            base, ref_pin = split_pinned_ref(str(binding.get("ref") or ""))
            if base != skill_id:
                continue
            is_project = item.get("kind") == "project"
            found.append(
                SkillBinding(
                    name=str(item.get("name") or item["assistantId"]),
                    is_project=is_project,
                    shared=is_project or item.get("visibility") in _SHARED_VISIBILITIES,
                    pinned_version=binding_pin(binding.get("config")) or ref_pin,
                )
            )
            break
    return sorted(found, key=lambda b: (b.is_project, b.name.lower()))


def lookup_skill_reach(skill_id: str, owner_id: str) -> SkillReach:
    """Where ``owner_id``'s skill ``skill_id`` is bound. Never raises."""
    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        return SkillReach(skill_id, complete=False, error="the agents table is not configured")
    try:
        items, complete = _query_owned_assistant_bindings(table_name, owner_id)
    except Exception as exc:  # noqa: BLE001 - a reach line must never block the approval
        logger.warning("Skill reach lookup failed for %s", skill_id, exc_info=True)
        return SkillReach(skill_id, complete=False, error=type(exc).__name__)
    return SkillReach(skill_id, bindings_from_items(skill_id, items), complete=complete)


def _query_owned_assistant_bindings(table_name: str, owner_id: str):
    """Every Agent the owner holds, drafts and project harnesses included.

    The same partition ``_list_user_assistants_cloud`` reads, without its two
    filters: a draft Agent is still the owner's, and a harness is exactly the
    kind of binding this lookup exists to find. Projected to the five
    attributes the classification needs, so long instructions never travel.
    """
    import boto3
    from boto3.dynamodb.conditions import Key

    from apis.shared.assistants.serialization import from_ddb

    table = boto3.resource("dynamodb").Table(table_name)
    params: Dict[str, Any] = {
        "IndexName": "OwnerStatusIndex",
        "KeyConditionExpression": Key("GSI_PK").eq(f"OWNER#{owner_id}"),
        "ProjectionExpression": "assistantId, #n, visibility, #k, bindings",
        "ExpressionAttributeNames": {"#n": "name", "#k": "kind"},
    }
    items: List[Dict[str, Any]] = []
    for _ in range(MAX_PAGES):
        response = table.query(**params)
        items.extend(from_ddb(item) for item in response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items, True
        params["ExclusiveStartKey"] = last_key
    return items, False
