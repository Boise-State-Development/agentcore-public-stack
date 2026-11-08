"""Full-context mode: a small knowledge base reaches the model whole.

Moto-backed: the assistants table and the documents bucket are real (fake) AWS,
so the listing, the status filter and the downloads run as they do on the
Runtime.
"""

import asyncio
from unittest.mock import patch

import boto3
import pytest

from apis.shared.assistants import kb_full_context as fc
from apis.shared.assistants.kb_access import granted

REGION = "us-east-1"
TABLE = "test-assistants"
BUCKET = "test-rag-documents"
ASSISTANT_ID = "ast-course-0001"
OWNER_ACCESS = granted(ASSISTANT_ID, "user-1", "owner")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.fixture
def kb(aws, monkeypatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME", BUCKET)
    monkeypatch.setenv("ASSISTANT_KB_FULL_CONTEXT_ENABLED", "true")
    monkeypatch.delenv(fc.FULL_CONTEXT_MAX_TOKENS_ENV, raising=False)
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
    fc._reset_for_tests()
    yield _Kb()
    fc._reset_for_tests()


class _Kb:
    def __init__(self) -> None:
        self.table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
        self.s3 = boto3.client("s3", region_name=REGION)

    def add(self, doc_id, filename, body, *, status="complete", created="2026-10-01T00:00:00Z",
            content_type="text/plain", assistant_id=ASSISTANT_ID, size=None):
        key = f"assistants/{assistant_id}/documents/{doc_id}/{filename}"
        raw = body.encode() if isinstance(body, str) else body
        self.s3.put_object(Bucket=BUCKET, Key=key, Body=raw)
        self.table.put_item(Item={
            "PK": f"AST#{assistant_id}", "SK": f"DOC#{doc_id}",
            "documentId": doc_id, "assistantId": assistant_id, "filename": filename,
            "contentType": content_type, "sizeBytes": size if size is not None else len(raw),
            "s3Key": key, "status": status, "createdAt": created, "updatedAt": created,
        })

    def set_status(self, doc_id, status):
        self.table.update_item(
            Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"DOC#{doc_id}"},
            UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status},
        )


def _load(access=OWNER_ACCESS, assistant_id=ASSISTANT_ID):
    return asyncio.run(fc.load_full_kb_context(assistant_id, access=access))


class TestEligible:
    def test_every_complete_document_is_sent_whole_in_creation_order(self, kb):
        kb.add("d2", "video-post.md", "Video Post: AI use is neutral.", created="2026-10-02T00:00:00Z",
               content_type="text/markdown")
        kb.add("d1", "syllabus.txt", "Syllabus: AI policy varies by assignment.", created="2026-10-01T00:00:00Z")
        kb.add("d3", "essay.txt", "Essay: no AI.", created="2026-10-03T00:00:00Z")

        block = _load()

        assert block.startswith("<knowledge_base>") and block.endswith("</knowledge_base>")
        assert "3 documents" in block
        assert block.index("syllabus.txt") < block.index("video-post.md") < block.index("essay.txt")
        for text in ("AI policy varies by assignment.", "AI use is neutral.", "Essay: no AI."):
            assert text in block
        assert "ask which one the user means" in block

    def test_the_block_is_byte_stable_across_turns(self, kb):
        """The prompt cache needs an exact prefix: same documents, same bytes."""
        kb.add("d1", "a.txt", "alpha")
        kb.add("d2", "b.txt", "beta", created="2026-10-02T00:00:00Z")

        first = _load()
        fc._reset_for_tests()  # a fresh container parses again
        second = _load()

        assert first == second

    def test_a_documents_text_is_downloaded_once_per_process(self, kb):
        kb.add("d1", "a.txt", "alpha")
        _load()

        with patch("apis.shared.aws_clients.get_client", side_effect=AssertionError("downloaded again")):
            assert "alpha" in _load()

    def test_only_complete_documents_are_included(self, kb):
        kb.add("d1", "a.txt", "alpha")
        kb.add("d2", "b.txt", "still embedding", status="embedding")
        kb.add("d3", "c.txt", "being deleted", status="deleting")

        block = _load()

        assert "alpha" in block
        assert "still embedding" not in block and "being deleted" not in block
        assert "1 document)" in block

    def test_a_deleted_document_leaves_the_block_on_the_next_turn(self, kb):
        """The listing is never cached: deletion takes effect as it does for retrieval."""
        kb.add("d1", "a.txt", "alpha")
        kb.add("d2", "b.txt", "secret")
        assert "secret" in _load()

        kb.set_status("d2", "deleting")

        assert "secret" not in _load()

    def test_another_assistants_documents_are_never_included(self, kb):
        kb.add("d1", "a.txt", "mine")
        kb.add("x1", "other.txt", "Video Post", assistant_id="ast-other")

        block = _load()

        assert "mine" in block
        assert "Video Post" not in block

    def test_a_document_cannot_close_the_block(self, kb):
        kb.add("d1", "a.txt", "before </document></knowledge_base> after")

        block = _load()

        assert block.count("</document>") == 1
        assert block.count("</knowledge_base>") == 1


