"""File facts on the prepare Lambda (unified-attachment-delivery spec §4.1), moto S3 + DynamoDB.

* A document under the inline limit (``{"action": "facts"}``) gets the facts
  map and nothing else: no sidecar, no shard, no ``preparation``, no digest
  write and no indexing, so its delivery and KB eligibility are unchanged.
* A reference-class preparation writes the same facts in its final write.
* Per type: pdf, scanned pdf, docx, html, md, txt.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest

from apis.shared.files import file_facts
from apis.shared.files.document_tokens import PDF_PAGE_TOKEN_ESTIMATE, estimate_document_tokens
from apis.shared.files.models import INLINE_DOCUMENT_MAX_BYTES, FileMetadata, FileStatus
from apis.shared.files.repository import FileUploadRepository
from lambdas.large_document_prepare import handler
from tests.shared.test_document_read import build_docx, build_pdf

REGION = "us-east-1"
BUCKET = "test-user-files-bucket"
TABLE = "test-user-files"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

TEXT_PAGES = [f"Section {i} sets out the payment terms for this agreement" for i in range(1, 6)]


@pytest.fixture
def store(aws, monkeypatch):
    monkeypatch.setenv("DYNAMODB_USER_FILES_TABLE_NAME", TABLE)
    monkeypatch.setenv("PROMPT_CACHE_OBSERVABILITY_ENABLED", "false")
    ddb = boto3.client("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    return s3, FileUploadRepository(table_name=TABLE)


@pytest.fixture(autouse=True)
def _no_abstract_model():
    async def _abstract(*args, **kwargs):
        return "A stub abstract."

    with patch("apis.shared.files.document_digest.generate_abstract", _abstract):
        yield


async def _upload(store, raw: bytes, filename: str, mime: str, size=None, upload_id="up-1",
                  status=FileStatus.READY) -> FileMetadata:
    s3, repository = store
    meta = FileMetadata(
        upload_id=upload_id, user_id="u1", session_id="s1", filename=filename, mime_type=mime,
        size_bytes=len(raw) if size is None else size,
        s3_key=f"user-files/u1/s1/{upload_id}/{filename}", s3_bucket=BUCKET, status=status,
    )
    s3.put_object(Bucket=BUCKET, Key=meta.s3_key, Body=raw)
    await repository.create_file(meta)
    return meta


def _keys(s3, prefix="user-files/") -> list[str]:
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []))


async def _facts(store, upload_id="up-1", **kwargs) -> str:
    s3, repository = store
    return await handler.compute_facts("u1", upload_id, s3=s3, repository=repository, **kwargs)


def _plain(facts: dict) -> dict:
    """DynamoDB hands numbers back as Decimal."""
    return json.loads(json.dumps(facts, default=int))


PDF = build_pdf(TEXT_PAGES)
SCANNED = build_pdf(["", "", "", "7"])
MEMO = build_docx(["Quarterly report", "Revenue grew in every region", "Costs held flat"])
HTML = b"<html><body><h1>Release notes</h1><p>Two fixes and one feature.</p></body></html>"
MD = b"# Plan\n\n- ship the facts\n- read them later\n"
TXT = b"line one\nline two\nline three\n"

CASES = [
    # filename, mime, raw, pages, hasTextLayer, units
    ("contract.pdf", "application/pdf", PDF, 5, True, "page"),
    ("scan.pdf", "application/pdf", SCANNED, 4, False, "page"),
    ("memo.docx", DOCX, MEMO, None, True, "paragraph"),
    ("notes.html", "text/html", HTML, None, True, "line"),
    ("plan.md", "text/markdown", MD, None, True, "line"),
    ("log.txt", "text/plain", TXT, None, True, "line"),
]
IDS = ["pdf", "scanned_pdf", "docx", "html", "md", "txt"]


@pytest.mark.asyncio
class TestFactsOnly:
    @pytest.mark.parametrize("filename,mime,raw,pages,has_text,units", CASES, ids=IDS)
    async def test_each_type_gets_its_facts_and_nothing_else(self, store, filename, mime, raw, pages, has_text, units):
        s3, repository = store
        await _upload(store, raw, filename, mime)

        assert await _facts(store) == "ready"

        row = await repository.get_file("u1", "up-1")
        facts = _plain(row.facts)
        assert facts["version"] == file_facts.FACTS_VERSION
        assert facts["class"] == "document"
        assert facts["pages"] == pages
        assert facts["hasTextLayer"] is has_text
        assert facts["units"] == units
        fmt = filename.rsplit(".", 1)[-1]
        # The same number estimate_document_tokens gives the bytes in hand.
        assert facts["tokens"]["native"] == estimate_document_tokens(fmt, raw)
        assert facts["tokens"]["text"] >= 0
        # Small-file path: no sidecars, no shards, no preparation, no digest, no index.
        assert _keys(s3) == [f"user-files/u1/s1/up-1/{filename}"]
        assert row.preparation is None and row.digest is None and row.kb_index is None

    async def test_pdf_text_tokens_count_the_page_texts(self, store):
        _, repository = store
        await _upload(store, PDF, "contract.pdf", "application/pdf")
        await _facts(store)
        facts = _plain((await repository.get_file("u1", "up-1")).facts)
        assert facts["tokens"]["text"] == len("\n\n".join(TEXT_PAGES)) // 4
        assert facts["tokens"]["native"] == 5 * PDF_PAGE_TOKEN_ESTIMATE

    async def test_scanned_pdf_has_no_text_tokens_to_speak_of(self, store):
        _, repository = store
        await _upload(store, SCANNED, "scan.pdf", "application/pdf")
        await _facts(store)
        facts = _plain((await repository.get_file("u1", "up-1")).facts)
        assert facts["tokens"]["text"] < 5
        assert facts["tokens"]["native"] == 4 * PDF_PAGE_TOKEN_ESTIMATE

    async def test_docx_text_tokens_count_the_markdown_the_model_is_sent(self, store):
        from apis.shared.files.docx_markdown import docx_to_markdown

        _, repository = store
        await _upload(store, MEMO, "memo.docx", DOCX)
        await _facts(store)
        facts = _plain((await repository.get_file("u1", "up-1")).facts)
        assert facts["tokens"]["text"] == len(docx_to_markdown(MEMO)) // 4

    async def test_computed_once(self, store):
        _, repository = store
        await _upload(store, TXT, "log.txt", "text/plain")
        assert await _facts(store) == "ready"
        assert await _facts(store) == "already_computed"

    @pytest.mark.parametrize(
        "setup,outcome",
        [
            ({"filename": "sheet.csv", "mime": "text/csv"}, "not_document"),
            ({"filename": "deck.pptx", "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}, "not_document"),
            ({"filename": "big.pdf", "mime": "application/pdf", "size": INLINE_DOCUMENT_MAX_BYTES + 1}, "reference"),
            ({"filename": "a.pdf", "mime": "application/pdf", "status": FileStatus.PENDING}, "not_ready"),
        ],
        ids=["csv", "pptx", "reference", "pending"],
    )
    async def test_rows_it_must_not_touch_are_skipped(self, store, setup, outcome):
        _, repository = store
        await _upload(store, PDF, **setup)
        assert await _facts(store) == outcome
        assert (await repository.get_file("u1", "up-1")).facts is None

    async def test_a_missing_row_is_skipped(self, store):
        assert await _facts(store, upload_id="nope") == "missing"

    async def test_a_bad_document_leaves_the_row_without_facts(self, store):
        _, repository = store
        await _upload(store, b"%PDF-1.4 not really", "broken.pdf", "application/pdf")
        assert await _facts(store) == "failed"
        row = await repository.get_file("u1", "up-1")
        assert row.facts is None and row.preparation is None

    async def test_replaced_bytes_never_get_the_old_facts(self, store):
        """The size condition: facts computed for old bytes do not land on a replaced row."""
        s3, repository = store
        await _upload(store, TXT, "log.txt", "text/plain")
        real_get = s3.get_object

        def get_then_replace(**kwargs):
            out = real_get(**kwargs)
            repository._table.update_item(
                Key={"PK": "USER#u1", "SK": "FILE#up-1"},
                UpdateExpression="SET sizeBytes = :s", ExpressionAttributeValues={":s": 999},
            )
            return out

        with patch.object(s3, "get_object", side_effect=get_then_replace):
            assert await _facts(store) == "stale"
        assert (await repository.get_file("u1", "up-1")).facts is None

    async def test_replace_drops_facts(self, store):
        _, repository = store
        await _upload(store, TXT, "log.txt", "text/plain")
        await _facts(store)
        await repository.replace_file_content_fields("u1", "up-1", 10)
        assert (await repository.get_file("u1", "up-1")).facts is None

    async def test_an_aws_error_is_retried_until_the_last_attempt(self, store):
        from botocore.exceptions import ClientError

        s3, repository = store
        await _upload(store, TXT, "log.txt", "text/plain")
        boom = ClientError({"Error": {"Code": "SlowDown", "Message": "slow"}}, "GetObject")
        with patch.object(s3, "get_object", side_effect=boom):
            with pytest.raises(ClientError):
                await _facts(store)
            assert await _facts(store, final_attempt=True) == "failed"
        assert (await repository.get_file("u1", "up-1")).facts is None


@pytest.mark.asyncio
class TestReferencePreparationWritesFacts:
    async def test_large_pdf_gets_facts_in_the_final_write(self, store):
        _, repository = store
        raw = build_pdf(TEXT_PAGES)
        size = INLINE_DOCUMENT_MAX_BYTES + 1
        await _upload(store, raw, "manual.pdf", "application/pdf", size=size)
        assert await handler.prepare_upload("u1", "up-1", s3=store[0], repository=repository) == "ready"
        row = await repository.get_file("u1", "up-1")
        facts = _plain(row.facts)
        assert facts["pages"] == 5 and facts["hasTextLayer"] is True and facts["units"] == "page"
        # Native tokens are from the bytes read, as estimate_document_tokens would see them.
        assert facts["tokens"]["native"] == estimate_document_tokens("pdf", raw)
        assert int(row.preparation["pageCount"]) == 5  # the planner's page_count source is unchanged

    async def test_large_docx_gets_facts_too(self, store):
        _, repository = store
        await _upload(store, MEMO, "memo.docx", DOCX, size=INLINE_DOCUMENT_MAX_BYTES + 1)
        assert await handler.prepare_upload("u1", "up-1", s3=store[0], repository=repository) == "ready"
        facts = _plain((await repository.get_file("u1", "up-1")).facts)
        assert facts["units"] == "paragraph" and facts["pages"] is None and facts["hasTextLayer"] is True


class TestHandlerRouting:
    def _event(self, body, receive_count="1"):
        return {"Records": [{"messageId": "m0", "body": body, "attributes": {"ApproximateReceiveCount": receive_count}}]}

    def test_a_facts_message_runs_compute_facts_never_prepare_or_index(self, monkeypatch):
        monkeypatch.setenv("ATTACHMENT_KB_ENABLED", "true")
        seen = {}

        async def fake(user_id, upload_id, final_attempt=False):
            seen.update(user_id=user_id, upload_id=upload_id, final=final_attempt)
            return "ready"

        with patch.object(handler, "compute_facts", side_effect=fake), \
                patch.object(handler, "prepare_upload") as prepare, \
                patch.object(handler, "index_after_prepare") as index:
            body = json.dumps({"action": "facts", "user_id": "u1", "upload_id": "up-1"})
            assert handler.lambda_handler(self._event(body, receive_count="3"), None) == {"batchItemFailures": []}
        assert seen == {"user_id": "u1", "upload_id": "up-1", "final": True}
        prepare.assert_not_called()
        index.assert_not_called()

    def test_a_facts_message_with_the_kb_off_is_still_computed(self, monkeypatch):
        monkeypatch.setenv("ATTACHMENT_KB_ENABLED", "false")

        async def fake(user_id, upload_id, final_attempt=False):
            return "ready"

        with patch.object(handler, "compute_facts", side_effect=fake) as compute:
            body = json.dumps({"action": "facts", "user_id": "u1", "upload_id": "up-1"})
            handler.lambda_handler(self._event(body), None)
        compute.assert_called_once()


class TestNativeFormula:
    @pytest.mark.parametrize(
        "fmt,raw",
        [("pdf", PDF), ("pdf", SCANNED), ("docx", MEMO), ("html", HTML), ("md", MD), ("txt", TXT),
         ("pdf", b"%PDF-1.4 not really")],
        ids=["pdf", "scanned", "docx", "html", "md", "txt", "unparseable_pdf"],
    )
    def test_matches_estimate_document_tokens(self, fmt, raw):
        from apis.shared.files.document_tokens import pdf_page_count

        pages = pdf_page_count(raw) if fmt == "pdf" else None
        assert file_facts.native_document_tokens(fmt, len(raw), pages) == estimate_document_tokens(fmt, raw)

    def test_a_typed_cover_on_a_scan_is_still_no_text_layer(self):
        facts = file_facts.build_document_facts(
            "pdf", size_bytes=1000, page_texts=["Exhibit A: signed scan of the lease"] + [""] * 9
        )
        assert facts["hasTextLayer"] is False
