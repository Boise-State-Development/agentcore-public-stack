"""large-document-prepare Lambda (moto S3 + DynamoDB).

The real repository and a real (moto) bucket, so the sidecar keys, the row's
``preparation`` map and the conditional final write are checked against the
storage the services use. Only the digest's abstract model call is stubbed.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest

from apis.shared.files import large_document as ld
from apis.shared.files.models import INLINE_DOCUMENT_MAX_BYTES, FileMetadata, FileStatus
from apis.shared.files.repository import FileUploadRepository
from lambdas.large_document_prepare import handler
from tests.shared.test_document_read import build_docx, build_pdf

REGION = "us-east-1"
BUCKET = "test-user-files-bucket"
TABLE = "test-user-files"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
LARGE = INLINE_DOCUMENT_MAX_BYTES + 1


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


async def _upload(store, raw: bytes, filename="manual.pdf", mime="application/pdf", size=LARGE,
                  upload_id="up-1", status=FileStatus.READY) -> FileMetadata:
    s3, repository = store
    meta = FileMetadata(
        upload_id=upload_id, user_id="u1", session_id="s1", filename=filename, mime_type=mime,
        size_bytes=size, s3_key=f"user-files/u1/s1/{upload_id}/{filename}", s3_bucket=BUCKET, status=status,
    )
    s3.put_object(Bucket=BUCKET, Key=meta.s3_key, Body=raw)
    await repository.create_file(meta)
    return meta


def _keys(s3, prefix="user-files/") -> list[str]:
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []))


def _body(s3, key) -> bytes:
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


async def _prepare(store, upload_id="up-1", **kwargs) -> str:
    s3, repository = store
    return await handler.prepare_upload("u1", upload_id, s3=s3, repository=repository, **kwargs)


PAGES = [f"Section {i} terms" for i in range(1, 24)]


@pytest.mark.asyncio
class TestPrepare:
    async def test_pdf_gets_a_text_layer_shards_a_digest_and_a_ready_row(self, store):
        s3, repository = store
        meta = await _upload(store, build_pdf(PAGES))

        assert await _prepare(store) == "ready"

        base = "user-files/u1/s1/up-1/"
        assert _keys(s3) == [
            f"{base}_derived/pages-0001-0010.pdf",
            f"{base}_derived/pages-0011-0020.pdf",
            f"{base}_derived/pages-0021-0023.pdf",
            f"{base}_derived/text.jsonl",
            f"{base}manual.pdf",
        ]
        assert ld.decode_text_layer(_body(s3, f"{base}_derived/text.jsonl")) == PAGES
        row = await repository.get_file("u1", "up-1")
        prep = row.preparation
        assert prep["status"] == "ready" and prep["format"] == "pdf"
        assert int(prep["pageCount"]) == 23 and int(prep["shardPages"]) == 10
        assert int(prep["textBytes"]) == len(_body(s3, f"{base}_derived/text.jsonl"))
        assert "error" not in prep
        assert ld.is_prepared(row)
        assert row.digest["status"] == "ready" and row.digest["count"] == 23
        assert row.digest["abstract"] == "A stub abstract."
        assert row.status == FileStatus.READY.value and row.size_bytes == meta.size_bytes

    async def test_each_shard_holds_its_own_pages(self, store):
        from apis.shared.files.document_read import _open_pdf, _pdf_page_text

        s3, _ = store
        await _upload(store, build_pdf(PAGES))
        await _prepare(store)
        pdf = _open_pdf(_body(s3, "user-files/u1/s1/up-1/_derived/pages-0011-0020.pdf"))
        try:
            assert [_pdf_page_text(pdf, i) for i in range(len(pdf))] == PAGES[10:20]
        finally:
            pdf.close()

    @pytest.mark.parametrize(
        "filename,mime,raw,expected",
        [
            ("memo.docx", DOCX, build_docx(["One", "Two", "Three"]), ["One", "Two", "Three"]),
            ("notes.txt", "text/plain", b"a\r\nb\n\nc", ["a", "b", "", "c"]),
            ("page.html", "text/html", b"<h1>A</h1>\n<p>b</p>", ["<h1>A</h1>", "<p>b</p>"]),
        ],
        # Fixed ids: the default id embeds the raw bytes, and a .docx zip carries the
        # time it was built, so xdist workers collecting a second apart disagreed on
        # the test names and the whole run aborted at collection.
        ids=["docx", "txt", "html"],
    )
    async def test_other_formats_get_a_text_layer_and_no_shards(self, store, filename, mime, raw, expected):
        s3, repository = store
        await _upload(store, raw, filename=filename, mime=mime)
        assert await _prepare(store) == "ready"
        assert _keys(s3, "user-files/u1/s1/up-1/_derived/") == ["user-files/u1/s1/up-1/_derived/text.jsonl"]
        assert ld.decode_text_layer(_body(s3, "user-files/u1/s1/up-1/_derived/text.jsonl")) == expected
        prep = (await repository.get_file("u1", "up-1")).preparation
        assert prep["status"] == "ready" and int(prep["pageCount"]) == 0 and int(prep["shardPages"]) == 0

    async def test_a_prepared_row_is_not_prepared_again(self, store):
        s3, repository = store
        await _upload(store, build_pdf(PAGES))
        assert await _prepare(store) == "ready"
        first = (await repository.get_file("u1", "up-1")).preparation
        s3.delete_object(Bucket=BUCKET, Key="user-files/u1/s1/up-1/_derived/text.jsonl")

        assert await _prepare(store) == "already_prepared"
        assert (await repository.get_file("u1", "up-1")).preparation == first
        assert "user-files/u1/s1/up-1/_derived/text.jsonl" not in _keys(s3)

    async def test_a_rerun_after_failure_rewrites_the_same_keys(self, store):
        s3, repository = store
        await _upload(store, build_pdf(PAGES))
        await repository.update_file_preparation(
            "u1", "up-1", ld.preparation_record(status=ld.STATUS_FAILED, error="ClientError")
        )
        assert await _prepare(store) == "ready"
        assert len(_keys(s3, "user-files/u1/s1/up-1/_derived/")) == 4

    async def test_a_bad_document_records_failed_and_never_raises(self, store):
        s3, repository = store
        await _upload(store, b"%PDF-1.4 this is not a pdf")
        assert await _prepare(store) == "failed"
        prep = (await repository.get_file("u1", "up-1")).preparation
        assert prep["status"] == "failed"
        assert prep["error"] == "DocumentReadError"  # a class name, never a message
        assert _keys(s3, "user-files/u1/s1/up-1/_derived/") == []

    @pytest.mark.parametrize(
        "setup,outcome",
        [
            ({"size": 1000}, "not_reference"),
            ({"filename": "sheet.csv", "mime": "text/csv"}, "not_reference"),
            ({"status": FileStatus.PENDING}, "not_ready"),
        ],
    )
    async def test_rows_it_must_not_touch_are_skipped(self, store, setup, outcome):
        s3, repository = store
        await _upload(store, b"a,b\n1,2" if "mime" in setup else build_pdf(PAGES), **setup)
        assert await _prepare(store) == outcome
        assert (await repository.get_file("u1", "up-1")).preparation is None
        assert _keys(s3, "user-files/u1/s1/up-1/_derived/") == []

    async def test_a_missing_row_is_skipped(self, store):
        assert await _prepare(store, upload_id="nope") == "missing"

    async def test_a_key_outside_the_owners_folder_is_refused(self, store):
        s3, repository = store
        meta = await _upload(store, build_pdf(PAGES))
        await repository.delete_file("u1", "up-1")
        await repository.create_file(meta.model_copy(update={"s3_key": "user-files/u2/s9/up-9/other.pdf"}))
        assert await _prepare(store) == "failed"
        assert (await repository.get_file("u1", "up-1")).preparation["error"] == "UnexpectedKey"
        assert _keys(s3, "user-files/u2/") == []

    async def test_a_file_deleted_while_preparing_leaves_no_sidecars(self, store):
        s3, repository = store
        await _upload(store, build_pdf(PAGES))
        real_put = s3.put_object

        def put_then_delete_row(**kwargs):
            real_put(**kwargs)
            if kwargs["Key"].endswith("text.jsonl"):
                repository._table.delete_item(Key={"PK": "USER#u1", "SK": "FILE#up-1"})

        with patch.object(s3, "put_object", side_effect=put_then_delete_row):
            assert await _prepare(store) == "deleted"
        assert _keys(s3, "user-files/u1/s1/up-1/_derived/") == []

    async def test_shards_over_the_byte_cap_fail_and_are_cleaned_up(self, store, monkeypatch):
        s3, repository = store
        monkeypatch.setattr(handler, "MAX_SHARD_BYTES_RATIO", 0.5)
        await _upload(store, build_pdf(PAGES))
        assert await _prepare(store) == "failed"
        assert (await repository.get_file("u1", "up-1")).preparation["error"] == "ShardsTooLarge"
        assert _keys(s3, "user-files/u1/s1/up-1/_derived/") == []

    async def test_an_aws_error_is_retried_until_the_last_attempt(self, store):
        from botocore.exceptions import ClientError

        s3, repository = store
        await _upload(store, build_pdf(PAGES))
        boom = ClientError({"Error": {"Code": "SlowDown", "Message": "slow"}}, "PutObject")
        with patch.object(s3, "put_object", side_effect=boom):
            with pytest.raises(ClientError):
                await _prepare(store)
            assert (await repository.get_file("u1", "up-1")).preparation is None

            assert await _prepare(store, final_attempt=True) == "failed"
        prep = (await repository.get_file("u1", "up-1")).preparation
        assert prep["status"] == "failed" and prep["error"] == "ClientError"

    async def test_digest_kill_switch_leaves_the_digest_alone(self, store, monkeypatch):
        _, repository = store
        monkeypatch.setenv("DOCUMENT_DIGEST_ENABLED", "false")
        await _upload(store, build_pdf(PAGES))
        assert await _prepare(store) == "ready"
        row = await repository.get_file("u1", "up-1")
        assert row.digest is None and ld.is_prepared(row)


class TestHandler:
    def _event(self, *bodies, receive_count="1"):
        return {
            "Records": [
                {"messageId": f"m{i}", "body": body, "attributes": {"ApproximateReceiveCount": receive_count}}
                for i, body in enumerate(bodies)
            ]
        }

    def test_reports_only_the_failed_message(self):
        async def fake(user_id, upload_id, final_attempt=False):
            if upload_id == "bad":
                raise RuntimeError("boom")
            return "ready"

        with patch.object(handler, "prepare_upload", side_effect=fake):
            result = handler.lambda_handler(
                self._event(
                    json.dumps({"user_id": "u1", "upload_id": "ok"}),
                    json.dumps({"user_id": "u1", "upload_id": "bad"}),
                ),
                None,
            )
        assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}

    @pytest.mark.parametrize("body", ["not json", "[]", json.dumps({"user_id": "u1"}), json.dumps({"user_id": "", "upload_id": "x"})])
    def test_malformed_messages_are_dropped_not_retried(self, body):
        with patch.object(handler, "prepare_upload") as prepare:
            assert handler.lambda_handler(self._event(body), None) == {"batchItemFailures": []}
        prepare.assert_not_called()

    @pytest.mark.parametrize("count,final", [("1", False), ("2", False), ("3", True), ("7", True)])
    def test_the_last_receive_is_the_final_attempt(self, count, final):
        seen = {}

        async def fake(user_id, upload_id, final_attempt=False):
            seen["final"] = final_attempt
            return "ready"

        with patch.object(handler, "prepare_upload", side_effect=fake):
            handler.lambda_handler(self._event(json.dumps({"user_id": "u1", "upload_id": "x"}), receive_count=count), None)
        assert seen["final"] is final


class TestAttachmentKbMessages:
    """PR-3: the same queue carries the attachments-KB pipeline (spec §4E)."""

    def _event(self, *bodies):
        return {"Records": [{"messageId": f"m{i}", "body": b, "attributes": {}} for i, b in enumerate(bodies)]}

    @staticmethod
    def _s3_deleted(key: str) -> str:
        return json.dumps({
            "source": "aws.s3",
            "detail-type": "Object Deleted",
            "detail": {"bucket": {"name": "b"}, "object": {"key": key}, "reason": "DeleteObject"},
        })

    @pytest.mark.parametrize(
        "body,expected",
        [
            (json.dumps({"user_id": "u", "upload_id": "x"}), {"kind": "prepare", "user_id": "u", "upload_id": "x"}),
            (json.dumps({"action": "index", "user_id": "u", "upload_id": "x"}),
             {"kind": "index", "user_id": "u", "upload_id": "x", "force": False}),
            (json.dumps({"action": "index", "user_id": "u", "upload_id": "x", "force": True}),
             {"kind": "index", "user_id": "u", "upload_id": "x", "force": True}),
            (json.dumps({"action": "check", "user_id": "u", "upload_id": "x", "attempt": 3}),
             {"kind": "check", "user_id": "u", "upload_id": "x", "attempt": 3}),
            (json.dumps({"action": "reconcile"}), {"kind": "reconcile"}),
        ],
    )
    def test_message_kinds(self, body, expected):
        assert handler._parse(body) == expected

    def test_a_deleted_original_names_its_upload_and_a_deleted_sidecar_is_ignored(self):
        assert handler._parse(self._s3_deleted("user-files/u/s/up/manual.pdf")) == {
            "kind": "deleted", "user_id": "u", "session_id": "s", "upload_id": "up",
        }
        assert handler._parse(self._s3_deleted("user-files/u/s/up/_derived/pages-0001-0010.pdf")) == {"kind": "ignored"}

    @pytest.mark.parametrize("body", [json.dumps({"action": "index", "user_id": "u"}), json.dumps({"action": "nope", "user_id": "u", "upload_id": "x"})])
    def test_malformed_index_messages_are_dropped(self, body):
        assert handler._parse(body) is None

    def test_with_the_flag_off_index_messages_are_acknowledged_untouched(self, monkeypatch):
        monkeypatch.delenv("ATTACHMENT_KB_ENABLED", raising=False)
        with patch.object(handler, "_run_index_message") as run_index:
            result = handler.lambda_handler(
                self._event(json.dumps({"action": "reconcile"}), self._s3_deleted("user-files/u/s/up/a.pdf")), None
            )
        assert result == {"batchItemFailures": []}
        run_index.assert_not_called()

    def test_a_ready_preparation_indexes_when_the_flag_is_on(self, monkeypatch):
        monkeypatch.setenv("ATTACHMENT_KB_ENABLED", "true")
        calls = []

        async def prepare(user_id, upload_id, final_attempt=False):
            return "ready"

        async def index(user_id, upload_id, indexer=None):
            calls.append((user_id, upload_id))
            return "indexing"

        with patch.object(handler, "prepare_upload", side_effect=prepare), patch.object(handler, "index_after_prepare", side_effect=index):
            handler.lambda_handler(self._event(json.dumps({"user_id": "u", "upload_id": "x"})), None)
        assert calls == [("u", "x")]

    def test_an_index_failure_queues_a_retry_and_never_fails_the_preparation(self):
        sent = []

        class Boom:
            def send(self, body, delay=0):
                sent.append((body, delay))

        async def sync(indexer, user_id, upload_id, **kw):
            raise RuntimeError("throttled")

        from apis.shared.files import attachment_index

        with patch.object(attachment_index, "sync_upload", side_effect=sync):
            import asyncio

            outcome = asyncio.run(handler.index_after_prepare("u", "x", indexer=Boom()))
        assert outcome == "index_retry_queued"
        assert sent == [({"action": "index", "user_id": "u", "upload_id": "x"}, 60)]