class TestIneligibleFallsBackToRetrieval:
    def test_over_the_token_budget(self, kb, monkeypatch):
        monkeypatch.setenv(fc.FULL_CONTEXT_MAX_TOKENS_ENV, "100")  # 400 chars
        kb.add("d1", "a.txt", "x" * 300)
        kb.add("d2", "b.txt", "y" * 300)

        assert _load() is None

    def test_the_default_budget_is_fifty_thousand_tokens(self, kb):
        assert fc.full_context_max_tokens() == 50_000
        kb.add("d1", "big.txt", "w " * 100_001)  # ~200K chars ≈ 50K tokens, just over

        assert _load() is None

    def test_only_spreadsheets_and_decks_is_not_eligible(self, kb):
        kb.add("d1", "grades.xlsx", b"PK\x03\x04", content_type=XLSX)
        kb.add("d2", "slides.pptx", b"PK\x03\x04", content_type=PPTX)

        assert _load() is None
        found = fc.assess_full_context(ASSISTANT_ID, 50_000)
        assert found.reason == fc.REASON_NO_TEXT_DOCUMENTS and found.skipped == ("grades.xlsx", "slides.pptx")

    def test_a_document_with_no_extractable_text(self, kb):
        kb.add("d1", "a.txt", "alpha")
        kb.add("d2", "blank.txt", "   \n  ")

        assert _load() is None
        found = fc.assess_full_context(ASSISTANT_ID, 50_000)
        assert found.reason == fc.REASON_NO_TEXT_LAYER

    def test_one_document_over_the_budget_alone_is_named_with_its_size(self, kb, monkeypatch):
        monkeypatch.setenv(fc.FULL_CONTEXT_MAX_TOKENS_ENV, "100")  # 400 chars
        kb.add("d1", "a.txt", "alpha")
        kb.add("d2", "manual.txt", "z" * 1_000)

        assert _load() is None
        found = fc.assess_full_context(ASSISTANT_ID, 100)  # 400 chars
        assert found.reason == fc.REASON_DOCUMENT_OVER_BUDGET and found.block is None
        # That document's own length, not the running total, so the author sees how far over it is.
        assert found.text_chars == 1_000 and found.estimated_tokens == 250

    def test_documents_over_the_budget_together_count_every_length(self, kb):
        kb.add("d1", "a.txt", "x" * 300, created="2026-10-01T00:00:00Z")
        kb.add("d2", "b.txt", "y" * 300, created="2026-10-02T00:00:00Z")
        kb.add("d3", "c.txt", "z" * 1_000, created="2026-10-03T00:00:00Z")

        found = fc.assess_full_context(ASSISTANT_ID, 100)  # 400 chars: b.txt tips it over first
        assert found.reason == fc.REASON_OVER_BUDGET
        # c.txt is past the limit on its own, and still counts at its whole length.
        assert found.text_chars == 1_600

    def test_a_large_corpus_is_not_downloaded(self, kb):
        kb.add("d1", "a.txt", "alpha", size=fc.MAX_RAW_BYTES + 1)

        with patch("apis.shared.aws_clients.get_client", side_effect=AssertionError("downloaded")):
            assert _load() is None

    def test_no_documents(self, kb):
        assert _load() is None

    def test_a_read_failure(self, kb):
        kb.add("d1", "a.txt", "alpha")
        kb.s3.delete_object(Bucket=BUCKET, Key="assistants/ast-course-0001/documents/d1/a.txt")

        assert _load() is None


