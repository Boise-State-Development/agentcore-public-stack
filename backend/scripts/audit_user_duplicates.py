"""Audit — and, only when asked, retire — duplicate PROFILE rows in the users table.

Some emails own more than one ``USER#<id>/PROFILE`` row. The pre-Cognito login
(~March to mid-April 2026) keyed users by a numeric employee ID
(``USER#<employee id>``); the BFF/Cognito login keys them by the ``sub`` uuid, and
nothing migrated or retired the old rows. ``UserRepository.get_users_by_email``
now ranks them deterministically, so the API is correct with the duplicates in
place — this script exists to clean them up safely, not to fix a live bug.

WHAT IT DOES
------------
1. Scans the users table (PROFILE rows only — a few thousand small items) and
   groups rows by email. Every group with more than one row is ranked with
   ``live_profile_rank`` — the exact function the API uses — so the script and
   the API always agree on which row is live.
2. Classifies each group:
   * ``legacy_pair`` — exactly two rows, the live one a uuid and the other a
     numeric legacy id. The only kind this script will ever act on.
   * ``needs_review`` — anything else (three or more rows, two uuids, a
     numeric row that logged in more recently than its uuid twin). Probably
     Cognito user re-creation; reported with a Cognito lookup per id, never
     touched.
3. For every stale row, checks whether anything still references that user id:
   one cheap Query per known key path (see ``QUERY_CHECKS``), an S3 prefix
   probe per user-keyed bucket, AgentCore Memory sessions for the actor, and —
   with ``--deep`` — one Scan per table for un-indexed attributes.
4. Looks for evidence the old id is *still in use*, because ``lastLoginAt``
   cannot show it: an API key minted under the old id still authenticates as
   that id today, and the api-converse path never touches the profile's
   ``lastLoginAt``. A row is ``in_use`` if it owns an unexpired API key or an
   active scheduled prompt, or if anything under it (a model call, a message,
   a key use) is newer than the day its live twin was created — that person's
   own cutover. The api-converse route also 401s a key whose profile row is
   missing, so deleting an in-use row would break an integration outright.
5. Writes a JSON report (machine-readable) and a Markdown report next to it.

SAFETY
------
* **Read-only by default.** Nothing is written unless ``--apply`` is given.
* **Two-phase, reversible first.** ``--apply mark`` sets the stale row to
  ``status=inactive`` with ``mergedInto=<live id>`` and ``mergedAt``. It stays
  readable and restorable (flip ``status`` back, remove ``mergedInto``).
  ``--apply delete`` deletes only rows a previous run marked, at least
  ``--min-soak-days`` ago, whose ``mergedInto`` still names today's live row.
  (Not ``status=merged``: ``UserStatus`` has no such member, and an unknown
  status makes every read of that email raise.)
* **Only provably unreferenced rows.** A row is eligible only if its group is a
  ``legacy_pair``, its id is numeric, and every check returned zero. A check
  that errored makes the row ``incomplete``, never ``unreferenced``; a check can
  be waived only explicitly (``--waive-check``), and the report records it.
* **Race-safe writes.** Each write is conditional on the row still carrying the
  ``lastLoginAt`` the audit saw.
* **Typo-safe.** ``--apply`` requires ``--confirm-prefix`` equal to
  ``--project-prefix``.
* **Back up first.** The users table has PITR, but take an on-demand backup
  (or run ``scripts/backup-data``) before ``--apply delete``.

The reports contain email addresses. Keep them out of git (``.gitignore``
covers the default file names) and out of shared channels.

Run (a human, not CI — prod is read-only from agents)::

    AWS_PROFILE=prod-ai backend/.venv/bin/python backend/scripts/audit_user_duplicates.py \\
        --project-prefix boisestateai-v2 --region us-west-2                 # audit only
    ... --deep                                                              # + un-indexed scans
    ... --email someone@boisestate.edu --apply mark --confirm-prefix boisestateai-v2
    ... --apply delete --confirm-prefix boisestateai-v2                     # >= 7 days later
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apis.shared.users.repository import item_to_profile, live_profile_rank  # noqa: E402

logger = logging.getLogger("audit_user_duplicates")

BOTO_CONFIG = BotoConfig(
    retries={"max_attempts": 10, "mode": "adaptive"},
    user_agent_extra="agentcore-audit-user-duplicates/1.0",
)

_PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{1,20}$")
_TOKEN_SPLIT = re.compile(r"[#/]")

MERGE_REASON = "legacy-duplicate-profile"


# --------------------------------------------------------------------------- #
# Where user ids live.                                                        #
# Tables are discovered the way scripts/backup-data does it: an SSM parameter #
# under /{prefix}/..., or the {prefix}-{suffix} naming convention.            #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TableSource:
    logical: str
    ssm: Optional[str] = None       # parameter path under /{prefix}
    suffix: Optional[str] = None    # convention name {prefix}-{suffix}
    optional: bool = False          # absent => feature not deployed => no refs


TABLE_SOURCES: Tuple[TableSource, ...] = (
    TableSource("users", ssm="/users/users-table-name"),
    TableSource("api-keys", ssm="/auth/api-keys-table-name"),
    TableSource("app-roles", ssm="/rbac/app-roles-table-name"),
    TableSource("oauth-user-tokens", ssm="/oauth/user-tokens-table-name"),
    TableSource("user-quotas", ssm="/quota/user-quotas-table-name"),
    TableSource("quota-events", ssm="/quota/quota-events-table-name"),
    TableSource("sessions-metadata", ssm="/cost-tracking/sessions-metadata-table-name"),
    TableSource("user-cost-summary", ssm="/cost-tracking/user-cost-summary-table-name"),
    TableSource("system-cost-rollup", ssm="/cost-tracking/system-cost-rollup-table-name"),
    TableSource("user-settings", ssm="/settings/user-settings-table-name"),
    TableSource("user-file-uploads", ssm="/user-file-uploads/table-name"),
    TableSource("shared-conversations", ssm="/shares/shared-conversations-table-name"),
    TableSource("rag-assistants", ssm="/rag/assistants-table-name"),
    TableSource("announcements", ssm="/admin/announcements-table-name", optional=True),
    TableSource("audit-log", ssm="/audit/audit-log-table-name", optional=True),
    TableSource("user-artifacts", ssm="/artifacts/table-name", optional=True),
    TableSource("fine-tuning-jobs", ssm="/fine-tuning/jobs-table-name", optional=True),
    TableSource("memory-spaces", suffix="memory-spaces", optional=True),
    TableSource("projects", suffix="projects", optional=True),
    TableSource("bff-sessions", suffix="bff-sessions"),
)

# CDK table suffixes that hold no user-id reference at all, with the reason.
# tests/test_audit_user_duplicates.py fails when a CDK table is in neither this
# map nor TABLE_SOURCES, so a new user-keyed table can't be silently skipped.
TABLES_WITHOUT_USER_IDS: Dict[str, str] = {
    "fine-tuning-access": "keyed by email (PK=EMAIL#<email>)",
    "auth-providers": "IdP configuration",
    "oauth-providers": "OAuth provider configuration",
    "managed-models": "model catalog",
    "system-prompts": "createdBy is an email",
    "agent-templates": "createdBy is an email",
    "user-menu-links": "createdBy is an email",
    "oidc-state": "ephemeral login state, TTL minutes",
    "voice-ticket-replay": "ephemeral {jti, ttl} rows",
}


@dataclass(frozen=True)
class QueryCheck:
    """One Query that counts rows referencing a user id on a known key path."""

    check_id: str
    table: str
    key: str                         # partition-key attribute (table or index)
    value: str                       # format string; {id} is the user id
    index: Optional[str] = None
    sk_prefix: Optional[str] = None  # restrict to begins_with(SK, prefix)
    by_sk_family: bool = False       # break the count down by SK prefix


QUERY_CHECKS: Tuple[QueryCheck, ...] = (
    QueryCheck("api-keys", "api-keys", "PK", "USER#{id}"),
    QueryCheck("quota-assignments", "user-quotas", "GSI2PK", "USER#{id}", index="UserAssignmentIndex"),
    QueryCheck("quota-overrides", "user-quotas", "GSI4PK", "USER#{id}", index="UserOverrideIndex"),
    QueryCheck("quota-events", "quota-events", "PK", "USER#{id}"),
    QueryCheck("sessions-metadata", "sessions-metadata", "PK", "USER#{id}", by_sk_family=True),
    QueryCheck("cost-summaries", "user-cost-summary", "PK", "USER#{id}"),
    QueryCheck("user-settings", "user-settings", "PK", "USER#{id}", by_sk_family=True),
    QueryCheck("announcement-acks", "announcements", "PK", "USER#{id}", sk_prefix="ACK#"),
    QueryCheck("tool-skill-preferences", "app-roles", "PK", "USER#{id}"),
    QueryCheck("skills-owned", "app-roles", "GSI4PK", "OWNER#{id}", index="SkillOwnerIndex"),
    QueryCheck("audit-actions", "audit-log", "GSI1PK", "ACTOR#{id}", index="ActorIndex"),
    QueryCheck("file-uploads", "user-file-uploads", "PK", "USER#{id}"),
    QueryCheck("artifacts", "user-artifacts", "PK", "USER#{id}"),
    QueryCheck("conversation-shares-owned", "shared-conversations", "owner_id", "{id}", index="OwnerShareIndex"),
    QueryCheck("memory-spaces-owned", "memory-spaces", "GSI1PK", "OWNER#{id}", index="OwnerIndex"),
    QueryCheck("projects-owned", "projects", "GSI1PK", "OWNER#{id}", index="OwnerIndex"),
    QueryCheck("agents-owned", "rag-assistants", "GSI_PK", "OWNER#{id}", index="OwnerStatusIndex"),
    QueryCheck("agent-user-rows", "rag-assistants", "PK", "USER#{id}"),
    QueryCheck("fine-tuning-jobs", "fine-tuning-jobs", "PK", "USER#{id}"),
    QueryCheck("oauth-disconnects", "oauth-user-tokens", "PK", "USER#{id}"),
    QueryCheck("scheduled-run-grants", "bff-sessions", "grant_user_id", "{id}", index="HeadlessGrantUserIndex"),
)


@dataclass(frozen=True)
class BucketSource:
    logical: str
    ssm: str
    prefixes: Tuple[str, ...]        # format strings; {id} is the user id
    optional: bool = False


BUCKET_SOURCES: Tuple[BucketSource, ...] = (
    BucketSource("user-file-uploads", "/user-file-uploads/bucket-name",
                 ("user-files/{id}/", "compaction-offload/{id}/")),
    BucketSource("artifacts", "/artifacts/bucket-name", ("{id}/",), optional=True),
    BucketSource("fine-tuning-data", "/fine-tuning/data-bucket-name",
                 ("datasets/{id}/", "output/{id}/", "checkpoints/{id}/",
                  "inference-input/{id}/", "inference-output/{id}/"), optional=True),
)

SSM_USER_POOL_ID = "/auth/cognito/user-pool-id"
SSM_MEMORY_ID = "/inference-api/memory-id"

# Tables whose user-id references are not all on an indexed key path. --deep
# scans each once for every stale id at the same time (see _references).
DEEP_SCAN_DEFAULT: Tuple[str, ...] = (
    "projects",            # MEMBER#.userId / invitedBy, COST#<period>#USER#<id>
    "rag-assistants",      # reporterId, createdBy, submittedBy, ELIG#<pub>#<id>, ...
    "memory-spaces",       # INDEX.updatedBy
    "app-roles",           # role / role-pin createdBy
    "user-quotas",         # tier / assignment createdBy
    "system-cost-rollup",  # SK = <id> under ACTIVE#DAILY#/MONTHLY#/MODEL#
    "bff-sessions",        # SESSION#.user_id (TTL'd, so normally empty)
    "user-artifacts",      # SHARE#<id>/META.owner_id
)


# --------------------------------------------------------------------------- #
# Report model                                                                #
# --------------------------------------------------------------------------- #
@dataclass
class CheckStatus:
    check_id: str
    kind: str            # "query" | "s3" | "memory" | "cognito" | "deep"
    target: str          # table / bucket / service it reads
    status: str          # "ok" | "not_deployed" | "error" | "waived" | "skipped"
    detail: str = ""


@dataclass
class ProfileRow:
    user_id: str
    email: str
    last_login_at: str
    created_at: str
    status: str
    numeric: bool
    raw_last_login_at: Optional[str] = None          # as stored; the write condition
    role: str = ""                                   # "live" | "stale"
    merged_into: Optional[str] = None
    merged_at: Optional[str] = None
    cognito_user: str = "n/a"                        # "exists" | "absent" | "error" | "n/a"
    references: Dict[str, int] = field(default_factory=dict)
    breakdown: Dict[str, Dict[str, int]] = field(default_factory=dict)
    deep_samples: List[str] = field(default_factory=list)
    last_activity_at: Optional[str] = None           # newest timestamp found under this id
    activity: Dict[str, str] = field(default_factory=dict)  # source -> newest timestamp
    in_use: List[str] = field(default_factory=list)  # why the old id is still live
    errors: List[str] = field(default_factory=list)
    verdict: str = ""                                # "in_use" | "referenced" | "unreferenced" | "incomplete" | ""
    action: str = ""


@dataclass
class DuplicateGroup:
    email: str
    kind: str                                        # "legacy_pair" | "needs_review"
    live_user_id: str
    rows: List[ProfileRow]


@dataclass
class Environment:
    tables: Dict[str, str] = field(default_factory=dict)
    buckets: Dict[str, str] = field(default_factory=dict)
    user_pool_id: Optional[str] = None
    memory_id: Optional[str] = None
    statuses: Dict[str, str] = field(default_factory=dict)   # logical -> "ok" | "not_deployed" | "error: ..."


@dataclass
class Clients:
    dynamodb: Any
    s3: Any
    ssm: Any
    cognito: Any
    agentcore: Any

    @classmethod
    def from_session(cls, session: boto3.Session) -> "Clients":
        return cls(
            dynamodb=session.resource("dynamodb", config=BOTO_CONFIG),
            s3=session.client("s3", config=BOTO_CONFIG),
            ssm=session.client("ssm", config=BOTO_CONFIG),
            cognito=session.client("cognito-idp", config=BOTO_CONFIG),
            agentcore=session.client("bedrock-agentcore", config=BOTO_CONFIG),
        )


# --------------------------------------------------------------------------- #
# Discovery                                                                   #
# --------------------------------------------------------------------------- #
def _ssm_param(clients: Clients, name: str) -> Optional[str]:
    try:
        return clients.ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise


def discover(clients: Clients, prefix: str) -> Environment:
    """Resolve every table, bucket and id the checks need; record what is missing.

    A missing *optional* source means the feature isn't deployed, so nothing
    can reference a user there. A missing required one is an error: it almost
    always means the wrong prefix or region, and "found nothing" would be a lie.
    """
    env = Environment()

    for src in TABLE_SOURCES:
        try:
            if src.ssm:
                name = _ssm_param(clients, f"/{prefix}{src.ssm}")
            else:
                name = f"{prefix}-{src.suffix}"
                try:
                    clients.dynamodb.meta.client.describe_table(TableName=name)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                        raise
                    name = None
        except (ClientError, BotoCoreError) as exc:
            env.statuses[src.logical] = f"error: {exc}"
            continue
        if name:
            env.tables[src.logical] = name
            env.statuses[src.logical] = "ok"
        elif src.optional:
            env.statuses[src.logical] = "not_deployed"
        else:
            env.statuses[src.logical] = "error: required table not found (wrong prefix/region?)"

    for bucket in BUCKET_SOURCES:
        key = f"s3:{bucket.logical}"
        try:
            name = _ssm_param(clients, f"/{prefix}{bucket.ssm}")
        except (ClientError, BotoCoreError) as exc:
            env.statuses[key] = f"error: {exc}"
            continue
        if name:
            env.buckets[bucket.logical] = name
            env.statuses[key] = "ok"
        elif bucket.optional:
            env.statuses[key] = "not_deployed"
        else:
            env.statuses[key] = "error: required bucket parameter not found"

    for key, path in (("cognito", SSM_USER_POOL_ID), ("memory", SSM_MEMORY_ID)):
        try:
            value = _ssm_param(clients, f"/{prefix}{path}")
        except (ClientError, BotoCoreError) as exc:
            env.statuses[key] = f"error: {exc}"
            continue
        env.statuses[key] = "ok" if value else "not_deployed"
        if key == "cognito":
            env.user_pool_id = value
        else:
            env.memory_id = value

    return env


# --------------------------------------------------------------------------- #
# Grouping                                                                    #
# --------------------------------------------------------------------------- #
def scan_profiles(table: Any) -> Iterator[Dict[str, Any]]:
    kwargs: Dict[str, Any] = {"FilterExpression": Attr("SK").eq("PROFILE")}
    while True:
        response = table.scan(**kwargs)
        yield from response.get("Items", [])
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return
        kwargs["ExclusiveStartKey"] = last_key


def _row(item: Dict[str, Any]) -> Tuple[Any, ProfileRow]:
    profile = item_to_profile(item)
    status = profile.status.value if hasattr(profile.status, "value") else str(profile.status)
    row = ProfileRow(
        user_id=profile.user_id,
        email=profile.email.lower(),
        last_login_at=profile.last_login_at,
        created_at=profile.created_at,
        status=status,
        numeric=profile.user_id.isdigit(),
        raw_last_login_at=item.get("lastLoginAt"),
        merged_into=item.get("mergedInto"),
        merged_at=item.get("mergedAt"),
    )
    return profile, row


def build_groups(
    items: Iterable[Dict[str, Any]],
    only_emails: Optional[Set[str]] = None,
) -> Tuple[List[DuplicateGroup], List[ProfileRow], Dict[str, int]]:
    """``(duplicate groups, numeric rows with no twin, counters)``."""
    by_email: Dict[str, List[Tuple[Any, ProfileRow]]] = {}
    counters = {"profileRows": 0, "numericProfileRows": 0, "unparseableRows": 0}
    for item in items:
        counters["profileRows"] += 1
        try:
            profile, row = _row(item)
        except (KeyError, ValueError, TypeError):
            counters["unparseableRows"] += 1
            logger.warning("Skipping unparseable PROFILE row %s", item.get("PK"))
            continue
        if row.numeric:
            counters["numericProfileRows"] += 1
        by_email.setdefault(row.email, []).append((profile, row))

    groups: List[DuplicateGroup] = []
    orphans: List[ProfileRow] = []
    for email in sorted(by_email):
        if only_emails and email not in only_emails:
            continue
        members = sorted(by_email[email], key=lambda pr: live_profile_rank(pr[0]), reverse=True)
        rows = [row for _, row in members]
        if len(rows) == 1:
            if rows[0].numeric:
                orphans.append(rows[0])
            continue
        for i, row in enumerate(rows):
            row.role = "live" if i == 0 else "stale"
        is_pair = len(rows) == 2 and not rows[0].numeric and rows[1].numeric
        groups.append(DuplicateGroup(
            email=email,
            kind="legacy_pair" if is_pair else "needs_review",
            live_user_id=rows[0].user_id,
            rows=rows,
        ))
    return groups, orphans, counters


# --------------------------------------------------------------------------- #
# Reference checks                                                            #
# --------------------------------------------------------------------------- #
def run_query_check(table: Any, check: QueryCheck, user_id: str) -> Tuple[int, Dict[str, int]]:
    """``(row count, count per SK family)`` for one user id on one key path."""
    condition = Key(check.key).eq(check.value.format(id=user_id))
    if check.sk_prefix:
        condition = condition & Key("SK").begins_with(check.sk_prefix)
    kwargs: Dict[str, Any] = {"KeyConditionExpression": condition}
    if check.index:
        kwargs["IndexName"] = check.index
    if check.by_sk_family:
        kwargs["ProjectionExpression"] = "SK"
    else:
        kwargs["Select"] = "COUNT"

    total = 0
    families: Dict[str, int] = {}
    while True:
        response = table.query(**kwargs)
        if check.by_sk_family:
            for item in response.get("Items", []):
                family = str(item.get("SK", "")).split("#", 1)[0] or "?"
                families[family] = families.get(family, 0) + 1
            total += len(response.get("Items", []))
        else:
            total += int(response.get("Count", 0))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return total, families
        kwargs["ExclusiveStartKey"] = last_key


def _references(value: Any, ids: Set[str]) -> Set[str]:
    """Which of ``ids`` a DynamoDB value names — whole value or ``#``/``/`` token.

    Token matching, not substring: a numeric id must not match inside a cost
    figure or a timestamp that happens to contain the same digits.
    """
    if isinstance(value, str):
        if value in ids:
            return {value}
        return set(_TOKEN_SPLIT.split(value)) & ids
    if isinstance(value, dict):
        found: Set[str] = set()
        for k, v in value.items():
            found |= _references(k, ids) | _references(v, ids)
        return found
    if isinstance(value, (list, set, tuple)):
        found = set()
        for v in value:
            found |= _references(v, ids)
        return found
    return set()


def deep_scan(table: Any, ids: Set[str], sleep: float) -> Dict[str, List[str]]:
    """One Scan of ``table``; for each id, a description of every item naming it."""
    hits: Dict[str, List[str]] = {}
    kwargs: Dict[str, Any] = {}
    while True:
        response = table.scan(**kwargs)
        for item in response.get("Items", []):
            for attr, value in item.items():
                for user_id in _references(value, ids):
                    hits.setdefault(user_id, []).append(
                        f"{item.get('PK', item.get('share_id', '?'))} / {item.get('SK', '-')} ({attr})"
                    )
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return hits
        kwargs["ExclusiveStartKey"] = last_key
        if sleep:
            time.sleep(sleep)


def _s3_has_objects(s3: Any, bucket: str, prefix: str) -> bool:
    return s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1).get("KeyCount", 0) > 0


def _cognito_user(cognito: Any, pool_id: str, sub: str) -> str:
    users = cognito.list_users(UserPoolId=pool_id, Filter=f'sub = "{sub}"', Limit=1).get("Users", [])
    return "exists" if users else "absent"


def _memory_sessions(agentcore: Any, memory_id: str, actor_id: str) -> int:
    response = agentcore.list_sessions(memoryId=memory_id, actorId=actor_id, maxResults=1)
    return len(response.get("sessionSummaries", []))


def _status_of(env: Environment, logical: str, waived: Set[str], check_id: str) -> str:
    if check_id in waived:
        return "waived"
    raw = env.statuses.get(logical, "error: not discovered")
    return "error" if raw.startswith("error") else raw


def check_references(
    clients: Clients,
    env: Environment,
    groups: Sequence[DuplicateGroup],
    *,
    deep_tables: Sequence[str] = (),
    waived: Optional[Set[str]] = None,
    sleep: float = 0.0,
    now: Optional[datetime] = None,
) -> List[CheckStatus]:
    """Fill ``references``/``errors``/``cognito_user`` on every row; return check statuses.

    Stale rows get every check. Cognito is looked up for every non-numeric id
    in every group, live rows included — for a ``needs_review`` group, "which
    of these subs still exists" is most of the answer.
    """
    waived = waived or set()
    stale = [row for g in groups for row in g.rows if row.role == "stale"]
    statuses: List[CheckStatus] = []

    for check in QUERY_CHECKS:
        status = _status_of(env, check.table, waived, check.check_id)
        target = f"{check.table}{'/' + check.index if check.index else ''}"
        statuses.append(CheckStatus(check.check_id, "query", target, status,
                                    env.statuses.get(check.table, "")))
        if status != "ok":
            if status == "error":
                for row in stale:
                    row.errors.append(f"{check.check_id}: {env.statuses.get(check.table)}")
            continue
        table = clients.dynamodb.Table(env.tables[check.table])
        for row in stale:
            try:
                count, families = run_query_check(table, check, row.user_id)
            except (ClientError, BotoCoreError) as exc:
                row.errors.append(f"{check.check_id}: {exc}")
                continue
            row.references[check.check_id] = count
            if families:
                row.breakdown[check.check_id] = families
            if sleep:
                time.sleep(sleep)

    for bucket in BUCKET_SOURCES:
        logical = f"s3:{bucket.logical}"
        status = _status_of(env, logical, waived, logical)
        statuses.append(CheckStatus(logical, "s3", bucket.logical, status, env.statuses.get(logical, "")))
        if status != "ok":
            if status == "error":
                for row in stale:
                    row.errors.append(f"{logical}: {env.statuses.get(logical)}")
            continue
        for row in stale:
            for template in bucket.prefixes:
                prefix = template.format(id=row.user_id)
                check_id = f"s3:{bucket.logical}/{template.split('{')[0] or '<root>'}"
                try:
                    row.references[check_id] = int(_s3_has_objects(clients.s3, env.buckets[bucket.logical], prefix))
                except (ClientError, BotoCoreError) as exc:
                    row.errors.append(f"{check_id}: {exc}")

    status = _status_of(env, "memory", waived, "agentcore-memory")
    statuses.append(CheckStatus("agentcore-memory", "memory", "bedrock-agentcore list_sessions", status,
                                env.statuses.get("memory", "")))
    if status == "ok":
        for row in stale:
            try:
                row.references["agentcore-memory-sessions"] = _memory_sessions(
                    clients.agentcore, env.memory_id, row.user_id)
            except (ClientError, BotoCoreError) as exc:
                row.errors.append(f"agentcore-memory: {exc}")
    elif status == "error":
        for row in stale:
            row.errors.append(f"agentcore-memory: {env.statuses.get('memory')}")

    # Informational only — never part of the verdict: a legacy numeric id is
    # never a Cognito sub, and a uuid's absence doesn't make its data safe.
    cognito_status = "ok" if env.user_pool_id else _status_of(env, "cognito", set(), "cognito")
    statuses.append(CheckStatus("cognito", "cognito", "cognito-idp list_users", cognito_status,
                                env.statuses.get("cognito", "")))
    if env.user_pool_id:
        for group in groups:
            for row in group.rows:
                if row.numeric:
                    continue
                try:
                    row.cognito_user = _cognito_user(clients.cognito, env.user_pool_id, row.user_id)
                except (ClientError, BotoCoreError) as exc:
                    row.cognito_user = "error"
                    logger.warning("Cognito lookup failed for %s: %s", row.user_id, exc)

    if deep_tables:
        ids = {row.user_id for row in stale}
        for logical in deep_tables:
            check_id = f"deep:{logical}"
            status = _status_of(env, logical, waived, check_id)
            statuses.append(CheckStatus(check_id, "deep", logical, status, env.statuses.get(logical, "")))
            if status != "ok" or not ids:
                if status == "error":
                    for row in stale:
                        row.errors.append(f"{check_id}: {env.statuses.get(logical)}")
                continue
            logger.info("Deep-scanning %s for %d ids", env.tables[logical], len(ids))
            try:
                hits = deep_scan(clients.dynamodb.Table(env.tables[logical]), ids, sleep)
            except (ClientError, BotoCoreError) as exc:
                for row in stale:
                    row.errors.append(f"{check_id}: {exc}")
                continue
            for row in stale:
                found = hits.get(row.user_id, [])
                row.references[check_id] = len(found)
                row.deep_samples.extend(f"{logical}: {s}" for s in found[:5])

    for group in groups:
        live_created = group.rows[0].created_at
        for row in group.rows[1:]:
            try:
                gather_activity(clients, env, row, live_created, now or datetime.now(timezone.utc))
            except (ClientError, BotoCoreError) as exc:
                row.errors.append(f"activity: {exc}")

    for row in stale:
        if row.errors:
            row.verdict = "incomplete"
        elif row.in_use:
            row.verdict = "in_use"
        elif any(row.references.values()):
            row.verdict = "referenced"
        else:
            row.verdict = "unreferenced"

    return statuses


# --------------------------------------------------------------------------- #
# Is the old id still in use?                                                 #
# --------------------------------------------------------------------------- #
def _instant(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("+00:00Z", "Z").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _query_partition(table: Any, pk: str, projection: str, names: Dict[str, str]) -> Iterator[Dict[str, Any]]:
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(pk),
        "ProjectionExpression": projection,
        "ExpressionAttributeNames": names,
    }
    while True:
        response = table.query(**kwargs)
        yield from response.get("Items", [])
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return
        kwargs["ExclusiveStartKey"] = last_key


def gather_activity(clients: Clients, env: Environment, row: ProfileRow, cutover: Optional[str], now: datetime) -> None:
    """Record the newest activity under ``row.user_id`` and every sign it is live.

    Sources: API keys (``lastUsedAt``, and whether any is unexpired), and the
    sessions-metadata partition — ``C#<ts>#…`` model-call rows (written for
    api-converse calls too), ``S#`` sessions' ``lastMessageAt``, and
    ``SCHEDPROMPT#`` rows, which still fire while they carry ``nextRunAt``.
    """
    newest: Dict[str, datetime] = {}

    def note(source: str, when: Optional[datetime]) -> None:
        if when and (source not in newest or when > newest[source]):
            newest[source] = when

    if "api-keys" in env.tables:
        table = clients.dynamodb.Table(env.tables["api-keys"])
        for item in _query_partition(table, f"USER#{row.user_id}", "#n, keyId, lastUsedAt, expiresAt",
                                     {"#n": "name"}):
            note("api-key used", _instant(item.get("lastUsedAt")))
            expires = _instant(item.get("expiresAt"))
            if expires is None or expires > now:
                row.in_use.append(
                    f"unexpired API key {item.get('name') or item.get('keyId')!r} "
                    f"(last used {item.get('lastUsedAt') or 'never'}, expires {item.get('expiresAt') or 'never'})"
                )

    if "sessions-metadata" in env.tables:
        table = clients.dynamodb.Table(env.tables["sessions-metadata"])
        for item in _query_partition(table, f"USER#{row.user_id}", "SK, lastMessageAt, nextRunAt", {}):
            sk = str(item.get("SK", ""))
            if sk.startswith("C#"):
                note("model call", _instant(sk.split("#")[1] if sk.count("#") >= 2 else None))
            elif sk.startswith("S#"):
                note("message", _instant(item.get("lastMessageAt")))
            elif sk.startswith("SCHEDPROMPT#") and item.get("nextRunAt"):
                row.in_use.append(f"active scheduled prompt {sk} (next run {item['nextRunAt']})")

    row.activity = {k: v.strftime("%Y-%m-%dT%H:%M:%SZ") for k, v in sorted(newest.items())}
    if newest:
        latest_source, latest = max(newest.items(), key=lambda kv: kv[1])
        row.last_activity_at = latest.strftime("%Y-%m-%dT%H:%M:%SZ")
        cut = _instant(cutover)
        if cut and latest > cut:
            row.in_use.append(
                f"{latest_source} at {row.last_activity_at}, after this person's live profile "
                f"was created ({cutover})"
            )


# --------------------------------------------------------------------------- #
# Apply                                                                       #
# --------------------------------------------------------------------------- #
def ineligibility(group: DuplicateGroup, row: ProfileRow) -> Optional[str]:
    """Why ``row`` may not be retired, or None if it may."""
    if row.role != "stale":
        return "live row"
    if group.kind != "legacy_pair":
        return "group needs review"
    if not row.numeric:
        return "not a legacy numeric id"
    if row.verdict != "unreferenced":
        return f"verdict is {row.verdict}"
    return None


def _unchanged_since_audit(row: ProfileRow) -> Any:
    """Condition: the row still carries the ``lastLoginAt`` the audit judged."""
    if row.raw_last_login_at is None:
        return Attr("lastLoginAt").not_exists()
    return Attr("lastLoginAt").eq(row.raw_last_login_at)


def apply_actions(
    users_table: Any,
    groups: Sequence[DuplicateGroup],
    mode: str,
    *,
    now: datetime,
    min_soak_days: int,
) -> int:
    """Mark or delete every eligible stale row; returns how many were written.

    ``mode`` is "dry-run", "mark" or "delete". In dry-run every row still gets
    the action it *would* take, so the report doubles as the plan.
    """
    written = 0
    for group in groups:
        for row in group.rows:
            if row.role != "stale":
                row.action = "keep (live)"
                continue
            reason = ineligibility(group, row)
            if reason:
                row.action = f"skip: {reason}"
                continue

            key = {"PK": f"USER#{row.user_id}", "SK": "PROFILE"}
            current = users_table.get_item(Key=key).get("Item")
            if current is None:
                row.action = "skip: row already gone"
                continue
            if current.get("lastLoginAt") != row.raw_last_login_at:
                row.action = "skip: signed in since the audit read it"
                continue

            if mode in ("dry-run", "mark"):
                if current.get("mergedInto"):
                    row.action = f"already marked (mergedInto {current['mergedInto']})"
                    continue
                if mode == "dry-run":
                    row.action = "would mark"
                    continue
                try:
                    users_table.update_item(
                        Key=key,
                        UpdateExpression=(
                            "SET #status = :inactive, GSI3PK = :status_pk, mergedInto = :live, "
                            "mergedAt = :now, mergeReason = :reason"
                        ),
                        ConditionExpression=(
                            Attr("PK").exists() & Attr("mergedInto").not_exists()
                            & _unchanged_since_audit(row)
                        ),
                        ExpressionAttributeNames={"#status": "status"},
                        ExpressionAttributeValues={
                            ":inactive": "inactive",
                            ":status_pk": "STATUS#inactive",
                            ":live": group.live_user_id,
                            ":now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            ":reason": MERGE_REASON,
                        },
                    )
                except ClientError as exc:
                    row.action = f"failed: {exc.response.get('Error', {}).get('Code')}"
                    continue
                row.action = "marked"
                written += 1
                continue

            # mode == "delete": only rows a previous run marked, after the soak.
            if current.get("mergedInto") != group.live_user_id:
                row.action = "skip: not marked for this live id (run --apply mark first)"
                continue
            try:
                marked_at = datetime.fromisoformat(str(current.get("mergedAt", "")).replace("Z", "+00:00"))
            except ValueError:
                row.action = "skip: unreadable mergedAt"
                continue
            if now - marked_at < timedelta(days=min_soak_days):
                row.action = f"skip: marked {marked_at:%Y-%m-%d}, soak is {min_soak_days}d"
                continue
            try:
                users_table.delete_item(
                    Key=key,
                    ConditionExpression=Attr("mergedInto").eq(group.live_user_id) & _unchanged_since_audit(row),
                )
            except ClientError as exc:
                row.action = f"failed: {exc.response.get('Error', {}).get('Code')}"
                continue
            row.action = "deleted"
            written += 1
    return written


# --------------------------------------------------------------------------- #
# Reports                                                                     #
# --------------------------------------------------------------------------- #
def summarize(
    groups: Sequence[DuplicateGroup], orphans: Sequence[ProfileRow], counters: Dict[str, int], written: int,
) -> Dict[str, int]:
    stale = [row for g in groups for row in g.rows if row.role == "stale"]
    return {
        **counters,
        "duplicateEmails": len(groups),
        "legacyPairs": sum(1 for g in groups if g.kind == "legacy_pair"),
        "needsReview": sum(1 for g in groups if g.kind == "needs_review"),
        "orphanNumericRows": len(orphans),
        "staleRows": len(stale),
        "inUse": sum(1 for r in stale if r.verdict == "in_use"),
        "unreferenced": sum(1 for r in stale if r.verdict == "unreferenced"),
        "referenced": sum(1 for r in stale if r.verdict == "referenced"),
        "incomplete": sum(1 for r in stale if r.verdict == "incomplete"),
        "eligible": sum(1 for g in groups for r in g.rows if ineligibility(g, r) is None),
        "written": written,
    }


def build_report(
    *, prefix: str, region: str, account: str, mode: str, generated_at: datetime,
    statuses: Sequence[CheckStatus], groups: Sequence[DuplicateGroup],
    orphans: Sequence[ProfileRow], summary: Dict[str, int],
) -> Dict[str, Any]:
    return {
        "reportVersion": 1,
        "generatedAt": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "projectPrefix": prefix,
        "region": region,
        "accountId": account,
        "mode": mode,
        "summary": summary,
        "checks": [asdict(s) for s in statuses],
        "groups": [asdict(g) for g in groups],
        "orphanNumericRows": [asdict(r) for r in orphans],
    }


def _refs_text(row: ProfileRow) -> str:
    parts = []
    for check_id, count in sorted(row.references.items()):
        if not count:
            continue
        families = row.breakdown.get(check_id)
        detail = f" ({', '.join(f'{k} {v}' for k, v in sorted(families.items()))})" if families else ""
        parts.append(f"{check_id} {count}{detail}")
    if row.errors:
        parts.append(f"ERRORS: {'; '.join(row.errors)}")
    return "; ".join(parts) or "—"


def render_markdown(report: Dict[str, Any], groups: Sequence[DuplicateGroup], orphans: Sequence[ProfileRow]) -> str:
    s = report["summary"]
    out = [
        f"# Users table duplicate audit — {report['projectPrefix']} ({report['region']})",
        "",
        f"Generated {report['generatedAt']} · account {report['accountId']} · mode **{report['mode']}**",
        "",
        "> Contains email addresses. Do not commit or post publicly.",
        "",
        "## Summary",
        "",
        "| | |",
        "|---|---:|",
    ]
    labels = [
        ("profileRows", "PROFILE rows"), ("numericProfileRows", "numeric-id rows"),
        ("unparseableRows", "unparseable rows"), ("duplicateEmails", "emails with >1 row"),
        ("legacyPairs", "legacy pairs (numeric + uuid)"), ("needsReview", "groups needing review"),
        ("orphanNumericRows", "numeric rows with no twin"), ("staleRows", "stale rows checked"),
        ("inUse", "**old id still in use**"), ("referenced", "referenced (history only)"),
        ("unreferenced", "unreferenced"),
        ("incomplete", "incomplete (a check failed)"), ("eligible", "eligible to retire"),
        ("written", "rows written this run"),
    ]
    out += [f"| {label} | {s[key]} |" for key, label in labels]

    out += ["", "## Checks", "", "| check | reads | status | detail |", "|---|---|---|---|"]
    out += [f"| {c['check_id']} | {c['target']} | {c['status']} | {c['detail'] if c['status'] != 'ok' else ''} |"
            for c in report["checks"]]

    in_use = [(g, r) for g in groups for r in g.rows if r.verdict == "in_use"]
    out += ["", f"## ⚠️ Old id still in use ({len(in_use)})", "",
            "Never retired. Each needs a person: move the key or schedule to the live id, or confirm with the user.",
            ""]
    for g, r in in_use:
        out.append(f"- **{g.email}** `{r.user_id}` (live: `{g.live_user_id}`): " + "; ".join(r.in_use))

    pairs = [g for g in groups if g.kind == "legacy_pair"]
    out += ["", f"## Legacy pairs ({len(pairs)})", "",
            "| email | live id · created · last login | stale id · last login | last activity on stale id "
            "| references on stale id | verdict | action |",
            "|---|---|---|---|---|---|---|"]
    for g in pairs:
        live, stale = g.rows
        out.append(
            f"| {g.email} | `{live.user_id}` · {live.created_at} · {live.last_login_at} "
            f"| `{stale.user_id}` · {stale.last_login_at} | {stale.last_activity_at or '—'} "
            f"| {_refs_text(stale)} | {stale.verdict} | {stale.action} |"
        )

    review = [g for g in groups if g.kind == "needs_review"]
    out += ["", f"## Needs review ({len(review)})", "",
            "Never acted on. The top row is what the API resolves the email to today.", ""]
    for g in review:
        out += [f"### {g.email}", "",
                "| user id | role | last login | created | status | Cognito sub | last activity | references | verdict |",
                "|---|---|---|---|---|---|---|---|---|"]
        for r in g.rows:
            refs = _refs_text(r) if r.role == "stale" else "(live — not checked)"
            out.append(f"| `{r.user_id}` | {r.role} | {r.last_login_at} | {r.created_at} | {r.status} "
                       f"| {r.cognito_user} | {r.last_activity_at or '—'} | {refs} | {r.verdict or '—'} |")
        out += [f"- `{r.user_id}` in use: {'; '.join(r.in_use)}" for r in g.rows if r.in_use]
        samples = [s for r in g.rows for s in r.deep_samples]
        if samples:
            out += ["", "Deep-scan hits:", ""] + [f"- `{s}`" for s in samples]
        out.append("")

    out += [f"## Numeric rows with no twin ({len(orphans)})", "",
            "The only profile for their email; nobody has signed in to them since the login changed. Report only.", "",
            "| email | user id | last login |", "|---|---|---|"]
    out += [f"| {r.email} | `{r.user_id}` | {r.last_login_at} |" for r in orphans]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-prefix", required=True, help="CDK_PROJECT_PREFIX, e.g. boisestateai-v2")
    p.add_argument("--region", required=True)
    p.add_argument("--profile", default=None, help="AWS profile name")
    p.add_argument("--email", action="append", default=[],
                   help="Only this email's group (repeatable) — use for a canary run")
    p.add_argument("--deep", action="store_true",
                   help=f"Also Scan tables with un-indexed user ids: {', '.join(DEEP_SCAN_DEFAULT)}")
    p.add_argument("--deep-tables", default=None,
                   help="Comma-separated logical table names to deep-scan instead of the default set")
    p.add_argument("--waive-check", action="append", default=[],
                   help="Treat this check id as passed (repeatable; recorded in the report)")
    p.add_argument("--sleep", type=float, default=0.05, help="Seconds between queries / scan pages")
    p.add_argument("--apply", choices=("mark", "delete"), default=None,
                   help="mark: set stale rows inactive + mergedInto. delete: remove rows marked >= soak ago")
    p.add_argument("--confirm-prefix", default=None, help="Required with --apply; must equal --project-prefix")
    p.add_argument("--min-soak-days", type=int, default=7, help="Days between mark and delete")
    p.add_argument("--out-dir", default=".", help="Where to write the JSON + Markdown reports")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def run(args: argparse.Namespace, clients: Clients, account: str, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    mode = args.apply or "dry-run"

    env = discover(clients, args.project_prefix)
    if "users" not in env.tables:
        logger.error("Users table not found: %s", env.statuses.get("users"))
        return 2
    users_table = clients.dynamodb.Table(env.tables["users"])

    only = {e.lower().strip() for e in args.email} or None
    groups, orphans, counters = build_groups(scan_profiles(users_table), only)
    logger.info("%d PROFILE rows, %d duplicate emails", counters["profileRows"], len(groups))

    if args.deep_tables:
        deep = tuple(t.strip() for t in args.deep_tables.split(",") if t.strip())
    else:
        deep = DEEP_SCAN_DEFAULT if args.deep else ()
    statuses = check_references(clients, env, groups, deep_tables=deep,
                                waived=set(args.waive_check), sleep=args.sleep, now=now)
    written = apply_actions(users_table, groups, mode, now=now, min_soak_days=args.min_soak_days)

    summary = summarize(groups, orphans, counters, written)
    report = build_report(prefix=args.project_prefix, region=args.region, account=account, mode=mode,
                          generated_at=now, statuses=statuses, groups=groups, orphans=orphans, summary=summary)

    os.makedirs(args.out_dir, exist_ok=True)
    stem = os.path.join(args.out_dir, f"user-duplicates-{args.project_prefix}-{now:%Y%m%dT%H%M%SZ}")
    with open(f"{stem}.json", "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    with open(f"{stem}.md", "w") as fh:
        fh.write(render_markdown(report, groups, orphans))
    logger.info("Wrote %s.json and %s.md", stem, stem)
    logger.info("Summary: %s", json.dumps(summary))

    return 1 if any(c.status == "error" for c in statuses) or summary["incomplete"] else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not _PREFIX_RE.match(args.project_prefix):
        logger.error("Invalid --project-prefix %r (must match %s)", args.project_prefix, _PREFIX_RE.pattern)
        return 2
    if args.apply and args.confirm_prefix != args.project_prefix:
        logger.error("--apply %s needs --confirm-prefix %s", args.apply, args.project_prefix)
        return 2

    session = boto3.Session(region_name=args.region, profile_name=args.profile)
    account = session.client("sts", config=BOTO_CONFIG).get_caller_identity()["Account"]
    return run(args, Clients.from_session(session), account)


if __name__ == "__main__":
    sys.exit(main())
