"""Backfill: give existing Agent and project bindings the script reviews they need (skill-code-sandbox PR-2d).

``script_trust.script_verdict`` lets someone else's skill script run only with an
approved review row for the turn's assistant at the script set's hash
(``SCRIPTS_REVIEW#AST#{assistantId}#{hash}``, §8.4). Saves since PR-2c write those
rows, but bindings made before then have none. The Word tools run those scripts
in production today, in shadow (D14). Without this backfill they would refuse
for every member on the release that enforces (PR-2g).

For every skill binding on an Agent or a project harness (the assistants table's
``SK=METADATA`` rows), where the skill is a **user** skill with at least one
``script`` manifest entry, this script does the following:

* **Pins a live binding** at its current content through ``pinning.pin_current``,
  and writes ``config.version: n`` onto the binding (§8.3, D5: a review always
  names a frozen version). The write is one conditional ``UpdateItem`` on the
  row's ``bindings``, so a save that lands mid-run makes the write fail rather
  than be overwritten. Rerun the script and it picks the binding up again.
* **Records a review** with ``method: backfill`` at the pinned version's hash,
  through ``script_reviews._put_review``, and writes ``skill.scripts_reviewed``.
  The reviewer is the person the scripts already run on behalf of:

  - an Agent: its owner (invoke-through clause 3);
  - a project: the binding's ``addedBy`` (clause 4), or the harness's owner (the
    project's creator, clause 3) when the adder didn't write the skill or isn't
    recorded.

  A binding whose reviewer didn't write the skill is **skipped**
  (``owner_mismatch``). Its scripts don't resolve for members today
  (``skills/access.py``), so there is nothing to keep working.
* **Covers a published Agent's snapshot.** Members of a published Agent run its
  published ``AgentVersion``, not the draft row. A skill pinned there is reviewed
  at that version's hash. A skill bound live there is reviewed at the live hash
  but can't be pinned: a published snapshot is immutable. That row says
  ``published_live``, and the next publish carries the draft's pin.
* **Fills a missing ``contentHash``** on the live skill row by hashing the file's
  S3 bytes (``resource_store.compute_content_hash``). ``scripts_hash`` is ``None``
  until every script has one, and ``pin_current`` can't freeze a file without
  one. The write is conditional on the entry still having no hash at the same
  ``s3Key``, and it doesn't bump ``updatedAt``: the bytes didn't change.

Skipped, with a reason: a catalog skill (``catalog``, it runs as ``admin``), a
binding already reviewed for that assistant and hash (``already_reviewed``), a
pin whose version is missing (``pinned_version_missing``), and a pinned version
whose scripts carry no hash (``pinned_version_unhashed``).

**A project's pin cuts no settings version.** A project's skills save normally
goes through ``HarnessSettingsService``, which cuts a settings version. This script
writes the harness row directly instead, for three reasons. The service needs an
authorized editor and re-runs ``validate_agent_write`` as that person. It would
record ``authored`` reviews, not ``backfill`` ones. And the project history
attributes each version to a person, while a version with no author reads
"Starting point". The pin itself is on the binding (``config.version``), where
Settings reads it ("Pinned to vN"), on the review row (``version``), and on the
audit record.

SAFETY
------
* **Dry run by default.** It reads only (DynamoDB, plus S3 for a missing hash)
  and prints what it would do. ``--apply`` writes.
* **Content-free output.** Ids, kinds, versions, eight hex characters of a hash,
  an action and a reason. No names, instructions or file contents.
* **Idempotent.** A second ``--apply`` finds every binding pinned and reviewed,
  and writes nothing.
* **Exit status.** Non-zero if any write (or the S3 read a fill needs) failed.

Runbook (dev, then prod after the release carrying PR-2b and PR-2c deploys, and
before PR-2g enforces)::

    cd backend
    AWS_PROFILE=dev-ai AWS_REGION=us-west-2 uv run --no-sync python scripts/script_trust_backfill.py \\
        --prefix dev-boisestateai-v2                     # dry run
    AWS_PROFILE=dev-ai AWS_REGION=us-west-2 uv run --no-sync python scripts/script_trust_backfill.py \\
        --prefix dev-boisestateai-v2 --apply

``--prefix`` derives the four resource names. Pass ``--assistants-table``,
``--app-roles-table``, ``--audit-table`` or ``--skill-resources-bucket`` to
override one.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apis.shared.auth.models import User  # noqa: E402
from apis.shared.skills import script_reviews as sr  # noqa: E402
from apis.shared.skills import script_trust as st  # noqa: E402
from apis.shared.skills.models import SYSTEM_OWNER_ID  # noqa: E402
from apis.shared.skills.pins import (  # noqa: E402
    PIN_CONFIG_KEY,
    binding_adder,
    binding_pin,
    split_pinned_ref,
)

logger = logging.getLogger("script_trust_backfill")

SKILL = "skill"
KIND_AGENT = "agent"
KIND_PROJECT = "project"
SOURCE_DRAFT = "draft"

# Actions.
PIN_AND_REVIEW = "pin+review"
REVIEW = "review"
PIN = "pin"
SKIP = "skip"

# Reasons.
CATALOG = "catalog"
OWNER_MISMATCH = "owner_mismatch"
ALREADY_REVIEWED = "already_reviewed"
PINNED_VERSION_MISSING = "pinned_version_missing"
PINNED_VERSION_UNHASHED = "pinned_version_unhashed"
PUBLISHED_LIVE = "published_live"
HASH_UNAVAILABLE = "hash_unavailable"

#: The audit actor: the convention for writes no person made (``projects/schedules.py``).
BACKFILL_ACTOR = User(email="", user_id="system", name="Script trust backfill", roles=[])


# --------------------------------------------------------------------------- plan
@dataclass
class Target:
    """One skill binding a member's turn runs, and what the backfill does about it."""

    assistant_id: str
    kind: str  # agent | project
    source: str  # draft | published vN
    skill_id: str
    pinned: Optional[int]  # the binding's version, None for a live binding
    action: str = SKIP
    reason: str = ""
    reviewer: Optional[str] = None
    scripts_hash: Optional[str] = None
    # Filled by the apply step.
    version: Optional[int] = None
    error: Optional[str] = None
    # The index of this binding on the row's stored ``bindings`` (a draft pin only).
    binding_index: Optional[int] = None

    @property
    def binding(self) -> str:
        return f"pinned v{self.pinned}" if self.pinned else "live"


