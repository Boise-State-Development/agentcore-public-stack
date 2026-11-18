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

And one that checks the whole path rather than each piece: a burst of deletes
through the real route, against a table whose every call blocks like a network
round trip, must make none of those calls on the loop's thread. It counts calls
by thread rather than timing the loop, because a wall-clock bound on loop lag
fails whenever another thread in the test process holds the GIL: one spinning
thread put the healthy path at 800 ms against its usual 35, and CI failed the
old 250 ms bound at 1152 and 1385 ms on two unrelated branches (2026-11-18).

The shared assistant lookups go through ``apis.shared.blocking_io``: a thread on
app-api, inline on the Runtime, whose turn path would only pay for the hop.
"""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from apis.app_api.documents.routes import router as documents_router
from apis.app_api.documents.services import cleanup_service, document_service
from apis.shared import blocking_io
from apis.shared.assistants import service as assistant_service
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
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


@pytest.fixture(autouse=True)
def app_api_blocking_io_mode():
    """app-api's mode (a thread), whatever an earlier test's import of the Runtime set."""
    previous = blocking_io.blocking_io_runs_inline()
    blocking_io.run_blocking_io_inline(False)
    yield
    blocking_io.run_blocking_io_inline(previous)


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
    async def test_project_harness_role_lookup(self):
        """A project's agent resolves the caller's role from two more reads (project, member)."""
        loop_thread = threading.get_ident()
        threads: list[int] = []
        harness = SimpleNamespace(assistant_id=ASSISTANT_ID, kind="project", owner_id=OWNER_ID)

        def role(*_args) -> str:
            threads.append(threading.get_ident())
            return "editor"

        with (
            patch.object(assistant_service, "_get_assistant_cloud_without_ownership_check", AsyncMock(return_value=harness)),
            patch.object(assistant_service, "_project_harness_role", role),
            patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE}),
        ):
            await assistant_service.resolve_assistant_permission(ASSISTANT_ID, "member", "m@example.edu")
            await assistant_service.get_assistant_with_access_check(ASSISTANT_ID, "member", "m@example.edu")

        assert len(threads) == 2
        assert loop_thread not in threads

    @pytest.mark.asyncio
    async def test_the_runtime_keeps_the_lookups_inline(self, recording_table):
        """The turn path awaits these lookups before the first token; a hop there is pure cost."""
        loop_thread = threading.get_ident()
        blocking_io.run_blocking_io_inline()
        await assistant_service._get_assistant_cloud_without_ownership_check(ASSISTANT_ID, TABLE)
        await assistant_service.check_share_access(ASSISTANT_ID, "someone@example.edu")
        assert [thread for _, thread in recording_table.calls] == [loop_thread, loop_thread]

    def test_the_runtime_process_selects_inline(self):
        """`inference_api/main.py` picks the inline mode at startup."""
        from pathlib import Path

        import apis.inference_api

        source = (Path(apis.inference_api.__file__).parent / "main.py").read_text()
        assert "\nrun_blocking_io_inline()\n" in source

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


# ---------------------------------------------------------------------------
# A burst of deletes through the real route leaves the loop responsive
# ---------------------------------------------------------------------------

# A DynamoDB round trip, held the way a blocking boto3 call holds its thread.
_ROUND_TRIP_S = 0.01
# Twice the size of the 2026-11-16 burst's busiest minute in flight at once.
_BURST = 200


class _ThreadLedger:
    """Counts the blocking calls made on the event loop's thread and elsewhere."""

    def __init__(self, loop_thread: int) -> None:
        self.loop_thread = loop_thread
        self.on_loop = 0
        self.off_loop = 0
        self._lock = threading.Lock()

    def hold(self) -> None:
        """Record which thread is calling, then block it for a round trip."""
        with self._lock:
            if threading.get_ident() == self.loop_thread:
                self.on_loop += 1
            else:
                self.off_loop += 1
        time.sleep(_ROUND_TRIP_S)


def _assistant_item() -> dict:
    return {
        "PK": f"AST#{ASSISTANT_ID}",
        "SK": "METADATA",
        "assistantId": ASSISTANT_ID,
        "ownerId": OWNER_ID,
        "ownerName": "Owner",
        "name": "Burst project",
        "description": "",
        "instructions": "",
        "vectorIndexId": "idx",
        "visibility": "PRIVATE",
        "createdAt": "2026-11-16T00:00:00Z",
        "updatedAt": "2026-11-16T00:00:00Z",
        "status": "COMPLETE",
    }


