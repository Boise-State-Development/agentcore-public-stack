"""Where one user-authored skill is bound, and so who an edit to it reaches.

skill-creator spec D9. A user's own skill runs for people other than its author
in two ways, and this lookup reads both:

1. **Invoke-through** (clause 3 of ``resolve_invocable_skill_ids``): a binding
   on an Agent whose *owner* authored the skill. A project harness is such an
   Agent (``kind == "project"``) when the author created the project. Every such
   binding lives in the owner's partition of the assistants table
   (``OwnerStatusIndex``, ``GSI_PK = OWNER#{owner}``), drafts and harnesses
   included, because a harness keeps the ``ownerId`` it was created with and the
   resolver judges that same field.
2. **Added by its author** (clause 4): a binding on *someone else's* project
   harness that the author added as an editor (``config.addedBy``). Those are
   found through the author's memberships (the projects table's ``MemberIndex``,
   keyed by email, so one keyed read of the author's profile first), then one
   ``BatchGetItem`` for those projects and one for their harnesses. Only a
   binding whose ``addedBy`` is the author counts: on another person's Agent a
   binding the author did not add runs the skill for its author alone.

Clauses 1 and 2 never widen a private skill: no role grants one, and ownership
only admits the author.

What the memberships cannot see: a project the author has since left still runs a
skill they added, and nothing indexes a binding by adder. That case is not found.

Runs on a skill write only (the approval step of a ``my_skill_*`` tool, the REST
409), never on the turn path. Synchronous boto3; async callers wrap it in
``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from apis.shared.skills.pins import binding_adder, binding_pin, split_pinned_ref

logger = logging.getLogger(__name__)

# A user with more Agents than this many pages of projected rows is not someone
# this lookup should stall an approval prompt for. Hitting it is reported as an
# incomplete result rather than silently truncated.
MAX_PAGES = 10

# How many names each line of the summary spells out before "and N more".
_MAX_NAMES = 5

_SHARED_VISIBILITIES = frozenset({"SHARED", "PUBLIC"})

# DynamoDB's BatchGetItem key limit.
_BATCH_GET_LIMIT = 100

# The ``detail.code`` of the 409 a REST skill write returns while the edit would
# reach other people's unpinned bindings and ``confirmReach`` was not sent.
REACH_CONFIRMATION_CODE = "skill_reach_unconfirmed"


@dataclass(frozen=True)
class SkillBinding:
    """One Agent or project harness that binds the skill."""

    name: str
    is_project: bool
    # Whether anyone but the owner can invoke it. Always True for a project:
    # its harness runs for every member.
    shared: bool
    pinned_version: Optional[int]
    # Where it is, for acting on it (skill-version-history.md D9). Not compared, so
    # a binding still equals one built from its four classifying fields.
    agent_id: str = field(default="", compare=False)
    # The project, when ``is_project``: the id its settings routes take.
    project_id: Optional[str] = field(default=None, compare=False)
    # Whether the skill's owner may move this binding's pin today: their own
    # Agents and projects, and projects where they are still an editor.
    can_update: bool = field(default=True, compare=False)
    # A store Agent with a published version, whose users run that snapshot.
    published: bool = field(default=False, compare=False)


@dataclass
class SkillReach:
    """Every binding of one skill in its owner's Agents and projects."""

    skill_id: str
    bindings: List[SkillBinding] = field(default_factory=list)
    # False when the lookup failed or stopped at MAX_PAGES. The summary then
    # says so instead of claiming the skill is bound nowhere.
    complete: bool = True
    error: Optional[str] = None
    # False when the projects the owner joined as an editor could not be read.
    # The owner's own Agents and projects were still checked, so ``bindings`` is
    # kept and the summary adds one sentence instead of discarding it.
    editor_projects_checked: bool = True

    @property
    def unpinned(self) -> List[SkillBinding]:
        return [b for b in self.bindings if b.pinned_version is None]

    @property
    def pinned(self) -> List[SkillBinding]:
        return [b for b in self.bindings if b.pinned_version is not None]

    @property
    def reaches_others_unpinned(self) -> bool:
        """Whether an edit would change what runs in someone else's session next turn.

        True when an unpinned binding is on a shared Agent or a project: those run
        for people other than the skill's owner. A private Agent runs only for the
        owner, and a pinned binding keeps running the version it pinned, so
        neither needs the owner to confirm (agentskills-conformance S3).
        """
        return any(b.shared for b in self.unpinned)

    def conflict_detail(self) -> Dict[str, Any]:
        """The 409 body a REST write returns until the caller confirms the reach.

        ``reach`` is :meth:`summary`, the same paragraph the ``my_skill_*``
        approval card shows, so the two surfaces never word it differently.
        """
        return {
            "code": REACH_CONFIRMATION_CODE,
            "message": "This change reaches other people's agents or projects. Confirm to save it.",
            "reach": self.summary(),
        }

    @property
    def shared_bindings(self) -> List[SkillBinding]:
        """Bindings that run for someone other than the owner, pinned or not."""
        return [b for b in self.bindings if b.shared]

    @property
    def delete_reaches_others(self) -> bool:
        """Whether deleting the skill would take it away from someone else.

        Unlike an edit, a pin does not shield a binding from a delete: a version
        is overlaid on the live row (``versions.apply_version``), so once the row
        is gone every binding of it stops running the skill.
        """
        return bool(self.shared_bindings)

    def delete_conflict_detail(self) -> Dict[str, Any]:
        """The 409 body ``DELETE /skills/mine/{id}`` returns until the caller confirms."""
        return {
            "code": REACH_CONFIRMATION_CODE,
            "message": "Other people's agents or projects use this skill. Confirm to delete it.",
            "reach": self.delete_summary(),
        }

    def delete_summary(self) -> str:
        """One paragraph for the delete confirmation: who loses the skill."""
        shared = self.shared_bindings
        agents = [b for b in shared if not b.is_project]
        projects = [b for b in shared if b.is_project]
        groups = []
        if projects:
            groups.append(_count(len(projects), "project") + f" ({_names(projects)})")
        if agents:
            groups.append(_count(len(agents), "shared agent") + f" ({_names(agents)})")
        parts = [
            f"This skill is used by {' and '.join(groups)}. Deleting it removes it from "
            "them for everyone who uses them, including where it is pinned to a version."
        ]
        if not self.complete:
            parts.append(f"Only the first {MAX_PAGES} pages of your agents were checked.")
        if not self.editor_projects_checked:
            parts.append("Projects you added it to as an editor could not be checked.")
        return " ".join(parts)

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
            # "1 of your private agents" is the grammatical form, and "also"
            # only reads right after a sentence about shared bindings.
            lead = "It is also used" if live_shared else "This skill is used"
            parts.append(
                f"{lead} unpinned by {len(live_private)} of your private agents"
                f" ({_names(live_private)}), which only you use."
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
        if not self.editor_projects_checked:
            parts.append(
                "Projects you added it to as an editor could not be checked."
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


def _binding_order(binding: SkillBinding):
    return (binding.is_project, binding.name.lower())


def bindings_from_items(
    skill_id: str,
    items: Iterable[Dict[str, Any]],
    *,
    added_by: Optional[str] = None,
) -> List[SkillBinding]:
    """The bindings of ``skill_id`` among projected assistant items.

    With ``added_by``, only project harnesses count, and only a binding that user
    added (clause 4): the items are someone else's harnesses, where a binding
    anyone else added runs the skill for its author alone.

    Pure, so the classification is testable without DynamoDB. Ordered by name so
    the approval card reads the same on every call.
    """
    found: List[SkillBinding] = []
    for item in items:
        if not item.get("assistantId"):
            continue
        if added_by is not None and item.get("kind") != "project":
            continue
        for binding in item.get("bindings") or []:
            if not isinstance(binding, dict) or binding.get("kind") != "skill":
                continue
            if added_by is not None and binding_adder(binding.get("config")) != added_by:
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
                    agent_id=str(item["assistantId"]),
                    project_id=item.get("projectId") if is_project else None,
                    can_update=bool(item.get("_canUpdate", True)),
                    published=bool(item.get("publishedVersion")),
                )
            )
            break
    return sorted(found, key=_binding_order)


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
    bindings = bindings_from_items(skill_id, items)

    editor_checked = True
    try:
        owned = {item.get("assistantId") for item in items}
        harnesses = [
            item
            for item in _query_editor_project_harnesses(table_name, owner_id)
            if item.get("assistantId") not in owned
        ]
        bindings = sorted(
            bindings + bindings_from_items(skill_id, harnesses, added_by=owner_id),
            key=_binding_order,
        )
    except Exception:  # noqa: BLE001 - same rule: report, never block
        logger.warning("Skill reach editor-project lookup failed for %s", skill_id, exc_info=True)
        editor_checked = False
    return SkillReach(
        skill_id, bindings, complete=complete, editor_projects_checked=editor_checked
    )


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
        "ProjectionExpression": "assistantId, #n, visibility, #k, bindings, projectId, publishedVersion",
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