@dataclass
class Assistant:
    """What the backfill reads from one ``SK=METADATA`` row."""

    assistant_id: str
    owner_id: str
    kind: str
    bindings: List[Dict[str, Any]]
    published_version: Optional[int] = None


@dataclass
class Report:
    targets: List[Target] = field(default_factory=list)
    # skill_id -> {resource index: (s3Key, sha256)} a fill writes
    fills: Dict[str, Dict[int, Tuple[str, str]]] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.errors) or any(t.error for t in self.targets)


def read_assistant(item: Dict[str, Any]) -> Optional[Assistant]:
    """The fields the backfill needs from a raw assistants row, or ``None`` if it binds no skill."""
    if item.get("SK") != "METADATA":
        return None
    bindings = [b for b in item.get("bindings") or [] if isinstance(b, dict)]
    published = (item.get("listing") or {}).get("publishedVersion")
    if not published and not any(b.get("kind") == SKILL for b in bindings):
        return None
    return Assistant(
        assistant_id=str(item.get("assistantId") or str(item.get("PK", "")).removeprefix("AST#")),
        owner_id=str(item.get("ownerId") or ""),
        kind=KIND_PROJECT if item.get("kind") == "project" else KIND_AGENT,
        bindings=bindings,
        published_version=int(published) if published else None,
    )


def binding_version(binding: Any) -> Tuple[str, Optional[int]]:
    """``(skill_id, pinned version or None)`` for a raw binding or an ``AgentBinding``."""
    ref = binding.get("ref") if isinstance(binding, dict) else getattr(binding, "ref", "")
    config = binding.get("config") if isinstance(binding, dict) else getattr(binding, "config", None)
    skill_id, ref_version = split_pinned_ref(str(ref or ""))
    return skill_id, binding_pin(config) or ref_version


