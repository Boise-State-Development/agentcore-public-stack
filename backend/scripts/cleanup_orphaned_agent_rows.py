"""Clean up what deleted agents left behind in the rag-assistants table.

Until the ``fix/agents-delete-cleanup`` change, ``DELETE /agents/{id}`` (the route the
SPA's Agents page calls) deleted only an agent's ``METADATA`` row, plus its versions
and reports. Everything else in the ``AST#{agentId}`` partition stayed: documents
still ``complete`` (with their S3 objects and legacy vector chunks), share rows,
crawl rows, sync policies, and the ``KB#`` record with its managed Bedrock knowledge
base. Deletes through either route also left ``SHARE#`` rows. The route is fixed; this
script clears what was left before the fix.

WHAT IT DOES
------------
1. Scans the table and groups rows by ``AST#`` partition. An **orphan** is a partition
   with no ``METADATA`` row, i.e. an agent that no longer exists.
2. Skips an orphan whose newest row is younger than ``--min-age-hours`` (default 24):
   a delete still running its background document cleanup looks exactly like an
   orphan for a few minutes.
3. Reports every orphan: row counts by type, document statuses, bytes, and the S3
   objects still under ``assistants/{agentId}/``.
4. With ``--apply``, for each orphan (re-checking with a consistent read that the
   ``METADATA`` row is still absent):

   * ``KB#``: queued for teardown (``records.request_teardown``). The kb-migration
     worker deletes the data source and knowledge base, then the record. This script
     never deletes a knowledge base itself.
   * ``DOC#``: the app's own document cleanup
     (``cleanup_service.cleanup_document_resources``): legacy vectors, managed-KB
     document, S3 object, then the row, which is kept if any phase fails. A row with
     no ``s3Key`` (a failed upload or a stray row) never produced vectors, so it is
     just removed.
   * Any S3 object still under ``assistants/{agentId}/`` (icons, stragglers) is deleted.
   * ``SYNCPOL#``, ``VERSION#``, ``REPORT#``: through their own delete functions.
   * ``SHARE#``, ``CRAWL#``: the rows are deleted.
   * ``KBTOMB#`` is left to the teardown that owns it. Any other row type is reported
     and left alone.

S3 PREFIX MODE (``--s3-prefixes``)
----------------------------------
The partition scan above can't see an agent whose partition is already empty. Until
agent delete learned to delete ``assistants/{agentId}/icons/`` (``delete_agent_icons``),
every deleted agent with an icon left exactly that: no rows, one or more S3 objects. This
mode starts from S3 instead:

1. Lists the ``assistants/{agentId}/`` prefixes in the documents bucket.
2. Drops any whose agent has a ``METADATA`` row (alive) or any other ``AST#`` row (a
   partition orphan, which the default mode cleans, S3 prefix included).
3. Skips a prefix whose newest object is younger than ``--min-age-hours``.
4. Reports the rest: object counts by folder (``icons``, ``documents``, other) and bytes.
5. With ``--apply``, re-checks that the partition is still empty with a consistent read
   and deletes the objects. An object whose key a row anywhere in the table still names
   (``iconKey``/``s3Key``, e.g. a publisher profile or an admin-set ``iconKey`` pointed
   at another agent's icon) is reported and kept.

The bucket is versioned, so a delete adds a delete marker and the bytes stay as a
noncurrent version until a lifecycle rule expires them; this matches every other delete
the app makes in that bucket.

SAFETY
------
* **Read-only by default.** Nothing is written without ``--apply``, and ``--apply``
  requires ``--confirm-prefix`` equal to ``--project-prefix``.
* Only partitions with no ``METADATA`` row, checked again right before acting.
* ``--agent`` limits a run to named agent ids; start with one.
* The table has PITR. Take an on-demand backup before a first production run anyway.

Run (a human, not CI; production is read-only from agents)::

    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/cleanup_orphaned_agent_rows.py \\
        --project-prefix dev-boisestateai-v2 --region us-west-2                  # report only
    ... --apply --confirm-prefix dev-boisestateai-v2 --agent ast-0123456789ab   # one agent
    ... --apply --confirm-prefix dev-boisestateai-v2                              # all orphans
    ... --s3-prefixes                                                 # S3 prefix mode, report only
    ... --s3-prefixes --apply --confirm-prefix dev-boisestateai-v2               # S3 prefix mode

Resource names are derived from the prefix and the caller's account, the way CDK names
them (``{prefix}-rag-assistants``, ``{prefix}-rag-documents-{account}``,
``{prefix}-rag-vector-store-v1-{account}``, ``{prefix}-rag-vector-index-v1``), and can
be overridden. The report names agent and document ids only, never share emails.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

#: Row types and what --apply does with each.
HANDLED = ("KB", "DOC", "SHARE", "CRAWL", "SYNCPOL", "VERSION", "REPORT")
#: Owned by the knowledge-base teardown, which clears them itself.
LEFT_FOR_TEARDOWN = ("KBTOMB",)
_TIMESTAMP_FIELDS = ("updatedAt", "createdAt", "completedAt", "startedAt")
_DOC_CONCURRENCY = 8


@dataclass
class Orphan:
    agent_id: str
    rows: List[Dict[str, Any]]
    newest: str = ""
    s3_objects: int = 0
    s3_bytes: int = 0
    result: Dict[str, Any] = field(default_factory=dict)

    def by_type(self) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
        for row in self.rows:
            grouped[row_type(row["SK"])].append(row)
        return grouped

    def summary(self) -> Dict[str, Any]:
        grouped = self.by_type()
        docs = grouped.get("DOC", [])
        return {
            "agentId": self.agent_id,
            "newest": self.newest,
            "rows": {kind: len(rows) for kind, rows in sorted(grouped.items())},
            "documentStatuses": dict(collections.Counter(str(d.get("status")) for d in docs)),
            "documentBytes": sum(int(d.get("sizeBytes") or 0) for d in docs),
            "s3Objects": self.s3_objects,
            "s3Bytes": self.s3_bytes,
            "unhandled": sorted({k for k in grouped if k not in HANDLED + LEFT_FOR_TEARDOWN}),
            **({"result": self.result} if self.result else {}),
        }


def row_type(sort_key: str) -> str:
    return sort_key.split("#", 1)[0]


def newest_timestamp(rows: Sequence[Dict[str, Any]]) -> str:
    stamps = [str(row[f]) for row in rows for f in _TIMESTAMP_FIELDS if row.get(f)]
    return max(stamps) if stamps else ""


def find_orphans(
    items: Sequence[Dict[str, Any]], now: datetime, min_age_hours: float
) -> tuple[List[Orphan], List[Orphan]]:
    """(orphans old enough to act on, orphans too young), from a table scan."""
    partitions: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for item in items:
        if str(item.get("PK", "")).startswith("AST#"):
            partitions[item["PK"]].append(item)

    cutoff = (now - timedelta(hours=min_age_hours)).strftime("%Y-%m-%dT%H:%M:%S")
    ready, young = [], []
    for pk, rows in sorted(partitions.items()):
        if any(row["SK"] == "METADATA" for row in rows):
            continue
        orphan = Orphan(agent_id=pk.split("#", 1)[1], rows=rows, newest=newest_timestamp(rows))
        (young if orphan.newest and orphan.newest[:19] > cutoff else ready).append(orphan)
    return ready, young


@dataclass
class OrphanPrefix:
    """An ``assistants/{agentId}/`` S3 prefix whose agent has no row left at all."""

    agent_id: str
    objects: List[Dict[str, Any]]
    referenced: List[str] = field(default_factory=list)
    result: Dict[str, Any] = field(default_factory=dict)

    @property
    def newest(self) -> Optional[datetime]:
        stamps = [o["LastModified"] for o in self.objects if o.get("LastModified")]
        return max(stamps) if stamps else None

    def deletable(self) -> List[Dict[str, Any]]:
        keep = set(self.referenced)
        return [o for o in self.objects if o["Key"] not in keep]

    def summary(self) -> Dict[str, Any]:
        newest = self.newest
        return {
            "agentId": self.agent_id,
            "newest": newest.isoformat() if newest else "",
            "objects": dict(sorted(collections.Counter(prefix_folder(o["Key"]) for o in self.objects).items())),
            "bytes": sum(int(o.get("Size") or 0) for o in self.objects),
            **({"referencedKept": len(self.referenced)} if self.referenced else {}),
            **({"result": self.result} if self.result else {}),
        }


def prefix_folder(key: str) -> str:
    """``icons`` / ``documents`` / ``other`` for a key under ``assistants/{agentId}/``."""
    parts = key.split("/", 3)
    folder = parts[2] if len(parts) > 3 else ""
    return folder if folder in ("icons", "documents") else "other"


def table_index(items: Sequence[Dict[str, Any]]) -> tuple[set, set, set]:
    """(agent ids with a METADATA row, agent ids with any row, every S3 key a row names)."""
    live, any_rows, referenced = set(), set(), set()
    for item in items:
        for attr in ("iconKey", "s3Key"):
            if isinstance(item.get(attr), str):
                referenced.add(item[attr])
        pk = str(item.get("PK", ""))
        if pk.startswith("AST#"):
            agent_id = pk.split("#", 1)[1]
            any_rows.add(agent_id)
            if item.get("SK") == "METADATA":
                live.add(agent_id)
    return live, any_rows, referenced


def classify_prefixes(
    prefix_ids: Sequence[str], items: Sequence[Dict[str, Any]]
) -> Dict[str, List[str]]:
    """Sort agent prefixes into ``live``, ``partitionOrphan`` (the default mode's) and ``empty``."""
    live, any_rows, _ = table_index(items)
    groups: Dict[str, List[str]] = {"live": [], "partitionOrphan": [], "empty": []}
    for agent_id in sorted(prefix_ids):
        key = "live" if agent_id in live else "partitionOrphan" if agent_id in any_rows else "empty"
        groups[key].append(agent_id)
    return groups


def split_by_age(
    prefixes: Sequence[OrphanPrefix], now: datetime, min_age_hours: float
) -> tuple[List[OrphanPrefix], List[OrphanPrefix]]:
    """(old enough to act on, too young). An object written in the last few minutes may
    belong to an agent whose create is still in flight."""
    cutoff = now - timedelta(hours=min_age_hours)
    ready, young = [], []
    for prefix in prefixes:
        newest = prefix.newest
        (young if newest is not None and newest > cutoff else ready).append(prefix)
    return ready, young


# ── AWS ──────────────────────────────────────────────────────────────────────
def scan_table(table) -> List[Dict[str, Any]]:
    kwargs: Dict[str, Any] = {}
    items: List[Dict[str, Any]] = []
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def s3_prefix_objects(s3, bucket: str, agent_id: str) -> List[Dict[str, Any]]:
    objects: List[Dict[str, Any]] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"assistants/{agent_id}/"):
        objects.extend(page.get("Contents", []))
    return objects


