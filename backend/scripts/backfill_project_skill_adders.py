"""Backfill: record who added each of a project's skills (``config.addedBy``).

Since PR #1780, ``PUT /projects/{id}/skills`` stamps the saver's user id on every skill it
newly binds, and the run-time predicate's clause 4 lets a skill run for every member when
its author is the person who added it (shared-projects §9.6). A binding made before that
has no ``addedBy``, so a skill an *editor* authored and added keeps running for the editor
alone until someone removes and re-adds it. This script spares them that: it reads each
project's settings history, finds the version that first bound each unattributed skill,
and records that version's ``createdBy`` as the adder.

What it will and will not guess:

* A version's ``createdBy`` is the user who made that save, and the first version whose
  bindings carry the ref is the save that added it. That is the same fact the route now
  records at save time, read back from the history instead.
* The initial snapshot (cut on the first save, from the state the project was created
  with) has no ``createdBy``. A skill already bound there was added before the history
  began, so its adder is **unknown and left unset**: the script never falls back to the
  project's creator, because an editor could have added it, and attributing it to the
  creator would hand that editor's skill to members on the creator's say-so. Such a
  binding keeps today's behaviour (owner-authored still runs for members).
* A binding that already carries ``addedBy`` is never touched.

SAFETY
------
* **Dry-run by default.** Pass ``--apply`` to write. Every decision is printed either way.
* **Idempotent.** A second run finds nothing to do.
* **No settings version is cut.** Nothing about the project's configuration changes; only
  the attribution the route would have written is added, so the history stays as it was.
* **Scoped.** ``--project`` limits the run to one project id.

Run against dev first, then prod (read the dry-run before ``--apply``)::

    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_project_skill_adders.py \\
        --projects-table dev-boisestateai-v2-projects \\
        --assistants-table dev-boisestateai-v2-rag-assistants            # dry-run
    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_project_skill_adders.py \\
        --projects-table dev-boisestateai-v2-projects \\
        --assistants-table dev-boisestateai-v2-rag-assistants --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apis.shared.assistants.models import AgentBinding, AgentVersion, Assistant  # noqa: E402
from apis.shared.skills.pins import (  # noqa: E402
    ADDED_BY_CONFIG_KEY,
    base_skill_id,
    binding_adder,
)

logger = logging.getLogger("backfill_project_skill_adders")

SKILL = "skill"


@dataclass
class Resolution:
    """What one project's history says about its unattributed skills."""

    harness_id: str
    # ref -> the user id of whoever made the save that first bound it
    adders: Dict[str, str] = field(default_factory=dict)
    # refs bound before the history began (or whose adding version names no one)
    unknown: List[str] = field(default_factory=list)
    # refs that already carry an adder
    attributed: List[str] = field(default_factory=list)


def unattributed_skill_refs(bindings: Iterable[AgentBinding]) -> List[str]:
    """The skill refs on a harness that record no adder, in binding order, deduplicated."""
    refs: List[str] = []
    for binding in bindings:
        if binding.kind != SKILL or binding_adder(binding.config):
            continue
        ref = base_skill_id(binding.ref)
        if ref not in refs:
            refs.append(ref)
    return refs


def resolve_adders(harness: Assistant, versions: List[AgentVersion]) -> Resolution:
    """Decide each unattributed skill's adder from the project's settings history.

    ``versions`` may arrive in any order; they are judged oldest first. For each
    unattributed ref, the adder is the ``createdBy`` of the first version whose bindings
    carry the ref. A first appearance in a version with no ``createdBy`` (the initial
    snapshot) means the skill predates the history, and it is reported as unknown rather
    than guessed. Pure, so the rule is unit-testable without DynamoDB.
    """
    result = Resolution(harness_id=harness.assistant_id)
    bindings = harness.bindings or []
    result.attributed = [
        base_skill_id(b.ref) for b in bindings if b.kind == SKILL and binding_adder(b.config)
    ]
    pending = unattributed_skill_refs(bindings)
    if not pending:
        return result

    for version in sorted(versions, key=lambda v: v.version or 0):
        bound_here = {base_skill_id(b.ref) for b in (version.bindings or []) if b.kind == SKILL}
        for ref in list(pending):
            if ref not in bound_here:
                continue
            pending.remove(ref)
            if version.created_by:
                result.adders[ref] = version.created_by
            else:
                result.unknown.append(ref)
    # A ref in no version at all: bound without a settings save (never through the
    # route), so nobody is on record for it.
    result.unknown.extend(pending)
    return result