def reviewer_for(assistant: Assistant, binding: Any, skill_owner: str) -> Optional[str]:
    """Who the scripts already run on behalf of, or ``None`` when nobody who wrote them is on record.

    Mirrors ``resolve_invocable_skill_ids``: an Agent's owner (clause 3), or on a
    project the binding's adder (clause 4) and then the harness's owner (clause 3).
    ``system`` is never a reviewer.
    """
    candidates: List[Optional[str]] = []
    if assistant.kind == KIND_PROJECT:
        config = binding.get("config") if isinstance(binding, dict) else getattr(binding, "config", None)
        candidates.append(binding_adder(config))
    candidates.append(assistant.owner_id)
    for candidate in candidates:
        if candidate and candidate != SYSTEM_OWNER_ID and candidate == skill_owner:
            return candidate
    return None


def _skill_bindings(bindings: Iterable[Any]) -> List[Tuple[int, Any]]:
    out = []
    for index, binding in enumerate(bindings):
        kind = binding.get("kind") if isinstance(binding, dict) else getattr(binding, "kind", None)
        if kind == SKILL:
            out.append((index, binding))
    return out


async def _missing_hashes(record: Any, store: Any) -> Dict[int, Tuple[str, str]]:
    """``{resource index: (s3Key, sha256)}`` for entries with no ``contentHash``, hashed from S3."""
    from apis.shared.skills.resource_store import compute_content_hash

    out: Dict[int, Tuple[str, str]] = {}
    for index, ref in enumerate(getattr(record, "resources", None) or []):
        if ref.content_hash:
            continue
        content = await asyncio.to_thread(store.get, ref.s3_key)
        out[index] = (ref.s3_key, compute_content_hash(content))
    return out


def _with_hashes(record: Any, filled: Dict[int, Tuple[str, str]]) -> Any:
    if not filled:
        return record
    resources = [
        ref.model_copy(update={"content_hash": filled[i][1]}) if i in filled else ref
        for i, ref in enumerate(record.resources)
    ]
    return record.model_copy(update={"resources": resources})


async def plan(
    assistants: Sequence[Assistant],
    *,
    repository: Any,
    store: Any,
    roles_table: Any,
    load_version: Any,
) -> Report:
    """Decide every target. Reads only: DynamoDB, and S3 for an entry with no hash.

    ``load_version(assistant_id, n)`` returns an ``AgentVersion`` or ``None`` (a
    published Agent's snapshot).
    """
    from apis.shared.skills.versions import apply_version

    report = Report()
    sources: List[Tuple[Assistant, str, List[Tuple[int, Any]]]] = []
    for assistant in assistants:
        sources.append((assistant, SOURCE_DRAFT, _skill_bindings(assistant.bindings)))
        if assistant.kind == KIND_AGENT and assistant.published_version:
            snapshot = await load_version(assistant.assistant_id, assistant.published_version)
            if snapshot is None:
                report.errors.append(
                    f"{assistant.assistant_id}: published version {assistant.published_version} not found"
                )
            else:
                sources.append(
                    (assistant, f"published v{assistant.published_version}", _skill_bindings(snapshot.bindings or []))
                )

    skill_ids = sorted({binding_version(b)[0] for _, _, bs in sources for _, b in bs})
    live = {r.skill_id: r for r in await repository.batch_get_skills(skill_ids)} if skill_ids else {}

    # Fill missing hashes on each user skill with scripts, once per skill.
    for skill_id, record in sorted(live.items()):
        if record.owner_id == SYSTEM_OWNER_ID or not st.has_scripts(record):
            continue
        try:
            filled = await _missing_hashes(record, store)
        except Exception as e:  # noqa: BLE001 - reported, and the skill's targets skip
            report.errors.append(f"skill {skill_id}: a file with no contentHash could not be read ({type(e).__name__})")
            continue
        if filled:
            report.fills[skill_id] = filled
            live[skill_id] = _with_hashes(record, filled)

    pinned_needed = sorted(
        {binding_version(b) for _, _, bs in sources for _, b in bs if binding_version(b)[1]}
    )
    versions = await repository.batch_get_skill_versions(pinned_needed) if pinned_needed else {}

    seen: set = set()
    for assistant, source, bindings in sources:
        for index, binding in bindings:
            skill_id, pinned = binding_version(binding)
            record = live.get(skill_id)
            if record is None or not st.has_scripts(record):
                continue
            key = (assistant.assistant_id, source, skill_id)
            if key in seen:
                continue
            seen.add(key)
            target = Target(assistant.assistant_id, assistant.kind, source, skill_id, pinned)
            report.targets.append(target)

            if record.owner_id == SYSTEM_OWNER_ID:
                target.reason = CATALOG
                continue
            target.reviewer = reviewer_for(assistant, binding, record.owner_id)
            if target.reviewer is None:
                target.reason = OWNER_MISMATCH
                continue

            if pinned:
                version = versions.get((skill_id, pinned))
                if version is None:
                    target.reason = PINNED_VERSION_MISSING
                    continue
                target.scripts_hash = st.scripts_hash(apply_version(record, version))
                target.version = pinned
                if target.scripts_hash is None:
                    target.reason = PINNED_VERSION_UNHASHED
                    continue
                target.action = REVIEW
            else:
                target.scripts_hash = st.scripts_hash(record)
                if target.scripts_hash is None:
                    target.reason = HASH_UNAVAILABLE
                    continue
                if source == SOURCE_DRAFT:
                    target.action = PIN_AND_REVIEW
                    target.binding_index = index
                else:
                    target.action = REVIEW
                    target.reason = PUBLISHED_LIVE
            if skill_id in report.fills:
                fill = f"fills {len(report.fills[skill_id])} hash(es)"
                target.reason = f"{target.reason}; {fill}" if target.reason else fill

    # Existing reviews, and two targets that would write the same row (a draft pin and
    # a published live binding at the same hash), write once.
    keys = [
        (t, (st.skill_pk(t.skill_id), st.review_sk(st.SCOPE_ASSISTANT, t.assistant_id, t.scripts_hash)))
        for t in report.targets
        if t.action in (REVIEW, PIN_AND_REVIEW)
    ]
    existing = await asyncio.to_thread(sr._existing_reviews, [key for _, key in keys], roles_table)
    planned: set = set()
    for target, key in keys:
        if key in existing or key in planned:
            target.action = PIN if target.action == PIN_AND_REVIEW else SKIP
            target.reason = ALREADY_REVIEWED if not target.reason else f"{ALREADY_REVIEWED}; {target.reason}"
            continue
        planned.add(key)
    return report