def s3_agent_prefixes(s3, bucket: str) -> List[str]:
    """Every agent id with an ``assistants/{agentId}/`` prefix in the bucket."""
    ids: List[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="assistants/", Delimiter="/"):
        for common in page.get("CommonPrefixes", []):
            agent_id = common["Prefix"][len("assistants/"):].rstrip("/")
            if agent_id:
                ids.append(agent_id)
    return ids


def partition_empty(table, agent_id: str) -> bool:
    from boto3.dynamodb.conditions import Key

    response = table.query(
        KeyConditionExpression=Key("PK").eq(f"AST#{agent_id}"), Limit=1, ConsistentRead=True
    )
    return not response.get("Items")


def clean_prefix(prefix: OrphanPrefix, table, s3, bucket: str) -> Dict[str, Any]:
    """Delete one orphan prefix's objects, keeping any a row still names."""
    if not partition_empty(table, prefix.agent_id):
        return {"skipped": "the partition has rows now"}
    objects = prefix.deletable()
    deleted, errors = 0, []
    for start in range(0, len(objects), 1000):
        chunk = objects[start:start + 1000]
        response = s3.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": o["Key"]} for o in chunk], "Quiet": True}
        )
        failed = response.get("Errors", [])
        errors.extend(f"{e.get('Key')}: {e.get('Code')}" for e in failed)
        deleted += len(chunk) - len(failed)
    return {"s3ObjectsDeleted": deleted, **({"errors": errors} if errors else {})}