def _query_editor_project_harnesses(assistants_table: str, owner_id: str) -> List[Dict[str, Any]]:
    """The harnesses of every project ``owner_id`` is a member of, projected.

    Three keyed reads, no scan: the owner's email (memberships are keyed by
    email), their ``MemberIndex`` rows, then one ``BatchGetItem`` for the projects
    and one for the harnesses. Each harness item carries the project's name,
    which is what a member sees. Empty when a table is not configured (a
    deployment without projects) or the owner has no profile row.
    """
    users_table = os.environ.get("DYNAMODB_USERS_TABLE_NAME")
    projects_table = os.environ.get("DYNAMODB_PROJECTS_TABLE_NAME")
    if not users_table or not projects_table or not owner_id:
        return []

    import boto3

    from apis.shared.assistants.serialization import from_ddb
    from apis.shared.projects.repository import ProjectRepository

    dynamodb = boto3.resource("dynamodb")
    profile = dynamodb.Table(users_table).get_item(
        Key={"PK": f"USER#{owner_id}", "SK": "PROFILE"},
        ProjectionExpression="email",
    ).get("Item") or {}
    email = str(profile.get("email") or "")
    if not email:
        return []

    repository = ProjectRepository(projects_table)
    roles = {m.project_id: m.role for m in repository.list_memberships(email)}
    project_ids = list(roles)
    if not project_ids:
        return []
    projects = {
        project.harness_agent_id: project
        for project in repository.batch_get_projects(project_ids).values()
        if project.harness_agent_id
    }
    names = {agent_id: project.name for agent_id, project in projects.items()}

    items: List[Dict[str, Any]] = []
    harness_ids = sorted(names)
    for start in range(0, len(harness_ids), _BATCH_GET_LIMIT):
        request: Dict[str, Any] = {
            assistants_table: {
                "Keys": [
                    {"PK": f"AST#{agent_id}", "SK": "METADATA"}
                    for agent_id in harness_ids[start:start + _BATCH_GET_LIMIT]
                ],
                "ProjectionExpression": "assistantId, #k, bindings",
                "ExpressionAttributeNames": {"#k": "kind"},
            }
        }
        for _ in range(MAX_PAGES):
            response = dynamodb.batch_get_item(RequestItems=request)
            for raw in response.get("Responses", {}).get(assistants_table, []):
                item = from_ddb(raw)
                project = projects.get(item.get("assistantId"))
                item["name"] = names.get(item.get("assistantId"), item.get("assistantId"))
                if project is not None:
                    item["projectId"] = project.project_id
                    # An editor can move a pin; a member since made a viewer cannot.
                    item["_canUpdate"] = roles.get(project.project_id) == "editor"
                items.append(item)
            request = response.get("UnprocessedKeys") or {}
            if not request:
                break
        else:
            raise RuntimeError("harness reads stayed unprocessed")
    return items
