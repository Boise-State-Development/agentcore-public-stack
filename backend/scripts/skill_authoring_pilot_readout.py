#!/usr/bin/env python3
"""Read out a skill-authoring pilot (docs/specs/skill-creator.md, PR-D gate).

The spec widens the Skill Creator and the ``my_skill_*`` tools only "once the
pilot says the skills produced are worth keeping". This script answers that
from data the platform already records. It is **read-only**: it scans the
app-roles table, reads skill rows, and runs one CloudWatch Logs Insights query.

What it reports:

- **Reach:** which roles grant ``my_skill_create`` and ``skill_creator``
  (explicitly, or through a ``*`` wildcard).
- **Opt-in:** how many users turned the tool and the skill on (their
  ``TOOL_PREFERENCES`` / ``SKILL_PREFERENCES`` rows).
- **Writes:** per tool, how many approval prompts users saw, how many writes
  went through, and the difference (declined, or failed after approval). From
  the runtime's ``Pausing for user approval`` and ``<tool> applied|created``
  log lines.
- **Skills created through the tools:** for each, whether it still exists,
  whether its owner turned it on, whether it was edited after creation, and its
  file count. "Kept" means it still exists at least ``--kept-days`` after it was
  created.

Not covered, and why: how often a created skill was *activated* later. Skill
activations are not logged per skill today, so that needs a log line in the
skills plugin before it can be counted.

Usage:
    AWS_PROFILE=dev-ai python backend/scripts/skill_authoring_pilot_readout.py \\
        --prefix dev-boisestateai-v2 --since 2026-10-12

``--json`` prints the raw summary instead of the table.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

TOOL_ID = "my_skill_create"
SKILL_ID = "skill_creator"
WRITE_TOOLS = ("my_skill_create", "my_skill_update", "my_skill_write_file", "my_skill_delete_file")

# Shapes written by agents/builtin_tools/skill_authoring and the approval hook.
_PAUSE_RE = re.compile(r"Pausing for user approval: tool=(my_skill_\w+) tool_use_id=(\S+)")
_APPLIED_RE = re.compile(r"\b(my_skill_\w+) (?:applied|created) ([a-z][a-z0-9_]*)\b")

LOG_QUERY = (
    "fields @timestamp, body"
    " | filter body like /Pausing for user approval: tool=my_skill_/"
    " or body like /my_skill_\\w+ (applied|created) /"
    " | sort @timestamp asc | limit 10000"
)


@dataclass
class CreatedSkill:
    skill_id: str
    exists: bool
    owner_turned_on: bool = False
    edited_after_create: bool = False
    files: int = 0
    age_days: Optional[float] = None
    kept: bool = False


@dataclass
class Readout:
    since: str
    roles_granting_tool: List[str]
    roles_granting_skill: List[str]
    users_tool_on: int
    users_skill_on: int
    approvals_asked: Dict[str, int]
    writes_applied: Dict[str, int]
    writes_not_applied: Dict[str, int]
    created: List[CreatedSkill] = field(default_factory=list)

    @property
    def kept(self) -> int:
        return sum(1 for s in self.created if s.kept)


# ---------------------------------------------------------------------------
# Pure parts (unit-tested)
# ---------------------------------------------------------------------------


def roles_granting(role_items: Iterable[Dict[str, Any]], kind: str, wanted: str) -> List[str]:
    """Role ids whose effective ``kind`` ("tools"/"skills") reaches ``wanted``."""
    out = []
    for item in role_items:
        if item.get("enabled") is False:
            continue
        effective = (item.get("effectivePermissions") or {}).get(kind) or []
        if wanted in effective or "*" in effective:
            out.append(str(item.get("roleId") or str(item.get("PK", "")).removeprefix("ROLE#")))
    return sorted(out)


def count_opted_in(pref_items: Iterable[Dict[str, Any]], attribute: str, wanted: str) -> int:
    return sum(1 for item in pref_items if (item.get(attribute) or {}).get(wanted) is True)


def parse_log_lines(lines: Iterable[str]):
    """``(asked, applied, created_ids)`` from runtime log bodies.

    A pause is logged on the first pass and again when the resumed turn re-enters
    the hook, so asks are counted per unique ``tool_use_id``.
    """
    asked: Dict[str, set] = defaultdict(set)
    applied: Counter = Counter()
    created: List[str] = []
    for line in lines:
        pause = _PAUSE_RE.search(line)
        if pause:
            asked[pause.group(1)].add(pause.group(2))
            continue
        done = _APPLIED_RE.search(line)
        if done:
            applied[done.group(1)] += 1
            if done.group(1) == "my_skill_create" and done.group(2) not in created:
                created.append(done.group(2))
    return {t: len(ids) for t, ids in asked.items()}, dict(applied), created


def classify_created(
    skill_id: str,
    row: Optional[Dict[str, Any]],
    owner_prefs: Optional[Dict[str, Any]],
    now: datetime,
    kept_days: int,
) -> CreatedSkill:
    if not row:
        return CreatedSkill(skill_id=skill_id, exists=False)
    created_at = _parse_ts(row.get("createdAt"))
    updated_at = _parse_ts(row.get("updatedAt"))
    age = (now - created_at).total_seconds() / 86400 if created_at else None
    return CreatedSkill(
        skill_id=skill_id,
        exists=True,
        owner_turned_on=((owner_prefs or {}).get("skillPreferences") or {}).get(skill_id) is True,
        # A write a minute or more after creation is an edit, not the create's own
        # follow-up file write in the same breath.
        edited_after_create=bool(created_at and updated_at and updated_at - created_at > timedelta(minutes=1)),
        files=len(row.get("resources") or []),
        age_days=round(age, 1) if age is not None else None,
        kept=age is not None and age >= kept_days,
    )


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def render(readout: Readout) -> str:
    lines = [
        f"Skill authoring pilot readout since {readout.since}",
        "",
        f"Roles granting {TOOL_ID}: {', '.join(readout.roles_granting_tool) or '(none)'}",
        f"Roles granting {SKILL_ID}: {', '.join(readout.roles_granting_skill) or '(none)'}",
        f"Users who turned the tool on: {readout.users_tool_on}",
        f"Users who turned the Skill Creator on: {readout.users_skill_on}",
        "",
        f"{'tool':24s} {'asked':>6s} {'applied':>8s} {'not applied':>12s}",
    ]
    for tool in WRITE_TOOLS:
        lines.append(
            f"{tool:24s} {readout.approvals_asked.get(tool, 0):6d} "
            f"{readout.writes_applied.get(tool, 0):8d} {readout.writes_not_applied.get(tool, 0):12d}"
        )
    lines += ["", f"Skills created through the tools: {len(readout.created)} (kept: {readout.kept})"]
    for s in readout.created:
        if not s.exists:
            lines.append(f"  {s.skill_id}: deleted")
            continue
        lines.append(
            f"  {s.skill_id}: age {s.age_days}d, files {s.files}, "
            f"turned on {'yes' if s.owner_turned_on else 'no'}, "
            f"edited later {'yes' if s.edited_after_create else 'no'}, "
            f"kept {'yes' if s.kept else 'no'}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AWS reads
# ---------------------------------------------------------------------------


def _scan(table, **kwargs) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _log_bodies(session, prefix: str, since: datetime) -> List[str]:
    runtime_id = session.client("ssm").get_parameter(
        Name=f"/{prefix}/inference-api/runtime-id"
    )["Parameter"]["Value"]
    logs = session.client("logs")
    query_id = logs.start_query(
        logGroupName=f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT",
        startTime=int(since.timestamp()),
        endTime=int(time.time()),
        queryString=LOG_QUERY,
    )["queryId"]
    while True:
        result = logs.get_query_results(queryId=query_id)
        if result["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(1)
    if result["status"] != "Complete":
        raise RuntimeError(f"Logs Insights query ended {result['status']}")
    return [
        field_["value"]
        for row in result["results"]
        for field_ in row
        if field_["field"] == "body"
    ]


def collect(session, prefix: str, since: datetime, kept_days: int) -> Readout:
    from boto3.dynamodb.conditions import Attr

    table = session.resource("dynamodb").Table(f"{prefix}-app-roles")
    roles = _scan(table, FilterExpression=Attr("SK").eq("DEFINITION") & Attr("PK").begins_with("ROLE#"))
    prefs = _scan(table, FilterExpression=Attr("SK").is_in(["TOOL_PREFERENCES", "SKILL_PREFERENCES"]))
    tool_prefs = [p for p in prefs if p["SK"] == "TOOL_PREFERENCES"]
    skill_prefs = {p["PK"]: p for p in prefs if p["SK"] == "SKILL_PREFERENCES"}

    asked, applied, created_ids = parse_log_lines(_log_bodies(session, prefix, since))
    now = datetime.now(timezone.utc)
    created = []
    for skill_id in created_ids:
        row = table.get_item(Key={"PK": f"SKILL#{skill_id}", "SK": "METADATA"}).get("Item")
        owner = row.get("ownerId") if row else None
        created.append(classify_created(skill_id, row, skill_prefs.get(f"USER#{owner}"), now, kept_days))

    return Readout(
        since=since.date().isoformat(),
        roles_granting_tool=roles_granting(roles, "tools", TOOL_ID),
        roles_granting_skill=roles_granting(roles, "skills", SKILL_ID),
        users_tool_on=count_opted_in(tool_prefs, "toolPreferences", TOOL_ID),
        users_skill_on=count_opted_in(skill_prefs.values(), "skillPreferences", SKILL_ID),
        approvals_asked=asked,
        writes_applied=applied,
        writes_not_applied={t: max(asked.get(t, 0) - applied.get(t, 0), 0) for t in WRITE_TOOLS},
        created=created,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prefix", required=True, help="Resource prefix, e.g. dev-boisestateai-v2")
    parser.add_argument("--since", required=True, help="Pilot start date (YYYY-MM-DD, UTC)")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--kept-days", type=int, default=7, help="Age at which an existing skill counts as kept")
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON")
    args = parser.parse_args(argv)

    import boto3

    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    readout = collect(boto3.Session(region_name=args.region), args.prefix, since, args.kept_days)
    if args.json:
        print(json.dumps({**asdict(readout), "kept": readout.kept}, indent=2, default=str))
    else:
        print(render(readout))
    return 0


if __name__ == "__main__":
    sys.exit(main())
