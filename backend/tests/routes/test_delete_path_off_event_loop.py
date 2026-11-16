"""The document delete path keeps blocking boto3 off app-api's event loop.

On 2026-11-16 one project was emptied in a burst (8 web-source deletes, then
658 single-document deletes in about four minutes). Every one of those requests
made several blocking DynamoDB calls on the event loop, and each web page's
background cleanup re-read the whole document partition, so the loop stalled for
up to 8.5 s at a time and ECS replaced every app-api task on failed health
checks.

These tests pin the three things that fix it:

* every DynamoDB call on that path runs in a worker thread, not on the loop's;
* the orphaned-crawl cascade runs once per burst, not once per page;
* background cleanups are capped per task.
"""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from apis.app_api.documents.services import cleanup_service, document_service
from apis.shared.assistants import service as assistant_service
from apis.shared.sync_policies import service as sync_policy_service

ASSISTANT_ID = "ast-offloop"
OWNER_ID = "user-offloop"
TABLE = "assistants-table"


class _RecordingTable:
    """A boto3 Table stand-in that records which thread each call ran on."""

    def __init__(self, responses: dict | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, int]] = []

    def __getattr__(self, name: str):
        def call(**_kwargs):
            self.calls.append((name, threading.get_ident()))
            return self.responses.get(name, {})

        return call


class _RecordingResource:
    def __init__(self, table: _RecordingTable) -> None:
        self._table = table

    def Table(self, _name: str) -> _RecordingTable:  # noqa: N802 - boto3's spelling
        return self._table


@pytest.fixture()
def recording_table(monkeypatch):
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
    table = _RecordingTable()
    with patch("boto3.resource", return_value=_RecordingResource(table)):
        yield table


def _assert_all_off_loop(table: _RecordingTable, loop_thread: int, expected: set[str]) -> None:
    assert {name for name, _ in table.calls} >= expected, table.calls
    on_loop = [name for name, thread in table.calls if thread == loop_thread]
    assert not on_loop, f"ran on the event loop thread: {on_loop}"


# ---------------------------------------------------------------------------
# Every DynamoDB call on the delete path runs in a worker thread
# ---------------------------------------------------------------------------


