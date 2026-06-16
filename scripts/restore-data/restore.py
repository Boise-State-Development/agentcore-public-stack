"""Restore tool for AgentCore Public Stack.

Reads a backup manifest.json from a backup bucket, discovers the target
infrastructure via SSM Parameter Store, and imports all backed-up data into
the deployed two-stack (PlatformStack + BackendStack) environment.

Restore steps (in order):
  1. DynamoDB tables — BatchWriteItem from DynamoDB-JSON exports
  2. S3 buckets — aws s3 sync from backup bucket to target bucket
  3. Cognito — re-create identity providers + app clients using preserved
     secrets, then import users/groups
  4. AgentCore Memory — replay raw events via CreateEvent. Preserves
     actorId (with sub remap), sessionId, eventTimestamp, payload,
     metadata, and branch references. Strategy extraction reruns
     synchronously on the target, so semantic memories rebuild
     automatically.

Usage:
  uv run python restore.py \
    --backup-bucket {prefix}-backup-{timestamp} \
    --manifest-key {prefix}/{timestamp}/manifest.json \
    --target-prefix {new-prefix} \
    --region us-west-2

The tool is idempotent: re-running it skips items that already exist
(DynamoDB conditional writes, S3 sync is naturally idempotent, Cognito
providers/clients are checked before creation, users use AdminCreateUser
with MessageAction=SUPPRESS which is idempotent on existing users).

Requires the target infrastructure (PlatformStack + BackendStack) to be
fully deployed before running — the tool resolves target table names and
bucket names from SSM.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import logging
import os
import re
import secrets
import string
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer
import botocore
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

LOG = logging.getLogger("restore")

BOTO_CONFIG = BotoConfig(
    retries={"max_attempts": 10, "mode": "adaptive"},
    user_agent_extra="agentcore-restore/1.0",
    # urllib3's default connection pool size is 10. The restore tool runs
    # S3 copies and AgentCore Memory replays through a ThreadPoolExecutor
    # with max_workers=16, so the default cap was forcing every request
    # past the 10th to open a fresh TCP+TLS connection and discard it
    # afterwards (the urllib3 "Connection pool is full, discarding
    # connection" warnings). No data was lost — every request still
    # completed — but the lack of connection reuse hurts throughput.
    # Bump comfortably above max_workers so multi-call operations
    # (e.g. CopyObject + tag fetch) and any side clients also fit.
    max_pool_connections=32,
)

# Maps backup logical names → SSM parameter paths for the target table names.
# Must stay in sync with the backup tool's DYNAMODB_TABLES list.
TABLE_SSM_MAP: dict[str, str] = {
    "users":                "/users/users-table-name",
    "app-roles":            "/rbac/app-roles-table-name",
    "api-keys":             "/auth/api-keys-table-name",
    "auth-providers":       "/auth/auth-providers-table-name",
    "oauth-providers":      "/oauth/providers-table-name",
    "oauth-user-tokens":    "/oauth/user-tokens-table-name",
    "user-quotas":          "/quota/user-quotas-table-name",
    "quota-events":         "/quota/quota-events-table-name",
    "sessions-metadata":    "/cost-tracking/sessions-metadata-table-name",
    "user-cost-summary":    "/cost-tracking/user-cost-summary-table-name",
    "system-cost-rollup":   "/cost-tracking/system-cost-rollup-table-name",
    "managed-models":       "/admin/managed-models-table-name",
    "user-menu-links":      "/admin/user-menu-links-table-name",
    "user-settings":        "/settings/user-settings-table-name",
    "user-file-uploads":    "/user-file-uploads/table-name",
    "shared-conversations": "/shares/shared-conversations-table-name",
    "rag-assistants":       "/rag/assistants-table-name",
    "artifacts":            "/artifacts/table-name",
    "fine-tuning-jobs":     "/fine-tuning/jobs-table-name",
    "fine-tuning-access":   "/fine-tuning/access-table-name",
}

# Convention-named tables (not in SSM).
# Currently empty — the previously-listed `assistants` table was
# decommissioned in commit c977e04e (the project uses the
# rag-assistants table for both assistant config and document
# metadata via DYNAMODB_ASSISTANTS_TABLE_NAME). Restoring an
# `assistants` component from an old backup will skip cleanly with
# "target table not found via SSM".
TABLE_CONVENTION_MAP: dict[str, str] = {}

BUCKET_SSM_MAP: dict[str, str] = {
    "user-file-uploads": "/user-file-uploads/bucket-name",
    "rag-documents":     "/rag/documents-bucket-name",
    "artifacts":         "/artifacts/bucket-name",
    "fine-tuning-data":  "/fine-tuning/data-bucket-name",
}

# S3 Vectors indexes. Mirrors `VECTOR_INDEXES` in scripts/backup-data/backup.py.
# Each entry maps a logical name to the SSM paths that hold the target
# bucket name and index name on the destination platform. The backup
# component layout is `vectors/{logical}.jsonl.gz` and the restore step
# replays those vectors via `s3vectors.put_vectors`.
VECTOR_INDEXES: list[dict[str, str]] = [
    {"logical": "rag-vectors",
     "bucket_ssm": "/rag/vector-bucket-name",
     "index_ssm":  "/rag/vector-index-name"},
]

SSM_USER_POOL_ID = "/auth/cognito/user-pool-id"
SSM_MEMORY_ID = "/inference-api/memory-id"


@dataclass
class RestoreContext:
    backup_bucket: str
    manifest_key: str
    target_prefix: str
    region: str
    session: boto3.Session
    manifest: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    skip_cognito_users: bool = False
    skip_memory_replay: bool = False
    results: list[dict[str, Any]] = field(default_factory=list)
    # ----- Cognito sub remapping -----
    # AWS Cognito does NOT permit setting `sub` on user creation —
    # it is auto-generated and immutable. When a user pool is
    # destroyed and re-created (which happens on every full
    # teardown + redeploy), every user's sub changes. The app keys
    # all DynamoDB rows by USER#<sub> and embeds <sub> inside other
    # string attributes (owner refs, audit trails, S3 keys, etc.),
    # so a naive restore that just copies the old data would orphan
    # every user's history.
    #
    # `sub_map` is built during restore_cognito, populated as each
    # user is recreated and we capture the new sub from the
    # AdminCreateUser response. Subsequent DynamoDB and S3 restore
    # passes look up every string attribute / key against this map
    # and rewrite any matching old-sub UUIDs to their new
    # equivalents before persisting. The compiled regex is cached
    # alongside the map for O(1) per-string scans regardless of
    # user count.
    sub_map: dict[str, str] = field(default_factory=dict)
    sub_map_pattern: re.Pattern[str] | None = None


# --------------------------------------------------------------------------- #
# SSM helpers                                                                  #
# --------------------------------------------------------------------------- #
def get_ssm_param(session: boto3.Session, prefix: str, path: str) -> str | None:
    ssm = session.client("ssm", config=BOTO_CONFIG)
    try:
        return ssm.get_parameter(Name=f"/{prefix}{path}")["Parameter"]["Value"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise


# --------------------------------------------------------------------------- #
# Cognito sub remapping                                                        #
#                                                                              #
# When a Cognito user pool is recreated, every user's `sub` UUID changes.      #
# The app keys all per-user DynamoDB partitions and several S3 prefixes by    #
# `<sub>`, so we have to rewrite every string occurrence of an old sub to     #
# its new sub between read (from the backup) and write (to the new            #
# infrastructure). The mapping is built during restore_cognito as users are   #
# recreated, then applied generically here.                                   #
# --------------------------------------------------------------------------- #
def compile_sub_pattern(sub_map: dict[str, str]) -> re.Pattern[str] | None:
    """Compile a single regex matching any old-sub UUID in `sub_map`.

    Returns None if the map is empty (caller short-circuits remapping).
    Subs are 36-char Cognito UUIDs (alpha + digits + hyphens) that are
    extremely unlikely to collide with anything else in user data, so
    plain alternation is safe.
    """
    if not sub_map:
        return None
    return re.compile("|".join(re.escape(s) for s in sub_map.keys()))


def remap_subs(value: Any, pattern: re.Pattern[str] | None, sub_map: dict[str, str]) -> Any:
    """Recursively replace every occurrence of any old sub in a Python value.

    Handles the four DynamoDB-deserialised Python shapes:
      - str         → re.sub against the compiled pattern
      - dict        → recurse on each value (keys are never subs in this
                      app — partition/sort keys are always 'PK'/'SK', etc.)
      - list / set  → recurse on each element
      - everything else (bytes, int, Decimal, bool, None) is returned as-is
    """
    if pattern is None:
        return value
    if isinstance(value, str):
        return pattern.sub(lambda m: sub_map[m.group(0)], value)
    if isinstance(value, dict):
        return {k: remap_subs(v, pattern, sub_map) for k, v in value.items()}
    if isinstance(value, list):
        return [remap_subs(v, pattern, sub_map) for v in value]
    if isinstance(value, set):
        return {remap_subs(v, pattern, sub_map) for v in value}
    return value


def remap_subs_in_key(key: str, pattern: re.Pattern[str] | None, sub_map: dict[str, str]) -> str:
    """Remap any old-sub UUID inside an S3 key path component."""
    if pattern is None:
        return key
    return pattern.sub(lambda m: sub_map[m.group(0)], key)


# --------------------------------------------------------------------------- #
# DynamoDB restore                                                             #
# --------------------------------------------------------------------------- #
def restore_dynamodb_table(ctx: RestoreContext, logical_name: str, component: dict) -> dict:
    """Restore a single DynamoDB table from its ExportTableToPointInTime output."""
    detail = component.get("detail", {})
    # Backup records the AWS-returned ExportManifest field as `export_manifest`
    # (a path like '<prefix>/AWSDynamoDB/<exportId>/manifest-summary.json').
    # Earlier versions of this script looked for `export_manifest_key`,
    # which never existed in the backup output — see manifest.json from any
    # backup-data run for the canonical field name.
    export_summary_key = detail.get("export_manifest")
    if not export_summary_key:
        return {"logical": logical_name, "status": "skipped", "reason": "no export_manifest in backup"}

    # Resolve target table name
    target_table = None
    if logical_name in TABLE_SSM_MAP:
        target_table = get_ssm_param(ctx.session, ctx.target_prefix, TABLE_SSM_MAP[logical_name])
    elif logical_name in TABLE_CONVENTION_MAP:
        target_table = f"{ctx.target_prefix}-{TABLE_CONVENTION_MAP[logical_name]}"

    if not target_table:
        return {"logical": logical_name, "status": "skipped", "reason": f"target table not found via SSM"}

    LOG.info(f"[DynamoDB] Restoring {logical_name} → {target_table}")
    if ctx.dry_run:
        return {"logical": logical_name, "status": "dry-run", "target_table": target_table}

    s3 = ctx.session.client("s3", config=BOTO_CONFIG)
    dynamodb = ctx.session.resource("dynamodb", config=BOTO_CONFIG)
    table = dynamodb.Table(target_table)

    # AWS DynamoDB ExportTableToPointInTime emits a two-level manifest:
    #   1. <prefix>/AWSDynamoDB/<exportId>/manifest-summary.json — metadata,
    #      includes a `manifestFilesS3Key` field pointing to (2).
    #   2. <prefix>/AWSDynamoDB/<exportId>/manifest-files.json — line-
    #      delimited JSON, each line carries a `dataFileS3Key` for the
    #      actual gzipped DDB-JSON data file.
    # We have to indirect through both to find the data files.
    summary_obj = s3.get_object(Bucket=ctx.backup_bucket, Key=export_summary_key)
    summary = json.loads(summary_obj["Body"].read().decode("utf-8"))
    manifest_files_key = summary.get("manifestFilesS3Key")
    if not manifest_files_key:
        return {
            "logical": logical_name,
            "status": "failed",
            "error": f"manifest-summary at {export_summary_key} missing manifestFilesS3Key",
        }

    files_obj = s3.get_object(Bucket=ctx.backup_bucket, Key=manifest_files_key)
    files_body = files_obj["Body"].read().decode("utf-8")

    data_files: list[str] = []
    for line in files_body.strip().split("\n"):
        if not line.strip():
            continue
        entry = json.loads(line)
        if "dataFileS3Key" in entry:
            data_files.append(entry["dataFileS3Key"])

    items_written = 0
    items_remapped = 0
    for data_key in data_files:
        obj = s3.get_object(Bucket=ctx.backup_bucket, Key=data_key)
        body = obj["Body"].read()

        # DynamoDB exports are gzipped
        if data_key.endswith(".gz"):
            body = gzip.decompress(body)

        # Each line is a JSON object with an "Item" key containing DynamoDB-JSON
        with table.batch_writer() as batch:
            for line in body.decode("utf-8").strip().split("\n"):
                if not line.strip():
                    continue
                record = json.loads(line)
                item = record.get("Item", record)
                # Convert DynamoDB-JSON to Python dict
                deserialized = _deserialize_dynamodb_json(item)
                # Apply Cognito sub remapping. Even if no users were
                # recreated (empty sub_map), `remap_subs` short-circuits
                # in O(1) via the None pattern check.
                if ctx.sub_map_pattern is not None:
                    remapped = remap_subs(deserialized, ctx.sub_map_pattern, ctx.sub_map)
                    if remapped != deserialized:
                        items_remapped += 1
                    deserialized = remapped
                batch.put_item(Item=deserialized)
                items_written += 1

    LOG.info(
        f"[DynamoDB] {logical_name}: wrote {items_written} items to {target_table}"
        + (f" ({items_remapped} sub-remapped)" if items_remapped else "")
    )
    return {
        "logical": logical_name,
        "status": "ok",
        "items_written": items_written,
        "items_remapped": items_remapped,
        "target_table": target_table,
    }


def _decode_binary_values(value: Any) -> Any:
    """Recursively convert DynamoDB-JSON binary attribute values from
    base64-encoded strings to raw bytes, in place.

    AWS's DynamoDB Export-to-S3 feature serializes binary attributes
    (the ``B`` and ``BS`` types) as base64-encoded strings on disk.
    boto3's :class:`TypeDeserializer`, however, expects ``B`` to already
    be ``bytes``/``bytearray`` (because the live DynamoDB API path
    base64-decodes the wire payload before the deserializer sees it).
    Feeding it the raw base64 string raises::

        TypeError: Value must be of the following types: <class 'bytearray'>, <class 'bytes'>

    This helper walks an attribute-value tree (DynamoDB-JSON shape:
    ``{"<type>": <value>}``) and decodes any ``B``/``BS`` strings into
    bytes before deserialization. It also recurses into ``L`` and ``M``
    so nested binary values inside lists/maps are handled.
    """
    if not isinstance(value, dict) or len(value) != 1:
        return value
    type_key = next(iter(value))
    type_val = value[type_key]
    if type_key == "B" and isinstance(type_val, str):
        return {"B": base64.b64decode(type_val)}
    if type_key == "BS" and isinstance(type_val, list):
        return {"BS": [base64.b64decode(s) if isinstance(s, str) else s for s in type_val]}
    if type_key == "L" and isinstance(type_val, list):
        return {"L": [_decode_binary_values(v) for v in type_val]}
    if type_key == "M" and isinstance(type_val, dict):
        return {"M": {k: _decode_binary_values(v) for k, v in type_val.items()}}
    return value


def _deserialize_dynamodb_json(item: dict) -> dict:
    """Convert DynamoDB-JSON (typed attribute values) to plain Python dict."""
    deserializer = TypeDeserializer()
    return {k: deserializer.deserialize(_decode_binary_values(v)) for k, v in item.items()}


# --------------------------------------------------------------------------- #
# S3 restore                                                                   #
# --------------------------------------------------------------------------- #
def restore_s3_bucket(ctx: RestoreContext, logical_name: str, component: dict) -> dict:
    """Restore an S3 bucket from backup, applying Cognito sub remapping to keys.

    The previous implementation shelled out to `aws s3 sync` — fast for
    bulk copies but unable to transform keys in-flight. Since the new
    Cognito sub for each user differs from the backed-up one, any key
    embedding a sub (e.g. `users/<sub>/<file-id>` in user-file-uploads)
    has to be rewritten on the way through. We list source objects and
    copy them one by one, applying `remap_subs_in_key` between source
    and target.
    """
    # Resolve target bucket name
    target_bucket = get_ssm_param(ctx.session, ctx.target_prefix, BUCKET_SSM_MAP.get(logical_name, ""))
    if not target_bucket:
        return {"logical": logical_name, "status": "skipped", "reason": "target bucket not found via SSM"}

    # Source path in backup bucket. Same root-prefix slash gotcha as
    # the cognito restore — the manifest stores `root_prefix` without
    # a trailing slash, so concatenation needs an explicit separator.
    root_prefix = ctx.manifest.get("root_prefix", "")
    if root_prefix and not root_prefix.endswith("/"):
        root_prefix = root_prefix + "/"
    source_prefix = component.get("detail", {}).get("s3_prefix", f"s3/{logical_name}/")
    if not source_prefix.endswith("/"):
        source_prefix = source_prefix + "/"
    source_full_prefix = f"{root_prefix}{source_prefix}"
    source_uri = f"s3://{ctx.backup_bucket}/{source_full_prefix}"
    target_uri = f"s3://{target_bucket}/"

    LOG.info(f"[S3] Restoring {logical_name}: {source_uri} → {target_uri}")
    if ctx.dry_run:
        return {"logical": logical_name, "status": "dry-run", "source": source_uri, "target": target_uri}

    s3 = ctx.session.client("s3", config=BOTO_CONFIG)
    paginator = s3.get_paginator("list_objects_v2")

    objects_copied = 0
    keys_remapped = 0

    def _copy_one(source_key: str) -> tuple[bool, bool]:
        """Copy one object, returning (copied, remapped)."""
        if not source_key.startswith(source_full_prefix):
            return (False, False)
        relative_key = source_key[len(source_full_prefix):]
        if not relative_key:
            return (False, False)  # skip the prefix itself if it shows up
        target_key = remap_subs_in_key(relative_key, ctx.sub_map_pattern, ctx.sub_map)
        s3.copy_object(
            Bucket=target_bucket,
            Key=target_key,
            CopySource={"Bucket": ctx.backup_bucket, "Key": source_key},
            MetadataDirective="COPY",
        )
        return (True, target_key != relative_key)

    # Parallelise object copies. ThreadPoolExecutor handles the
    # connection pooling for boto3 s3.copy_object cleanly. 16 workers
    # is a safe default — boto3 default connection pool is 10 per
    # session; we override BOTO_CONFIG.max_pool_connections=20 below.
    keys_to_copy: list[str] = []
    for page in paginator.paginate(Bucket=ctx.backup_bucket, Prefix=source_full_prefix):
        for obj in page.get("Contents", []) or []:
            keys_to_copy.append(obj["Key"])

    if not keys_to_copy:
        LOG.info(f"[S3] {logical_name}: source prefix is empty, nothing to copy")
        return {"logical": logical_name, "status": "ok", "objects_copied": 0, "target_bucket": target_bucket}

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(_copy_one, k) for k in keys_to_copy]
        for f in as_completed(futures):
            try:
                copied, remapped = f.result()
            except ClientError as e:
                LOG.warning(f"[S3] {logical_name}: copy failed for one object: {e}")
                continue
            if copied:
                objects_copied += 1
            if remapped:
                keys_remapped += 1

    LOG.info(
        f"[S3] {logical_name}: copied {objects_copied} objects to {target_bucket}"
        + (f" ({keys_remapped} sub-remapped keys)" if keys_remapped else "")
    )
    return {
        "logical": logical_name,
        "status": "ok",
        "objects_copied": objects_copied,
        "keys_remapped": keys_remapped,
        "target_bucket": target_bucket,
    }


def _compute_created_username(
    original_username: str,
    old_sub: str | None,
    identities: list[dict[str, Any]],
) -> str:
    """Pick a safe username for AdminCreateUser when restoring a Cognito user.

    Cognito auto-provisions a federated user (one that signed in via an
    external IdP) with the **internal-reserved username pattern**
    ``<provider>_<provider_user_id>``. ``list-users`` exports those users
    with that exact ``Username``. Feeding it straight back into
    ``AdminCreateUser`` produces a NATIVE user wearing what Cognito
    treats as a reserved federated-user username. Two things break as
    a consequence:

    1. ``AdminLinkProviderForUser`` refuses the link with the misleading
       error: ``Invalid SourceUser: Cognito users with a username/password
       may not be passed in as a SourceUser, only as a DestinationUser``.
    2. The next IdP login fails with ``User already exists with provider
       user id`` because Cognito tries to auto-create its own internal
       federated-user record with that username and collides with the
       impostor native user we just made.

    Fix: for users with a non-empty ``identities`` array (i.e. federated
    users), mint a deterministic non-federated username. Native users
    (empty identities) keep their original username — that's a
    no-behavior-change path for the common case.

    The chosen scheme — ``migrated-<old-sub>`` — is:

    * **Deterministic** so re-runs hit ``AdminCreateUser``'s
      ``UsernameExistsException`` path and pick up the existing sub
      idempotently.
    * **Collision-free** because ``<old-sub>`` is a UUID generated by
      the source pool; the resulting string can never match Cognito's
      ``<provider>_<provider_user_id>`` reserved pattern.
    * **Debuggable** — ``migrated-`` is visible in the user list and
      makes the migration provenance obvious.

    A SHA-256-based fallback covers the rare case where the backup is
    missing ``sub`` for a federated user; the resulting username is
    still deterministic and collision-free.
    """
    if not identities:
        return original_username
    if old_sub:
        return f"migrated-{old_sub}"
    first = identities[0]
    seed = f"{first.get('providerName', '')}/{first.get('userId', '')}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()[:16]
    return f"migrated-{digest}"


# --------------------------------------------------------------------------- #
# S3 Vectors restore                                                          #
# --------------------------------------------------------------------------- #
def restore_vector_index(ctx: RestoreContext, logical_name: str) -> dict:
    """Replay backed-up vectors into the target S3 Vectors index.

    Reads `vectors/{logical}.jsonl.gz` from the backup bucket — written by
    `scripts/backup-data/backup.py::backup_vector_index` — and pushes each
    record back into the destination index via `s3vectors.put_vectors`.
    Each line of the file is a JSON object with the exact shape
    put_vectors accepts:
        {"key": str, "data": {"float32": [floats]}, "metadata": {...}}

    `put_vectors` is an upsert keyed on `key`, so this function is
    idempotent on re-run: a partially-completed previous restore can be
    resumed by re-invoking the script. We also tolerate the backup file
    being missing (older backups predate the vectors backup feature):
    in that case we return a `skipped` status with a clear reason rather
    than failing the whole restore.

    Batch size matches `bedrock_embeddings.store_embeddings_in_s3`'s
    BATCH_SIZE=50, which is the safe upper bound for the S3 Vectors
    PutVectors request body limit.
    """
    # Find the matching backup-side config to know which SSM paths to look up.
    cfg = next((c for c in VECTOR_INDEXES if c["logical"] == logical_name), None)
    if cfg is None:
        return {"logical": logical_name, "status": "skipped",
                "reason": "no matching VECTOR_INDEXES entry"}

    target_bucket = get_ssm_param(ctx.session, ctx.target_prefix, cfg["bucket_ssm"])
    target_index = get_ssm_param(ctx.session, ctx.target_prefix, cfg["index_ssm"])
    if not target_bucket or not target_index:
        return {"logical": logical_name, "status": "skipped",
                "reason": "target vector bucket/index not found via SSM"}

    root_prefix = ctx.manifest.get("root_prefix", "")
    if root_prefix and not root_prefix.endswith("/"):
        root_prefix = root_prefix + "/"
    backup_key = f"{root_prefix}vectors/{logical_name}.jsonl.gz"

    s3 = ctx.session.client("s3", config=BOTO_CONFIG)
    try:
        obj = s3.get_object(Bucket=ctx.backup_bucket, Key=backup_key)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return {"logical": logical_name, "status": "skipped",
                    "reason": f"no vectors backup file at {backup_key} "
                              "(backup may pre-date vectors support)"}
        return {"logical": logical_name, "status": "failed",
                "error": f"GetObject {backup_key}: {e}"}

    body = gzip.decompress(obj["Body"].read())
    if not body.strip():
        return {"logical": logical_name, "status": "ok",
                "vectors_written": 0, "target_bucket": target_bucket,
                "target_index": target_index,
                "reason": "backup file empty"}

    if ctx.dry_run:
        line_count = sum(1 for line in body.decode("utf-8").splitlines() if line.strip())
        return {"logical": logical_name, "status": "skipped",
                "reason": "dry-run", "vectors_in_backup": line_count}

    s3vectors = ctx.session.client("s3vectors", config=BOTO_CONFIG)
    BATCH_SIZE = 50
    batch: list[dict[str, Any]] = []
    vectors_written = 0
    batches_sent = 0

    def _flush() -> None:
        nonlocal batch, vectors_written, batches_sent
        if not batch:
            return
        s3vectors.put_vectors(
            vectorBucketName=target_bucket,
            indexName=target_index,
            vectors=batch,
        )
        vectors_written += len(batch)
        batches_sent += 1
        batch = []

    for line in body.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError as parse_err:
            LOG.warning(f"[Vectors] {logical_name}: skipping malformed line: {parse_err}")
            continue
        # Defensive shape validation — the line must have key + data + metadata
        # in the put_vectors-compatible form. metadata is permitted to be
        # absent/None on the live API but we backed up returnMetadata=True
        # so it should always be present.
        if "key" not in record or "data" not in record:
            LOG.warning(f"[Vectors] {logical_name}: skipping record missing 'key' or 'data'")
            continue
        batch.append(record)
        if len(batch) >= BATCH_SIZE:
            _flush()

    _flush()

    LOG.info(f"[Vectors] {logical_name}: wrote {vectors_written} vectors "
             f"to {target_bucket}/{target_index} ({batches_sent} batches)")
    return {"logical": logical_name, "status": "ok",
            "vectors_written": vectors_written,
            "batches_sent": batches_sent,
            "target_bucket": target_bucket,
            "target_index": target_index}


# --------------------------------------------------------------------------- #
# Cognito restore                                                              #
# --------------------------------------------------------------------------- #
def restore_cognito(ctx: RestoreContext) -> list[dict]:
    """Restore Cognito identity providers, app clients, and users."""
    results = []
    # Append a trailing '/' so subsequent f-strings concat cleanly.
    # Backup writes `root_prefix` without a trailing slash (e.g.
    # 'ai-sbmt-api/20260521T181146Z'); the previous restore code
    # forgot the separator and ended up looking up keys like
    # 'ai-sbmt-api/20260521T181146Zcognito/users.jsonl.gz' which
    # don't exist — every cognito component reported "no backup file".
    root = ctx.manifest.get("root_prefix", "")
    if root and not root.endswith("/"):
        root = root + "/"
    s3 = ctx.session.client("s3", config=BOTO_CONFIG)

    target_pool_id = get_ssm_param(ctx.session, ctx.target_prefix, SSM_USER_POOL_ID)
    if not target_pool_id:
        results.append({"component": "cognito", "status": "skipped", "reason": "target user pool not found"})
        return results

    cognito = ctx.session.client("cognito-idp", config=BOTO_CONFIG)

    # --- Identity Providers ---
    # Backup writes `cognito/identity-providers.json` as
    #   {"providers": [{...idp1...}, {...idp2...}]}
    # — see scripts/backup-data/backup.py:480. Earlier versions of
    # this restore code iterated the top-level dict directly, which
    # yielded the dict KEYS (strings) and triggered
    # `'str' object has no attribute 'get'`. Read the wrapped list.
    idps: list[dict] = []  # populated below; referenced by app-client wiring
    try:
        idp_obj = s3.get_object(Bucket=ctx.backup_bucket, Key=f"{root}cognito/identity-providers.json")
        idp_blob = json.loads(idp_obj["Body"].read())
        idps = idp_blob.get("providers", []) if isinstance(idp_blob, dict) else idp_blob
        for idp in idps:
            provider_name = idp.get("ProviderName")
            if not provider_name:
                continue
            LOG.info(f"[Cognito] Creating IdP: {provider_name}")
            if not ctx.dry_run:
                try:
                    cognito.create_identity_provider(
                        UserPoolId=target_pool_id,
                        ProviderName=provider_name,
                        ProviderType=idp.get("ProviderType", "OIDC"),
                        ProviderDetails=idp.get("ProviderDetails", {}),
                        AttributeMapping=idp.get("AttributeMapping", {}),
                        IdpIdentifiers=idp.get("IdpIdentifiers", []),
                    )
                except ClientError as e:
                    if e.response["Error"]["Code"] == "DuplicateProviderException":
                        LOG.info(f"[Cognito] IdP {provider_name} already exists, skipping")
                    else:
                        raise
        results.append({"component": "cognito-idps", "status": "ok", "count": len(idps)})
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            results.append({"component": "cognito-idps", "status": "skipped", "reason": "no backup file"})
        else:
            results.append({"component": "cognito-idps", "status": "failed", "error": str(e)})

    # --- App Clients ---
    # Same wrapper-list shape as identity-providers — backup writes
    # {"clients": [...]}. See scripts/backup-data/backup.py:507.
    #
    # The app client is CDK-managed and already exists by the time
    # restore runs. However, CDK creates it BEFORE IdPs are restored,
    # so its SupportedIdentityProviders list only contains "COGNITO".
    # Cognito's hosted UI will not show a login button for any IdP
    # not in that list, producing the "Login option is not available"
    # error immediately after a restore.
    #
    # Fix: after IdPs are created above, call update_user_pool_client
    # for every CDK-managed client to add the restored provider names
    # to SupportedIdentityProviders. We do a describe-first to avoid
    # overwriting any other settings CDK has on the client.
    restored_idp_names: list[str] = [
        idp.get("ProviderName") for idp in idps
        if idp.get("ProviderName")
    ]
    try:
        clients_obj = s3.get_object(Bucket=ctx.backup_bucket, Key=f"{root}cognito/app-clients.json")
        clients_blob = json.loads(clients_obj["Body"].read())
        clients = clients_blob.get("clients", []) if isinstance(clients_blob, dict) else clients_blob
        updated_clients = 0
        for client in clients:
            client_name = client.get("ClientName")
            LOG.info(f"[Cognito] Noting app client: {client_name} (CDK manages creation; re-wiring IdP providers)")
            if not restored_idp_names or ctx.dry_run:
                continue
            # Find the live CDK-created client by name.
            live_client_id = None
            paginator = cognito.get_paginator("list_user_pool_clients")
            for page in paginator.paginate(UserPoolId=target_pool_id, MaxResults=60):
                for c in page.get("UserPoolClients", []):
                    if c["ClientName"] == client_name:
                        live_client_id = c["ClientId"]
                        break
                if live_client_id:
                    break
            if not live_client_id:
                LOG.warning(f"[Cognito] App client '{client_name}' not found in target pool — skipping IdP re-wire")
                continue
            # Describe the live client so we can patch SupportedIdentityProviders
            # without clobbering any other CDK-managed settings.
            live = cognito.describe_user_pool_client(
                UserPoolId=target_pool_id, ClientId=live_client_id
            )["UserPoolClient"]
            current_idps: list[str] = live.get("SupportedIdentityProviders", [])
            missing = [n for n in restored_idp_names if n not in current_idps]
            if not missing:
                LOG.info(f"[Cognito] App client '{client_name}' already has all restored IdPs — skipping")
                continue
            merged = list(current_idps) + missing
            LOG.info(f"[Cognito] Updating app client '{client_name}': adding IdPs {missing}")
            # update_user_pool_client requires re-sending the full set of
            # mutable fields; omitting optional fields that are absent on
            # the live client to avoid sending empty lists/dicts that
            # override CDK's configured values.
            update_kwargs: dict[str, Any] = {
                "UserPoolId": target_pool_id,
                "ClientId": live_client_id,
                "SupportedIdentityProviders": merged,
            }
            for field in (
                "RefreshTokenValidity", "AccessTokenValidity", "IdTokenValidity",
                "TokenValidityUnits", "ReadAttributes", "WriteAttributes",
                "ExplicitAuthFlows", "CallbackURLs", "LogoutURLs",
                "AllowedOAuthFlows", "AllowedOAuthScopes",
                "AllowedOAuthFlowsUserPoolClient", "AnalyticsConfiguration",
                "PreventUserExistenceErrors", "EnableTokenRevocation",
                "EnablePropagateAdditionalUserContextData", "AuthSessionValidity",
            ):
                val = live.get(field)
                if val is not None:
                    update_kwargs[field] = val
            cognito.update_user_pool_client(**update_kwargs)
            updated_clients += 1
        results.append({"component": "cognito-clients", "status": "ok", "count": len(clients),
                       "updated_idp_wiring": updated_clients})
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            results.append({"component": "cognito-clients", "status": "skipped"})

    # --- Users ---
    # This is the load-bearing section for the cross-pool migration.
    # Cognito does NOT permit setting `sub` on user creation (the
    # field is auto-generated and immutable), so when a user pool is
    # destroyed and recreated each user gets a fresh sub. Every
    # USER#<sub> partition in DynamoDB and every <sub>-keyed S3 path
    # would otherwise be orphaned. To keep the data consistent we:
    #
    #   1. Read each backed-up user, extract their OLD sub from the
    #      attributes captured by list-users.
    #   2. AdminCreateUser with the same Username and the sanitised
    #      attribute set (immutable Cognito-managed names dropped).
    #   3. Pull the NEW sub from the AdminCreateUser response (or
    #      AdminGetUser if the user already exists, for re-runs).
    #   4. For any federated identity recorded in the backup
    #      (`identities` JSON), call AdminLinkProviderForUser so the
    #      next IdP login resolves to this user (otherwise Cognito
    #      would create a SECOND user with yet another sub).
    #   5. Record (old_sub → new_sub) in ctx.sub_map. Subsequent DDB
    #      and S3 restore passes apply this mapping to every string
    #      they write.
    if ctx.skip_cognito_users:
        results.append({"component": "cognito-users", "status": "skipped", "reason": "--skip-cognito-users"})
        # Don't early-return — groups/memberships still need to run
        # below in case the operator wants those without users.
        # Compile an empty pattern (None) so downstream remap is a no-op.
        ctx.sub_map_pattern = compile_sub_pattern(ctx.sub_map)
    else:
        try:
            users_obj = s3.get_object(Bucket=ctx.backup_bucket, Key=f"{root}cognito/users.jsonl.gz")
            body = gzip.decompress(users_obj["Body"].read()).decode("utf-8")
            user_count = 0
            users_remapped = 0
            users_linked = 0
            users_failed: list[str] = []
            for line in body.strip().split("\n"):
                if not line.strip():
                    continue
                user = json.loads(line)
                username = user.get("Username")
                if not username:
                    continue

                # Pull old sub + identities BEFORE we strip them.
                source_attrs = user.get("Attributes", []) or []
                attr_by_name = {a["Name"]: a["Value"] for a in source_attrs if a.get("Value")}
                old_sub = attr_by_name.get("sub")
                identities_blob = attr_by_name.get("identities", "")
                identities: list[dict[str, Any]] = []
                if identities_blob:
                    try:
                        identities = json.loads(identities_blob)
                    except (TypeError, ValueError):
                        identities = []

                # Pick a safe username for AdminCreateUser. For native
                # users (no identities) this is the original username.
                # For federated users we mint `migrated-<old-sub>` so
                # the new native user does not wear Cognito's reserved
                # `<provider>_<provider_user_id>` pattern — see the
                # docstring on _compute_created_username for why.
                created_username = _compute_created_username(
                    original_username=username,
                    old_sub=old_sub,
                    identities=identities,
                )

                if ctx.dry_run:
                    user_count += 1
                    continue

                # Sanitised attribute set — Cognito-managed names
                # rejected by AdminCreateUser get dropped here.
                COGNITO_IMMUTABLE_ATTRS = {
                    "sub",
                    "cognito:user_status",
                    "cognito:mfa_enabled",
                    "identities",
                }
                attrs = [
                    {"Name": a["Name"], "Value": a["Value"]}
                    for a in source_attrs
                    if a.get("Value") and a.get("Name") not in COGNITO_IMMUTABLE_ATTRS
                ]

                new_sub: str | None = None
                user_was_created = False
                try:
                    create_resp = cognito.admin_create_user(
                        UserPoolId=target_pool_id,
                        Username=created_username,
                        UserAttributes=attrs,
                        MessageAction="SUPPRESS",
                    )
                    user_count += 1
                    user_was_created = True
                    new_sub = next(
                        (a["Value"] for a in create_resp["User"]["Attributes"] if a["Name"] == "sub"),
                        None,
                    )
                except ClientError as e:
                    code = e.response["Error"]["Code"]
                    if code == "UsernameExistsException":
                        # Idempotent re-run: pick up the existing sub.
                        try:
                            get_resp = cognito.admin_get_user(
                                UserPoolId=target_pool_id, Username=created_username
                            )
                            new_sub = next(
                                (a["Value"] for a in get_resp["UserAttributes"] if a["Name"] == "sub"),
                                None,
                            )
                            user_count += 1
                        except ClientError as ge:
                            LOG.warning(
                                f"[Cognito] Failed to re-fetch existing user "
                                f"{created_username} (orig={username!r}): {ge}"
                            )
                            users_failed.append(created_username)
                            continue
                    else:
                        LOG.warning(
                            f"[Cognito] Failed to create user "
                            f"{created_username} (orig={username!r}): {e}"
                        )
                        users_failed.append(created_username)
                        continue

                # Flip the user from FORCE_CHANGE_PASSWORD → CONFIRMED.
                #
                # AdminCreateUser with MessageAction=SUPPRESS leaves the
                # user in FORCE_CHANGE_PASSWORD state, which silently
                # breaks the hosted UI's "Forgot password" flow: Cognito
                # shows "code sent" but never delivers a code because
                # there's nothing to reset on a user that hasn't yet
                # completed initial setup. The standard cross-pool
                # migration pattern is to set a random throwaway
                # password as Permanent=True, which transitions the
                # user to CONFIRMED. The password is never
                # communicated to anyone — the user does ForgotPassword
                # on first login and chooses a real one. The throwaway
                # is sized to comfortably exceed the pool's password
                # policy regardless of customisation.
                #
                # Done unconditionally on every loop iteration (not just
                # user_was_created) so re-runs against a pool where the
                # users are still FORCE_CHANGE_PASSWORD also get fixed.
                # AdminSetUserPassword is idempotent — calling it on a
                # CONFIRMED user just resets to the new password, so
                # re-runs are safe.
                throwaway_password = (
                    "Aa1!" + "".join(
                        secrets.choice(string.ascii_letters + string.digits + "!@#$%^&*")
                        for _ in range(28)
                    )
                )
                try:
                    cognito.admin_set_user_password(
                        UserPoolId=target_pool_id,
                        Username=created_username,
                        Password=throwaway_password,
                        Permanent=True,
                    )
                except ClientError as pe:
                    LOG.warning(
                        f"[Cognito] {created_username}: created but could not transition to "
                        f"CONFIRMED via admin_set_user_password: {pe}. ForgotPassword "
                        f"may not work for this user until manually fixed."
                    )

                # Record the sub mapping. If old_sub or new_sub
                # is missing, we can't remap downstream data for
                # this user — log and continue, but flag it.
                if old_sub and new_sub:
                    ctx.sub_map[old_sub] = new_sub
                    users_remapped += 1
                else:
                    LOG.warning(
                        f"[Cognito] {created_username} (orig={username!r}): missing sub mapping "
                        f"(old={old_sub!r}, new={new_sub!r}); user data will not be remapped"
                    )

                # Link any federated identities so the next IdP login
                # resolves to this user (and not a duplicate auto-
                # provisioned one). The DestinationUser must be the
                # native user we just created (created_username), NOT
                # the original federated-pattern username from the
                # backup — see _compute_created_username for the why.
                for identity in identities:
                    provider_name = identity.get("providerName")
                    idp_user_id = identity.get("userId")
                    if not provider_name or not idp_user_id:
                        continue
                    try:
                        cognito.admin_link_provider_for_user(
                            UserPoolId=target_pool_id,
                            DestinationUser={
                                "ProviderName": "Cognito",
                                "ProviderAttributeValue": created_username,
                            },
                            SourceUser={
                                "ProviderName": provider_name,
                                "ProviderAttributeName": "Cognito_Subject",
                                "ProviderAttributeValue": idp_user_id,
                            },
                        )
                        users_linked += 1
                    except ClientError as le:
                        # Already-linked is fine for re-runs.
                        if le.response["Error"]["Code"] in (
                            "InvalidParameterException",  # AWS uses this for "already linked"
                            "DuplicateProviderException",
                        ) and "already linked" in str(le).lower():
                            continue
                        LOG.warning(
                            f"[Cognito] Failed to link {provider_name} identity "
                            f"({idp_user_id}) to {created_username}: {le}"
                        )

            # Compile the regex once after the full mapping is built.
            # Subsequent DDB/S3 passes consult ctx.sub_map_pattern.
            ctx.sub_map_pattern = compile_sub_pattern(ctx.sub_map)

            # Persist the mapping as an audit artifact in the source
            # backup bucket. Operators can use this to verify the
            # remap and to debug any orphaned data after the fact.
            try:
                mapping_audit = {
                    "target_prefix": ctx.target_prefix,
                    "user_pool_id": target_pool_id,
                    "old_to_new": ctx.sub_map,
                    "users_failed": users_failed,
                    "identities_linked": users_linked,
                }
                s3.put_object(
                    Bucket=ctx.backup_bucket,
                    Key=f"{root}cognito/sub-mapping-{ctx.target_prefix}.json",
                    Body=json.dumps(mapping_audit, indent=2).encode("utf-8"),
                    ContentType="application/json",
                )
            except ClientError as ae:
                LOG.warning(f"[Cognito] Failed to persist sub-mapping audit: {ae}")

            user_result = {
                "component": "cognito-users",
                "status": "ok",
                "count": user_count,
                "subs_remapped": users_remapped,
                "identities_linked": users_linked,
            }
            if users_failed:
                user_result["failed"] = users_failed
            results.append(user_result)
        except ClientError:
            results.append({"component": "cognito-users", "status": "skipped", "reason": "no users backup file"})
            # No users → empty mapping, but still set a None pattern
            # so downstream remap calls short-circuit.
            ctx.sub_map_pattern = compile_sub_pattern(ctx.sub_map)

    # --- Groups + Memberships ---
    try:
        groups_obj = s3.get_object(Bucket=ctx.backup_bucket, Key=f"{root}cognito/groups.jsonl.gz")
        body = gzip.decompress(groups_obj["Body"].read()).decode("utf-8")
        group_count = 0
        for line in body.strip().split("\n"):
            if not line.strip():
                continue
            group = json.loads(line)
            group_name = group.get("GroupName")
            if not group_name or ctx.dry_run:
                group_count += 1
                continue
            try:
                cognito.create_group(
                    UserPoolId=target_pool_id,
                    GroupName=group_name,
                    Description=group.get("Description", ""),
                )
                group_count += 1
            except ClientError as e:
                if e.response["Error"]["Code"] == "GroupExistsException":
                    group_count += 1
                else:
                    LOG.warning(f"[Cognito] Failed to create group {group_name}: {e}")
        results.append({"component": "cognito-groups", "status": "ok", "count": group_count})
    except ClientError:
        results.append({"component": "cognito-groups", "status": "skipped"})

    try:
        memberships_obj = s3.get_object(Bucket=ctx.backup_bucket, Key=f"{root}cognito/group-memberships.jsonl.gz")
        body = gzip.decompress(memberships_obj["Body"].read()).decode("utf-8")
        membership_count = 0
        for line in body.strip().split("\n"):
            if not line.strip():
                continue
            m = json.loads(line)
            if ctx.dry_run:
                membership_count += 1
                continue
            try:
                cognito.admin_add_user_to_group(
                    UserPoolId=target_pool_id,
                    Username=m["Username"],
                    GroupName=m["GroupName"],
                )
                membership_count += 1
            except ClientError as e:
                LOG.warning(f"[Cognito] Failed to add {m.get('Username')} to {m.get('GroupName')}: {e}")
        results.append({"component": "cognito-memberships", "status": "ok", "count": membership_count})
    except ClientError:
        results.append({"component": "cognito-memberships", "status": "skipped"})

    return results


# --------------------------------------------------------------------------- #
# AgentCore Memory replay                                                      #
#                                                                              #
# The data plane CreateEvent API accepts an explicit `eventTimestamp`, an     #
# `actorId`, a `sessionId`, a `payload`, optional `metadata`, an optional     #
# `branch` (with a `rootEventId` reference to a previously-created event),    #
# and a `clientToken` for idempotent retries — see                            #
# https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_CreateEvent.html
#                                                                              #
# That gives us everything needed for a faithful replay:                      #
#                                                                              #
#   • actorId — typically the user's Cognito sub. Remap via ctx.sub_map so    #
#     events land on the right (new) user identity.                           #
#   • sessionId — preserved verbatim; the app's chat-session GSI in DDB       #
#     references these IDs, so cross-table consistency is maintained.         #
#   • eventTimestamp — preserved (parsed from the ISO string the backup       #
#     wrote via _scrub_datetimes).                                            #
#   • payload + metadata — preserved verbatim.                                #
#   • branch.rootEventId — references a NEW event_id that AWS assigns on      #
#     CreateEvent. We maintain a session-local old_event_id → new_event_id    #
#     map so branch refs are rewritten as we go.                              #
#   • clientToken — deterministic hash of (target_memory_id, original_event_id)
#     so re-runs hit AWS's idempotency window and don't double-write.          #
#                                                                              #
# Strategy: replay events sequentially within a single session (so branch     #
# ordering is preserved) and parallel across sessions (CreateEvent is          #
# rate-limited but not strictly per-resource). The default 16-thread pool     #
# is the same as restore_s3_bucket.                                           #
#                                                                              #
# CreateEvent on a memory with extraction strategies configured triggers      #
# semantic-fact extraction synchronously, which means MemoryRecords          #
# (long-term semantic memories, summaries, preferences) rebuild themselves    #
# automatically as events are replayed. We do NOT separately replay           #
# MemoryRecords — that would double-write and contradict the strategy.        #
# --------------------------------------------------------------------------- #
def _parse_event_timestamp(ts: Any) -> datetime:
    """Parse the eventTimestamp shape the backup wrote.

    backup.py runs `_scrub_datetimes` on the boto3 response which converts
    `datetime` → ISO 8601 UTC string. We reverse that here. If the backup
    ever switches to a numeric epoch (or the AWS SDK changes), this
    function is the single place to update.
    """
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts))
    if isinstance(ts, str):
        # datetime.fromisoformat accepts the +00:00 form _scrub_datetimes emits.
        return datetime.fromisoformat(ts)
    raise ValueError(f"Unparseable eventTimestamp: {ts!r}")


def _client_token(target_memory_id: str, original_event_id: str) -> str:
    """Deterministic 64-char idempotency key per (target_memory, source_event).

    AWS's CreateEvent treats matching client tokens within a 24h window as
    the same request and returns the prior result. Re-runs of the restore
    therefore no-op cleanly without double-writing.
    """
    digest = hashlib.sha256(
        f"{target_memory_id}|{original_event_id}".encode("utf-8")
    ).hexdigest()
    return digest[:64]


def restore_agentcore_memory(ctx: RestoreContext) -> list[dict]:
    """Replay AgentCore Memory events from the backup."""
    results: list[dict[str, Any]] = []

    if ctx.skip_memory_replay:
        results.append({
            "component": "agentcore-memory",
            "status": "skipped",
            "reason": "--skip-memory-replay",
        })
        return results

    target_memory_id = get_ssm_param(ctx.session, ctx.target_prefix, SSM_MEMORY_ID)
    if not target_memory_id:
        results.append({
            "component": "agentcore-memory",
            "status": "skipped",
            "reason": "target memory id not found via SSM",
        })
        return results

    root = ctx.manifest.get("root_prefix", "")
    if root and not root.endswith("/"):
        root = root + "/"

    s3 = ctx.session.client("s3", config=BOTO_CONFIG)

    # Read events.jsonl.gz. Absence is fine — a backup against a memory
    # with no traffic emits an empty file, or the file may be missing
    # entirely if the backup was taken before memory was provisioned.
    try:
        events_obj = s3.get_object(
            Bucket=ctx.backup_bucket,
            Key=f"{root}agentcore-memory/events.jsonl.gz",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            results.append({
                "component": "agentcore-memory",
                "status": "skipped",
                "reason": "no events backup file",
            })
            return results
        results.append({
            "component": "agentcore-memory",
            "status": "failed",
            "error": str(e),
        })
        return results

    body = gzip.decompress(events_obj["Body"].read()).decode("utf-8")

    # Group by sessionId so each session can be replayed sequentially while
    # different sessions run in parallel. Within a session we sort by
    # eventTimestamp so branch references are always created after their
    # parent.
    sessions: dict[str, list[dict]] = {}
    total_events = 0
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = rec.get("event") or {}
        if not ev.get("sessionId"):
            # Sessionless events can't be reliably replayed.
            continue
        sessions.setdefault(ev["sessionId"], []).append(ev)
        total_events += 1

    if not sessions:
        results.append({
            "component": "agentcore-memory",
            "status": "ok",
            "events_replayed": 0,
            "note": "events file present but contained zero sessionful events",
        })
        return results

    LOG.info(
        f"[Memory] Replaying {total_events} events across {len(sessions)} sessions "
        f"into target memory {target_memory_id}"
    )

    if ctx.dry_run:
        results.append({
            "component": "agentcore-memory",
            "status": "dry-run",
            "events_to_replay": total_events,
            "sessions_to_replay": len(sessions),
            "target_memory_id": target_memory_id,
        })
        return results

    dp = ctx.session.client("bedrock-agentcore", config=BOTO_CONFIG)

    # Aggregated counters protected by a per-call lock — Python's GIL
    # technically makes the int += atomic, but explicit accumulation per
    # session avoids the contention path entirely.
    def _replay_one_session(session_id: str, events: list[dict]) -> dict[str, int]:
        local_counts = {
            "replayed": 0,
            "actor_remapped": 0,
            "branch_rewritten": 0,
            "failed": 0,
        }
        # Sort events by timestamp so a branch-child never tries to
        # reference a parent we haven't replayed yet.
        events_sorted = sorted(
            events,
            key=lambda e: e.get("eventTimestamp") or "",
        )
        old_to_new_event_id: dict[str, str] = {}

        for ev in events_sorted:
            original_event_id = ev.get("eventId")
            actor_id = ev.get("actorId")
            payload = ev.get("payload") or []
            metadata = ev.get("metadata") or {}
            ev_branch = ev.get("branch") or {}

            # actorId remap — fall back to original if not in sub_map
            # (e.g., system-actor IDs that aren't user subs).
            if ctx.sub_map_pattern is not None and actor_id in ctx.sub_map:
                new_actor_id = ctx.sub_map[actor_id]
                local_counts["actor_remapped"] += 1
            else:
                new_actor_id = actor_id

            # branch.rootEventId rewrite — only if the parent was
            # replayed earlier in this session.
            new_branch: dict[str, Any] | None = None
            if ev_branch:
                br_name = ev_branch.get("name")
                br_root = ev_branch.get("rootEventId")
                if br_root and br_root in old_to_new_event_id:
                    new_branch = {
                        "name": br_name,
                        "rootEventId": old_to_new_event_id[br_root],
                    }
                    local_counts["branch_rewritten"] += 1
                elif br_root:
                    # Parent unknown — drop the branch field rather
                    # than fail the event. The event becomes a root
                    # in the new memory.
                    LOG.warning(
                        f"[Memory] {session_id}: branch parent {br_root} not "
                        f"found in replay session; dropping branch on event "
                        f"{original_event_id}"
                    )
                    new_branch = None
                else:
                    new_branch = {"name": br_name} if br_name else None

            try:
                event_timestamp = _parse_event_timestamp(ev.get("eventTimestamp"))
            except ValueError as ve:
                LOG.warning(
                    f"[Memory] {session_id}: skipping event {original_event_id}: {ve}"
                )
                local_counts["failed"] += 1
                continue

            kwargs: dict[str, Any] = {
                "memoryId": target_memory_id,
                "actorId": new_actor_id,
                "sessionId": session_id,
                "eventTimestamp": event_timestamp,
                "payload": payload,
                "clientToken": _client_token(target_memory_id, original_event_id or ""),
            }
            if metadata:
                kwargs["metadata"] = metadata
            if new_branch is not None:
                kwargs["branch"] = new_branch

            try:
                resp = dp.create_event(**kwargs)
                local_counts["replayed"] += 1
                new_id = (resp.get("event") or {}).get("eventId")
                if original_event_id and new_id:
                    old_to_new_event_id[original_event_id] = new_id
            except ClientError as ce:
                LOG.warning(
                    f"[Memory] {session_id}: create_event failed for "
                    f"{original_event_id}: {ce}"
                )
                local_counts["failed"] += 1

        return local_counts

    totals = {
        "replayed": 0,
        "actor_remapped": 0,
        "branch_rewritten": 0,
        "failed": 0,
    }
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [
            pool.submit(_replay_one_session, sid, evs)
            for sid, evs in sessions.items()
        ]
        for f in as_completed(futures):
            try:
                counts = f.result()
            except Exception as exc:
                LOG.warning(f"[Memory] session worker raised: {exc}")
                continue
            for k, v in counts.items():
                totals[k] += v

    LOG.info(
        f"[Memory] Replay complete: {totals['replayed']}/{total_events} events written, "
        f"{totals['actor_remapped']} actor-remapped, "
        f"{totals['branch_rewritten']} branch refs rewritten, "
        f"{totals['failed']} failed"
    )

    results.append({
        "component": "agentcore-memory",
        "status": "ok",
        "target_memory_id": target_memory_id,
        "sessions_replayed": len(sessions),
        "events_replayed": totals["replayed"],
        "events_actor_remapped": totals["actor_remapped"],
        "events_branch_rewritten": totals["branch_rewritten"],
        "events_failed": totals["failed"],
    })
    return results


# --------------------------------------------------------------------------- #
# Main orchestration                                                           #
# --------------------------------------------------------------------------- #
def run_restore(ctx: RestoreContext) -> dict:
    """Execute the full restore pipeline.

    Order matters: Cognito MUST run before DynamoDB and S3 so the
    `ctx.sub_map` (old_sub → new_sub for every recreated user) is
    populated before any data is rewritten. The downstream DDB and
    S3 passes consult `ctx.sub_map_pattern` for inline string
    substitution; if Cognito ran AFTER, every user partition would
    land with a dead old-pool sub.
    """
    s3 = ctx.session.client("s3", config=BOTO_CONFIG)

    # Load manifest
    LOG.info(f"Loading manifest from s3://{ctx.backup_bucket}/{ctx.manifest_key}")
    manifest_obj = s3.get_object(Bucket=ctx.backup_bucket, Key=ctx.manifest_key)
    ctx.manifest = json.loads(manifest_obj["Body"].read())

    components = ctx.manifest.get("components", {})
    root_prefix = ctx.manifest.get("root_prefix", "")

    # --- Cognito (first — populates ctx.sub_map) ---
    cognito_components = components.get("cognito", [])
    if cognito_components:
        LOG.info("Restoring Cognito (must run before DDB + S3 to build sub mapping)...")
        cognito_results = restore_cognito(ctx)
        ctx.results.extend(cognito_results)
    else:
        LOG.info("No Cognito backup found, skipping")
        # Even with no Cognito, ensure pattern is initialised so
        # downstream passes don't error on a missing attribute.
        ctx.sub_map_pattern = compile_sub_pattern(ctx.sub_map)
    LOG.info(f"Cognito sub_map size: {len(ctx.sub_map)} (will be applied to DDB items + S3 keys)")

    # --- DynamoDB ---
    ddb_components = components.get("dynamodb", [])
    LOG.info(f"Restoring {len(ddb_components)} DynamoDB tables...")
    for comp in ddb_components:
        if comp.get("status") != "ok":
            ctx.results.append({"logical": comp["logical_name"], "status": "skipped", "reason": "backup status was not ok"})
            continue
        result = restore_dynamodb_table(ctx, comp["logical_name"], comp)
        ctx.results.append(result)

    # --- S3 ---
    s3_components = components.get("s3", [])
    LOG.info(f"Restoring {len(s3_components)} S3 buckets...")
    for comp in s3_components:
        if comp.get("status") != "ok":
            ctx.results.append({"logical": comp["logical_name"], "status": "skipped", "reason": "backup status was not ok"})
            continue
        result = restore_s3_bucket(ctx, comp["logical_name"], comp)
        ctx.results.append(result)

    # --- S3 Vectors ---
    # Runs after the S3 documents bucket so the on-disk originals exist
    # before the assistants RAG knowledge base goes live. Vectors are an
    # entirely separate AWS service from regular S3, hence its own step.
    # The backup may pre-date vectors support (older snapshots before
    # PR #N), in which case each entry skips cleanly with a clear reason.
    vector_components = components.get("vectors", [])
    if vector_components:
        LOG.info(f"Restoring {len(vector_components)} S3 Vectors index(es)...")
        for comp in vector_components:
            if comp.get("status") != "ok":
                ctx.results.append({"logical": comp["logical_name"], "status": "skipped",
                                    "reason": "backup status was not ok"})
                continue
            result = restore_vector_index(ctx, comp["logical_name"])
            ctx.results.append(result)
    else:
        # Older backup with no vectors component at all. Try the
        # configured indexes anyway — restore_vector_index() will skip
        # cleanly if the corresponding backup file isn't present.
        LOG.info("No vectors component in manifest; probing configured indexes "
                 "(harmless skip if backup pre-dates vectors support)")
        for cfg in VECTOR_INDEXES:
            ctx.results.append(restore_vector_index(ctx, cfg["logical"]))

    # --- AgentCore Memory ---
    # Runs after Cognito (so sub_map is populated for actorId remap) AND
    # after DDB (so chat-session metadata exists in DDB; if memory events
    # reference a session that was restored to DDB, both ends now agree).
    memory_components = components.get("agentcore-memory", [])
    if memory_components and any(c.get("status") == "ok" for c in memory_components):
        LOG.info("Replaying AgentCore Memory events (uses sub_map from Cognito)...")
        memory_results = restore_agentcore_memory(ctx)
        ctx.results.extend(memory_results)
    else:
        LOG.info("No AgentCore Memory backup found, skipping replay")

    # --- Summary ---
    ok = sum(1 for r in ctx.results if r.get("status") == "ok")
    skipped = sum(1 for r in ctx.results if r.get("status") in ("skipped", "dry-run"))
    failed = sum(1 for r in ctx.results if r.get("status") == "failed")

    summary = {
        "backup_bucket": ctx.backup_bucket,
        "target_prefix": ctx.target_prefix,
        "total": len(ctx.results),
        "ok": ok,
        "skipped": skipped,
        "failed": failed,
        "dry_run": ctx.dry_run,
        "results": ctx.results,
    }

    LOG.info(f"Restore complete: {ok} ok, {skipped} skipped, {failed} failed")
    return summary


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Restore AgentCore data from a backup")
    parser.add_argument("--backup-bucket", required=True, help="S3 bucket containing the backup")
    parser.add_argument("--manifest-key", required=True, help="S3 key of manifest.json in the backup bucket")
    parser.add_argument("--target-prefix", required=True, help="CDK project prefix of the target environment")
    parser.add_argument("--region", required=True, help="AWS region of the target environment")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be restored without writing")
    parser.add_argument("--skip-cognito-users", action="store_true",
                       help="Skip Cognito user import (useful if users will self-register)")
    parser.add_argument("--skip-memory-replay", action="store_true",
                       help="Skip AgentCore Memory event replay. Replay is sequential per-session "
                            "and triggers strategy extraction (Bedrock model invocations) on the "
                            "target memory, which can be slow + costly for large histories. "
                            "Set this if you want to defer replay or rely on fresh memory.")
    parser.add_argument("--profile", help="AWS profile name")

    args = parser.parse_args()

    session = boto3.Session(
        region_name=args.region,
        profile_name=args.profile,
    )

    ctx = RestoreContext(
        backup_bucket=args.backup_bucket,
        manifest_key=args.manifest_key,
        target_prefix=args.target_prefix,
        region=args.region,
        session=session,
        dry_run=args.dry_run,
        skip_cognito_users=args.skip_cognito_users,
        skip_memory_replay=args.skip_memory_replay,
    )

    summary = run_restore(ctx)

    # Write summary to stdout as JSON
    print(json.dumps(summary, indent=2, default=str))

    if summary["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
