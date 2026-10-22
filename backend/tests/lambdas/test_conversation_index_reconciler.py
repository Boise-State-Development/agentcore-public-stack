"""The daily conversation-index reconciler (docs/specs/conversation-search.md §3, PR-2c).

S3 and DynamoDB are moto; Bedrock is a fake that holds a document store and
pages ``ListKnowledgeBaseDocuments``. The assertions are about what is left in
the archive and the knowledge base afterwards.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import boto3
import pytest

from apis.app_api.conversation_index import reconciler as rc
from apis.shared.conversation_archive.documents import ArchivedTurn
from apis.shared.kb_backend import records as r
from apis.shared.kb_backend.reserved import CONVERSATIONS_KB_ID

REGION = "us-east-1"
BUCKET = "test-conversation-archive"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


class FakeBedrockAgent:
    def __init__(self, page_size: int = 3) -> None:
        self.documents: Dict[str, str] = {}
        self.delete_calls: List[List[str]] = []
        self.list_calls = 0
        self.page_size = page_size
        self.fail_list: Optional[Exception] = None

    def add(self, *document_ids: str, status: str = "INDEXED") -> None:
        for document_id in document_ids:
            self.documents[document_id] = status

    def list_knowledge_base_documents(self, **kwargs):
        self.list_calls += 1
        if self.fail_list is not None:
            raise self.fail_list
        assert kwargs["knowledgeBaseId"] == "KBCONV0001"
        assert kwargs["dataSourceId"] == "DSCONV0001"
        # A managed knowledge base rejects anything above 100.
        assert kwargs["maxResults"] <= 100
        ids = sorted(self.documents)
        start = int(kwargs.get("nextToken") or 0)
        page = ids[start : start + self.page_size]
        out: Dict[str, Any] = {
            "documentDetails": [
                {
                    "identifier": {"dataSourceType": "CUSTOM", "custom": {"id": document_id}},
                    "status": self.documents[document_id],
                }
                for document_id in page
            ]
        }
        if start + self.page_size < len(ids):
            out["nextToken"] = str(start + self.page_size)
        return out

    def delete_knowledge_base_documents(self, **kwargs):
        ids = [identifier["custom"]["id"] for identifier in kwargs["documentIdentifiers"]]
        assert len(ids) <= 10
        self.delete_calls.append(ids)
        for document_id in ids:
            self.documents.pop(document_id, None)
        return {"documentDetails": []}


@pytest.fixture()
def env(assistants_table, monkeypatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    monkeypatch.delenv("CONVERSATION_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("CONVERSATION_INDEX_ENABLED", raising=False)
    monkeypatch.setenv("CONVERSATION_RECONCILER_DELETE_PAUSE_SECONDS", "0")
    boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
    fake = FakeBedrockAgent()
    monkeypatch.setattr(rc, "bedrock_agent_client", lambda: fake)
    return fake


def _provisioned():
    boto3.resource("dynamodb", region_name=REGION).Table("test-assistants").put_item(
        Item={
            "PK": r.kb_pk(CONVERSATIONS_KB_ID),
            "SK": r.kb_sk(CONVERSATIONS_KB_ID),
            "awsKbId": "KBCONV0001",
            "awsDataSourceId": "DSCONV0001",
        }
    )


def _archive(session_id: str, index: int, user_id: str = "user-a") -> str:
    turn = ArchivedTurn(
        user_id=user_id,
        session_id=session_id,
        message_index=index,
        user_text="hello",
        assistant_text="hi",
        created_at="2026-10-01T00:00:00Z",
    )
    boto3.client("s3", region_name=REGION).put_object(Bucket=BUCKET, Key=turn.key, Body=turn.to_json())
    return turn.key


def _keys() -> List[str]:
    page = boto3.client("s3", region_name=REGION).list_objects_v2(Bucket=BUCKET)
    return sorted(obj["Key"] for obj in page.get("Contents", []))


def _run(env, *, now=NOW, dry_run=False, sleeps=None):
    return rc.reconcile(
        bucket=BUCKET,
        s3=boto3.client("s3", region_name=REGION),
        client=env,
        now=now,
        dry_run=dry_run,
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )


class TestOrphanDocuments:
    def test_document_without_an_object_is_deleted(self, env):
        _provisioned()
        _archive("s1", 0)
        env.add("conv#user-a#s1#0", "conv#user-a#gone#0", "conv#user-a#gone#2")

        report = _run(env)

        assert sorted(env.documents) == ["conv#user-a#s1#0"]
        assert report.orphan_documents == 2
        assert report.orphan_documents_deleted == 2
        assert report.aborted is None

    def test_runs_whatever_the_index_flag_says(self, env, monkeypatch):
        """§4 "Turning the index off": the reconciler is the cleanup for that time."""
        monkeypatch.setenv("CONVERSATION_INDEX_ENABLED", "false")
        _provisioned()
        _archive("s1", 0)
        env.add("conv#user-a#s1#0", "conv#user-a#deleted-while-off#0")

        _run(env)

        assert sorted(env.documents) == ["conv#user-a#s1#0"]

    def test_deletes_ten_at_a_time_one_call_in_flight_with_a_pause(self, env, monkeypatch):
        monkeypatch.setenv("CONVERSATION_RECONCILER_DELETE_PAUSE_SECONDS", "1.5")
        _provisioned()
        _archive("keep", 0)
        env.add("conv#user-a#keep#0", *[f"conv#user-a#gone#{i}" for i in range(23)])
        sleeps: List[float] = []

        _run(env, sleeps=sleeps)

        assert [len(call) for call in env.delete_calls] == [10, 10, 3]
        assert sleeps == [1.5, 1.5]

    def test_per_run_cap(self, env, monkeypatch):
        monkeypatch.setenv("CONVERSATION_RECONCILER_MAX_DOCUMENT_DELETES", "5")
        _provisioned()
        _archive("keep", 0)
        env.add("conv#user-a#keep#0", *[f"conv#user-a#gone#{i}" for i in range(8)])

        report = _run(env)

        assert report.orphan_documents == 8
        assert report.orphan_documents_deleted == 5
        assert report.limit_reached is True
        assert len(env.documents) == 1 + 3

    def test_documents_already_deleting_are_left_alone(self, env):
        _provisioned()
        _archive("s1", 0)
        env.add("conv#user-a#s1#0")
        env.add("conv#user-a#gone#0", status="DELETE_IN_PROGRESS")

        report = _run(env)

        assert env.delete_calls == []
        assert report.documents_already_deleting == 1

    def test_deleted_documents_listed_as_not_found_are_not_orphans(self, env):
        """A deleted document lingers in the listing as NOT_FOUND."""
        _provisioned()
        _archive("s1", 0)
        env.add("conv#user-a#s1#0")
        env.add("conv#user-a#deleted-yesterday#0", status="NOT_FOUND")

        report = _run(env)

        assert env.delete_calls == []
        assert report.orphan_documents == 0
        assert report.documents_already_deleted == 1

    def test_first_format_documents_are_deleted_and_current_ones_kept(self, env):
        """``conv#{session}#{index}`` predates the user in the id; nothing writes it
        any more, so every such document is an orphan, even when its turn exists."""
        _provisioned()
        _archive("s1", 0)
        env.add("conv#user-a#s1#0", "conv#s1#0", "conv#s1#2")

        report = _run(env)

        assert sorted(env.documents) == ["conv#user-a#s1#0"]
        assert report.legacy_documents == 2
        assert report.orphan_documents == 2

    def test_two_users_sharing_a_session_id_each_keep_their_document(self, env):
        _provisioned()
        _archive("shared", 0, user_id="user-a")
        _archive("shared", 0, user_id="user-b")
        env.add("conv#user-a#shared#0", "conv#user-b#shared#0", "conv#user-c#shared#0")

        _run(env)

        assert sorted(env.documents) == ["conv#user-a#shared#0", "conv#user-b#shared#0"]

    def test_ids_it_did_not_mint_are_never_touched(self, env):
        _provisioned()
        _archive("s1", 0)
        env.add("conv#user-a#s1#0", "some-other-document", "conv#bad", "conv#u#s#x")

        _run(env)

        assert env.delete_calls == []

    def test_dry_run_reports_and_deletes_nothing(self, env):
        _provisioned()
        _archive("s1", 0)
        env.add("conv#user-a#s1#0", "conv#user-a#gone#0")

        report = _run(env, dry_run=True)

        assert report.orphan_documents == 1
        assert report.orphan_documents_deleted == 0
        assert "conv#user-a#gone#0" in env.documents

    def test_no_knowledge_base_means_nothing_to_list_or_delete(self, env):
        _archive("s1", 0)

        report = _run(env)

        assert report.kb_provisioned is False
        assert env.list_calls == 0
        assert env.delete_calls == []


class TestExpiry:
    def test_objects_past_retention_go_and_their_documents_with_them(self, env, monkeypatch):
        monkeypatch.setenv("CONVERSATION_RETENTION_DAYS", "30")
        _provisioned()
        _archive("old", 0)
        _archive("old", 2)
        env.add("conv#user-a#old#0", "conv#user-a#old#2")

        # moto stamps LastModified with the real clock, so run 31 days ahead.
        report = _run(env, now=datetime.now(timezone.utc) + timedelta(days=31))

        assert _keys() == []
        # In the same pass, not left to the consumer (which may be off).
        assert env.documents == {}
        assert report.expired_objects == 2
        assert report.expired_objects_deleted == 2
        assert report.orphan_documents_deleted == 2

    def test_young_objects_stay(self, env):
        _provisioned()
        key = _archive("s1", 0)
        env.add("conv#user-a#s1#0")

        report = _run(env, now=datetime.now(timezone.utc))

        assert _keys() == [key]
        assert report.expired_objects == 0
        assert env.documents == {"conv#user-a#s1#0": "INDEXED"}

    def test_dry_run_counts_expiry_without_deleting(self, env, monkeypatch):
        monkeypatch.setenv("CONVERSATION_RETENTION_DAYS", "1")
        _provisioned()
        key = _archive("s1", 0)
        env.add("conv#user-a#s1#0")

        report = _run(env, now=datetime.now(timezone.utc) + timedelta(days=2), dry_run=True)

        assert _keys() == [key]
        assert report.expired_objects == 1
        assert report.orphan_documents == 1  # what a live run would delete
        assert env.delete_calls == []

    def test_bad_retention_setting_refuses_to_run(self, env, monkeypatch):
        monkeypatch.setenv("CONVERSATION_RETENTION_DAYS", "30.5")
        with pytest.raises(ValueError):
            _run(env)


class TestSafety:
    def test_knowledge_base_is_listed_before_the_archive(self, env, monkeypatch):
        """A turn archived and ingested between the two listings must not look orphaned."""
        _provisioned()
        order: List[str] = []
        real_list_docs = rc.list_document_ids
        real_list_archive = rc.list_archive
        monkeypatch.setattr(rc, "list_document_ids", lambda *a: (order.append("kb"), real_list_docs(*a))[1])
        monkeypatch.setattr(rc, "list_archive", lambda *a: (order.append("archive"), real_list_archive(*a))[1])
        _archive("s1", 0)
        env.add("conv#user-a#s1#0")

        _run(env)

        assert order == ["kb", "archive"]

    def test_a_failed_document_listing_deletes_nothing(self, env):
        _provisioned()
        _archive("s1", 0)
        env.add("conv#user-a#gone#0")
        env.fail_list = RuntimeError("throttled")

        report = _run(env)

        assert report.aborted
        assert env.delete_calls == []

    def test_a_failed_archive_listing_deletes_nothing(self, env, monkeypatch):
        _provisioned()
        env.add("conv#user-a#s1#0")

        def boom(*_a):
            raise RuntimeError("AccessDenied")

        monkeypatch.setattr(rc, "list_archive", boom)
        report = _run(env)

        assert report.aborted
        assert env.delete_calls == []

    def test_an_empty_archive_beside_a_full_index_deletes_nothing(self, env):
        """A wrong bucket name or a regressed grant must not empty the index."""
        _provisioned()
        env.add("conv#user-a#s1#0", "conv#user-a#s2#0")

        report = _run(env)

        assert report.aborted
        assert sorted(env.documents) == ["conv#user-a#s1#0", "conv#user-a#s2#0"]

    def test_handler_raises_on_abort_so_the_error_alarm_fires(self, env):
        _provisioned()
        env.add("conv#user-a#s1#0")
        with pytest.raises(RuntimeError, match="aborted"):
            rc.lambda_handler({}, None)

    def test_handler_event_can_only_make_a_run_safer(self, env):
        _provisioned()
        _archive("s1", 0)
        env.add("conv#user-a#s1#0", "conv#user-a#gone#0")

        result = rc.lambda_handler({"dryRun": True}, None)

        assert result["mode"] == "dry-run"
        assert "conv#user-a#gone#0" in env.documents

    def test_handler_live_by_default(self, env):
        _provisioned()
        _archive("s1", 0)
        env.add("conv#user-a#s1#0", "conv#user-a#gone#0")

        result = rc.lambda_handler({}, None)

        assert result["mode"] == "live"
        assert result["orphanDocumentsDeleted"] == 1