class TestDeletePathRunsOffTheLoop:
    @pytest.mark.asyncio
    async def test_soft_delete_document(self, recording_table):
        loop_thread = threading.get_ident()
        with patch(
            "apis.shared.assistants.service.get_assistant",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(assistant_id=ASSISTANT_ID, owner_id=OWNER_ID),
        ):
            await document_service.soft_delete_document(ASSISTANT_ID, "DOC-1", OWNER_ID)
        _assert_all_off_loop(recording_table, loop_thread, {"update_item"})

    @pytest.mark.asyncio
    async def test_hard_delete_document(self, recording_table):
        loop_thread = threading.get_ident()
        assert await document_service.hard_delete_document(ASSISTANT_ID, "DOC-1")
        _assert_all_off_loop(recording_table, loop_thread, {"delete_item"})

    @pytest.mark.asyncio
    async def test_list_assistant_documents(self, recording_table):
        """The crawl delete lists every document of the project before it starts."""
        loop_thread = threading.get_ident()
        recording_table.responses["query"] = {"Items": []}
        with patch(
            "apis.shared.assistants.service.get_assistant",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(assistant_id=ASSISTANT_ID, owner_id=OWNER_ID),
        ):
            await document_service.list_assistant_documents(ASSISTANT_ID, OWNER_ID)
        _assert_all_off_loop(recording_table, loop_thread, {"query"})

    @pytest.mark.asyncio
    async def test_assistant_and_share_lookups(self, recording_table):
        """`resolve_assistant_permission` and `get_assistant` run on every delete."""
        loop_thread = threading.get_ident()
        await assistant_service._get_assistant_cloud(ASSISTANT_ID, OWNER_ID, TABLE)
        await assistant_service._get_assistant_cloud_without_ownership_check(ASSISTANT_ID, TABLE)
        await assistant_service.check_share_access(ASSISTANT_ID, "someone@example.edu")
        _assert_all_off_loop(recording_table, loop_thread, {"get_item"})
        assert len(recording_table.calls) == 3

    @pytest.mark.asyncio
    async def test_sync_policy_cleanup(self, recording_table):
        loop_thread = threading.get_ident()
        recording_table.responses["query"] = {"Items": []}
        await sync_policy_service.delete_sync_policies_for_source(ASSISTANT_ID, "DOC-1")
        await sync_policy_service.delete_sync_policy(ASSISTANT_ID, "syn-1")
        await sync_policy_service.exclude_folder_file(ASSISTANT_ID, "syn-1", "file-1")
        _assert_all_off_loop(recording_table, loop_thread, {"query", "delete_item", "update_item"})

    @pytest.mark.asyncio
    async def test_cascade_partition_read(self, recording_table):
        loop_thread = threading.get_ident()
        recording_table.responses["query"] = {"Items": [{"sourceFileId": "https://x/a", "status": "complete"}]}
        urls = await cleanup_service._list_surviving_web_source_file_ids(ASSISTANT_ID)
        assert urls == ["https://x/a"]
        _assert_all_off_loop(recording_table, loop_thread, {"query"})

    @pytest.mark.asyncio
    async def test_byte_settlement(self):
        """`settle_bytes_on_delete` makes up to five DynamoDB calls per delete."""
        loop_thread = threading.get_ident()
        threads: list[tuple[str, int]] = []

        def recorder(name, value):
            def call(*_args, **_kwargs):
                threads.append((name, threading.get_ident()))
                return value

            return call

        document = SimpleNamespace(
            assistant_id=ASSISTANT_ID, document_id="DOC-1", size_bytes=10, source_adapter_key=None
        )
        with (
            patch("apis.shared.kb_backend.byte_cap.release_reingest_once", recorder("release_reingest_once", 0)),
            patch("apis.shared.kb_backend.byte_cap.refund_once", recorder("refund_once", 0)),
            patch("apis.shared.kb_backend.byte_cap.reserved_at_request", return_value=10),
            patch("apis.shared.kb_backend.records.get_kb_record", recorder("get_kb_record", None)),
        ):
            await document_service.settle_bytes_on_delete(document, "complete")

        assert [name for name, _ in threads] == ["release_reingest_once", "refund_once", "get_kb_record"]
        assert all(thread != loop_thread for _, thread in threads), threads


# ---------------------------------------------------------------------------
# The orphaned-crawl cascade runs once per burst, not once per page
# ---------------------------------------------------------------------------