def apply_adders(bindings: List[AgentBinding], adders: Dict[str, str]) -> List[AgentBinding]:
    """The harness's bindings with ``addedBy`` written onto the resolved skills, nothing else changed."""
    out: List[AgentBinding] = []
    for binding in bindings:
        ref = base_skill_id(binding.ref) if binding.kind == SKILL else None
        if ref in adders and not binding_adder(binding.config):
            binding = AgentBinding(
                kind=SKILL, ref=binding.ref, config={**binding.config, ADDED_BY_CONFIG_KEY: adders[ref]}
            )
        out.append(binding)
    return out


# --------------------------------------------------------------------------- AWS side
def _iter_projects(projects_table: str, only: Optional[str]):
    from apis.shared.projects.repository import ProjectRepository

    repo = ProjectRepository(projects_table)
    if only:
        project = repo.get_project(only)
        if project is None:
            raise SystemExit(f"Project {only} not found in {projects_table}")
        yield project
        return
    after: Optional[str] = None
    while True:
        page, after = repo.scan_projects(limit=100, after_project_id=after)
        yield from page
        if not after:
            return


async def _process(project, assistants_table: str, apply: bool) -> Resolution:
    from apis.shared.assistants.service import (
        _get_assistant_cloud_without_ownership_check,
        update_assistant,
    )
    from apis.shared.assistants.version_repository import list_versions

    harness = await _get_assistant_cloud_without_ownership_check(project.harness_agent_id, assistants_table)
    if harness is None:
        logger.warning("project %s: harness %s not found", project.project_id, project.harness_agent_id)
        return Resolution(harness_id=project.harness_agent_id)
    if not unattributed_skill_refs(harness.bindings or []):
        return resolve_adders(harness, [])

    resolution = resolve_adders(harness, await list_versions(harness.assistant_id))
    for ref, adder in resolution.adders.items():
        logger.info("project %s: %s added by %s%s", project.project_id, ref, adder, "" if apply else " (dry-run)")
    for ref in resolution.unknown:
        logger.info("project %s: %s predates the history; left unattributed", project.project_id, ref)
    if apply and resolution.adders:
        updated = await update_assistant(
            assistant_id=harness.assistant_id,
            owner_id=harness.owner_id,
            bindings=apply_adders(harness.bindings or [], resolution.adders),
        )
        if updated is None:
            logger.error("project %s: write failed", project.project_id)
    return resolution


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--projects-table", required=True, help="projects DynamoDB table")
    parser.add_argument("--assistants-table", required=True, help="rag-assistants DynamoDB table (harnesses + versions)")
    parser.add_argument("--project", help="Only process this project id")
    parser.add_argument("--apply", action="store_true", help="Write the adders (default: dry-run)")
    parser.add_argument("--sleep", type=float, default=0.05, help="Seconds between projects")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # The version repository and the assistant service bind their table from the environment.
    os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"] = args.assistants_table
    os.environ["DYNAMODB_PROJECTS_TABLE_NAME"] = args.projects_table

    projects = attributed = harnesses = unknown = 0
    for project in _iter_projects(args.projects_table, args.project):
        projects += 1
        resolution = asyncio.run(_process(project, args.assistants_table, args.apply))
        attributed += len(resolution.adders)
        harnesses += 1 if resolution.adders else 0
        unknown += len(resolution.unknown)
        time.sleep(args.sleep)

    logger.info(
        "%d projects scanned; %d skill bindings %s on %d harnesses; %d left unattributed (predate the history)",
        projects, attributed, "written" if args.apply else "would be written", harnesses, unknown,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
