"""DynamoDB repository for Memory Spaces (PR-1, data layer).

Owns the row shapes on the dedicated ``memory-spaces`` table and all
(de)serialization between DynamoDB items (camelCase, ``Decimal`` numbers) and
the Pydantic models. No access control lives here — that is the service's job
(``resolve_permission``); the repository is a thin, permission-agnostic CRUD
layer, matching how ``apis/shared`` repositories are structured elsewhere.

Row shapes (see ``models.py``):

  - ``PK=SPACE#{id}  SK=META``            + ``GSI1PK=OWNER#{owner_id}`` (personal scope only)
  - ``PK=SPACE#{id}  SK=INDEX``
  - ``PK=SPACE#{id}  SK=MEMBER#{email}``  + ``GSI2PK=MEMBER#{email}``
  - ``PK=SPACE#{id}  SK=FILEVER#{slug}#{n:06d}``  (per-file history, no index)
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, Iterator, List, Optional

try:  # boto3 is absent in some local-dev setups
    import boto3
    from boto3.dynamodb.conditions import Key
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - exercised only without boto3
    boto3 = None
    Key = None  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[assignment, misc]

from .models import FileVersion, MemoryEntryRef, MemoryIndex, MemorySpace, SpaceMember

logger = logging.getLogger(__name__)


class OptimisticLockError(RuntimeError):
    """A conditional manifest write failed because the row moved under us.

    Raised by :meth:`MemorySpaceRepository.put_index` when a caller supplies an
    ``expected_version`` that no longer matches the stored one. The service
    translates this into a re-read-and-retry loop (and ultimately a
    ``MemorySpaceConcurrencyError`` if it can't converge). Kept repository-local
    so this layer stays free of the service's error taxonomy.
    """

class ManifestTooLargeError(RuntimeError):
    """The manifest row would pass :data:`MANIFEST_MAX_BYTES`.

    DynamoDB's hard item limit is 400 KB; refusing at 300 KB leaves room and
    turns an opaque SDK failure into a clear message.
    """


_META_SK = "META"
_INDEX_SK = "INDEX"
_MEMBER_SK_PREFIX = "MEMBER#"
_FILEVER_SK_PREFIX = "FILEVER#"

MANIFEST_MAX_BYTES = 300 * 1024

OWNER_INDEX = "OwnerIndex"
MEMBER_INDEX = "MemberIndex"


def _space_pk(space_id: str) -> str:
    return f"SPACE#{space_id}"


def _member_sk(email: str) -> str:
    return f"{_MEMBER_SK_PREFIX}{email.strip().lower()}"


def _file_version_sk(slug: str, version: int) -> str:
    return f"{_FILEVER_SK_PREFIX}{slug}#{version:06d}"


def _to_dynamo(obj: Any) -> Any:
    """Recursively convert floats to Decimal for DynamoDB writes."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_dynamo(v) for v in obj]
    return obj


def _from_dynamo(obj: Any) -> Any:
    """Recursively convert Decimal to int/float for JSON-friendly reads."""
    if isinstance(obj, Decimal):
        # Whole numbers come back as int (sizes, counts); keep fractions float.
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: _from_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_dynamo(v) for v in obj]
    return obj


