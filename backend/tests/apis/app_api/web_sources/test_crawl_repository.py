"""moto-backed tests for the CrawlJob persistence layer.

`test_routes.py` mocks `create_crawl_job` to keep the HTTP-surface tests
fast and pure. That left no coverage for the DynamoDB write itself —
which actually hit a runtime `TypeError: Float types are not supported`
on the very first manual smoke test, because `CrawlSettings` carries
float delay knobs and DynamoDB rejects bare Python floats.

These tests pin the contract: put → get → list survive a settings dict
with floats, and `Decimal` round-trips back into the Pydantic model as
float seamlessly.
"""

from __future__ import annotations

import time

import boto3
import pytest
from moto import mock_aws

from apis.app_api.web_sources import crawl_repository
from apis.app_api.web_sources.models import CrawlSettings


TABLE = "test-assistants-table"
REGION = "us-east-1"
ASSISTANT_ID = "ast-001"
USER_ID = "user-001"


@pytest.fixture
def ddb(monkeypatch: pytest.MonkeyPatch):
    with mock_aws():
        monkeypatch.setenv("AWS_REGION", REGION)
        monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
        client = boto3.client("dynamodb", region_name=REGION)
        client.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


@pytest.mark.asyncio
async def test_create_crawl_job_writes_floats_as_decimals(ddb) -> None:
    """Regression: putting CrawlSettings with float delays must not raise."""
    settings = CrawlSettings(
        maxDepth=2,
        maxPages=25,
        concurrency=2,
        minDelay=1.5,  # float
        maxDelay=3.5,  # float
        sameDomainOnly=True,
    )
    job = await crawl_repository.create_crawl_job(
        assistant_id=ASSISTANT_ID,
        root_url="https://example.com/",
        settings=settings,
        started_by_user_id=USER_ID,
    )
    assert job.status == "running"
    assert job.settings.min_delay_seconds == 1.5
    assert job.settings.max_delay_seconds == 3.5

    # Round-trip the row back as a Pydantic model.
    refetched = await crawl_repository.get_crawl_job(ASSISTANT_ID, job.crawl_id)
    assert refetched is not None
    assert refetched.crawl_id == job.crawl_id
    assert refetched.settings.min_delay_seconds == 1.5
    assert refetched.settings.max_delay_seconds == 3.5


@pytest.mark.asyncio
async def test_list_active_crawls_returns_only_running(ddb) -> None:
    """Filter expression must pin to status='running' only."""
    settings = CrawlSettings()
    running = await crawl_repository.create_crawl_job(
        assistant_id=ASSISTANT_ID,
        root_url="https://example.com/r",
        settings=settings,
        started_by_user_id=USER_ID,
    )
    done = await crawl_repository.create_crawl_job(
        assistant_id=ASSISTANT_ID,
        root_url="https://example.com/d",
        settings=settings,
        started_by_user_id=USER_ID,
    )
    await crawl_repository.finalize_crawl(
        assistant_id=ASSISTANT_ID,
        crawl_id=done.crawl_id,
        status="complete",
    )
    active = await crawl_repository.list_active_crawls(ASSISTANT_ID)
    active_ids = {c.crawl_id for c in active}
    assert running.crawl_id in active_ids
    assert done.crawl_id not in active_ids


@pytest.mark.asyncio
async def test_list_active_crawls_reaps_stale_running_rows(ddb) -> None:
    """Self-healing: a `running` row older than the crawler budget must be
    auto-finalized and excluded from the active list. Otherwise the SPA's
    crawl-watcher polls forever on a dead row owned by a crashed process.
    """
    # Create a fresh running crawl, then back-date its `startedAt` to before
    # the staleness threshold so the reaper triggers.
    job = await crawl_repository.create_crawl_job(
        assistant_id=ASSISTANT_ID,
        root_url="https://example.com/",
        settings=CrawlSettings(),
        started_by_user_id=USER_ID,
    )
    stale_started = "2020-01-01T00:00:00Z"  # well past _STALE_RUNNING_SECONDS
    ddb.update_item(
        Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"CRAWL#{job.crawl_id}"},
        UpdateExpression="SET startedAt = :s",
        ExpressionAttributeValues={":s": stale_started},
    )

    # First call reaps the stale row and returns []; subsequent call confirms
    # the row was actually finalized (not just hidden in-memory).
    first = await crawl_repository.list_active_crawls(ASSISTANT_ID)
    assert first == [], "stale running row should be excluded from active list"

    second = await crawl_repository.list_active_crawls(ASSISTANT_ID)
    assert second == [], "reaped row must stay reaped on subsequent reads"

    refetched = await crawl_repository.get_crawl_job(ASSISTANT_ID, job.crawl_id)
    assert refetched is not None
    assert refetched.status == "failed"
    assert refetched.error is not None
    assert "interrupted" in refetched.error.lower()