# --------------------------------------------------------------------------- apply
def _fill_hashes(roles_table: Any, skill_id: str, filled: Dict[int, Tuple[str, str]]) -> None:
    """Write the hashes, each only where the entry still has none at the same ``s3Key``."""
    sets, conditions = [], []
    values: Dict[str, Any] = {":empty": ""}
    for n, (index, (s3_key, digest)) in enumerate(sorted(filled.items())):
        sets.append(f"resources[{index}].contentHash = :h{n}")
        conditions.append(
            f"resources[{index}].s3Key = :k{n} AND "
            f"(attribute_not_exists(resources[{index}].contentHash) OR resources[{index}].contentHash = :empty)"
        )
        values[f":h{n}"] = digest
        values[f":k{n}"] = s3_key
    roles_table.update_item(
        Key={"PK": st.skill_pk(skill_id), "SK": "METADATA"},
        UpdateExpression="SET " + ", ".join(sets),
        ConditionExpression=" AND ".join(f"({c})" for c in conditions),
        ExpressionAttributeValues=values,
    )


def _write_pins(assistants_table: Any, assistant: Assistant, pins: Dict[int, int]) -> None:
    """Set ``config.version`` on the bindings at ``pins``' indexes, if the row's bindings haven't changed."""
    new = []
    for index, binding in enumerate(assistant.bindings):
        if index in pins:
            binding = {**binding, "config": {**dict(binding.get("config") or {}), PIN_CONFIG_KEY: pins[index]}}
        new.append(binding)
    assistants_table.update_item(
        Key={"PK": f"AST#{assistant.assistant_id}", "SK": "METADATA"},
        UpdateExpression="SET bindings = :new",
        ConditionExpression="bindings = :old",
        ExpressionAttributeValues={":new": new, ":old": assistant.bindings},
    )