def metadata_exists(table, agent_id: str) -> bool:
    item = table.get_item(Key={"PK": f"AST#{agent_id}", "SK": "METADATA"}, ConsistentRead=True).get("Item")
    return item is not None


async def _clean_documents(agent_id: str, docs: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    from apis.app_api.documents.services.cleanup_service import cleanup_document_resources
    from apis.app_api.documents.services.document_service import hard_delete_document

    semaphore = asyncio.Semaphore(_DOC_CONCURRENCY)
    outcome = collections.Counter()

    async def one(doc: Dict[str, Any]) -> None:
        document_id = row_id = doc["SK"].split("#", 1)[1]
        async with semaphore:
            if not doc.get("s3Key"):
                # Nothing was ever stored or indexed for it.
                ok = await hard_delete_document(agent_id, row_id)
            else:
                chunk_count = doc.get("chunkCount")
                ok = await cleanup_document_resources(
                    document_id=document_id,
                    assistant_id=agent_id,
                    s3_key=doc["s3Key"],
                    chunk_count=int(chunk_count) if chunk_count is not None else None,
                    source_connector_id=doc.get("sourceConnectorId"),
                    source_file_id=doc.get("sourceFileId"),
                )
        outcome["deleted" if ok else "kept"] += 1

    await asyncio.gather(*(one(doc) for doc in docs))
    return dict(outcome)


def _delete_rows(table, rows: Sequence[Dict[str, Any]]) -> int:
    with table.batch_writer() as batch:
        for row in rows:
            batch.delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
    return len(rows)


async def clean_orphan(orphan: Orphan, table, s3, bucket: str) -> Dict[str, Any]:
    """Apply the cleanup to one orphan. Returns what was done."""
    from apis.shared.assistants.reports import delete_reports_for_agent
    from apis.shared.assistants.version_repository import delete_versions_for_agent
    from apis.shared.kb_backend import records
    from apis.shared.sync_policies.service import delete_sync_policies_for_assistant
    from apis.shared.timestamps import utc_now_iso

    agent = orphan.agent_id
    if metadata_exists(table, agent):
        return {"skipped": "the agent exists now"}

    grouped = orphan.by_type()
    done: Dict[str, Any] = {}
    if grouped.get("KB"):
        queued = records.request_teardown(agent, agent, utc_now_iso())
        done["kbTeardownQueued"] = queued is not None
    if grouped.get("DOC"):
        done["documents"] = await _clean_documents(agent, grouped["DOC"])
    remaining = s3_prefix_objects(s3, bucket, agent)
    for start in range(0, len(remaining), 1000):
        chunk = remaining[start:start + 1000]
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": o["Key"]} for o in chunk], "Quiet": True})
    done["s3ObjectsDeleted"] = len(remaining)
    if grouped.get("SYNCPOL"):
        done["syncPolicies"] = await delete_sync_policies_for_assistant(agent)
    if grouped.get("VERSION"):
        done["versions"] = await delete_versions_for_agent(agent)
    if grouped.get("REPORT"):
        done["reports"] = await delete_reports_for_agent(agent)
    for kind in ("SHARE", "CRAWL"):
        if grouped.get(kind):
            done[kind.lower() + "Rows"] = _delete_rows(table, grouped[kind])
    return done


# ── CLI ──────────────────────────────────────────────────────────────────────
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--project-prefix", required=True, help="CDK_PROJECT_PREFIX, e.g. dev-boisestateai-v2")
    p.add_argument("--region", required=True)
    p.add_argument("--profile", default=None, help="AWS profile name (or set AWS_PROFILE)")
    p.add_argument("--agent", action="append", default=[], help="Only these agent ids (repeatable)")
    p.add_argument("--min-age-hours", type=float, default=24.0,
                   help="Skip orphans whose newest row is younger than this")
    p.add_argument("--s3-prefixes", action="store_true",
                   help="Find assistants/{id}/ S3 prefixes with no rows at all (see S3 PREFIX MODE)")
    p.add_argument("--apply", action="store_true", help="Clean up; without it the run only reports")
    p.add_argument("--confirm-prefix", default=None, help="Required with --apply; must equal --project-prefix")
    p.add_argument("--table", default=None, help="Override {prefix}-rag-assistants")
    p.add_argument("--documents-bucket", default=None, help="Override {prefix}-rag-documents-{account}")
    p.add_argument("--vector-bucket", default=None, help="Override {prefix}-rag-vector-store-v1-{account}")
    p.add_argument("--vector-index", default=None, help="Override {prefix}-rag-vector-index-v1")
    p.add_argument("--out", default=None, help="Write the JSON report here")
    return p.parse_args(argv)


def configure(args: argparse.Namespace, account: str) -> Dict[str, str]:
    """Resource names, exported for the app modules, which read them at import."""
    prefix = args.project_prefix
    names = {
        "DYNAMODB_ASSISTANTS_TABLE_NAME": args.table or f"{prefix}-rag-assistants",
        "S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME": args.documents_bucket or f"{prefix}-rag-documents-{account}",
        "S3_ASSISTANTS_VECTOR_STORE_BUCKET_NAME": args.vector_bucket or f"{prefix}-rag-vector-store-v1-{account}",
        "S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME": args.vector_index or f"{prefix}-rag-vector-index-v1",
        "AWS_REGION": args.region,
        "AWS_DEFAULT_REGION": args.region,
    }
    if args.profile:
        names["AWS_PROFILE"] = args.profile
    os.environ.update(names)
    return names


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.apply and args.confirm_prefix != args.project_prefix:
        print("--apply requires --confirm-prefix equal to --project-prefix", file=sys.stderr)
        return 2
    if args.profile:
        os.environ["AWS_PROFILE"] = args.profile

    import boto3

    account = boto3.client("sts", region_name=args.region).get_caller_identity()["Account"]
    names = configure(args, account)
    table = boto3.resource("dynamodb", region_name=args.region).Table(names["DYNAMODB_ASSISTANTS_TABLE_NAME"])
    s3 = boto3.client("s3", region_name=args.region)
    bucket = names["S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME"]

    if args.s3_prefixes:
        return run_s3_prefixes(args, names, table, s3, bucket)

    ready, young = find_orphans(scan_table(table), datetime.now(timezone.utc), args.min_age_hours)
    if args.agent:
        wanted = set(args.agent)
        ready = [o for o in ready if o.agent_id in wanted]
        young = [o for o in young if o.agent_id in wanted]
    for orphan in ready:
        objects = s3_prefix_objects(s3, bucket, orphan.agent_id)
        orphan.s3_objects, orphan.s3_bytes = len(objects), sum(o.get("Size", 0) for o in objects)

    print(f"{'APPLY' if args.apply else 'REPORT ONLY'}: {names['DYNAMODB_ASSISTANTS_TABLE_NAME']}, "
          f"{len(ready)} orphaned agents to clean, {len(young)} too recent to touch")
    async def run_all() -> None:
        # One event loop for the whole run: the app's document code holds loop-bound
        # semaphores, which a fresh loop per agent would trip over.
        for orphan in ready:
            if args.apply:
                orphan.result = await clean_orphan(orphan, table, s3, bucket)
            print(json.dumps(orphan.summary(), default=str), flush=True)

    asyncio.run(run_all())
    for orphan in young:
        print(json.dumps({"agentId": orphan.agent_id, "newest": orphan.newest, "skipped": "too recent"}))

    if args.out:
        report = {
            "table": names["DYNAMODB_ASSISTANTS_TABLE_NAME"],
            "applied": args.apply,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "orphans": [o.summary() for o in ready],
            "tooRecent": [{"agentId": o.agent_id, "newest": o.newest} for o in young],
        }
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=2, default=str)
    return 0