class TestCascadeCoalesced:
    @pytest.mark.asyncio
    async def test_single_delete_runs_one_pass_inline(self):
        passes = AsyncMock()
        with patch.object(cleanup_service, "_cascade_delete_orphaned_crawl_jobs", passes):
            await cleanup_service._cascade_coalesced(ASSISTANT_ID)
        passes.assert_awaited_once_with(ASSISTANT_ID)
        assert ASSISTANT_ID not in cleanup_service._cascade_running

    @pytest.mark.asyncio
    async def test_burst_during_a_pass_costs_one_more_pass(self):
        """Fifty deletions landing while a pass runs fold into a single follow-up."""
        release = asyncio.Event()
        passes = 0

        async def slow_pass(_assistant_id: str) -> None:
            nonlocal passes
            passes += 1
            if passes == 1:
                await release.wait()

        with patch.object(cleanup_service, "_cascade_delete_orphaned_crawl_jobs", slow_pass):
            first = asyncio.create_task(cleanup_service._cascade_coalesced(ASSISTANT_ID))
            await asyncio.sleep(0)
            assert passes == 1

            # Later callers return at once: they only mark the assistant.
            await asyncio.gather(*(cleanup_service._cascade_coalesced(ASSISTANT_ID) for _ in range(50)))
            assert passes == 1

            release.set()
            await first

        assert passes == 2
        assert ASSISTANT_ID not in cleanup_service._cascade_running
        assert ASSISTANT_ID not in cleanup_service._cascade_rerun

    @pytest.mark.asyncio
    async def test_other_assistants_are_not_coalesced(self):
        seen: list[str] = []

        async def record(assistant_id: str) -> None:
            seen.append(assistant_id)

        with patch.object(cleanup_service, "_cascade_delete_orphaned_crawl_jobs", record):
            await asyncio.gather(
                cleanup_service._cascade_coalesced("ast-a"),
                cleanup_service._cascade_coalesced("ast-b"),
            )
        assert sorted(seen) == ["ast-a", "ast-b"]

    @pytest.mark.asyncio
    async def test_a_failed_pass_does_not_wedge_the_assistant(self):
        async def boom(_assistant_id: str) -> None:
            raise RuntimeError("list failed")

        with patch.object(cleanup_service, "_cascade_delete_orphaned_crawl_jobs", boom):
            with pytest.raises(RuntimeError):
                await cleanup_service._cascade_coalesced(ASSISTANT_ID)
        assert ASSISTANT_ID not in cleanup_service._cascade_running

        passes = AsyncMock()
        with patch.object(cleanup_service, "_cascade_delete_orphaned_crawl_jobs", passes):
            await cleanup_service._cascade_coalesced(ASSISTANT_ID)
        passes.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_web_page_cleanup_goes_through_the_coalescer(self):
        coalesced = AsyncMock()
        with (
            patch.object(cleanup_service, "_resolve_engine", AsyncMock(return_value="legacy")),
            patch.object(cleanup_service, "_delete_vectors_with_retries", AsyncMock(return_value=True)),
            patch.object(cleanup_service, "_delete_managed_documents_with_retries", AsyncMock(return_value=True)),
            patch.object(cleanup_service, "_delete_s3_with_retries", AsyncMock(return_value=True)),
            patch(
                "apis.app_api.documents.services.document_service.hard_delete_document",
                AsyncMock(return_value=True),
            ),
            patch.object(cleanup_service, "_cascade_coalesced", coalesced),
        ):
            assert await cleanup_service.cleanup_document_resources(
                document_id="DOC-1",
                assistant_id=ASSISTANT_ID,
                s3_key="k",
                chunk_count=1,
                source_connector_id="web",
            )
        coalesced.assert_awaited_once_with(ASSISTANT_ID)


# ---------------------------------------------------------------------------
# Background cleanups are capped per task
# ---------------------------------------------------------------------------


class TestCleanupConcurrencyCap:
    @pytest.mark.asyncio
    async def test_a_burst_never_exceeds_the_cap(self):
        in_flight = 0
        peak = 0
        finished = 0

        async def body(**_kwargs) -> bool:
            nonlocal in_flight, peak, finished
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.001)
            in_flight -= 1
            finished += 1
            return True

        burst = cleanup_service.MAX_CONCURRENT_CLEANUPS * 5
        with patch.object(cleanup_service, "_cleanup_document_resources", body):
            results = await asyncio.gather(
                *(
                    cleanup_service.cleanup_document_resources(
                        document_id=f"DOC-{i}", assistant_id=ASSISTANT_ID, s3_key="k", chunk_count=1
                    )
                    for i in range(burst)
                )
            )

        assert results == [True] * burst
        assert finished == burst
        assert peak == cleanup_service.MAX_CONCURRENT_CLEANUPS

    @pytest.mark.asyncio
    async def test_bulk_cleanup_is_capped_too(self):
        """`cleanup_assistant_documents` (web-source and agent deletion) shares the cap."""
        in_flight = 0
        peak = 0

        async def body(**_kwargs) -> bool:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.001)
            in_flight -= 1
            return True

        docs = [
            SimpleNamespace(document_id=f"DOC-{i}", s3_key="k", chunk_count=1)
            for i in range(cleanup_service.MAX_CONCURRENT_CLEANUPS * 3)
        ]
        with patch.object(cleanup_service, "_cleanup_document_resources", body):
            success, failure = await cleanup_service.cleanup_assistant_documents(ASSISTANT_ID, docs)

        assert (success, failure) == (len(docs), 0)
        assert peak <= cleanup_service.MAX_CONCURRENT_CLEANUPS