class MemorySpaceRepository:
    """CRUD for the ``memory-spaces`` single table (META / INDEX / MEMBER rows)."""

    def __init__(self, table_name: Optional[str] = None):
        self.table_name = table_name or os.environ.get(
            "DYNAMODB_MEMORY_SPACES_TABLE_NAME", "memory-spaces"
        )
        self._dynamodb = boto3.resource("dynamodb")
        self._table = self._dynamodb.Table(self.table_name)

    # ---- serialization -------------------------------------------------

    @staticmethod
    def _space_to_item(space: MemorySpace) -> dict:
        item = {
            "PK": _space_pk(space.space_id),
            "SK": _META_SK,
            "spaceId": space.space_id,
            "name": space.name,
            "template": space.template,
            "ownerId": space.owner_id,
            "ownerEmail": space.owner_email,
            "createdAt": space.created_at,
            "updatedAt": space.updated_at,
        }
        if space.index_s3_key is not None:
            item["indexS3Key"] = space.index_s3_key
        if space.index_content_hash is not None:
            item["indexContentHash"] = space.index_content_hash
        if space.file_format != "freeform":
            item["fileFormat"] = space.file_format
        if space.is_project_space:
            # Left out of OwnerIndex (a sparse index), so a project's spaces
            # never appear in anyone's own list of spaces or binding picker.
            item["scope"] = space.scope
            item["projectId"] = space.project_id
            if space.user_id:
                item["userId"] = space.user_id
        else:
            item["GSI1PK"] = f"OWNER#{space.owner_id}"
            item["GSI1SK"] = _space_pk(space.space_id)
        return item

    @staticmethod
    def _item_to_space(item: dict) -> MemorySpace:
        return MemorySpace(
            space_id=item.get("spaceId", ""),
            name=item.get("name", ""),
            template=item.get("template", "blank"),
            owner_id=item.get("ownerId", ""),
            owner_email=item.get("ownerEmail", ""),
            created_at=item.get("createdAt", ""),
            updated_at=item.get("updatedAt", ""),
            index_s3_key=item.get("indexS3Key"),
            index_content_hash=item.get("indexContentHash"),
            file_format=item.get("fileFormat", "freeform"),
            scope=item.get("scope", "personal"),
            project_id=item.get("projectId"),
            user_id=item.get("userId"),
        )

    @staticmethod
    def _entry_to_item(r: MemoryEntryRef) -> dict:
        entry: Dict[str, Any] = {
            "slug": r.slug,
            "type": r.entry_type,
            "description": r.description,
            "contentHash": r.content_hash,
            "size": int(r.size),
            "s3Key": r.s3_key,
            "updated": r.updated,
            "updatedBy": r.updated_by,
            "indexed": _to_dynamo(r.indexed),
        }
        # Fields added with file history (Shared Projects 2.3) are written only
        # when set, so an entry saved before then keeps its exact shape.
        if r.aliases:
            entry["aliases"] = list(r.aliases)
        if r.tokens is not None:
            entry["tokens"] = int(r.tokens)
        if r.tokens_method:
            entry["tokensMethod"] = r.tokens_method
        if r.item_count is not None:
            entry["itemCount"] = int(r.item_count)
        if r.archived:
            entry["archived"] = True
        if r.version:
            entry["version"] = int(r.version)
        return entry

    @classmethod
    def _index_to_item(cls, index: MemoryIndex) -> dict:
        entries = [cls._entry_to_item(r) for r in index.entries]
        return {
            "PK": _space_pk(index.space_id),
            "SK": _INDEX_SK,
            "spaceId": index.space_id,
            "entries": entries,
            "version": int(index.version),
        }

    @staticmethod
    def _item_to_index(item: dict, space_id: str) -> MemoryIndex:
        entries = [
            MemoryEntryRef(
                slug=r.get("slug", ""),
                entry_type=r.get("type", "fact"),
                description=r.get("description", ""),
                content_hash=r.get("contentHash", ""),
                size=int(r.get("size", 0)),
                s3_key=r.get("s3Key", ""),
                updated=r.get("updated", ""),
                updated_by=r.get("updatedBy", ""),
                indexed=_from_dynamo(r.get("indexed") or {}),
                aliases=list(r.get("aliases") or []),
                tokens=int(r["tokens"]) if r.get("tokens") is not None else None,
                tokens_method=r.get("tokensMethod"),
                item_count=int(r["itemCount"]) if r.get("itemCount") is not None else None,
                archived=bool(r.get("archived", False)),
                version=int(r.get("version", 0)),
            )
            for r in (item.get("entries") or [])
        ]
        return MemoryIndex(
            space_id=space_id,
            entries=entries,
            version=int(item.get("version", 0)),
        )

    @staticmethod
    def _member_to_item(space_id: str, member: SpaceMember) -> dict:
        email = member.email.strip().lower()
        return {
            "PK": _space_pk(space_id),
            "SK": _member_sk(email),
            "GSI2PK": f"MEMBER#{email}",
            "GSI2SK": _space_pk(space_id),
            "spaceId": space_id,
            "email": email,
            "permission": member.permission,
            "createdAt": member.created_at,
        }

    @staticmethod
    def _item_to_member(item: dict) -> SpaceMember:
        return SpaceMember(
            email=item.get("email", ""),
            permission=item.get("permission", "viewer"),
            created_at=item.get("createdAt", ""),
        )

    # ---- space META ----------------------------------------------------

    def put_space(self, space: MemorySpace) -> None:
        self._table.put_item(Item=self._space_to_item(space))

    def get_space(self, space_id: str) -> Optional[MemorySpace]:
        resp = self._table.get_item(
            Key={"PK": _space_pk(space_id), "SK": _META_SK}
        )
        item = resp.get("Item")
        return self._item_to_space(item) if item else None

    def list_owned(self, owner_id: str) -> List[MemorySpace]:
        resp = self._table.query(
            IndexName=OWNER_INDEX,
            KeyConditionExpression=Key("GSI1PK").eq(f"OWNER#{owner_id}"),
        )
        return [self._item_to_space(i) for i in resp.get("Items", [])]

    def delete_space(self, space_id: str) -> None:
        """Delete every row for a space (META, INDEX, MEMBER and FILEVER rows).

        Paginated: with version history a space can hold more than one
        query page of rows.
        """
        keys = list(
            self._query_pages(
                KeyConditionExpression=Key("PK").eq(_space_pk(space_id)),
                ProjectionExpression="PK, SK",
            )
        )
        with self._table.batch_writer() as batch:
            for item in keys:
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})

    def _query_pages(self, **kwargs: Any) -> Iterator[dict]:
        while True:
            resp = self._table.query(**kwargs)
            yield from resp.get("Items", [])
            last = resp.get("LastEvaluatedKey")
            if not last:
                return
            kwargs["ExclusiveStartKey"] = last

    # ---- index manifest ------------------------------------------------

    def get_index(self, space_id: str) -> MemoryIndex:
        resp = self._table.get_item(
            Key={"PK": _space_pk(space_id), "SK": _INDEX_SK}
        )
        item = resp.get("Item")
        if not item:
            return MemoryIndex(space_id=space_id, entries=[], version=0)
        return self._item_to_index(item, space_id)

    def put_index(
        self, index: MemoryIndex, *, expected_version: Optional[int] = None
    ) -> None:
        """Persist the manifest row.

        With ``expected_version`` the write is conditional on the stored
        ``version`` still matching it — optimistic concurrency for shared
        spaces. On a mismatch it raises :class:`OptimisticLockError` so the
        service can re-read and retry. The ``attribute_not_exists`` branch
        admits the very first write (``create_space`` seeds version 0, so this
        is a safety net rather than the common path).
        """
        item = self._index_to_item(index)
        size = len(json.dumps(item, default=str).encode("utf-8"))
        if size > MANIFEST_MAX_BYTES:
            raise ManifestTooLargeError(
                f"manifest for space '{index.space_id}' would be {size} bytes "
                f"(limit {MANIFEST_MAX_BYTES})"
            )
        if expected_version is None:
            self._table.put_item(Item=item)
            return
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK) OR #v = :expected",
                ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues={":expected": int(expected_version)},
            )
        except ClientError as e:  # narrow to the conditional-failure case
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise OptimisticLockError(
                    f"manifest for space '{index.space_id}' changed concurrently"
                ) from e
            raise

    # ---- members -------------------------------------------------------

    def put_member(self, space_id: str, member: SpaceMember) -> None:
        self._table.put_item(Item=self._member_to_item(space_id, member))

    def get_member(self, space_id: str, email: str) -> Optional[SpaceMember]:
        resp = self._table.get_item(
            Key={"PK": _space_pk(space_id), "SK": _member_sk(email)}
        )
        item = resp.get("Item")
        return self._item_to_member(item) if item else None

    def delete_member(self, space_id: str, email: str) -> None:
        self._table.delete_item(
            Key={"PK": _space_pk(space_id), "SK": _member_sk(email)}
        )

    def list_members(self, space_id: str) -> List[SpaceMember]:
        resp = self._table.query(
            KeyConditionExpression=Key("PK").eq(_space_pk(space_id))
            & Key("SK").begins_with(_MEMBER_SK_PREFIX)
        )
        return [self._item_to_member(i) for i in resp.get("Items", [])]

    def list_member_space_ids(self, email: str) -> List[str]:
        """Return the space_ids a user (by email) has been granted access to."""
        normalized = email.strip().lower()
        resp = self._table.query(
            IndexName=MEMBER_INDEX,
            KeyConditionExpression=Key("GSI2PK").eq(f"MEMBER#{normalized}"),
        )
        return [i.get("spaceId", "") for i in resp.get("Items", []) if i.get("spaceId")]

    # ---- file versions (FILEVER) ----------------------------------------

    @staticmethod
    def _file_version_to_item(space_id: str, v: FileVersion) -> dict:
        item: Dict[str, Any] = {
            "PK": _space_pk(space_id),
            "SK": _file_version_sk(v.slug, v.version),
            "slug": v.slug,
            "version": int(v.version),
            "contentHash": v.content_hash,
            "size": int(v.size),
            "updatedBy": v.updated_by,
            "updatedAt": v.updated_at,
            "reason": v.reason,
        }
        if v.tokens is not None:
            item["tokens"] = int(v.tokens)
        if v.tokens_method:
            item["tokensMethod"] = v.tokens_method
        if v.proposal_id:
            item["proposalId"] = v.proposal_id
        if v.run_id:
            item["runId"] = v.run_id
        return item

    @staticmethod
    def _item_to_file_version(item: dict) -> FileVersion:
        return FileVersion(
            slug=item.get("slug", ""),
            version=int(item.get("version", 0)),
            content_hash=item.get("contentHash", ""),
            size=int(item.get("size", 0)),
            tokens=int(item["tokens"]) if item.get("tokens") is not None else None,
            tokens_method=item.get("tokensMethod"),
            updated_by=item.get("updatedBy", ""),
            updated_at=item.get("updatedAt", ""),
            reason=item.get("reason", "edit"),
            proposal_id=item.get("proposalId"),
            run_id=item.get("runId"),
        )

    def put_file_version(self, space_id: str, version: FileVersion) -> None:
        self._table.put_item(Item=self._file_version_to_item(space_id, version))

    def get_file_version(self, space_id: str, slug: str, version: int) -> Optional[FileVersion]:
        resp = self._table.get_item(
            Key={"PK": _space_pk(space_id), "SK": _file_version_sk(slug, version)}
        )
        item = resp.get("Item")
        return self._item_to_file_version(item) if item else None

    def list_file_versions(self, space_id: str, slug: Optional[str] = None) -> List[FileVersion]:
        """Versions of one file (oldest first), or of every file when ``slug`` is None.

        The sort-key prefix for ``a`` also matches a freeform slug like
        ``a#b``, so results are filtered on the stored slug.
        """
        prefix = _FILEVER_SK_PREFIX if slug is None else f"{_FILEVER_SK_PREFIX}{slug}#"
        items = self._query_pages(
            KeyConditionExpression=Key("PK").eq(_space_pk(space_id)) & Key("SK").begins_with(prefix)
        )
        versions = [self._item_to_file_version(i) for i in items]
        if slug is not None:
            versions = [v for v in versions if v.slug == slug]
        return versions

    def delete_file_versions(self, space_id: str, slug: str) -> List[FileVersion]:
        """Delete every version row of one file; return what was deleted."""
        versions = self.list_file_versions(space_id, slug)
        with self._table.batch_writer() as batch:
            for v in versions:
                batch.delete_item(Key={"PK": _space_pk(space_id), "SK": _file_version_sk(v.slug, v.version)})
        return versions
