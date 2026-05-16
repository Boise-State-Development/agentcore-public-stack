"""Artifact writer — S3 upload + DynamoDB version/HEAD rows.

Frozen cross-PR contract (must stay in sync with the render Lambda
`backend/src/lambdas/artifact_render/handler.py` and the app-api minter
`backend/src/apis/app_api/artifacts/service.py`):

  Version row : PK=USER#{user_id}  SK=ARTIFACT#{aid}#V#{version:05d}
                attrs storage="s3", content_key, content_type
  HEAD row    : PK=USER#{user_id}  SK=ARTIFACT#{aid}#HEAD
                + GSI1PK=SESSION#{session_id}
                + GSI1SK=ARTIFACT#{updated_at}#{aid}   (SessionIndex)
  S3 layout   : {user_id}/{aid}/v{n}/index.html

Versions are immutable (no DeleteObject grant in inference-api) — an
update writes a new version and re-points HEAD.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_DEFAULT_CONTENT_TYPE = "text/html; charset=utf-8"

_cached_bucket: Optional[str] = None
_cached_table: Optional[str] = None
_ssm_client = None
_s3_client = None
_ddb_resource = None


class ArtifactError(Exception):
    """Base class for artifact write failures."""


class ArtifactNotFoundError(ArtifactError):
    """Update target does not exist for this user."""


class ArtifactConfigError(ArtifactError):
    """Artifacts feature is not configured for this environment."""


def _reset_caches_for_tests() -> None:
    global _cached_bucket, _cached_table, _ssm_client, _s3_client, _ddb_resource
    _cached_bucket = None
    _cached_table = None
    _ssm_client = None
    _s3_client = None
    _ddb_resource = None


def _region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )


def _resolve(env_var: str, ssm_suffix: str) -> str:
    """Env var first, then SSM under the runtime's PROJECT_PREFIX.

    inference-api exposes PROJECT_PREFIX and holds ssm:GetParameter on
    `/{prefix}/*`, so the artifacts params published by the artifacts
    stack are readable without any extra wiring."""
    value = os.environ.get(env_var)
    if value:
        return value
    global _ssm_client
    prefix = os.environ.get("PROJECT_PREFIX")
    if not prefix:
        raise ArtifactConfigError(
            f"{env_var} unset and PROJECT_PREFIX unavailable"
        )
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm", region_name=_region())
    try:
        resp = _ssm_client.get_parameter(
            Name=f"/{prefix}/artifacts/{ssm_suffix}"
        )
    except ClientError as exc:
        raise ArtifactConfigError(
            f"artifacts {ssm_suffix} parameter not found"
        ) from exc
    return resp["Parameter"]["Value"]


def _bucket_name() -> str:
    global _cached_bucket
    if _cached_bucket is None:
        _cached_bucket = _resolve("S3_ARTIFACTS_BUCKET_NAME", "bucket-name")
    return _cached_bucket


def _table():
    global _cached_table, _ddb_resource
    if _cached_table is None:
        _cached_table = _resolve("DYNAMODB_ARTIFACTS_TABLE_NAME", "table-name")
    if _ddb_resource is None:
        _ddb_resource = boto3.resource("dynamodb", region_name=_region())
    return _ddb_resource.Table(_cached_table)


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=_region())
    return _s3_client


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _put_object(user_id: str, artifact_id: str, version: int,
                content: str, content_type: str) -> str:
    key = f"{user_id}/{artifact_id}/v{version}/index.html"
    _s3().put_object(
        Bucket=_bucket_name(),
        Key=key,
        Body=content.encode("utf-8"),
        ContentType=content_type,
    )
    return key


def create_artifact_record(
    user_id: str,
    session_id: str,
    title: str,
    content: str,
    content_type: str,
) -> tuple[str, int]:
    """Create v1 of a new artifact. Returns (artifact_id, version)."""
    artifact_id = uuid.uuid4().hex
    version = 1
    content_type = content_type or _DEFAULT_CONTENT_TYPE
    now = _now_iso()
    content_key = _put_object(user_id, artifact_id, version, content, content_type)

    pk = f"USER#{user_id}"
    common = {
        "storage": "s3",
        "content_key": content_key,
        "content_type": content_type,
        "version": version,
        "artifact_id": artifact_id,
        "user_id": user_id,
        "session_id": session_id,
        "title": title,
        "created_at": now,
    }
    table = _table()
    try:
        table.put_item(
            Item={**common, "PK": pk, "SK": f"ARTIFACT#{artifact_id}#V#{version:05d}"},
            ConditionExpression="attribute_not_exists(SK)",
        )
        table.put_item(
            Item={
                **common,
                "PK": pk,
                "SK": f"ARTIFACT#{artifact_id}#HEAD",
                "updated_at": now,
                "GSI1PK": f"SESSION#{session_id}",
                "GSI1SK": f"ARTIFACT#{now}#{artifact_id}",
            },
            ConditionExpression="attribute_not_exists(SK)",
        )
    except ClientError as exc:
        raise ArtifactError("failed to write artifact metadata") from exc

    logger.info(
        "created artifact user=%s artifact=%s v=%s session=%s",
        user_id, artifact_id, version, session_id,
    )
    return artifact_id, version


def update_artifact_record(
    user_id: str,
    artifact_id: str,
    content: str,
    title: Optional[str],
    content_type: Optional[str],
) -> int:
    """Append a new immutable version and re-point HEAD. Returns version."""
    pk = f"USER#{user_id}"
    table = _table()
    try:
        head = table.get_item(
            Key={"PK": pk, "SK": f"ARTIFACT#{artifact_id}#HEAD"}
        ).get("Item")
    except ClientError as exc:
        raise ArtifactError("artifact metadata lookup failed") from exc
    if not head:
        raise ArtifactNotFoundError(artifact_id)

    current = int(head["version"])
    version = current + 1
    title = title or head.get("title", "")
    content_type = content_type or head.get("content_type") or _DEFAULT_CONTENT_TYPE
    now = _now_iso()
    content_key = _put_object(user_id, artifact_id, version, content, content_type)

    common = {
        "storage": "s3",
        "content_key": content_key,
        "content_type": content_type,
        "version": version,
        "artifact_id": artifact_id,
        "user_id": user_id,
        "session_id": head.get("session_id", ""),
        "title": title,
        "created_at": head.get("created_at", now),
    }
    try:
        table.put_item(
            Item={**common, "PK": pk, "SK": f"ARTIFACT#{artifact_id}#V#{version:05d}"},
            ConditionExpression="attribute_not_exists(SK)",
        )
        # Optimistic lock: HEAD must still be at the version we read, so
        # two concurrent updates can't silently clobber each other.
        table.put_item(
            Item={
                **common,
                "PK": pk,
                "SK": f"ARTIFACT#{artifact_id}#HEAD",
                "updated_at": now,
                "GSI1PK": f"SESSION#{head.get('session_id', '')}",
                "GSI1SK": f"ARTIFACT#{now}#{artifact_id}",
            },
            ConditionExpression="version = :cur",
            ExpressionAttributeValues={":cur": current},
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            raise ArtifactError(
                "artifact changed concurrently; retry the update"
            ) from exc
        raise ArtifactError("failed to write artifact metadata") from exc

    logger.info(
        "updated artifact user=%s artifact=%s v=%s", user_id, artifact_id, version
    )
    return version


def set_produced_by_message_index(
    user_id: str, artifact_id: str, message_index: int
) -> None:
    """Stamp the HEAD row with the index of the assistant message that
    produced (or last updated) the artifact this turn.

    The artifact tool can't know this at write time — the turn isn't
    finished — so the stream coordinator writes it back post-turn using
    the same odd-position index it already computes for per-message
    metadata (`initial_message_count + 2*i + 1`). That index matches the
    `idx` the messages endpoint enumerates on reload, so the SPA can
    render the card inline after the right assistant message.

    Best-effort: a SET on a single attribute, keyed by the HEAD row, that
    deliberately does not touch `version` so it can never collide with
    the update_artifact optimistic lock. Failures are swallowed by the
    caller (linkage is a UX nicety, never worth breaking a turn over).
    """
    table = _table()
    table.update_item(
        Key={"PK": f"USER#{user_id}", "SK": f"ARTIFACT#{artifact_id}#HEAD"},
        UpdateExpression="SET produced_by_message_index = :idx",
        ExpressionAttributeValues={":idx": message_index},
        ConditionExpression="attribute_exists(SK)",
    )


_SESSION_INDEX = "SessionIndex"


def list_session_artifacts(user_id: str, session_id: str) -> list[dict]:
    """Current HEAD of every artifact written in a chat session.

    Read side of the same SessionIndex GSI the app-api list endpoint
    uses; the stream coordinator calls this post-turn to emit the live
    `artifact` SSE event. Only HEAD rows carry GSI1PK/GSI1SK, so the
    query returns one row per artifact (its current version). GSI1PK is
    SESSION#-scoped (not user-scoped) so every row is re-checked against
    the authenticated user's id.
    """
    table = _table()
    items: list[dict] = []
    kwargs: dict = {
        "IndexName": _SESSION_INDEX,
        "KeyConditionExpression": Key("GSI1PK").eq(f"SESSION#{session_id}"),
        "ScanIndexForward": False,  # GSI1SK embeds updated_at → newest first
    }
    try:
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
            kwargs["ExclusiveStartKey"] = last
    except ClientError as exc:
        raise ArtifactError("artifact list query failed") from exc

    out: list[dict] = []
    for item in items:
        if item.get("user_id") != user_id:
            continue
        out.append(
            {
                "artifact_id": item.get("artifact_id", ""),
                "version": int(item.get("version", 0)),
                "title": item.get("title", ""),
                "content_type": item.get(
                    "content_type", _DEFAULT_CONTENT_TYPE
                ),
                "updated_at": item.get("updated_at", ""),
                "created_at": item.get("created_at"),
                "produced_by_message_index": item.get(
                    "produced_by_message_index"
                ),
            }
        )
    return out