def _review(roles_table: Any, target: Target, scripts: Dict[str, str]) -> None:
    sr._put_review(
        target.skill_id,
        target.assistant_id,
        target.scripts_hash,
        reviewed_by=target.reviewer,
        method=sr.METHOD_BACKFILL,
        version=target.version,
        scripts=scripts,
        table=roles_table,
    )
    audited = sr._audit(
        sr.AuditAction.SKILL_SCRIPTS_REVIEWED,
        BACKFILL_ACTOR,
        target.skill_id,
        sr.review_audit_fields(
            scripts_hash=target.scripts_hash,
            scripts=scripts,
            scope_kind=target.kind,
            scope_id=target.assistant_id,
            version=target.version,
            method=sr.METHOD_BACKFILL,
            reviewed_by=target.reviewer,
        ),
    )
    if not audited:
        raise RuntimeError("the skill.scripts_reviewed audit record was not written")


async def apply(
    report: Report,
    assistants: Sequence[Assistant],
    *,
    repository: Any,
    roles_table: Any,
    assistants_table: Any,
) -> None:
    """Write what :func:`plan` decided. Records each failure on its target; never raises."""
    from apis.shared.skills.pinning import pin_current
    from apis.shared.skills.versions import apply_version

    failed_fills = set()
    for skill_id, filled in sorted(report.fills.items()):
        try:
            await asyncio.to_thread(_fill_hashes, roles_table, skill_id, filled)
        except Exception as e:  # noqa: BLE001
            failed_fills.add(skill_id)
            report.errors.append(f"skill {skill_id}: contentHash fill failed ({type(e).__name__})")

    by_id = {a.assistant_id: a for a in assistants}
    by_assistant: Dict[str, List[Target]] = {}
    for target in report.targets:
        if target.action != SKIP:
            by_assistant.setdefault(target.assistant_id, []).append(target)

    for assistant_id, targets in by_assistant.items():
        assistant = by_id[assistant_id]
        live_targets = [t for t in targets if t.action in (PIN, PIN_AND_REVIEW)]
        pins: Dict[int, int] = {}
        for target in targets:
            if target.skill_id in failed_fills:
                target.error = "contentHash fill failed"
        for target in live_targets:
            if target.error:
                continue
            try:
                version = await pin_current(target.skill_id, pinned_by=target.reviewer)
                record = await repository.get_skill(target.skill_id)
                digest = st.scripts_hash(apply_version(record, version)) if record else None
                if digest is None:
                    raise RuntimeError("the pinned version has no scripts hash")
                if digest != target.scripts_hash and target.action == PIN:
                    # The skill changed since the plan, so the review that exists is for
                    # other scripts: this binding needs its own.
                    target.action = PIN_AND_REVIEW
                target.version, target.scripts_hash = version.version, digest
                pins[target.binding_index] = version.version
            except Exception as e:  # noqa: BLE001
                target.error = f"pin failed ({type(e).__name__})"
        if pins:
            try:
                await asyncio.to_thread(_write_pins, assistants_table, assistant, pins)
            except Exception as e:  # noqa: BLE001 - e.g. a save landed mid-run; rerun
                for target in live_targets:
                    if target.binding_index in pins:
                        target.error = f"binding write failed ({type(e).__name__})"

        for target in targets:
            if target.error or target.action not in (REVIEW, PIN_AND_REVIEW):
                continue
            try:
                if target.action == PIN_AND_REVIEW or target.pinned:
                    version = await repository.get_skill_version(target.skill_id, target.version)
                    record = await repository.get_skill(target.skill_id)
                    effective = apply_version(record, version)
                else:
                    effective = await repository.get_skill(target.skill_id)
                if st.scripts_hash(effective) != target.scripts_hash:
                    raise RuntimeError("the skill's scripts changed since the plan; rerun")
                await asyncio.to_thread(_review, roles_table, target, dict(st.script_entries(effective)))
            except Exception as e:  # noqa: BLE001
                target.error = f"review failed ({type(e).__name__}: {e})"[:200]


# --------------------------------------------------------------------------- output
COLUMNS = ("assistant", "kind", "source", "skill", "binding", "hash", "action", "reason")


