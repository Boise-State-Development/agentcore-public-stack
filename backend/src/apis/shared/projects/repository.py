"""DynamoDB repository for Shared Projects (docs/specs/shared-projects.md §3.1).

Owns the row shapes on the ``{prefix}-projects`` table. No access control lives
here — that is the service's job; this layer is a permission-agnostic data
layer, like every other ``apis/shared`` repository.

Row shapes:

  - ``PK=PROJECT#{id}  SK=META``           + ``GSI1PK=OWNER#{owner_id}  GSI1SK=PROJECT#{id}``
  - ``PK=PROJECT#{id}  SK=MEMBER#{email}`` + ``GSI2PK=MEMBER#{email}    GSI2SK=PROJECT#{id}``
  - ``PK=PROJECT#{id}  SK=SHARED_TASK#{sessionId}`` — the newest project share of one task

Three invariants the writes enforce, not the callers:

  - A project id is never reused (``attribute_not_exists`` on create).
  - ``memberCount`` on META always equals the number of MEMBER rows, and never
    exceeds the cap: adding or removing a member is one transaction that writes
    the member row and moves the count, conditioned on the cap and on the
    project being active.
  - Every META mutation bumps ``version``, and a full META write is conditional
    on the version it read — so an edit racing a member change fails loudly
    instead of writing back a stale count.
"""

from __future__ import annotations

import logging
import os
import re
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:  # boto3 is absent in some local-dev setups
    import boto3
    from boto3.dynamodb.conditions import Key
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - exercised only without boto3
    boto3 = None
    Key = None  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[assignment, misc]

from apis.shared.dynamo_errors import is_missing_index_error, log_missing_index

from .models import Project, ProjectMember, SharedTask, normalize_email

logger = logging.getLogger(__name__)

META_SK = "META"
MEMBER_SK_PREFIX = "MEMBER#"
COST_SK_PREFIX = "COST#"
SHARED_TASK_SK_PREFIX = "SHARED_TASK#"
OWNER_INDEX = "OwnerIndex"
MEMBER_INDEX = "MemberIndex"

_BATCH_GET_LIMIT = 100
_KEY_ATTRS = ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")


class ProjectWriteConflict(RuntimeError):
    """A conditional write or transaction was refused.

    ``failed`` lists the indexes of the transaction items whose condition
    failed (``[0]`` for a single conditional write). The repository does not
    guess what that means — the service re-reads and names the reason.
    """

    def __init__(self, failed: List[int]):
        super().__init__(f"conditional write refused (items {failed})")
        self.failed = failed


def project_pk(project_id: str) -> str:
    return f"PROJECT#{project_id}"


def member_sk(email: str) -> str:
    return f"{MEMBER_SK_PREFIX}{normalize_email(email)}"