def run_s3_prefixes(args: argparse.Namespace, names: Dict[str, str], table, s3, bucket: str) -> int:
    prefix_ids = s3_agent_prefixes(s3, bucket)
    if args.agent:
        wanted = set(args.agent)
        prefix_ids = [i for i in prefix_ids if i in wanted]
    items = scan_table(table)
    groups = classify_prefixes(prefix_ids, items)
    _, _, referenced = table_index(items)

    prefixes = []
    for agent_id in groups["empty"]:
        objects = s3_prefix_objects(s3, bucket, agent_id)
        prefixes.append(OrphanPrefix(agent_id, objects, referenced=sorted(
            o["Key"] for o in objects if o["Key"] in referenced)))
    ready, young = split_by_age(prefixes, datetime.now(timezone.utc), args.min_age_hours)

    folders = collections.Counter()
    for prefix in ready:
        folders.update(prefix_folder(o["Key"]) for o in prefix.objects)
    print(f"{'APPLY' if args.apply else 'REPORT ONLY'}: s3://{bucket}/assistants/, "
          f"{len(prefix_ids)} agent prefixes: {len(groups['live'])} live, "
          f"{len(groups['partitionOrphan'])} partition orphans (the default mode's), "
          f"{len(ready)} with no rows to clean ({sum(folders.values())} objects: "
          f"{dict(sorted(folders.items()))}, {sum(int(o.get('Size') or 0) for p in ready for o in p.objects)} bytes), "
          f"{len(young)} too recent to touch")
    for prefix in ready:
        if args.apply:
            prefix.result = clean_prefix(prefix, table, s3, bucket)
        print(json.dumps(prefix.summary(), default=str), flush=True)
    for prefix in young:
        print(json.dumps({**prefix.summary(), "skipped": "too recent"}, default=str))

    if args.out:
        report = {
            "bucket": bucket,
            "table": names["DYNAMODB_ASSISTANTS_TABLE_NAME"],
            "applied": args.apply,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "prefixes": {k: len(v) for k, v in groups.items()},
            "orphanPrefixes": [p.summary() for p in ready],
            "tooRecent": [p.summary() for p in young],
        }
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