@pytest.mark.asyncio
async def test_increment_counters_bumps_atomically(ddb) -> None:
    job = await crawl_repository.create_crawl_job(
        assistant_id=ASSISTANT_ID,
        root_url="https://example.com/",
        settings=CrawlSettings(),
        started_by_user_id=USER_ID,
    )
    await crawl_repository.increment_counters(
        assistant_id=ASSISTANT_ID,
        crawl_id=job.crawl_id,
        discovered_delta=3,
        fetched_delta=2,
        failed_delta=1,
    )
    await crawl_repository.increment_counters(
        assistant_id=ASSISTANT_ID,
        crawl_id=job.crawl_id,
        fetched_delta=4,
    )
    refetched = await crawl_repository.get_crawl_job(ASSISTANT_ID, job.crawl_id)
    assert refetched is not None
    assert refetched.discovered_count == 3
    assert refetched.fetched_count == 6
    assert refetched.failed_count == 1


@pytest.mark.asyncio
async def test_finalize_writes_status_and_error(ddb) -> None:
    job = await crawl_repository.create_crawl_job(
        assistant_id=ASSISTANT_ID,
        root_url="https://example.com/",
        settings=CrawlSettings(),
        started_by_user_id=USER_ID,
    )
    await crawl_repository.finalize_crawl(
        assistant_id=ASSISTANT_ID,
        crawl_id=job.crawl_id,
        status="failed",
        error="boom",
    )
    refetched = await crawl_repository.get_crawl_job(ASSISTANT_ID, job.crawl_id)
    assert refetched is not None
    assert refetched.status == "failed"
    assert refetched.error == "boom"
    assert refetched.completed_at is not None


@pytest.mark.asyncio
async def test_finalize_writes_ttl_as_future_epoch_seconds(ddb) -> None:
    """`finalize_crawl` must set a `ttl` epoch-seconds attribute the table's reaper can honor.

    Regression guard: DynamoDB silently ignores millisecond-precision TTL
    values, so the assertion bounds also enforce that the value is in
    seconds (~now + 30 days), not milliseconds (~13-digit).
    """
    job = await crawl_repository.create_crawl_job(
        assistant_id=ASSISTANT_ID,
        root_url="https://example.com/",
        settings=CrawlSettings(),
        started_by_user_id=USER_ID,
    )
    before = int(time.time())
    await crawl_repository.finalize_crawl(
        assistant_id=ASSISTANT_ID,
        crawl_id=job.crawl_id,
        status="complete",
    )
    after = int(time.time())

    # Read the raw item so we see the on-disk `ttl` attribute directly
    # (the Pydantic model doesn't surface it).
    response = ddb.get_item(
        Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"CRAWL#{job.crawl_id}"}
    )
    item = response.get("Item")
    assert item is not None, "finalize_crawl should have left the row in place"
    assert "ttl" in item, "finalize_crawl must set the `ttl` attribute"

    ttl_value = int(item["ttl"])  # DynamoDB returns Decimal — coerce
    thirty_days_seconds = 30 * 86400
    assert before + thirty_days_seconds - 5 <= ttl_value <= after + thirty_days_seconds + 5

    # Belt-and-suspenders: a millisecond-precision value would be ~13 digits.
    # Seconds-precision sits around 10 digits well into the 2030s.
    assert ttl_value < 10**12, (
        f"ttl={ttl_value} looks like milliseconds — DynamoDB TTL ignores those"
    )


# =========================================================================
# Cascade-delete: last-web-doc removal cleans up the CrawlJob row
# =========================================================================