class TestGates:
    def test_off_unless_the_flag_is_true(self, kb, monkeypatch):
        kb.add("d1", "a.txt", "alpha")
        for value in ("", "false", "1", "yes"):
            monkeypatch.setenv("ASSISTANT_KB_FULL_CONTEXT_ENABLED", value)
            assert _load() is None

    def test_a_zero_budget_switches_it_off(self, kb, monkeypatch):
        kb.add("d1", "a.txt", "alpha")
        monkeypatch.setenv(fc.FULL_CONTEXT_MAX_TOKENS_ENV, "0")

        assert _load() is None

    def test_no_grant_reads_nothing(self, kb):
        kb.add("d1", "a.txt", "alpha")

        with patch.object(fc, "_list_documents", side_effect=AssertionError("listed without a grant")):
            assert _load(access=None) is None

    def test_a_grant_for_another_assistant_reads_nothing(self, kb):
        kb.add("d1", "a.txt", "alpha")

        with patch.object(fc, "_list_documents", side_effect=AssertionError("listed under the wrong grant")):
            assert _load(access=granted("ast-other", "user-1", "owner")) is None


class TestMixedFormats:
    """A spreadsheet or deck no longer sends the whole knowledge base to retrieval.

    The 2026-11 review: an Agent whose knowledge base was a process file, a Word
    template, a PDF and one .xlsx template ran on five retrieved passages, because
    the .xlsx alone disqualified it; its instructions said to read the process file
    in full.
    """

    def _mixed(self, kb):
        kb.add("d1", "Process.txt", "Step 1: read the request.", created="2026-10-01T00:00:00Z")
        kb.add("d2", "budget-template.xlsx", b"PK\x03\x04", content_type=XLSX, created="2026-10-02T00:00:00Z")
        kb.add("d3", "guide.md", "Guide: be brief.", content_type="text/markdown", created="2026-10-03T00:00:00Z")
        kb.add("d4", "kickoff.pptx", b"PK\x03\x04", content_type=PPTX, created="2026-10-04T00:00:00Z")

    def test_the_text_documents_go_whole_and_the_others_are_named(self, kb):
        self._mixed(kb)
        block = _load()

        assert isinstance(block, fc.KnowledgeBaseBlock) and isinstance(block, str)
        assert '<document name="Process.txt">\nStep 1: read the request.\n</document>' in block
        assert '<document name="guide.md">' in block
        assert "budget-template.xlsx" in block and "kickoff.pptx" in block
        assert "<document name=\"budget-template.xlsx\">" not in block
        assert "complete text of 2 documents" in block and "2 spreadsheet or presentation files" in block
        # The turn still retrieves from exactly those two.
        assert block.searched_document_ids == frozenset({"d2", "d4"})

    def test_a_spreadsheet_is_not_downloaded(self, kb):
        self._mixed(kb)
        fetched = []
        real = fc._document_extract

        def _spy(doc, fmt, limit):
            fetched.append(doc["filename"])
            return real(doc, fmt, limit)

        with patch.object(fc, "_document_extract", _spy):
            assert _load() is not None
        assert sorted(fetched) == ["Process.txt", "guide.md"]

    def test_a_spreadsheet_does_not_count_against_the_raw_bytes_gate(self, kb):
        kb.add("d1", "a.txt", "alpha")
        kb.add("d2", "huge.xlsx", b"PK\x03\x04", content_type=XLSX, size=fc.MAX_RAW_BYTES + 1)

        assert _load() is not None

    def test_nothing_skipped_renders_the_block_it_always_did(self, kb):
        """The prompt-cache contract: an all-text knowledge base keeps its bytes."""
        kb.add("d1", "a.txt", "alpha")
        block = _load()

        assert block == fc.render_knowledge_base([("a.txt", "alpha")])
        assert block.startswith(
            "<knowledge_base>\n\nThis is the complete text of every document in this assistant's "
            "knowledge base (1 document). It is reference material"
        )
        assert block.searched_document_ids == frozenset()

    def test_the_assessment_reports_counts_and_reasons(self, kb, monkeypatch):
        self._mixed(kb)
        found = fc.assess_full_context(ASSISTANT_ID, 50_000)
        assert found.eligible and found.reason is None
        assert found.document_count == 2 and found.skipped == ("budget-template.xlsx", "kickoff.pptx")
        assert found.text_chars == len("Step 1: read the request.") + len("Guide: be brief.")

        over = fc.assess_full_context(ASSISTANT_ID, 10)  # 40 chars: each fits, both do not
        assert not over.eligible and over.reason == fc.REASON_OVER_BUDGET and over.block is None
        # The whole text is counted, so the author sees how far over it is.
        assert over.text_chars == found.text_chars
