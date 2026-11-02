"""The shared ``attachments`` knowledge base (large-document spec §4E, PR-3).

Moto for DynamoDB (file rows, the byte accounting, sessions) and S3; the managed
knowledge base itself is a recording fake. Pins:

- ids: ``att#{user}#{session}#{upload}`` and the original-object key shape;
- the per-user byte cap: reserved once per document, refused over the cap,
  released exactly once;
- every message means "make the index match the row": eligible → ingest by S3
  location and poll; gone, unprepared or deleted mid-ingest → remove;
- status checks reschedule until indexed, fail cleanly, and stop;
- eviction after inactivity measured from the session's last turn (or the last
  re-index), and the daily reconcile over the knowledge base;
- re-index on conversation open queues only the caller's evicted documents.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import boto3
import pytest

from apis.shared.files import attachment_index as ai
from apis.shared.files import large_document
from apis.shared.files.models import FileMetadata, FileStatus

MB = 1024 * 1024
REGION = "us-east-1"
USER = "user-1"
SESSION = "sess-1"


def _row(upload_id: str = "up-1", *, size: int = 38 * MB, prepared: bool = True, kb: Optional[dict] = None,
         user: str = USER, session: str = SESSION, bucket: str = "test-file-uploads",
         created: Optional[datetime] = None) -> FileMetadata:
    """``created`` pins ``created_at``, which ``_last_activity`` counts.

    Without it the field defaults to the wall clock, so any test asserting an
    eviction has to hope real "now" is far enough behind its fixed ``NOW`` —
    and silently stops testing anything the day it is not.
    """
    return FileMetadata(
        **({"created_at": created} if created is not None else {}),
        upload_id=upload_id,
        user_id=user,
        session_id=session,
        filename="manual.pdf",
        mime_type="application/pdf",
        size_bytes=size,
        s3_key=f"user-files/{user}/{session}/{upload_id}/manual.pdf",
        s3_bucket=bucket,
        status=FileStatus.READY,
        preparation=large_document.preparation_record(status="ready", fmt="pdf", page_count=10, shard_pages=10)
        if prepared else large_document.preparation_record(status="pending"),
        kb_index=kb,
    )


class FakeBackend:
    def __init__(self) -> None:
        self.ingested: List[Any] = []
        self.deleted: List[str] = []

    async def ingest_documents(self, kb_ref, sources):
        assert kb_ref == "attachments"
        self.ingested.extend(sources)

    async def delete_documents(self, kb_ref, ids):
        assert kb_ref == "attachments"
        self.deleted.extend(ids)

    def _locate(self, kb_ref):
        return "KB123", "DS456"


class FakeAgent:
    def __init__(self) -> None:
        self.statuses: Dict[str, str] = {}
        self.listing: List[tuple] = []

    def get_knowledge_base_documents(self, **kw):
        doc_id = kw["documentIdentifiers"][0]["custom"]["id"]
        return {"documentDetails": [{"status": self.statuses.get(doc_id, "NOT_FOUND")}]}

    def list_knowledge_base_documents(self, **kw):
        return {"documentDetails": [{"identifier": {"custom": {"id": i}}, "status": s} for i, s in self.listing]}


@pytest.fixture
def env(files_table, file_repository, assistants_table, s3_bucket, monkeypatch):
    monkeypatch.setenv("ATTACHMENT_KB_PER_USER_MAX_BYTES", str(100 * MB))
    s3 = boto3.client("s3", region_name=REGION)
    backend, agent, sent = FakeBackend(), FakeAgent(), []
    activity: Dict[str, Optional[datetime]] = {"at": None}

    async def ensure_kb():
        return None

    indexer = ai.Indexer(
        repository=file_repository,
        s3=s3,
        agent_client=agent,
        backend=backend,
        ensure_kb=ensure_kb,
        bytes_table=boto3.resource("dynamodb", region_name=REGION).Table("test-assistants"),
        send_message=lambda body, delay: sent.append((body, delay)),
        session_activity=lambda user, session: activity["at"],
    )

    async def put(row: FileMetadata, with_object: bool = True) -> FileMetadata:
        if with_object:
            s3.put_object(Bucket=row.s3_bucket, Key=row.s3_key, Body=b"%PDF")
        await file_repository.create_file(row)
        return row

    return {"indexer": indexer, "backend": backend, "agent": agent, "sent": sent, "s3": s3, "put": put,
            "repo": file_repository, "activity": activity}


def run(coro):
    return asyncio.run(coro)


def _owner_total(user: str = USER) -> int:
    item = boto3.resource("dynamodb", region_name=REGION).Table("test-assistants").get_item(
        Key={"PK": "AST#attachments", "SK": f"OWNER#{user}"}
    ).get("Item")
    return int(item["totalBytes"]) if item else 0


# ---------------------------------------------------------------------------
# Ids, keys, flag, reserved id
# ---------------------------------------------------------------------------


class TestIds:
    def test_document_id_round_trips(self):
        doc = ai.document_id("u", "s", "up")
        assert doc == "att#u#s#up"
        assert ai.parse_document_id(doc) == ("u", "s", "up")

    @pytest.mark.parametrize("bad", ["", "conv#u#s#1", "att#u#s", "att#u#s#up#x", "att##s#up"])
    def test_foreign_or_malformed_ids_do_not_parse(self, bad):
        assert ai.parse_document_id(bad) is None

    def test_an_id_part_may_not_carry_a_separator(self):
        with pytest.raises(ValueError):
            ai.document_id("u#2", "s", "up")

    @pytest.mark.parametrize(
        "key,expected",
        [
            ("user-files/u/s/up/manual.pdf", ("u", "s", "up")),
            ("user-files/u/s/up/_thumb.png", None),
            ("user-files/u/s/up/_derived/text.jsonl", None),
            ("user-files/u/s/manual.pdf", None),
            ("conversations/u/s/000001.json", None),
        ],
    )
    def test_only_an_original_key_names_an_upload(self, key, expected):
        assert ai.parse_original_key(key) == expected

    def test_the_kb_id_is_the_reserved_one(self):
        from apis.shared.kb_backend.reserved import ATTACHMENTS_KB_ID, is_reserved_kb_id

        assert ai.KB_ID == ATTACHMENTS_KB_ID
        assert is_reserved_kb_id("attachments")

    @pytest.mark.parametrize("value,on", [("", False), ("false", False), ("1", False), ("true", True), (" TRUE ", True)])
    def test_the_flag_is_opt_in(self, monkeypatch, value, on):
        from apis.shared.feature_flags import attachment_kb_enabled

        monkeypatch.setenv("ATTACHMENT_KB_ENABLED", value)
        assert attachment_kb_enabled() is on


class TestRowField:
    def test_kb_index_round_trips_and_never_recreates_a_row(self, file_repository):
        async def go():
            await file_repository.create_file(_row())
            stored = await file_repository.update_file_kb_index(USER, "up-1", {"status": "indexing"})
            missing = await file_repository.update_file_kb_index(USER, "gone", {"status": "indexing"})
            return stored, missing, await file_repository.get_file(USER, "gone")

        stored, missing, ghost = run(go())
        assert stored.kb_index == {"status": "indexing"}
        assert missing is None and ghost is None


# ---------------------------------------------------------------------------
# Byte cap
# ---------------------------------------------------------------------------


class TestByteCap:
    def test_reserved_once_released_once(self, assistants_table):
        table = boto3.resource("dynamodb", region_name=REGION).Table("test-assistants")
        assert ai.reserve_bytes(USER, "up-1", 10 * MB, cap=100 * MB, table=table) is True
        assert ai.reserve_bytes(USER, "up-1", 10 * MB, cap=100 * MB, table=table) is True  # redelivery
        assert _owner_total() == 10 * MB
        assert ai.release_bytes(USER, "up-1", table=table) == 10 * MB
        assert ai.release_bytes(USER, "up-1", table=table) == 0
        assert _owner_total() == 0

    def test_over_the_cap_is_refused_and_counts_nothing(self, assistants_table):
        table = boto3.resource("dynamodb", region_name=REGION).Table("test-assistants")
        assert ai.reserve_bytes(USER, "a", 60 * MB, cap=100 * MB, table=table) is True
        assert ai.reserve_bytes(USER, "b", 50 * MB, cap=100 * MB, table=table) is False
        assert ai.release_bytes(USER, "b", table=table) == 0
        assert _owner_total() == 60 * MB
        assert ai.reserve_bytes("someone-else", "c", 50 * MB, cap=100 * MB, table=table) is True


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


class TestSync:
    def test_an_eligible_upload_is_ingested_by_s3_location_and_polled(self, env):
        run(env["put"](_row()))

        outcome = run(ai.sync_upload(env["indexer"], USER, "up-1"))

        assert outcome == ai.STATUS_INDEXING
        [source] = env["backend"].ingested
        assert source.document_id == "att#user-1#sess-1#up-1"
        assert source.s3_key == "user-files/user-1/sess-1/up-1/manual.pdf"
        assert source.chunks is None
        assert source.metadata == {"user_id": USER, "session_id": SESSION, "upload_id": "up-1"}
        assert env["sent"] == [({"action": "check", "user_id": USER, "upload_id": "up-1", "attempt": 1}, 120)]
        row = run(env["repo"].get_file(USER, "up-1"))
        assert row.kb_index["status"] == ai.STATUS_INDEXING
        assert _owner_total() == 38 * MB

    def test_already_indexed_is_left_alone_unless_forced(self, env):
        run(env["put"](_row(kb={"status": "indexed", "indexedAt": "2026-10-01T00:00:00+00:00"})))

        assert run(ai.sync_upload(env["indexer"], USER, "up-1")) == "already_indexed"
        assert env["backend"].ingested == []
        assert run(ai.sync_upload(env["indexer"], USER, "up-1", force=True)) == ai.STATUS_INDEXING
        assert len(env["backend"].ingested) == 1

    def test_over_the_cap_is_skipped_and_never_ingested(self, env, monkeypatch):
        monkeypatch.setenv("ATTACHMENT_KB_PER_USER_MAX_BYTES", str(10 * MB))
        run(env["put"](_row()))

        assert run(ai.sync_upload(env["indexer"], USER, "up-1")) == ai.STATUS_SKIPPED_CAP
        assert env["backend"].ingested == []
        assert run(env["repo"].get_file(USER, "up-1")).kb_index["status"] == ai.STATUS_SKIPPED_CAP

    def test_an_unprepared_upload_touches_nothing(self, env):
        run(env["put"](_row(prepared=False)))

        assert run(ai.sync_upload(env["indexer"], USER, "up-1")) == "not_eligible"
        assert env["backend"].ingested == env["backend"].deleted == []

    def test_a_deleted_upload_is_removed_and_its_bytes_released(self, env):
        run(env["put"](_row()))
        run(ai.sync_upload(env["indexer"], USER, "up-1"))
        run(env["repo"].delete_file(USER, "up-1"))

        outcome = run(ai.sync_upload(env["indexer"], USER, "up-1", session_id=SESSION))

        assert outcome == ai.STATUS_REMOVED
        assert env["backend"].deleted == ["att#user-1#sess-1#up-1"]
        assert _owner_total() == 0

    def test_an_original_deleted_during_ingest_is_removed(self, env):
        row = run(env["put"](_row()))
        s3 = env["s3"]
        original_ingest = env["backend"].ingest_documents

        async def ingest_then_delete(kb_ref, sources):
            await original_ingest(kb_ref, sources)
            s3.delete_object(Bucket=row.s3_bucket, Key=row.s3_key)

        env["backend"].ingest_documents = ingest_then_delete

        assert run(ai.sync_upload(env["indexer"], USER, "up-1")) == ai.STATUS_REMOVED
        assert env["backend"].deleted == ["att#user-1#sess-1#up-1"]
        assert _owner_total() == 0


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


class TestCheck:
    def _indexing(self, env):
        run(env["put"](_row()))
        run(ai.sync_upload(env["indexer"], USER, "up-1"))
        env["sent"].clear()

    def test_indexed_settles_the_row(self, env):
        self._indexing(env)
        env["agent"].statuses["att#user-1#sess-1#up-1"] = "INDEXED"

        assert run(ai.check_upload(env["indexer"], USER, "up-1", 1)) == ai.STATUS_INDEXED
        kb = run(env["repo"].get_file(USER, "up-1")).kb_index
        assert kb["status"] == ai.STATUS_INDEXED and kb["indexedAt"]

    def test_in_flight_reschedules_with_the_next_delay(self, env):
        self._indexing(env)
        env["agent"].statuses["att#user-1#sess-1#up-1"] = "TEXT_INDEXED"

        assert run(ai.check_upload(env["indexer"], USER, "up-1", 2)) == "rescheduled"
        assert env["sent"] == [({"action": "check", "user_id": USER, "upload_id": "up-1", "attempt": 3}, 300)]

    def test_text_indexed_marks_the_row_searchable_once_and_keeps_polling(self, env):
        # Measured on dev: TEXT_INDEXED at +80 s, INDEXED at +10 m 47 s on a
        # 500-page PDF. Search works from the first; `indexed` waits for the second.
        self._indexing(env)
        env["agent"].statuses["att#user-1#sess-1#up-1"] = "TEXT_INDEXED"

        assert run(ai.check_upload(env["indexer"], USER, "up-1", 1)) == "rescheduled"
        row = run(env["repo"].get_file(USER, "up-1"))
        first = row.kb_index["textIndexedAt"]
        assert row.kb_index["status"] == ai.STATUS_INDEXING and first
        assert ai.is_text_indexed(row) and ai.search_status(row, kb_enabled=True) == ai.SEARCH_SEARCHABLE

        run(ai.check_upload(env["indexer"], USER, "up-1", 2))
        assert run(env["repo"].get_file(USER, "up-1")).kb_index["textIndexedAt"] == first

        env["agent"].statuses["att#user-1#sess-1#up-1"] = "INDEXED"
        run(ai.check_upload(env["indexer"], USER, "up-1", 3))
        kb = run(env["repo"].get_file(USER, "up-1")).kb_index
        assert kb["status"] == ai.STATUS_INDEXED and "textIndexedAt" not in kb

    def test_a_re_index_starts_over_without_the_text_indexed_mark(self, env):
        self._indexing(env)
        env["agent"].statuses["att#user-1#sess-1#up-1"] = "TEXT_INDEXED"
        run(ai.check_upload(env["indexer"], USER, "up-1", 1))

        run(ai.sync_upload(env["indexer"], USER, "up-1", force=True))
        row = run(env["repo"].get_file(USER, "up-1"))
        assert not ai.is_text_indexed(row) and ai.search_status(row, kb_enabled=True) == ai.SEARCH_INDEXING

    def test_in_flight_past_the_last_check_is_left_to_the_reconcile(self, env):
        self._indexing(env)
        env["agent"].statuses["att#user-1#sess-1#up-1"] = "IN_PROGRESS"

        assert run(ai.check_upload(env["indexer"], USER, "up-1", len(ai.CHECK_DELAYS_SECONDS))) == "still_in_flight"
        assert env["sent"] == []

    def test_failed_removes_the_document_and_returns_its_bytes(self, env):
        self._indexing(env)
        env["agent"].statuses["att#user-1#sess-1#up-1"] = "FAILED"

        assert run(ai.check_upload(env["indexer"], USER, "up-1", 1)) == ai.STATUS_FAILED
        assert env["backend"].deleted == ["att#user-1#sess-1#up-1"]
        assert _owner_total() == 0
        kb = run(env["repo"].get_file(USER, "up-1")).kb_index
        assert kb == {**kb, "status": ai.STATUS_FAILED, "error": "FAILED"}


# ---------------------------------------------------------------------------
# Eviction and the daily reconcile
# ---------------------------------------------------------------------------


class TestEviction:
    NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)

    def _indexed(self, env, indexed_at: datetime):
        run(env["put"](_row(kb={"status": "indexed", "indexedAt": indexed_at.isoformat()}, created=indexed_at)))
        ai.reserve_bytes(USER, "up-1", 38 * MB, table=env["indexer"].bytes_table)
        return run(env["repo"].get_file(USER, "up-1"))

    def test_a_document_idle_past_the_retention_is_evicted(self, env):
        row = self._indexed(env, self.NOW - timedelta(days=40))
        env["activity"]["at"] = self.NOW - timedelta(days=31)

        assert run(ai.evict_if_inactive(env["indexer"], row, now=self.NOW)) is True
        assert env["backend"].deleted == ["att#user-1#sess-1#up-1"]
        assert _owner_total() == 0
        assert run(env["repo"].get_file(USER, "up-1")).kb_index["status"] == ai.STATUS_EVICTED

    def test_a_recent_turn_keeps_it(self, env):
        row = self._indexed(env, self.NOW - timedelta(days=40))
        env["activity"]["at"] = self.NOW - timedelta(days=5)

        assert run(ai.evict_if_inactive(env["indexer"], row, now=self.NOW)) is False
        assert env["backend"].deleted == []

    def test_a_recent_re_index_keeps_it_even_in_an_old_conversation(self, env):
        row = self._indexed(env, self.NOW - timedelta(days=1))
        env["activity"]["at"] = self.NOW - timedelta(days=200)

        assert run(ai.evict_if_inactive(env["indexer"], row, now=self.NOW)) is False


class TestReconcile:
    NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)

    def test_orphans_are_removed_stale_statuses_settled_and_idle_documents_evicted(self, env):
        recent = (self.NOW - timedelta(days=2)).isoformat()
        run(env["put"](_row("settle", kb={"status": "indexing", "submittedAt": recent})))
        idle_at = self.NOW - timedelta(days=45)
        run(env["put"](_row("idle", kb={"status": "indexed", "indexedAt": idle_at.isoformat()}, created=idle_at)))
        env["activity"]["at"] = self.NOW - timedelta(days=40)
        env["agent"].listing = [
            ("att#user-1#sess-1#orphan", "INDEXED"),
            ("att#user-1#sess-1#settle", "INDEXED"),
            ("att#user-1#sess-1#idle", "INDEXED"),
            ("att#user-1#sess-1#leaving", "DELETING"),
            ("conv#someone", "INDEXED"),
        ]

        counts = run(ai.reconcile(env["indexer"], now=self.NOW))

        assert counts["documents"] == 4  # the foreign id is never ours to touch
        assert counts["removed"] == 1 and counts["settled"] == 1 and counts["evicted"] == 1
        assert sorted(env["backend"].deleted) == ["att#user-1#sess-1#idle", "att#user-1#sess-1#orphan"]
        assert run(env["repo"].get_file(USER, "settle")).kb_index["status"] == ai.STATUS_INDEXED
        assert run(env["repo"].get_file(USER, "idle")).kb_index["status"] == ai.STATUS_EVICTED

    def test_text_indexed_left_by_the_checks_is_marked_searchable(self, env):
        recent = (self.NOW - timedelta(days=1)).isoformat()
        run(env["put"](_row("slow", kb={"status": "indexing", "submittedAt": recent})))
        env["activity"]["at"] = self.NOW - timedelta(days=1)
        env["agent"].listing = [("att#user-1#sess-1#slow", "TEXT_INDEXED")]

        counts = run(ai.reconcile(env["indexer"], now=self.NOW))

        row = run(env["repo"].get_file(USER, "slow"))
        assert counts["settled"] == 1 and row.kb_index["status"] == ai.STATUS_INDEXING
        assert ai.search_status(row, kb_enabled=True) == ai.SEARCH_SEARCHABLE

    def test_no_knowledge_base_means_nothing_to_do(self, env):
        from apis.shared.kb_backend.managed_backend import ManagedKbNotProvisioned

        def not_there(kb_ref):
            raise ManagedKbNotProvisioned("no")

        env["backend"]._locate = not_there
        assert run(ai.reconcile(env["indexer"]))["documents"] == 0


# ---------------------------------------------------------------------------
# Re-index on conversation open
# ---------------------------------------------------------------------------


class TestReindexOnOpen:
    def test_only_evicted_eligible_documents_qualify(self):
        files = [
            _row("a", kb={"status": "evicted"}),
            _row("b", kb={"status": "indexed"}),
            _row("c", kb={"status": "evicted"}, prepared=False),
            _row("d"),
        ]
        assert ai.evicted_upload_ids(files) == ["a"]

    def test_the_caller_s_evicted_documents_are_queued(self, file_repository, monkeypatch):
        from apis.app_api.files import attachment_reindex

        monkeypatch.setenv("ATTACHMENT_KB_ENABLED", "true")
        monkeypatch.setenv("LARGE_DOCUMENT_PREPARE_QUEUE_URL", "https://sqs.invalid/q")
        sent: List[tuple] = []
        monkeypatch.setattr(attachment_reindex, "_send_all", lambda url, user, ids: sent.append((url, user, ids)))

        async def go():
            await file_repository.create_file(_row("mine", kb={"status": "evicted"}))
            await file_repository.create_file(_row("theirs", kb={"status": "evicted"}, user="user-2"))
            # The fixture's repository, not the process-wide singleton: an earlier
            # test may have built that against another table.
            return await attachment_reindex.requeue_evicted_attachments(SESSION, USER, repository=file_repository)

        assert run(go()) == 1
        assert sent == [("https://sqs.invalid/q", USER, ["mine"])]

    def test_nothing_while_the_flag_is_off(self, monkeypatch):
        from apis.app_api.files import attachment_reindex

        monkeypatch.delenv("ATTACHMENT_KB_ENABLED", raising=False)
        assert run(attachment_reindex.requeue_evicted_attachments(SESSION, USER)) == 0