def _put_web_doc(ddb, *, document_id: str, source_file_id: str, status: str = "complete") -> None:
    """Write a minimal `web` DOC row for cascade tests.

    Only the fields the cascade reads (`sourceConnectorId`, `sourceFileId`,
    `status`) need to be present; everything else is irrelevant here.
    """
    ddb.put_item(
        Item={
            "PK": f"AST#{ASSISTANT_ID}",
            "SK": f"DOC#{document_id}",
            "documentId": document_id,
            "assistantId": ASSISTANT_ID,
            "sourceConnectorId": "web",
            "sourceFileId": source_file_id,
            "status": status,
        }
    )


@pytest.mark.asyncio
async def test_cascade_deletes_crawl_when_last_web_doc_removed(ddb) -> None:
    """Removing the sole surviving web doc for a finalized crawl should drop the CrawlJob row."""
    from apis.app_api.documents.services.cleanup_service import (
        _cascade_delete_orphaned_crawl_jobs,
    )

    root_url = "https://example.com/docs/"
    crawl = await crawl_repository.create_crawl_job(
        assistant_id=ASSISTANT_ID,
        root_url=root_url,
        settings=CrawlSettings(),
        started_by_user_id=USER_ID,
    )
    await crawl_repository.finalize_crawl(
        assistant_id=ASSISTANT_ID,
        crawl_id=crawl.crawl_id,
        status="complete",
    )

    # No DOC rows remain referencing the root URL → the crawl is orphaned.
    await _cascade_delete_orphaned_crawl_jobs(ASSISTANT_ID)

    assert await crawl_repository.get_crawl_job(ASSISTANT_ID, crawl.crawl_id) is None


@pytest.mark.asyncio
async def test_cascade_keeps_crawl_when_other_web_docs_remain(ddb) -> None:
    """A finalized crawl with at least one surviving web doc must stay put."""
    from apis.app_api.documents.services.cleanup_service import (
        _cascade_delete_orphaned_crawl_jobs,
    )

    root_url = "https://example.com/docs/"
    crawl = await crawl_repository.create_crawl_job(
        assistant_id=ASSISTANT_ID,
        root_url=root_url,
        settings=CrawlSettings(),
        started_by_user_id=USER_ID,
    )
    await crawl_repository.finalize_crawl(
        assistant_id=ASSISTANT_ID,
        crawl_id=crawl.crawl_id,
        status="complete",
    )
    _put_web_doc(
        ddb,
        document_id="DOC-survivor",
        source_file_id=f"{root_url}page-2",
    )

    await _cascade_delete_orphaned_crawl_jobs(ASSISTANT_ID)

    assert await crawl_repository.get_crawl_job(ASSISTANT_ID, crawl.crawl_id) is not None


@pytest.mark.asyncio
async def test_cascade_ignores_soft_deleted_docs(ddb) -> None:
    """Docs still in `deleting` status are gone for cascade purposes — the cleanup task
    is mid-flight and will hard-delete them shortly."""
    from apis.app_api.documents.services.cleanup_service import (
        _cascade_delete_orphaned_crawl_jobs,
    )

    root_url = "https://example.com/docs/"
    crawl = await crawl_repository.create_crawl_job(
        assistant_id=ASSISTANT_ID,
        root_url=root_url,
        settings=CrawlSettings(),
        started_by_user_id=USER_ID,
    )
    await crawl_repository.finalize_crawl(
        assistant_id=ASSISTANT_ID,
        crawl_id=crawl.crawl_id,
        status="complete",
    )
    _put_web_doc(
        ddb,
        document_id="DOC-zombie",
        source_file_id=f"{root_url}page-3",
        status="deleting",
    )

    await _cascade_delete_orphaned_crawl_jobs(ASSISTANT_ID)

    assert await crawl_repository.get_crawl_job(ASSISTANT_ID, crawl.crawl_id) is None


@pytest.mark.asyncio
async def test_cascade_leaves_running_crawl_alone(ddb) -> None:
    """A `running` crawl is still spawning child docs — never reap it here."""
    from apis.app_api.documents.services.cleanup_service import (
        _cascade_delete_orphaned_crawl_jobs,
    )

    crawl = await crawl_repository.create_crawl_job(
        assistant_id=ASSISTANT_ID,
        root_url="https://example.com/",
        settings=CrawlSettings(),
        started_by_user_id=USER_ID,
    )
    # Deliberately do not finalize — status stays `running`.

    await _cascade_delete_orphaned_crawl_jobs(ASSISTANT_ID)

    assert await crawl_repository.get_crawl_job(ASSISTANT_ID, crawl.crawl_id) is not None