def render(report: Report, *, applied: bool) -> str:
    rows = [
        (
            t.assistant_id,
            t.kind,
            t.source,
            t.skill_id,
            t.binding,
            st.short_hash(t.scripts_hash),
            t.action,
            f"ERROR: {t.error}" if t.error else t.reason,
        )
        for t in sorted(report.targets, key=lambda t: (t.kind, t.assistant_id, t.source, t.skill_id))
    ]
    widths = [max([len(c)] + [len(str(r[i])) for r in rows]) for i, c in enumerate(COLUMNS)]
    line = lambda cells: "  ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()  # noqa: E731
    out = [line(COLUMNS), line("-" * w for w in widths)] + [line(r) for r in rows]
    counts: Dict[str, int] = {}
    for t in report.targets:
        counts[t.action] = counts.get(t.action, 0) + 1
    fills = sum(len(f) for f in report.fills.values())
    verb = "written" if applied else "would be written"
    out.append("")
    out.append(
        f"{len(report.targets)} binding(s) of skills with scripts: "
        + ", ".join(f"{n} {a}" for a, n in sorted(counts.items()))
        + f"; {fills} contentHash fill(s) {verb}."
    )
    for error in report.errors:
        out.append(f"ERROR: {error}")
    if not applied:
        out.append("Dry run: nothing was written. Pass --apply to write.")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- AWS side
def scan_assistants(assistants_table: Any, only: Optional[str] = None) -> List[Assistant]:
    from boto3.dynamodb.conditions import Attr

    if only:
        item = assistants_table.get_item(Key={"PK": f"AST#{only}", "SK": "METADATA"}).get("Item")
        found = read_assistant(item) if item else None
        return [found] if found else []
    out: List[Assistant] = []
    kwargs: Dict[str, Any] = {"FilterExpression": Attr("SK").eq("METADATA")}
    while True:
        response = assistants_table.scan(**kwargs)
        for item in response.get("Items", []):
            found = read_assistant(item)
            if found:
                out.append(found)
        if not response.get("LastEvaluatedKey"):
            return out
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


async def run(*, apply_changes: bool, only: Optional[str] = None) -> Report:
    """Plan and, with ``apply_changes``, write. Tables and the bucket come from the environment."""
    import boto3

    from apis.shared.assistants.version_repository import get_version
    from apis.shared.skills.repository import get_skill_catalog_repository
    from apis.shared.skills.resource_store import get_skill_resource_store

    dynamodb = boto3.resource("dynamodb")
    assistants_table = dynamodb.Table(os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"])
    roles_table = dynamodb.Table(os.environ["DYNAMODB_APP_ROLES_TABLE_NAME"])
    repository = get_skill_catalog_repository()

    assistants = await asyncio.to_thread(scan_assistants, assistants_table, only)
    report = await plan(
        assistants,
        repository=repository,
        store=get_skill_resource_store(),
        roles_table=roles_table,
        load_version=get_version,
    )
    if apply_changes:
        await apply(
            report, assistants, repository=repository, roles_table=roles_table, assistants_table=assistants_table
        )
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prefix", help="Resource prefix, e.g. dev-boisestateai-v2")
    parser.add_argument("--assistants-table", help="Default: {prefix}-rag-assistants")
    parser.add_argument("--app-roles-table", help="Default: {prefix}-app-roles")
    parser.add_argument("--audit-table", help="Default: {prefix}-audit-log")
    parser.add_argument("--skill-resources-bucket", help="Default: {prefix}-skill-resources")
    parser.add_argument("--assistant", help="Only this assistant id")
    parser.add_argument("--apply", action="store_true", help="Write (default: dry run)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    names = {
        "DYNAMODB_ASSISTANTS_TABLE_NAME": args.assistants_table or (args.prefix and f"{args.prefix}-rag-assistants"),
        "DYNAMODB_APP_ROLES_TABLE_NAME": args.app_roles_table or (args.prefix and f"{args.prefix}-app-roles"),
        "DYNAMODB_AUDIT_LOG_TABLE_NAME": args.audit_table or (args.prefix and f"{args.prefix}-audit-log"),
        "S3_SKILL_RESOURCES_BUCKET_NAME": args.skill_resources_bucket or (args.prefix and f"{args.prefix}-skill-resources"),
    }
    missing = [k for k, v in names.items() if not v]
    if missing:
        parser.error(f"set --prefix or pass names for: {', '.join(missing)}")
    os.environ.update({k: v for k, v in names.items() if v})

    report = asyncio.run(run(apply_changes=args.apply, only=args.assistant))
    sys.stdout.write(render(report, applied=args.apply))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