def _document_item(document_id: str) -> dict:
    return {
        "PK": f"AST#{ASSISTANT_ID}",
        "SK": f"DOC#{document_id}",
        "documentId": document_id,
        "assistantId": ASSISTANT_ID,
        "filename": f"{document_id}.html",
        "contentType": "text/html",
        "sizeBytes": 1024,
        "s3Key": f"assistants/{ASSISTANT_ID}/documents/{document_id}/page.html",
        "status": "complete",
        "chunkCount": 3,
        "createdAt": "2026-11-16T00:00:00Z",
        "updatedAt": "2026-11-16T00:00:00Z",
    }


class _SlowTable:
    """Answers like the assistants table, after blocking its thread for a round trip."""

    def __init__(self, ledger: _ThreadLedger) -> None:
        self._ledger = ledger

    def get_item(self, Key: dict, **_kwargs) -> dict:  # noqa: N803 - boto3's spelling
        self._ledger.hold()
        return {"Item": _assistant_item()} if Key["SK"] == "METADATA" else {}

    def update_item(self, Key: dict, **_kwargs) -> dict:  # noqa: N803
        self._ledger.hold()
        return {"Attributes": _document_item(Key["SK"].removeprefix("DOC#"))}

    def query(self, **_kwargs) -> dict:
        self._ledger.hold()
        return {"Items": []}

    def delete_item(self, **_kwargs) -> dict:
        self._ledger.hold()
        return {}


async def _delete_burst() -> tuple[_ThreadLedger, list[int], int]:
    """Send the burst through the route; return the call ledger, the statuses, the cleanups."""
    app = FastAPI()
    app.include_router(documents_router)
    app.dependency_overrides[get_current_user_from_session] = lambda: User(
        email="owner@example.edu", user_id=OWNER_ID, name="Owner", roles=["User"]
    )

    cleaned = 0
    all_cleaned = asyncio.Event()

    async def cleanup_body(*, document_id: str, assistant_id: str, **_kwargs) -> bool:
        # The background cleanup's own DynamoDB work: the hard delete and the
        # orphaned-crawl cascade's partition read. Vectors, S3 and the managed KB
        # are other services, already off the loop.
        nonlocal cleaned
        await document_service.hard_delete_document(assistant_id, document_id)
        await cleanup_service._list_surviving_web_source_file_ids(assistant_id)
        cleaned += 1
        if cleaned == _BURST:
            all_cleaned.set()
        return True

    ledger = _ThreadLedger(threading.get_ident())

    def no_kb_record(*_args, **_kwargs):
        ledger.hold()
        return None

    transport = httpx.ASGITransport(app=app)
    with (
        patch("boto3.resource", return_value=SimpleNamespace(Table=lambda _name: _SlowTable(ledger))),
        patch("apis.shared.kb_backend.records.get_kb_record", no_kb_record),
        patch.object(cleanup_service, "_cleanup_document_resources", cleanup_body),
        patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE}),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://app-api") as client:
            responses = await asyncio.gather(
                *(client.delete(f"/assistants/{ASSISTANT_ID}/documents/DOC-{i}") for i in range(_BURST))
            )
            await asyncio.wait_for(all_cleaned.wait(), timeout=60)
    return ledger, [r.status_code for r in responses], cleaned


class TestDeleteBurstKeepsTheLoopResponsive:
    @pytest.mark.asyncio
    async def test_a_burst_of_deletes_never_blocks_the_loop(self):
        ledger, statuses, cleaned = await _delete_burst()

        assert statuses == [204] * _BURST
        assert cleaned == _BURST
        assert ledger.off_loop >= _BURST, "the burst made no blocking calls, so it checked nothing"
        assert ledger.on_loop == 0, (
            f"{ledger.on_loop} of {ledger.on_loop + ledger.off_loop} blocking calls in a burst of {_BURST} "
            "deletes ran on the event loop's thread: a blocking call on the delete path is running on the loop"
        )

    @pytest.mark.asyncio
    async def test_the_ledger_sees_a_blocking_call(self):
        """The same burst with the assistant lookups inline puts calls on the loop, so the test has teeth."""
        blocking_io.run_blocking_io_inline()
        ledger, statuses, _ = await _delete_burst()

        assert statuses == [204] * _BURST
        assert ledger.on_loop >= _BURST
