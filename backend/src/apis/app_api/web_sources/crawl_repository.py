"""DynamoDB persistence for `CrawlJob` rows.

Reuses the assistants table via the adjacency-list pattern:
`PK = AST#{assistant_id}`, `SK = CRAWL#{crawl_id}`. This keeps the SPA's
list-by-assistant query a single `SK begins_with CRAWL#` scan and lets the
job ride the assistant's blast radius on delete.

A failed update never raises — the caller is a fire-and-forget background
task and we'd rather lose a progress tick than abort the crawl.
"""

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional

from apis.app_api.web_sources.models import CrawlJob, CrawlJobStatus, CrawlSettings

logger = logging.getLogger(__name__)

# Terminal crawl rows auto-expire after this many days via the table's
# `ttl` attribute. Long enough for any reasonable "what happened on my last
# crawl" follow-up; short enough that an idle assistant doesn't accumulate
# dead rows indefinitely.
_FINALIZED_TTL_DAYS = 30

# A `running` crawl whose started_at is older than this is presumed dead.
# The crawler's own internal budget is 15 minutes (CRAWL_BUDGET_SECONDS in
# crawler.py), and it always runs `finalize_crawl` in a finally block. The
# only way a row stays `running` past that is the process owning it died
# (server restart, OOM, hung event loop). 5-minute buffer past the budget
# avoids racing a crawl that's legitimately near its ceiling.
_STALE_RUNNING_SECONDS = 20 * 60


def _generate_crawl_id() -> str:
    return f"CRAWL-{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _table():
    import boto3

    table_name = os.environ.get("DYNAMODB_ASSISTANTS_TABLE_NAME")
    if not table_name:
        raise ValueError("DYNAMODB_ASSISTANTS_TABLE_NAME environment variable not set")
    return boto3.resource("dynamodb").Table(table_name)


def _ddb_safe(value: Any) -> Any:
    """Recursively coerce Python floats to `Decimal` for DynamoDB writes.

    DynamoDB's number type maps to `Decimal` on both ends — boto3's
    serializer raises `TypeError: Float types are not supported` when it
    encounters a bare `float`. `CrawlSettings` carries float delay knobs,
    so the settings sub-dict (and any list/nested dict that wraps a float)
    must be walked before `put_item`. Pydantic v2 happily coerces the
    Decimal back into a float when we read these rows.
    """
    if isinstance(value, float):
        # Decimal(<float>) introduces representation noise; round-trip
        # through str so 1.0 stays "1" instead of "1.0000000000…001".
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _ddb_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ddb_safe(v) for v in value]
    return value


async def create_crawl_job(
    *,
    assistant_id: str,
    root_url: str,
    settings: CrawlSettings,
    started_by_user_id: str,
    crawl_id: Optional[str] = None,
) -> CrawlJob:
    """Persist a new `running` crawl row and return its model.

    Raises on persistence failure — the route relies on the row existing
    before it fires the background task.
    """
    job = CrawlJob(
        crawl_id=crawl_id or _generate_crawl_id(),
        assistant_id=assistant_id,
        root_url=root_url,
        status="running",
        settings=settings,
        discovered_count=0,
        fetched_count=0,
        failed_count=0,
        started_at=_now(),
        started_by_user_id=started_by_user_id,
    )
    item = job.model_dump(by_alias=True, exclude_none=True)
    item["PK"] = f"AST#{assistant_id}"
    item["SK"] = f"CRAWL#{job.crawl_id}"
    _table().put_item(Item=_ddb_safe(item))
    logger.info(
        "Created crawl %s for assistant %s (root=%s)",
        job.crawl_id,
        assistant_id,
        root_url,
    )
    return job


async def get_crawl_job(assistant_id: str, crawl_id: str) -> Optional[CrawlJob]:
    response = _table().get_item(
        Key={"PK": f"AST#{assistant_id}", "SK": f"CRAWL#{crawl_id}"}
    )
    item = response.get("Item")
    if not item:
        return None
    try:
        return CrawlJob.model_validate(item)
    except Exception as e:
        logger.warning("Failed to parse CrawlJob row %s/%s: %s", assistant_id, crawl_id, e)
        return None


async def list_all_crawls(assistant_id: str) -> List[CrawlJob]:
    """Return every crawl row for an assistant regardless of status. Empty list on error.

    Used by the cascade-on-last-doc-delete path — we need to inspect every
    surviving crawl, not just `running` ones, to decide which (if any) have
    been orphaned by the deletion.
    """
    try:
        from boto3.dynamodb.conditions import Key

        response = _table().query(
            KeyConditionExpression=Key("PK").eq(f"AST#{assistant_id}")
            & Key("SK").begins_with("CRAWL#"),
        )
    except Exception as e:
        logger.error("Failed to list crawls for %s: %s", assistant_id, e)
        return []

    crawls: List[CrawlJob] = []
    for item in response.get("Items", []):
        try:
            crawls.append(CrawlJob.model_validate(item))
        except Exception as e:
            logger.warning("Skipping unparseable CrawlJob row: %s", e)
    return crawls


async def hard_delete_crawl_job(assistant_id: str, crawl_id: str) -> bool:
    """Unconditionally remove a CrawlJob row. Never raises.

    Called from the document-cleanup cascade after the last `web` doc
    referencing this crawl's root_url is removed. Returns True on success.
    """
    try:
        _table().delete_item(
            Key={"PK": f"AST#{assistant_id}", "SK": f"CRAWL#{crawl_id}"}
        )
        logger.info("Hard-deleted crawl %s for assistant %s", crawl_id, assistant_id)
        return True
    except Exception as e:
        logger.error(
            "Failed to hard-delete crawl %s for assistant %s: %s",
            crawl_id,
            assistant_id,
            e,
        )
        return False