def _plain(value: Any) -> Any:
    """DynamoDB ``Decimal`` → ``int``/``float``, recursively."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _strip_keys(item: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _plain(v) for k, v in item.items() if k not in _KEY_ATTRS}


_NO_FAILURE_CODES = {"None", "", None}


def _cancellation_codes(error: ClientError) -> List[Optional[str]]:
    """One reason code per transaction item.

    Real DynamoDB returns ``CancellationReasons``; moto has at times only put
    the codes in the message (``[ConditionalCheckFailed, None]``), so both are read.
    """
    reasons = error.response.get("CancellationReasons")
    if reasons:
        return [r.get("Code") for r in reasons]
    message = error.response.get("Error", {}).get("Message", "")
    match = re.search(r"\[(.*)\]", message)
    return [c.strip() for c in match.group(1).split(",")] if match else []


class ProjectRepository:
    """Thin CRUD over the projects table."""

    def __init__(self, table_name: Optional[str] = None):
        self.table_name = table_name or os.environ.get("DYNAMODB_PROJECTS_TABLE_NAME", "projects")
        self._dynamodb = boto3.resource("dynamodb")
        self._table = self._dynamodb.Table(self.table_name)
        # The resource's own client: it serializes plain Python values exactly as the
        # resource does, so transaction items are written like ``put_item`` items.
        self._client = self._dynamodb.meta.client

    # ── (de)serialization ───────────────────────────────────────────────

    @staticmethod
    def _project_to_item(project: Project) -> Dict[str, Any]:
        item = project.model_dump(by_alias=True, exclude_none=True)
        item.update(
            PK=project_pk(project.project_id),
            SK=META_SK,
            GSI1PK=f"OWNER#{project.owner_id}",
            GSI1SK=project_pk(project.project_id),
        )
        return item

    @staticmethod
    def _member_to_item(member: ProjectMember) -> Dict[str, Any]:
        email = normalize_email(member.email)
        item = member.model_dump(by_alias=True, exclude_none=True)
        item.update(
            email=email,
            PK=project_pk(member.project_id),
            SK=member_sk(email),
            GSI2PK=f"MEMBER#{email}",
            GSI2SK=project_pk(member.project_id),
        )
        return item

    @staticmethod
    def _key(project_id: str, sk: str) -> Dict[str, Any]:
        return {"PK": project_pk(project_id), "SK": sk}

    def _transact(self, items: List[Dict[str, Any]]) -> None:
        """Run a transaction; a refused condition becomes ``ProjectWriteConflict``.

        Only condition failures are conflicts. A cancellation for any other
        reason (throttling, a validation error, a transaction conflict) is
        re-raised as-is: the service reads a conflict as "exists", "full" or
        "archived", and must never be handed an infrastructure error to guess at.
        """
        try:
            self._client.transact_write_items(TransactItems=items)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            codes = _cancellation_codes(e)
            failed = [i for i, code in enumerate(codes) if code == "ConditionalCheckFailed"]
            unexplained = [code for code in codes if code not in _NO_FAILURE_CODES and code != "ConditionalCheckFailed"]
            if not failed or unexplained:
                raise
            raise ProjectWriteConflict(failed) from e

    # ── META ────────────────────────────────────────────────────────────

    def create_project(self, project: Project) -> None:
        try:
            self._table.put_item(
                Item=self._project_to_item(project),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ProjectWriteConflict([0]) from e
            raise

    def get_project(self, project_id: str) -> Optional[Project]:
        resp = self._table.get_item(Key={"PK": project_pk(project_id), "SK": META_SK})
        item = resp.get("Item")
        return Project.model_validate(_strip_keys(item)) if item else None

    def batch_get_projects(self, project_ids: Iterable[str]) -> Dict[str, Project]:
        """META rows for many projects, without an N+1 of ``get_item``."""
        ids = list(dict.fromkeys(project_ids))
        found: Dict[str, Project] = {}
        for start in range(0, len(ids), _BATCH_GET_LIMIT):
            keys = [{"PK": project_pk(pid), "SK": META_SK} for pid in ids[start:start + _BATCH_GET_LIMIT]]
            request: Dict[str, Any] = {self.table_name: {"Keys": keys}}
            while request:
                resp = self._dynamodb.batch_get_item(RequestItems=request)
                for item in resp.get("Responses", {}).get(self.table_name, []):
                    project = Project.model_validate(_strip_keys(item))
                    found[project.project_id] = project
                request = resp.get("UnprocessedKeys") or {}
        return found

    def put_project(self, project: Project, expected_version: int) -> Project:
        """Write META, conditional on the version the caller read. Returns the new row."""
        updated = project.model_copy(update={"version": expected_version + 1})
        try:
            self._table.put_item(
                Item=self._project_to_item(updated),
                ConditionExpression="attribute_exists(PK) AND #v = :expected",
                ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues={":expected": expected_version},
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise ProjectWriteConflict([0]) from e
            raise
        return updated

    def scan_projects(self, limit: int, after_project_id: Optional[str] = None) -> Tuple[List[Project], Optional[str]]:
        """Every project's META, for administration only (a scan; no index lists all projects).

        Returns up to ``limit`` projects and the id to pass back as ``after_project_id``.
        """
        from boto3.dynamodb.conditions import Attr

        params: Dict[str, Any] = {"FilterExpression": Attr("SK").eq(META_SK)}
        if after_project_id:
            params["ExclusiveStartKey"] = {"PK": project_pk(after_project_id), "SK": META_SK}
        found: List[Project] = []
        while True:
            resp = self._table.scan(**params)
            found.extend(Project.model_validate(_strip_keys(i)) for i in resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if len(found) >= limit or not last:
                break
            params["ExclusiveStartKey"] = last
        page = found[:limit]
        more = len(found) > limit or bool(last)
        return page, (page[-1].project_id if more and page else None)

    def list_owned(self, owner_id: str) -> List[Project]:
        items = self._query_index(OWNER_INDEX, Key("GSI1PK").eq(f"OWNER#{owner_id}"), "projects a user owns")
        return [Project.model_validate(_strip_keys(i)) for i in items]

    # ── MEMBER ──────────────────────────────────────────────────────────

    def get_member(self, project_id: str, email: str) -> Optional[ProjectMember]:
        resp = self._table.get_item(Key={"PK": project_pk(project_id), "SK": member_sk(email)})
        item = resp.get("Item")
        return ProjectMember.model_validate(_strip_keys(item)) if item else None

    def list_members(self, project_id: str) -> List[ProjectMember]:
        items = self._query_all(
            KeyConditionExpression=Key("PK").eq(project_pk(project_id)) & Key("SK").begins_with(MEMBER_SK_PREFIX)
        )
        return [ProjectMember.model_validate(_strip_keys(i)) for i in items]

    def list_memberships(self, email: str) -> List[ProjectMember]:
        """Every MEMBER row for one email, across projects (MemberIndex)."""
        items = self._query_index(
            MEMBER_INDEX, Key("GSI2PK").eq(f"MEMBER#{normalize_email(email)}"), "projects shared with a user"
        )
        return [ProjectMember.model_validate(_strip_keys(i)) for i in items]

    def add_member(self, member: ProjectMember, max_members: int, now: str) -> None:
        """Insert a member and bump ``memberCount`` atomically.

        Fails (``ProjectWriteConflict``) if the member exists (item 0), or if the
        project is missing, archived or full (item 1).
        """
        self._transact([
            {"Put": {
                "TableName": self.table_name,
                "Item": self._member_to_item(member),
                "ConditionExpression": "attribute_not_exists(PK)",
            }},
            {"Update": {
                "TableName": self.table_name,
                "Key": self._key(member.project_id, META_SK),
                "UpdateExpression": "SET memberCount = memberCount + :one, version = version + :one, updatedAt = :now",
                "ConditionExpression": "attribute_exists(PK) AND memberCount < :max AND #s = :active",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {":one": 1, ":now": now, ":max": max_members, ":active": "active"},
            }},
        ])

    def remove_member(self, project_id: str, email: str, now: str) -> None:
        """Delete a member and decrement ``memberCount`` atomically.

        Fails if the member is absent (item 0) or the project is missing (item 1).
        """
        self._transact([
            {"Delete": {
                "TableName": self.table_name,
                "Key": self._key(project_id, member_sk(email)),
                "ConditionExpression": "attribute_exists(PK)",
            }},
            {"Update": {
                "TableName": self.table_name,
                "Key": self._key(project_id, META_SK),
                "UpdateExpression": "SET memberCount = memberCount - :one, version = version + :one, updatedAt = :now",
                "ConditionExpression": "attribute_exists(PK)",
                "ExpressionAttributeValues": {":one": 1, ":now": now},
            }},
        ])

    def update_member_role(self, project_id: str, email: str, role: str, now: str) -> Optional[ProjectMember]:
        """Change a member's role. Returns the updated member, or None if absent."""
        try:
            resp = self._table.update_item(
                Key={"PK": project_pk(project_id), "SK": member_sk(email)},
                UpdateExpression="SET #r = :role, updatedAt = :now",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeNames={"#r": "role"},
                ExpressionAttributeValues={":role": role, ":now": now},
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        return ProjectMember.model_validate(_strip_keys(resp["Attributes"]))

    def set_member_user_id(self, project_id: str, email: str, user_id: str) -> None:
        """Back-fill ``userId`` the first time a member resolves. Never overwrites."""
        try:
            self._table.update_item(
                Key={"PK": project_pk(project_id), "SK": member_sk(email)},
                UpdateExpression="SET userId = :uid",
                ConditionExpression="attribute_exists(PK) AND attribute_not_exists(userId)",
                ExpressionAttributeValues={":uid": user_id},
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    def transfer_ownership(self, project: Project, new_owner: ProjectMember, now: str) -> None:
        """Swap owner and editor in one transaction.

        The new owner's MEMBER row is removed (ownership lives on META) and the
        old owner gains an editor row. Fails if META moved since it was read
        (item 0), the new owner is no longer an editor with a known user id
        (item 1), or the old owner somehow already has a member row (item 2).
        """
        old_owner = ProjectMember(
            project_id=project.project_id,
            email=project.owner_email,
            role="editor",
            user_id=project.owner_id,
            invited_by=new_owner.user_id or "",
            created_at=now,
            updated_at=now,
        )
        self._transact([
            {"Update": {
                "TableName": self.table_name,
                "Key": self._key(project.project_id, META_SK),
                "UpdateExpression": (
                    "SET ownerId = :oid, ownerEmail = :oemail, GSI1PK = :gsi1, "
                    "version = version + :one, updatedAt = :now"
                ),
                "ConditionExpression": "#v = :expected AND ownerId = :prev",
                "ExpressionAttributeNames": {"#v": "version"},
                "ExpressionAttributeValues": {
                    ":oid": new_owner.user_id,
                    ":oemail": normalize_email(new_owner.email),
                    ":gsi1": f"OWNER#{new_owner.user_id}",
                    ":one": 1,
                    ":now": now,
                    ":expected": project.version,
                    ":prev": project.owner_id,
                },
            }},
            {"Delete": {
                "TableName": self.table_name,
                "Key": self._key(project.project_id, member_sk(new_owner.email)),
                "ConditionExpression": "#r = :editor AND userId = :uid",
                "ExpressionAttributeNames": {"#r": "role"},
                "ExpressionAttributeValues": {":editor": "editor", ":uid": new_owner.user_id},
            }},
            {"Put": {
                "TableName": self.table_name,
                "Item": self._member_to_item(old_owner),
                "ConditionExpression": "attribute_not_exists(PK)",
            }},
        ])

    # ── cost ────────────────────────────────────────────────────────────

    def add_call_cost(
        self,
        project_id: str,
        user_id: str,
        period: str,
        cost: Decimal,
        input_tokens: int,
        output_tokens: int,
        now: str,
    ) -> None:
        """Add one model call to the project's month and to that member's share of it.

        Two rows rather than one row with a per-user map: each is a single atomic
        ``ADD`` (a nested map needs three updates to initialise safely), and the member
        rows are bounded by membership itself. ``UpdateItem`` creates either row on first
        use, which is all the runtime's grant allows — it has no ``PutItem``.

          - ``COST#{YYYY-MM}``                 — the project's month
          - ``COST#{YYYY-MM}#USER#{userId}``   — one member's share of it
        """
        values = {
            ":c": cost,
            ":i": input_tokens,
            ":o": output_tokens,
            ":one": 1,
            ":now": now,
        }
        expression = "ADD totalCost :c, inputTokens :i, outputTokens :o, calls :one SET updatedAt = :now"
        self._table.update_item(
            Key={"PK": project_pk(project_id), "SK": f"{COST_SK_PREFIX}{period}"},
            UpdateExpression=expression,
            ExpressionAttributeValues=values,
        )
        self._table.update_item(
            Key={"PK": project_pk(project_id), "SK": f"{COST_SK_PREFIX}{period}#USER#{user_id}"},
            UpdateExpression=expression + ", userId = :uid",
            ExpressionAttributeValues={**values, ":uid": user_id},
        )

    # ── shared tasks ────────────────────────────────────────────────────

    def put_shared_task(self, pointer: SharedTask) -> None:
        """Point the project at a task's share, replacing any older one for that task."""
        item = pointer.model_dump(by_alias=True, exclude_none=True)
        item.update(PK=project_pk(pointer.project_id), SK=f"{SHARED_TASK_SK_PREFIX}{pointer.session_id}")
        self._table.put_item(Item=item)

    def delete_shared_task(self, project_id: str, session_id: str) -> None:
        self._table.delete_item(Key=self._key(project_id, f"{SHARED_TASK_SK_PREFIX}{session_id}"))

    def list_shared_tasks(self, project_id: str) -> List[SharedTask]:
        items = self._query_all(
            KeyConditionExpression=Key("PK").eq(project_pk(project_id))
            & Key("SK").begins_with(SHARED_TASK_SK_PREFIX)
        )
        return [SharedTask.model_validate(_strip_keys(i)) for i in items]

    # ── whole project ───────────────────────────────────────────────────

    def delete_project_rows(self, project_id: str) -> int:
        """Delete every row under ``PROJECT#{id}``. Returns the number deleted."""
        items = self._query_all(
            KeyConditionExpression=Key("PK").eq(project_pk(project_id)),
            ProjectionExpression="PK, SK",
        )
        with self._table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
        return len(items)

    # ── query helpers ───────────────────────────────────────────────────

    def _query_all(self, **params: Any) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        while True:
            resp = self._table.query(**params)
            items.extend(resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                return items
            params["ExclusiveStartKey"] = last

    def _query_index(self, index: str, key_condition: Any, surface: str) -> List[Dict[str, Any]]:
        """A user-facing GSI read that degrades to empty while the index builds."""
        try:
            return self._query_all(IndexName=index, KeyConditionExpression=key_condition)
        except ClientError as e:
            if is_missing_index_error(e):
                log_missing_index(index, surface)
                return []
            raise
