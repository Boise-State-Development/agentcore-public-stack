"""Pre-migration backup tool for AgentCore Public Stack.

Discovers all application data sources for a given CDK_PROJECT_PREFIX via SSM
Parameter Store, creates a dedicated S3 backup bucket, and dumps:

* All DynamoDB tables (via ExportTableToPointInTime — portable DynamoDB-JSON,
  NOT AWS-Backup snapshots, so the restore step can transform freely into the
  new schema).
* All S3 user-content buckets (raw object copy via `aws s3 sync`).
* The full Cognito User Pool: pool config, identity providers (with OIDC
  client_secret preserved so future IdP re-registration can be automated),
  app clients (with ClientSecret preserved), resource servers, domain, UI
  customization, users, groups, group memberships.
* AgentCore Memory: best-effort enumeration of strategies + per-actor events.

Every run writes to a fresh bucket: {project_prefix}-backup-{utc_timestamp}.
A single `manifest.json` at the root describes everything and records per-
component pass/fail + counts. Intended to be invoked from the
`.github/workflows/backup-data.yml` workflow but is fully runnable locally
with valid AWS credentials.

Restore is intentionally out of scope — see scripts/backup-data/README.md.
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import boto3
import botocore
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

LOG = logging.getLogger("backup")

BOTO_CONFIG = BotoConfig(
    retries={"max_attempts": 10, "mode": "adaptive"},
    user_agent_extra="agentcore-backup/1.0",
)

# --------------------------------------------------------------------------- #
# SSM-discoverable resources.                                                 #
# `ssm` is the parameter path under `/{prefix}/...`; `logical` is the stable  #
# name used in the manifest + on-disk layout. Optional resources are skipped  #
# without error when the SSM parameter is missing.                            #
# --------------------------------------------------------------------------- #
DYNAMODB_TABLES: list[dict[str, Any]] = [
    {"logical": "users",                "ssm": "/users/users-table-name"},
    {"logical": "app-roles",            "ssm": "/rbac/app-roles-table-name"},
    {"logical": "api-keys",             "ssm": "/auth/api-keys-table-name"},
    {"logical": "auth-providers",       "ssm": "/auth/auth-providers-table-name"},
    {"logical": "oauth-providers",      "ssm": "/oauth/providers-table-name"},
    {"logical": "oauth-user-tokens",    "ssm": "/oauth/user-tokens-table-name"},
    {"logical": "user-quotas",          "ssm": "/quota/user-quotas-table-name"},
    {"logical": "quota-events",         "ssm": "/quota/quota-events-table-name"},
    {"logical": "sessions-metadata",    "ssm": "/cost-tracking/sessions-metadata-table-name"},
    {"logical": "user-cost-summary",    "ssm": "/cost-tracking/user-cost-summary-table-name"},
    {"logical": "system-cost-rollup",   "ssm": "/cost-tracking/system-cost-rollup-table-name"},
    {"logical": "managed-models",       "ssm": "/admin/managed-models-table-name"},
    {"logical": "user-menu-links",      "ssm": "/admin/user-menu-links-table-name"},
    {"logical": "user-settings",        "ssm": "/settings/user-settings-table-name"},
    {"logical": "user-file-uploads",    "ssm": "/user-file-uploads/table-name"},
    {"logical": "shared-conversations", "ssm": "/shares/shared-conversations-table-name"},
    {"logical": "rag-assistants",       "ssm": "/rag/assistants-table-name"},
    {"logical": "artifacts",            "ssm": "/artifacts/table-name",                 "optional": True},
    {"logical": "fine-tuning-jobs",     "ssm": "/fine-tuning/jobs-table-name",          "optional": True},
    {"logical": "fine-tuning-access",   "ssm": "/fine-tuning/access-table-name",        "optional": True},
]

# DYNAMODB_TABLES_BY_CONVENTION used to include the standalone `assistants`
# table from the pre-refactor architecture. That table was decommissioned in
# commit c977e04e — the python app uses the rag-assistants table for both
# assistant config and document metadata via DYNAMODB_ASSISTANTS_TABLE_NAME.
# Empty for now; convention-named tables that show up later go here.
DYNAMODB_TABLES_BY_CONVENTION: list[dict[str, str]] = []

# Ephemeral / TTL-driven tables. Excluded by default; include with --include-ephemeral.
DYNAMODB_TABLES_EPHEMERAL: list[dict[str, str]] = [
    {"logical": "bff-sessions",         "suffix": "bff-sessions"},
    {"logical": "oidc-state",           "suffix": "oidc-state"},
    {"logical": "voice-ticket-replay",  "suffix": "voice-ticket-replay"},
]

S3_BUCKETS: list[dict[str, Any]] = [
    {"logical": "user-file-uploads",    "ssm": "/user-file-uploads/bucket-name"},
    {"logical": "rag-documents",        "ssm": "/rag/documents-bucket-name"},
    {"logical": "artifacts",            "ssm": "/artifacts/bucket-name",                "optional": True},
    {"logical": "fine-tuning-data",     "ssm": "/fine-tuning/data-bucket-name",         "optional": True},
]

# S3 Vectors indexes. Distinct from S3_BUCKETS because S3 Vectors is a
# separate AWS service (`AWS::S3Vectors::*` / boto3 `s3vectors` client) —
# `aws s3 sync` cannot reach the vectors and `list_objects_v2` won't see
# them. Each entry is backed up via `s3vectors.list_vectors` paginated
# enumeration and replayed on restore via `s3vectors.put_vectors`.
#
# The vector index backs the assistants RAG knowledge base:
# `bedrock_embeddings.search_assistant_knowledgebase` issues
# `query_vectors(filter={"assistant_id": ...})` against this index, so an
# empty index post-restore = silently broken RAG even though the DDB
# document metadata and S3 originals are intact. See
# `tests/supply_chain/test_backup_coverage.py::TestBackupCoversVectorIndexes`
# for the canary that enforces this list stays in sync with CDK.
VECTOR_INDEXES: list[dict[str, Any]] = [
    {"logical": "rag-vectors",
     "bucket_ssm": "/rag/vector-bucket-name",
     "index_ssm":  "/rag/vector-index-name"},
]

SSM_USER_POOL_ID = "/auth/cognito/user-pool-id"
SSM_MEMORY_ID = "/inference-api/memory-id"


# --------------------------------------------------------------------------- #
# Data classes for the manifest.                                              #
# --------------------------------------------------------------------------- #
@dataclass
class ComponentResult:
    """One row in the manifest. status is 'ok' | 'skipped' | 'failed'."""
    component: str
    logical_name: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class BackupContext:
    project_prefix: str
    region: str
    timestamp: str               # UTC, e.g. 20260120T173042Z
    bucket: str                  # destination bucket
    root_prefix: str             # bucket key prefix, e.g. {prefix}/{ts}
    account_id: str
    include_ephemeral: bool
    dry_run: bool
    allow_partial: bool
    session: boto3.Session
    results: list[ComponentResult] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Discovery                                                                   #
# --------------------------------------------------------------------------- #
def get_ssm_param(session: boto3.Session, name: str) -> str | None:
    ssm = session.client("ssm", config=BOTO_CONFIG)
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None
        raise


# --------------------------------------------------------------------------- #
# Bucket creation                                                             #
# --------------------------------------------------------------------------- #
def ensure_backup_bucket(ctx: BackupContext) -> None:
    """Create the destination bucket with versioning, SSE, BPA, and a bucket
    policy permitting DynamoDB ExportTableToPointInTime to write into it."""
    s3 = ctx.session.client("s3", config=BOTO_CONFIG)
    LOG.info("Creating backup bucket s3://%s", ctx.bucket)

    if ctx.dry_run:
        LOG.info("[dry-run] would create bucket %s", ctx.bucket)
        return

    create_kwargs: dict[str, Any] = {"Bucket": ctx.bucket}
    if ctx.region != "us-east-1":
        create_kwargs["CreateBucketConfiguration"] = {"LocationConstraint": ctx.region}
    try:
        s3.create_bucket(**create_kwargs)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            LOG.warning("Bucket %s already exists; reusing", ctx.bucket)
        else:
            raise

    s3.get_waiter("bucket_exists").wait(Bucket=ctx.bucket)

    s3.put_public_access_block(
        Bucket=ctx.bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_versioning(Bucket=ctx.bucket, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_encryption(
        Bucket=ctx.bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )

    s3.put_bucket_tagging(
        Bucket=ctx.bucket,
        Tagging={"TagSet": [
            {"Key": "Project", "Value": ctx.project_prefix},
            {"Key": "Purpose", "Value": "pre-migration-backup"},
            {"Key": "CreatedAt", "Value": ctx.timestamp},
        ]},
    )

    # Bucket policy: allow DynamoDB ExportTableToPointInTime to write under
    # the dynamodb/ prefix, and deny any non-TLS access.
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowDynamoDBExportFromAccount",
                "Effect": "Allow",
                "Principal": {"Service": "dynamodb.amazonaws.com"},
                "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
                "Resource": f"arn:aws:s3:::{ctx.bucket}/{ctx.root_prefix}/dynamodb/*",
                "Condition": {"StringEquals": {"aws:SourceAccount": ctx.account_id}},
            },
            {
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [
                    f"arn:aws:s3:::{ctx.bucket}",
                    f"arn:aws:s3:::{ctx.bucket}/*",
                ],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
        ],
    }
    s3.put_bucket_policy(Bucket=ctx.bucket, Policy=json.dumps(policy))


# --------------------------------------------------------------------------- #
# DynamoDB                                                                    #
# --------------------------------------------------------------------------- #
def submit_dynamo_export(ctx: BackupContext, logical: str, table_name: str) -> ComponentResult:
    dynamo = ctx.session.client("dynamodb", config=BOTO_CONFIG)
    try:
        desc = dynamo.describe_table(TableName=table_name)["Table"]
    except ClientError as exc:
        return ComponentResult(
            "dynamodb", logical, "failed",
            detail={"table_name": table_name},
            error=f"DescribeTable: {exc.response.get('Error', {}).get('Code')}",
        )

    table_arn = desc["TableArn"]
    item_count_estimate = desc.get("ItemCount", 0)
    pitr_enabled = False
    try:
        pitr_desc = dynamo.describe_continuous_backups(TableName=table_name)
        pitr_enabled = (
            pitr_desc["ContinuousBackupsDescription"]
            ["PointInTimeRecoveryDescription"]["PointInTimeRecoveryStatus"]
            == "ENABLED"
        )
    except ClientError:
        pass

    if not pitr_enabled:
        return ComponentResult(
            "dynamodb", logical, "failed",
            detail={"table_name": table_name, "table_arn": table_arn,
                    "item_count_estimate": item_count_estimate},
            error="PointInTimeRecovery not enabled — cannot use ExportTableToPointInTime",
        )

    if ctx.dry_run:
        return ComponentResult(
            "dynamodb", logical, "skipped",
            detail={"table_name": table_name, "table_arn": table_arn,
                    "item_count_estimate": item_count_estimate, "reason": "dry-run"},
        )

    s3_prefix = f"{ctx.root_prefix}/dynamodb/{logical}"
    try:
        resp = dynamo.export_table_to_point_in_time(
            TableArn=table_arn,
            S3Bucket=ctx.bucket,
            S3Prefix=s3_prefix,
            ExportFormat="DYNAMODB_JSON",
            S3SseAlgorithm="AES256",
        )
    except ClientError as exc:
        return ComponentResult(
            "dynamodb", logical, "failed",
            detail={"table_name": table_name, "table_arn": table_arn},
            error=f"ExportTableToPointInTime: {exc.response.get('Error', {}).get('Code')}",
        )

    export_arn = resp["ExportDescription"]["ExportArn"]
    schema_blob = {
        "table_name": table_name,
        "table_arn": table_arn,
        "key_schema": desc.get("KeySchema", []),
        "attribute_definitions": desc.get("AttributeDefinitions", []),
        "global_secondary_indexes": [
            {"index_name": gsi["IndexName"], "key_schema": gsi["KeySchema"]}
            for gsi in desc.get("GlobalSecondaryIndexes", []) or []
        ],
        "local_secondary_indexes": [
            {"index_name": lsi["IndexName"], "key_schema": lsi["KeySchema"]}
            for lsi in desc.get("LocalSecondaryIndexes", []) or []
        ],
        "stream_specification": desc.get("StreamSpecification"),
        "ttl_attribute": _get_ttl_attribute(dynamo, table_name),
        "billing_mode": desc.get("BillingModeSummary", {}).get("BillingMode"),
    }
    put_json(ctx, f"dynamodb/{logical}.schema.json", schema_blob)

    return ComponentResult(
        "dynamodb", logical, "ok",
        detail={
            "table_name": table_name,
            "table_arn": table_arn,
            "export_arn": export_arn,
            "s3_prefix": s3_prefix,
            "item_count_estimate": item_count_estimate,
            "status": "IN_PROGRESS",
        },
    )


def _get_ttl_attribute(dynamo: Any, table_name: str) -> str | None:
    try:
        ttl = dynamo.describe_time_to_live(TableName=table_name)["TimeToLiveDescription"]
        if ttl.get("TimeToLiveStatus") in {"ENABLED", "ENABLING"}:
            return ttl.get("AttributeName")
    except ClientError:
        pass
    return None


def wait_for_dynamo_exports(ctx: BackupContext, results: list[ComponentResult]) -> None:
    """Poll all in-progress exports until they reach a terminal state."""
    dynamo = ctx.session.client("dynamodb", config=BOTO_CONFIG)
    pending = [r for r in results
               if r.component == "dynamodb" and r.status == "ok"
               and r.detail.get("status") == "IN_PROGRESS"]
    if not pending:
        return

    LOG.info("Waiting for %d DynamoDB exports to complete…", len(pending))
    while pending:
        time.sleep(30)
        still_pending: list[ComponentResult] = []
        for r in pending:
            try:
                desc = dynamo.describe_export(
                    ExportArn=r.detail["export_arn"]
                )["ExportDescription"]
            except ClientError as exc:
                r.status = "failed"
                r.error = f"DescribeExport: {exc.response.get('Error', {}).get('Code')}"
                continue
            state = desc["ExportStatus"]
            r.detail["status"] = state
            if state == "COMPLETED":
                r.detail["item_count_exported"] = desc.get("ItemCount", 0)
                r.detail["billed_size_bytes"] = desc.get("BilledSizeBytes", 0)
                r.detail["export_manifest"] = desc.get("ExportManifest")
                LOG.info("  %s: COMPLETED (%d items)", r.logical_name,
                         r.detail["item_count_exported"])
            elif state == "FAILED":
                r.status = "failed"
                r.error = (
                    f"Export FAILED: {desc.get('FailureCode')} "
                    f"{desc.get('FailureMessage')}"
                )
                LOG.error("  %s: FAILED — %s", r.logical_name, r.error)
            else:
                still_pending.append(r)
        pending = still_pending
        if pending:
            LOG.info("  …still waiting on %d", len(pending))


# --------------------------------------------------------------------------- #
# S3                                                                          #
# --------------------------------------------------------------------------- #
def backup_s3_bucket(ctx: BackupContext, logical: str, source_bucket: str) -> ComponentResult:
    """Mirror a source bucket into the backup bucket under s3/{logical}/."""
    s3 = ctx.session.client("s3", config=BOTO_CONFIG)
    paginator = s3.get_paginator("list_objects_v2")
    obj_count = 0
    total_bytes = 0
    try:
        for page in paginator.paginate(Bucket=source_bucket):
            for o in page.get("Contents", []) or []:
                obj_count += 1
                total_bytes += o.get("Size", 0)
    except ClientError as exc:
        return ComponentResult(
            "s3", logical, "failed",
            detail={"source_bucket": source_bucket},
            error=f"ListObjects: {exc.response.get('Error', {}).get('Code')}",
        )

    detail: dict[str, Any] = {
        "source_bucket": source_bucket,
        "object_count": obj_count,
        "total_bytes": total_bytes,
        "destination_prefix": f"s3://{ctx.bucket}/{ctx.root_prefix}/s3/{logical}/",
    }
    if ctx.dry_run:
        return ComponentResult("s3", logical, "skipped",
                               detail={**detail, "reason": "dry-run"})

    dest_uri = f"s3://{ctx.bucket}/{ctx.root_prefix}/s3/{logical}/"
    cmd = ["aws", "s3", "sync", f"s3://{source_bucket}/", dest_uri,
           "--region", ctx.region, "--only-show-errors"]
    LOG.info("Syncing %s → %s (%d objects, %.1f MiB)",
             source_bucket, dest_uri, obj_count, total_bytes / 1024 / 1024)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return ComponentResult(
            "s3", logical, "failed",
            detail=detail,
            error=f"aws s3 sync exit {proc.returncode}: {proc.stderr.strip()[:500]}",
        )

    dest_count = 0
    for page in paginator.paginate(
        Bucket=ctx.bucket, Prefix=f"{ctx.root_prefix}/s3/{logical}/"
    ):
        dest_count += len(page.get("Contents", []) or [])
    detail["destination_object_count"] = dest_count
    if dest_count < obj_count:
        return ComponentResult(
            "s3", logical, "failed",
            detail=detail,
            error=f"Destination count {dest_count} < source count {obj_count}",
        )
    return ComponentResult("s3", logical, "ok", detail=detail)


# --------------------------------------------------------------------------- #
# S3 Vectors                                                                  #
# --------------------------------------------------------------------------- #
def backup_vector_index(
    ctx: BackupContext,
    logical: str,
    vector_bucket: str,
    index_name: str,
) -> ComponentResult:
    """Snapshot every vector in an S3 Vectors index to a gzipped JSONL.

    Calls `s3vectors.list_vectors(returnData=True, returnMetadata=True)` in
    a paginated loop, streaming each `(key, data, metadata)` triple to
    `vectors/{logical}.jsonl.gz` in the backup bucket. The output format
    is byte-for-byte compatible with `s3vectors.put_vectors` on restore —
    each line is a JSON object that can be passed straight to the
    `vectors=[...]` argument.

    The API surface used here is documented at:
      https://docs.aws.amazon.com/cli/latest/reference/s3vectors/list-vectors.html

    Permissions required (granted by the backup workflow's IAM role):
      - s3vectors:ListVectors
      - s3vectors:GetVectors  (needed when returnData/returnMetadata=true)
    """
    s3vectors = ctx.session.client("s3vectors", config=BOTO_CONFIG)

    detail: dict[str, Any] = {
        "vector_bucket": vector_bucket,
        "index_name": index_name,
        "destination_key": f"s3://{ctx.bucket}/{ctx.root_prefix}/vectors/{logical}.jsonl.gz",
    }

    if ctx.dry_run:
        try:
            # Cheap probe to confirm the index exists / is reachable.
            probe = s3vectors.list_vectors(
                vectorBucketName=vector_bucket,
                indexName=index_name,
                maxResults=1,
            )
            detail["probe_vector_count"] = len(probe.get("vectors", []))
        except ClientError as exc:
            return ComponentResult(
                "vectors", logical, "failed",
                detail=detail,
                error=f"ListVectors probe: {exc.response.get('Error', {}).get('Code')}",
            )
        return ComponentResult("vectors", logical, "skipped",
                               detail={**detail, "reason": "dry-run"})

    rel_key = f"vectors/{logical}.jsonl.gz"
    vector_count = 0
    next_token: str | None = None

    try:
        with _GzS3Writer(ctx, rel_key) as writer:
            while True:
                kwargs: dict[str, Any] = {
                    "vectorBucketName": vector_bucket,
                    "indexName": index_name,
                    "returnData": True,
                    "returnMetadata": True,
                }
                if next_token is not None:
                    kwargs["nextToken"] = next_token
                resp = s3vectors.list_vectors(**kwargs)
                for v in resp.get("vectors", []):
                    # The list_vectors response shape:
                    #   {"key": str, "data": {"float32": [floats]},
                    #    "metadata": <document>}
                    # — exactly the shape put_vectors accepts on restore.
                    writer.write(
                        (json.dumps(_scrub_datetimes(v), separators=(",", ":")) + "\n").encode("utf-8")
                    )
                    vector_count += 1
                next_token = resp.get("nextToken")
                if not next_token:
                    break
    except ClientError as exc:
        return ComponentResult(
            "vectors", logical, "failed",
            detail=detail,
            error=f"ListVectors: {exc.response.get('Error', {}).get('Code')}",
        )

    detail["vector_count"] = vector_count
    LOG.info("Backed up %d vectors from %s/%s → %s",
             vector_count, vector_bucket, index_name, rel_key)
    return ComponentResult("vectors", logical, "ok", detail=detail)


# --------------------------------------------------------------------------- #
# Cognito                                                                     #
# --------------------------------------------------------------------------- #
def backup_cognito(ctx: BackupContext, user_pool_id: str) -> list[ComponentResult]:
    """Full Cognito dump. Each piece becomes its own ComponentResult."""
    idp = ctx.session.client("cognito-idp", config=BOTO_CONFIG)
    results: list[ComponentResult] = []

    # 1. User pool config
    try:
        pool = idp.describe_user_pool(UserPoolId=user_pool_id)["UserPool"]
        put_json(ctx, "cognito/user-pool.json", _scrub_datetimes(pool))
        results.append(ComponentResult(
            "cognito", "user-pool", "ok",
            detail={"user_pool_id": user_pool_id,
                    "estimated_users": pool.get("EstimatedNumberOfUsers")},
        ))
    except ClientError as exc:
        results.append(ComponentResult(
            "cognito", "user-pool", "failed",
            detail={"user_pool_id": user_pool_id},
            error=str(exc),
        ))
        return results  # Nothing else works without this.

    # 2. Identity providers — describe each so ProviderDetails (incl.
    # client_secret for OIDC/social) is preserved verbatim.
    idps_out: list[dict[str, Any]] = []
    redacted_secrets: list[str] = []
    try:
        names: list[str] = []
        paginator = idp.get_paginator("list_identity_providers")
        for page in paginator.paginate(UserPoolId=user_pool_id):
            names.extend(p["ProviderName"] for p in page.get("Providers", []))
        for name in names:
            full = idp.describe_identity_provider(
                UserPoolId=user_pool_id, ProviderName=name
            )["IdentityProvider"]
            details = full.get("ProviderDetails", {}) or {}
            if full.get("ProviderType") in {"OIDC", "Google", "Facebook",
                                            "SignInWithApple", "LoginWithAmazon"}:
                if "client_secret" in details and not details["client_secret"]:
                    redacted_secrets.append(name)
            idps_out.append(_scrub_datetimes(full))
        put_json(ctx, "cognito/identity-providers.json", {"providers": idps_out})
        status = "ok"
        error: str | None = None
        if redacted_secrets:
            status = "failed"
            error = (f"client_secret missing for IdP(s): "
                     f"{', '.join(redacted_secrets)} — backup would be unrestorable")
        results.append(ComponentResult("cognito", "identity-providers",
                                       status, detail={"count": len(idps_out)},
                                       error=error))
    except ClientError as exc:
        results.append(ComponentResult(
            "cognito", "identity-providers", "failed", detail={}, error=str(exc),
        ))

    # 3. App clients — describe each so ClientSecret is preserved.
    clients_out: list[dict[str, Any]] = []
    try:
        ids: list[str] = []
        paginator = idp.get_paginator("list_user_pool_clients")
        for page in paginator.paginate(UserPoolId=user_pool_id):
            ids.extend(c["ClientId"] for c in page.get("UserPoolClients", []))
        for cid in ids:
            full = idp.describe_user_pool_client(
                UserPoolId=user_pool_id, ClientId=cid
            )["UserPoolClient"]
            clients_out.append(_scrub_datetimes(full))
        put_json(ctx, "cognito/app-clients.json", {"clients": clients_out})
        results.append(ComponentResult(
            "cognito", "app-clients", "ok", detail={"count": len(clients_out)},
        ))
    except ClientError as exc:
        results.append(ComponentResult(
            "cognito", "app-clients", "failed", detail={}, error=str(exc),
        ))

    # 4. Resource servers
    try:
        out = []
        paginator = idp.get_paginator("list_resource_servers")
        for page in paginator.paginate(UserPoolId=user_pool_id, MaxResults=50):
            out.extend(page.get("ResourceServers", []))
        put_json(ctx, "cognito/resource-servers.json", {"resource_servers": out})
        results.append(ComponentResult("cognito", "resource-servers", "ok",
                                       detail={"count": len(out)}))
    except ClientError as exc:
        results.append(ComponentResult("cognito", "resource-servers", "failed",
                                       detail={}, error=str(exc)))

    # 5. Domain
    try:
        domain = pool.get("Domain") or pool.get("CustomDomain")
        domain_blob: dict[str, Any] = {"domain": domain}
        if domain:
            d = idp.describe_user_pool_domain(Domain=domain)["DomainDescription"]
            domain_blob["description"] = _scrub_datetimes(d)
        put_json(ctx, "cognito/domain.json", domain_blob)
        results.append(ComponentResult("cognito", "domain", "ok",
                                       detail={"domain": domain}))
    except ClientError as exc:
        results.append(ComponentResult("cognito", "domain", "failed",
                                       detail={}, error=str(exc)))

    # 6. UI customization (low value; warn-only on failure)
    try:
        ui = idp.get_ui_customization(UserPoolId=user_pool_id)["UICustomization"]
        put_json(ctx, "cognito/ui-customization.json", _scrub_datetimes(ui))
        results.append(ComponentResult("cognito", "ui-customization", "ok", detail={}))
    except ClientError as exc:
        results.append(ComponentResult("cognito", "ui-customization", "skipped",
                                       detail={}, error=str(exc)))

    # 7. Users
    users_count = 0
    try:
        with _GzS3Writer(ctx, "cognito/users.jsonl.gz") as fh:
            paginator = idp.get_paginator("list_users")
            for page in paginator.paginate(UserPoolId=user_pool_id):
                for u in page.get("Users", []):
                    fh.write(json.dumps(_scrub_datetimes(u),
                                        separators=(",", ":")).encode())
                    fh.write(b"\n")
                    users_count += 1
        results.append(ComponentResult(
            "cognito", "users", "ok",
            detail={"count": users_count,
                    "note": "Password hashes are not exportable from Cognito; "
                            "native-password users will need a reset on first login."},
        ))
    except ClientError as exc:
        results.append(ComponentResult("cognito", "users", "failed",
                                       detail={"count": users_count}, error=str(exc)))

    # 8. Groups
    groups_count = 0
    group_names: list[str] = []
    try:
        with _GzS3Writer(ctx, "cognito/groups.jsonl.gz") as fh:
            paginator = idp.get_paginator("list_groups")
            for page in paginator.paginate(UserPoolId=user_pool_id):
                for g in page.get("Groups", []):
                    group_names.append(g["GroupName"])
                    fh.write(json.dumps(_scrub_datetimes(g),
                                        separators=(",", ":")).encode())
                    fh.write(b"\n")
                    groups_count += 1
        results.append(ComponentResult("cognito", "groups", "ok",
                                       detail={"count": groups_count}))
    except ClientError as exc:
        results.append(ComponentResult("cognito", "groups", "failed",
                                       detail={"count": groups_count}, error=str(exc)))

    # 9. Group memberships
    membership_count = 0
    try:
        with _GzS3Writer(ctx, "cognito/group-memberships.jsonl.gz") as fh:
            for gname in group_names:
                paginator = idp.get_paginator("list_users_in_group")
                for page in paginator.paginate(UserPoolId=user_pool_id, GroupName=gname):
                    for u in page.get("Users", []):
                        rec = {
                            "GroupName": gname,
                            "Username": u.get("Username"),
                            "UserAttributes": u.get("Attributes", []),
                        }
                        fh.write(json.dumps(rec, separators=(",", ":")).encode())
                        fh.write(b"\n")
                        membership_count += 1
        results.append(ComponentResult("cognito", "group-memberships", "ok",
                                       detail={"count": membership_count}))
    except ClientError as exc:
        results.append(ComponentResult(
            "cognito", "group-memberships", "failed",
            detail={"count": membership_count}, error=str(exc),
        ))

    return results


# --------------------------------------------------------------------------- #
# AgentCore Memory                                                            #
# --------------------------------------------------------------------------- #
def backup_agentcore_memory(ctx: BackupContext, memory_id: str) -> ComponentResult:
    detail: dict[str, Any] = {"memory_id": memory_id}
    try:
        cp = ctx.session.client("bedrock-agentcore-control", config=BOTO_CONFIG)
    except botocore.exceptions.UnknownServiceError as exc:
        return ComponentResult("agentcore-memory", "memory", "failed",
                               detail=detail,
                               error=f"boto3 lacks bedrock-agentcore-control: {exc}")

    try:
        mem = cp.get_memory(memoryId=memory_id)["memory"]
        put_json(ctx, "agentcore-memory/memory.json", _scrub_datetimes(mem))
        detail["status"] = mem.get("status")
    except (ClientError, AttributeError) as exc:
        return ComponentResult("agentcore-memory", "memory", "failed",
                               detail=detail, error=str(exc))

    try:
        dp = ctx.session.client("bedrock-agentcore", config=BOTO_CONFIG)
    except botocore.exceptions.UnknownServiceError as exc:
        notes = (
            f"boto3 lacks bedrock-agentcore data plane client ({exc}); "
            "memory config preserved but per-actor events not exported."
        )
        put_text(ctx, "agentcore-memory/NOTES.md", notes)
        return ComponentResult("agentcore-memory", "memory", "ok",
                               detail={**detail, "actors_exported": 0,
                                       "events_exported": 0, "note": notes})

    actors_count = 0
    events_count = 0
    try:
        with _GzS3Writer(ctx, "agentcore-memory/events.jsonl.gz") as fh:
            paginator = dp.get_paginator("list_actors")
            for page in paginator.paginate(memoryId=memory_id):
                for actor in page.get("actorSummaries", []):
                    actors_count += 1
                    actor_id = actor.get("actorId")
                    sessions_paginator = dp.get_paginator("list_sessions")
                    for s_page in sessions_paginator.paginate(
                        memoryId=memory_id, actorId=actor_id
                    ):
                        for sess in s_page.get("sessionSummaries", []):
                            sess_id = sess.get("sessionId")
                            ev_paginator = dp.get_paginator("list_events")
                            for e_page in ev_paginator.paginate(
                                memoryId=memory_id,
                                actorId=actor_id,
                                sessionId=sess_id,
                            ):
                                for ev in e_page.get("events", []):
                                    rec = {
                                        "actorId": actor_id,
                                        "sessionId": sess_id,
                                        "event": _scrub_datetimes(ev),
                                    }
                                    fh.write(json.dumps(rec, separators=(",", ":")).encode())
                                    fh.write(b"\n")
                                    events_count += 1
    except (ClientError, KeyError, AttributeError) as exc:
        notes = (
            f"Partial AgentCore Memory event export: {exc}. "
            f"Exported {actors_count} actors / {events_count} events before error."
        )
        put_text(ctx, "agentcore-memory/NOTES.md", notes)
        return ComponentResult(
            "agentcore-memory", "memory", "ok",
            detail={**detail, "actors_exported": actors_count,
                    "events_exported": events_count, "note": notes},
        )

    return ComponentResult(
        "agentcore-memory", "memory", "ok",
        detail={**detail, "actors_exported": actors_count,
                "events_exported": events_count},
    )


# --------------------------------------------------------------------------- #
# Output helpers                                                              #
# --------------------------------------------------------------------------- #
def put_json(ctx: BackupContext, rel_key: str, obj: Any) -> None:
    body = json.dumps(obj, indent=2, default=str).encode()
    _put_bytes(ctx, rel_key, body, "application/json")


def put_text(ctx: BackupContext, rel_key: str, text: str) -> None:
    _put_bytes(ctx, rel_key, text.encode(), "text/plain")


def _put_bytes(ctx: BackupContext, rel_key: str, body: bytes, content_type: str) -> None:
    if ctx.dry_run:
        LOG.info("[dry-run] would PUT s3://%s/%s/%s (%d bytes)",
                 ctx.bucket, ctx.root_prefix, rel_key, len(body))
        return
    s3 = ctx.session.client("s3", config=BOTO_CONFIG)
    s3.put_object(
        Bucket=ctx.bucket,
        Key=f"{ctx.root_prefix}/{rel_key}",
        Body=body,
        ContentType=content_type,
    )


class _GzS3Writer:
    """Stream-write a gzipped JSONL file to S3 via a buffered upload."""
    def __init__(self, ctx: BackupContext, rel_key: str) -> None:
        self.ctx = ctx
        self.rel_key = rel_key
        self.buf = io.BytesIO()
        self.gz = gzip.GzipFile(fileobj=self.buf, mode="wb")

    def write(self, data: bytes) -> None:
        self.gz.write(data)

    def __enter__(self) -> "_GzS3Writer":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.gz.close()
        body = self.buf.getvalue()
        if self.ctx.dry_run:
            LOG.info("[dry-run] would PUT %s (%d bytes gz)", self.rel_key, len(body))
            return
        s3 = self.ctx.session.client("s3", config=BOTO_CONFIG)
        s3.put_object(
            Bucket=self.ctx.bucket,
            Key=f"{self.ctx.root_prefix}/{self.rel_key}",
            Body=body,
            ContentType="application/x-ndjson",
            ContentEncoding="gzip",
        )


def _scrub_datetimes(obj: Any) -> Any:
    """boto3 returns datetime objects; convert to ISO strings for JSON."""
    if isinstance(obj, datetime):
        return obj.astimezone(timezone.utc).isoformat()
    if isinstance(obj, dict):
        return {k: _scrub_datetimes(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_datetimes(v) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    return obj


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #
def run(ctx: BackupContext) -> int:
    LOG.info("Backup starting: project_prefix=%s region=%s bucket=%s prefix=%s",
             ctx.project_prefix, ctx.region, ctx.bucket, ctx.root_prefix)
    ensure_backup_bucket(ctx)

    # ---- DynamoDB: discover + submit exports in parallel ----
    dynamo_targets: list[tuple[str, str]] = []
    for cfg in DYNAMODB_TABLES:
        logical = cfg["logical"]
        path = f"/{ctx.project_prefix}{cfg['ssm']}"
        name = get_ssm_param(ctx.session, path)
        if not name:
            if cfg.get("optional"):
                ctx.results.append(ComponentResult(
                    "dynamodb", logical, "skipped",
                    detail={"ssm_param": path, "reason": "optional, not present"},
                ))
            else:
                ctx.results.append(ComponentResult(
                    "dynamodb", logical, "failed",
                    detail={"ssm_param": path},
                    error="Required SSM parameter not found",
                ))
            continue
        dynamo_targets.append((logical, name))

    for cfg in DYNAMODB_TABLES_BY_CONVENTION:
        dynamo_targets.append((cfg["logical"], f"{ctx.project_prefix}-{cfg['suffix']}"))

    if ctx.include_ephemeral:
        for cfg in DYNAMODB_TABLES_EPHEMERAL:
            dynamo_targets.append((cfg["logical"], f"{ctx.project_prefix}-{cfg['suffix']}"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(submit_dynamo_export, ctx, lg, nm): lg
                   for lg, nm in dynamo_targets}
        for fut in as_completed(futures):
            ctx.results.append(fut.result())

    # ---- S3 buckets (parallel) ----
    s3_targets: list[tuple[str, str]] = []
    for cfg in S3_BUCKETS:
        logical = cfg["logical"]
        path = f"/{ctx.project_prefix}{cfg['ssm']}"
        name = get_ssm_param(ctx.session, path)
        if not name:
            if cfg.get("optional"):
                ctx.results.append(ComponentResult(
                    "s3", logical, "skipped",
                    detail={"ssm_param": path, "reason": "optional, not present"},
                ))
            else:
                ctx.results.append(ComponentResult(
                    "s3", logical, "failed",
                    detail={"ssm_param": path},
                    error="Required SSM parameter not found",
                ))
            continue
        s3_targets.append((logical, name))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(backup_s3_bucket, ctx, lg, nm): lg
                   for lg, nm in s3_targets}
        for fut in as_completed(futures):
            ctx.results.append(fut.result())

    # ---- S3 Vectors indexes (sequential — usually 1 index, paginated API) ----
    for cfg in VECTOR_INDEXES:
        logical = cfg["logical"]
        bucket_path = f"/{ctx.project_prefix}{cfg['bucket_ssm']}"
        index_path = f"/{ctx.project_prefix}{cfg['index_ssm']}"
        vector_bucket = get_ssm_param(ctx.session, bucket_path)
        index_name = get_ssm_param(ctx.session, index_path)
        if not vector_bucket or not index_name:
            if cfg.get("optional"):
                ctx.results.append(ComponentResult(
                    "vectors", logical, "skipped",
                    detail={"bucket_ssm": bucket_path, "index_ssm": index_path,
                            "reason": "optional, not present"},
                ))
            else:
                ctx.results.append(ComponentResult(
                    "vectors", logical, "failed",
                    detail={"bucket_ssm": bucket_path, "index_ssm": index_path},
                    error="Required SSM parameters not found "
                          "(both bucket-name and index-name must be published)",
                ))
            continue
        ctx.results.append(backup_vector_index(ctx, logical, vector_bucket, index_name))

    # ---- Cognito ----
    user_pool_id = get_ssm_param(ctx.session, f"/{ctx.project_prefix}{SSM_USER_POOL_ID}")
    if not user_pool_id:
        ctx.results.append(ComponentResult(
            "cognito", "user-pool", "failed",
            detail={"ssm_param": f"/{ctx.project_prefix}{SSM_USER_POOL_ID}"},
            error="Required SSM parameter not found",
        ))
    else:
        ctx.results.extend(backup_cognito(ctx, user_pool_id))

    # ---- AgentCore Memory ----
    memory_id = get_ssm_param(ctx.session, f"/{ctx.project_prefix}{SSM_MEMORY_ID}")
    if not memory_id:
        ctx.results.append(ComponentResult(
            "agentcore-memory", "memory", "skipped",
            detail={"ssm_param": f"/{ctx.project_prefix}{SSM_MEMORY_ID}",
                    "reason": "not present"},
        ))
    else:
        ctx.results.append(backup_agentcore_memory(ctx, memory_id))

    # ---- Wait for DynamoDB exports to complete ----
    wait_for_dynamo_exports(ctx, ctx.results)

    # ---- Write manifest + summary ----
    manifest = build_manifest(ctx)
    put_json(ctx, "manifest.json", manifest)

    summary = manifest["summary"]
    LOG.info("Backup complete: %s", summary)
    _write_github_summary(ctx, manifest)

    if summary["failed"] > 0 and not ctx.allow_partial:
        return 1
    return 0


def build_manifest(ctx: BackupContext) -> dict[str, Any]:
    by_component: dict[str, list[dict[str, Any]]] = {}
    counts = {"ok": 0, "skipped": 0, "failed": 0}
    for r in ctx.results:
        by_component.setdefault(r.component, []).append(dataclasses.asdict(r))
        counts[r.status] = counts.get(r.status, 0) + 1
    return {
        "version": 1,
        "tool": "agentcore-backup-data/1.0",
        "project_prefix": ctx.project_prefix,
        "region": ctx.region,
        "account_id": ctx.account_id,
        "timestamp": ctx.timestamp,
        "bucket": ctx.bucket,
        "root_prefix": ctx.root_prefix,
        "include_ephemeral": ctx.include_ephemeral,
        "dry_run": ctx.dry_run,
        "summary": {
            "total": len(ctx.results),
            "ok": counts["ok"],
            "skipped": counts["skipped"],
            "failed": counts["failed"],
        },
        "components": by_component,
    }


def _write_github_summary(ctx: BackupContext, manifest: dict[str, Any]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    summary = manifest["summary"]
    lines = [
        "# Backup Summary",
        f"- **Project prefix:** `{ctx.project_prefix}`",
        f"- **Region:** `{ctx.region}`",
        f"- **Bucket:** `s3://{ctx.bucket}/{ctx.root_prefix}/`",
        f"- **Timestamp:** `{ctx.timestamp}`",
        f"- **Totals:** {summary['ok']} ok · {summary['skipped']} skipped · {summary['failed']} failed",
        "",
        "| Component | Logical | Status | Detail |",
        "|---|---|---|---|",
    ]
    for component_rows in manifest["components"].values():
        for row in component_rows:
            d = row.get("detail") or {}
            blurb = row.get("error") or ", ".join(
                f"{k}={v}" for k, v in d.items()
                if k in {"item_count_estimate", "item_count_exported",
                         "object_count", "total_bytes", "count",
                         "actors_exported", "events_exported"}
            )
            lines.append(
                f"| {row['component']} | {row['logical_name']} | {row['status']} | {blurb} |"
            )
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{1,20}$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backup all AgentCore Public Stack data for a given project prefix.",
    )
    p.add_argument("--project-prefix", required=True,
                   help="The CDK_PROJECT_PREFIX of the environment to back up.")
    p.add_argument("--region", required=True, help="AWS region.")
    p.add_argument("--include-ephemeral", action="store_true",
                   help="Also back up TTL-driven session/state tables.")
    p.add_argument("--dry-run", action="store_true",
                   help="Discover and list sources without performing any writes.")
    p.add_argument("--allow-partial", action="store_true",
                   help="Exit 0 even if some components failed (manifest still reflects state).")
    p.add_argument("--bucket-override", default=None,
                   help="Use this exact bucket name instead of computing one. "
                        "Bucket must already exist.")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not _PREFIX_RE.match(args.project_prefix):
        LOG.error("Invalid --project-prefix '%s' (must match %s)",
                  args.project_prefix, _PREFIX_RE.pattern)
        return 2

    session = boto3.Session(region_name=args.region)
    sts = session.client("sts", config=BOTO_CONFIG)
    account_id = sts.get_caller_identity()["Account"]

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bucket = args.bucket_override or f"{args.project_prefix}-backup-{timestamp.lower()}"
    if len(bucket) > 63:
        LOG.error("Computed bucket name '%s' exceeds 63 chars; use --bucket-override", bucket)
        return 2

    ctx = BackupContext(
        project_prefix=args.project_prefix,
        region=args.region,
        timestamp=timestamp,
        bucket=bucket,
        root_prefix=f"{args.project_prefix}/{timestamp}",
        account_id=account_id,
        include_ephemeral=args.include_ephemeral,
        dry_run=args.dry_run,
        allow_partial=args.allow_partial,
        session=session,
    )

    try:
        return run(ctx)
    except Exception:  # noqa: BLE001 — top-level catch so manifest still writes if possible
        LOG.error("Unhandled error:\n%s", traceback.format_exc())
        try:
            manifest = build_manifest(ctx)
            manifest["fatal_error"] = traceback.format_exc()
            put_json(ctx, "manifest.json", manifest)
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