def _is_crawl_stale(job: CrawlJob) -> bool:
    """A `running` crawl past the crawler's own budget is dead.

    The crawler always finalizes in a `finally` block; the only way a
    `running` row outlives the budget is the owning process died. Mirrors
    the same staleness pattern documents use in `document_service`.
    """
    try:
        started_str = job.started_at.rstrip("Z")
        started = datetime.fromisoformat(started_str).replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        return elapsed > _STALE_RUNNING_SECONDS
    except (ValueError, AttributeError) as e:
        logger.warning(
            "Unparseable started_at on crawl %s/%s (%s); treating as stale",
            job.assistant_id,
            job.crawl_id,
            e,
        )
        return True


async def list_active_crawls(assistant_id: str) -> List[CrawlJob]:
    """Return all `running` crawl jobs for an assistant. Empty list on error.

    Self-heals stale rows: a `running` job whose owning process died (server
    restart, OOM) would otherwise keep the SPA's crawl-watcher polling
    forever. On read, any such job past `_STALE_RUNNING_SECONDS` is
    finalized as `failed` and dropped from the result. Matches the existing
    "auto-fail stale processing docs on read" pattern in `document_service`.
    """
    try:
        from boto3.dynamodb.conditions import Attr, Key

        response = _table().query(
            KeyConditionExpression=Key("PK").eq(f"AST#{assistant_id}")
            & Key("SK").begins_with("CRAWL#"),
            FilterExpression=Attr("status").eq("running"),
        )
    except Exception as e:
        logger.error("Failed to list active crawls for %s: %s", assistant_id, e)
        return []

    alive: List[CrawlJob] = []
    for item in response.get("Items", []):
        try:
            job = CrawlJob.model_validate(item)
        except Exception as e:
            logger.warning("Skipping unparseable CrawlJob row: %s", e)
            continue

        if _is_crawl_stale(job):
            logger.info(
                "Auto-reaping stale crawl %s/%s (started_at=%s)",
                assistant_id,
                job.crawl_id,
                job.started_at,
            )
            await finalize_crawl(
                assistant_id=assistant_id,
                crawl_id=job.crawl_id,
                status="failed",
                error="Crawl interrupted (the owning process did not finish in time).",
            )
            continue

        alive.append(job)
    return alive


async def increment_counters(
    *,
    assistant_id: str,
    crawl_id: str,
    discovered_delta: int = 0,
    fetched_delta: int = 0,
    failed_delta: int = 0,
) -> None:
    """Atomically bump per-page counters on an in-flight job. Never raises."""
    if discovered_delta == 0 and fetched_delta == 0 and failed_delta == 0:
        return
    set_parts: list[str] = []
    add_parts: list[str] = []
    values: dict[str, object] = {":now": _now()}
    set_parts.append("updatedAt = :now")
    if discovered_delta:
        add_parts.append("discoveredCount :d_disc")
        values[":d_disc"] = discovered_delta
    if fetched_delta:
        add_parts.append("fetchedCount :d_fetch")
        values[":d_fetch"] = fetched_delta
    if failed_delta:
        add_parts.append("failedCount :d_fail")
        values[":d_fail"] = failed_delta

    expression_parts: list[str] = []
    if set_parts:
        expression_parts.append("SET " + ", ".join(set_parts))
    if add_parts:
        expression_parts.append("ADD " + ", ".join(add_parts))

    try:
        _table().update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": f"CRAWL#{crawl_id}"},
            UpdateExpression=" ".join(expression_parts),
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(PK)",
        )
    except Exception as e:
        logger.warning(
            "Failed to bump counters on crawl %s/%s: %s",
            assistant_id,
            crawl_id,
            e,
        )


async def finalize_crawl(
    *,
    assistant_id: str,
    crawl_id: str,
    status: CrawlJobStatus,
    error: Optional[str] = None,
) -> None:
    """Move a crawl out of `running`. Never raises.

    Callers must invoke this in a `finally` so a crashed task does not leave
    a job pinned at `running` forever (which would keep the editor's watcher
    loop spinning).
    """
    # DynamoDB TTL requires epoch *seconds* — millisecond values are silently
    # ignored by the reaper. `#ttl` is escaped because `ttl` is a reserved word.
    ttl_epoch_seconds = int(time.time()) + _FINALIZED_TTL_DAYS * 86400
    expression_attribute_names = {"#status": "status", "#ttl": "ttl"}
    set_parts = [
        "#status = :status",
        "completedAt = :completed_at",
        "updatedAt = :completed_at",
        "#ttl = :ttl",
    ]
    values: dict[str, object] = {
        ":status": status,
        ":completed_at": _now(),
        ":ttl": ttl_epoch_seconds,
    }
    if error is not None:
        set_parts.append("#err = :err")
        expression_attribute_names["#err"] = "error"
        # Trim long error strings so we never write a >400KB DynamoDB row.
        values[":err"] = (error or "")[:2000]

    try:
        _table().update_item(
            Key={"PK": f"AST#{assistant_id}", "SK": f"CRAWL#{crawl_id}"},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeValues=values,
            ExpressionAttributeNames=expression_attribute_names,
            ConditionExpression="attribute_exists(PK)",
        )
    except Exception as e:
        logger.error(
            "Failed to finalize crawl %s/%s (status=%s): %s",
            assistant_id,
            crawl_id,
            status,
            e,
        )
