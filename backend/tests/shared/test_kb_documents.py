"""The knowledge-base document helpers shared by full context and ``project_files``.

Moto-backed, like ``test_kb_full_context.py``: the listing paginates a real
(fake) Query, and the text comes from the documents bucket through the same
extraction the indexer and ``document_read`` use, tracked changes included.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import boto3
import pytest

from apis.shared.assistants import kb_documents as kd
from apis.shared.assistants.kb_access import granted
from tests.shared.docx_fixtures import build_docx, deleted, inserted, paragraph, run

REGION = "us-east-1"
TABLE = "test-assistants"
BUCKET = "test-rag-documents"
HARNESS = "ast-harness-0001"


@pytest.fixture
def kb(aws, monkeypatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME", BUCKET)
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)
    kd.reset_text_cache_for_tests()
    yield _Kb()
    kd.reset_text_cache_for_tests()


class _Kb:
    def __init__(self) -> None:
        self.table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
        self.s3 = boto3.client("s3", region_name=REGION)

    def add(self, doc_id, filename, body, *, status="complete", content_type="text/plain", assistant_id=HARNESS):
        key = f"assistants/{assistant_id}/documents/{doc_id}/{filename}"
        raw = body.encode() if isinstance(body, str) else body
        self.s3.put_object(Bucket=BUCKET, Key=key, Body=raw)
        item = {
            "PK": f"AST#{assistant_id}", "SK": f"DOC#{doc_id}",
            "documentId": doc_id, "assistantId": assistant_id, "filename": filename,
            "contentType": content_type, "sizeBytes": len(raw), "s3Key": key, "status": status,
            "createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-01T00:00:00Z",
        }
        self.table.put_item(Item=item)
        return item


class TestListing:
    def test_lists_exactly_this_assistants_documents_whatever_their_status(self, kb):
        kb.add("d1", "a.txt", "alpha")
        kb.add("d2", "b.txt", "beta", status="processing")
        kb.add("x1", "other.txt", "theirs", assistant_id="ast-other")

        docs = kd.list_kb_documents(HARNESS)

        assert sorted(d["documentId"] for d in docs) == ["d1", "d2"]
        assert {d["status"] for d in docs} == {"complete", "processing"}

    def test_the_listing_follows_pagination(self, kb, monkeypatch):
        for i in range(5):
            kb.add(f"d{i}", f"f{i}.txt", "x")
        real = kb.table.query
        pages = []

        def _one_at_a_time(**kwargs):
            response = real(Limit=1, **kwargs)
            pages.append(1)
            return response

        monkeypatch.setattr("apis.shared.aws_clients.get_dynamodb_table", lambda *a, **k: type("T", (), {"query": staticmethod(_one_at_a_time)})())
        assert len(kd.list_kb_documents(HARNESS)) == 5
        assert len(pages) >= 5

    def test_the_table_must_be_configured(self, kb, monkeypatch):
        monkeypatch.delenv("DYNAMODB_ASSISTANTS_TABLE_NAME")
        with pytest.raises(RuntimeError, match="DYNAMODB_ASSISTANTS_TABLE_NAME"):
            kd.list_kb_documents(HARNESS)


class TestText:
    def test_a_word_files_tracked_changes_read_as_the_indexer_renders_them(self, kb, monkeypatch):
        monkeypatch.setenv("WORD_TRACKED_CHANGES_ENABLED", "true")
        docx = build_docx(paragraph(run("Payment is due "), deleted("in 30 days"), inserted("within 90 days"), run(".")))
        doc = kb.add("d1", "redline.docx", docx,
                     content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

        text = kd.kb_document_text(doc, "docx", 1_000_000)

        assert text is not None
        assert "[deleted: in 30 days]" in text and "[inserted: within 90 days]" in text

    def test_with_the_flag_off_a_word_file_reads_flattened(self, kb, monkeypatch):
        monkeypatch.setenv("WORD_TRACKED_CHANGES_ENABLED", "false")
        docx = build_docx(paragraph(run("Payment is due "), deleted("in 30 days"), inserted("within 90 days"), run(".")))
        doc = kb.add("d1", "redline.docx", docx,
                     content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

        text = kd.kb_document_text(doc, "docx", 1_000_000)

        assert text == "Payment is due within 90 days."

    def test_the_text_is_downloaded_once_per_process_and_per_limit(self, kb):
        doc = kb.add("d1", "a.txt", "alpha")
        assert kd.kb_document_text(doc, "text", 100) == "alpha"
        with patch("apis.shared.aws_clients.get_client", side_effect=AssertionError("downloaded again")):
            assert kd.kb_document_text(doc, "text", 100) == "alpha"
            with pytest.raises(AssertionError):
                kd.kb_document_text(doc, "text", 200)  # a different budget is a different answer

    def test_a_changed_document_is_a_new_cache_entry(self, kb):
        doc = kb.add("d1", "a.txt", "alpha")
        assert kd.kb_document_text(doc, "text", 100) == "alpha"
        kb.s3.put_object(Bucket=BUCKET, Key=doc["s3Key"], Body=b"beta")
        updated = {**doc, "updatedAt": "2026-10-02T00:00:00Z"}
        assert kd.kb_document_text(updated, "text", 100) == "beta"

    def test_over_the_limit_or_empty_is_none(self, kb):
        over = kb.add("d1", "long.txt", "x" * 500)
        empty = kb.add("d2", "empty.txt", "   ")
        assert kd.kb_document_text(over, "text", 100) is None
        assert kd.kb_document_text(empty, "text", 100) is None

    def test_the_cache_is_bounded(self, kb, monkeypatch):
        monkeypatch.setattr(kd, "TEXT_CACHE_MAX_ENTRIES", 2)
        docs = [kb.add(f"d{i}", f"f{i}.txt", f"text {i}") for i in range(3)]
        for doc in docs:
            kd.kb_document_text(doc, "text", 100)
        assert len(kd._text_cache) == 2

    def test_the_bucket_must_be_configured(self, kb, monkeypatch):
        doc = kb.add("d1", "a.txt", "alpha")
        monkeypatch.delenv("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME")
        with pytest.raises(RuntimeError, match="S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME"):
            kd.kb_document_text(doc, "text", 100)


class TestEndToEndThroughTheTool:
    """``project_files`` over the real helpers: a member reads a project file whole."""

    def test_a_member_reads_a_redlined_submission_by_name(self, kb, monkeypatch):
        from agents.builtin_tools.project_files_tool import make_project_files_tool
        from apis.shared.auth.models import User

        monkeypatch.setenv("WORD_TRACKED_CHANGES_ENABLED", "true")
        member = User(user_id="u-m", email="m@example.edu", name="M", roles=["default"])
        monkeypatch.setattr(
            "apis.shared.assistants.kb_access.resolve_kb_access",
            AsyncMock(return_value=granted(HARNESS, member.user_id, "viewer")),
        )
        monkeypatch.setattr("agents.builtin_tools.project_files_tool.member_emails", lambda project_id: {})
        kb.add("d7", "submission-07.docx",
               build_docx(paragraph(run("Thesis: "), deleted("weak"), inserted("clear"), run("."))),
               content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        kb.add("d8", "submission-08.docx", b"not ready", status="processing",
               content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

        tool = make_project_files_tool("prj_1", HARNESS, member)
        listing = asyncio.run(tool())["content"][0]["json"]
        read = asyncio.run(tool(filename="Submission-07.docx"))["content"][0]["json"]

        assert [(f["filename"], f["status"]) for f in listing["files"]] == [
            ("submission-07.docx", "ready"), ("submission-08.docx", "not ready (processing)"),
        ]
        assert read["format"] == "docx"
        assert "[deleted: weak][inserted: clear]" in read["content"]
