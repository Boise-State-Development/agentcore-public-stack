"""Large-document fixtures: staging a reference upload, waiting for it, cleaning it up (eval-harness spec §9).

Real repository, moto S3/DynamoDB/SQS. The deployed prepare Lambda is the one
thing not exercised; its effect on the row is written directly here.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import boto3
import pytest
from moto import mock_aws

from apis.shared.evals import fixtures as fx
from apis.shared.evals.cases import LARGE_DOCUMENTS, CaseError, parse_case
from apis.shared.files import repository as repository_module
from apis.shared.files.large_document import STATUS_READY, preparation_record
from apis.shared.files.repository import FileUploadRepository

REGION = "us-east-1"
BUCKET = "eval-user-files"
TABLE = "eval-user-files"
USER = "eval-user"
SESSION = "eval-0123456789abcdef"


@pytest.fixture
def store(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION, "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing", "S3_USER_FILES_BUCKET_NAME": BUCKET, "DYNAMODB_USER_FILES_TABLE_NAME": TABLE,
    }.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
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
        queue_url = boto3.client("sqs", region_name=REGION).create_queue(QueueName="prepare")["QueueUrl"]
        monkeypatch.setenv("LARGE_DOCUMENT_PREPARE_QUEUE_URL", queue_url)
        repository = FileUploadRepository(table_name=TABLE)
        monkeypatch.setattr(repository_module, "_repository_instance", repository)
        yield s3, repository, queue_url


def test_a_reference_upload_is_stored_pending_and_queued_like_app_api_does(store):
    s3, repository, queue_url = store
    upload_id = asyncio.run(fx.register_reference_upload(USER, SESSION, "manual.pdf", "application/pdf", b"%PDF"))
    meta = asyncio.run(repository.get_file(USER, upload_id))
    assert meta.session_id == SESSION and meta.preparation["status"] == "pending"
    messages = boto3.client("sqs", region_name=REGION).receive_message(QueueUrl=queue_url)["Messages"]
    assert json.loads(messages[0]["Body"]) == {"user_id": USER, "upload_id": upload_id}


def test_without_a_prepare_queue_staging_is_a_setup_error(store, monkeypatch):
    monkeypatch.delenv("LARGE_DOCUMENT_PREPARE_QUEUE_URL")
    with pytest.raises(fx.FixtureError, match="LARGE_DOCUMENT_PREPARE_QUEUE_URL"):
        asyncio.run(fx.register_reference_upload(USER, SESSION, "m.pdf", "application/pdf", b"x"))


class TestSettled:
    def _state(self, preparation="ready", kb=None, text=False):
        return {"preparation": preparation, "kbIndex": kb, "textIndexed": text}

    def test_waits_for_preparation_then_for_search(self):
        assert not fx.document_settled(self._state("pending"), seconds_since_prepared=None)
        assert not fx.document_settled(self._state(kb="indexing"), seconds_since_prepared=600)
        assert fx.document_settled(self._state(kb="indexing", text=True), seconds_since_prepared=5)
        assert fx.document_settled(self._state(kb="indexed"), seconds_since_prepared=5)

    def test_states_that_will_not_change_are_settled(self):
        assert fx.document_settled(self._state("failed"), seconds_since_prepared=None)
        assert fx.document_settled(self._state(kb="skipped_cap"), seconds_since_prepared=1)

    def test_no_kb_status_after_the_grace_means_the_kb_is_off(self):
        assert not fx.document_settled(self._state(kb=None), seconds_since_prepared=fx.KB_GRACE_SECONDS - 1)
        assert fx.document_settled(self._state(kb=None), seconds_since_prepared=fx.KB_GRACE_SECONDS)


def test_wait_polls_until_every_document_is_searchable(store):
    _, repository, _ = store
    a = asyncio.run(fx.register_reference_upload(USER, SESSION, "a.pdf", "application/pdf", b"a"))
    b = asyncio.run(fx.register_reference_upload(USER, SESSION, "b.pdf", "application/pdf", b"b"))
    now = [0.0]
    polls = []

    async def sleep(seconds):
        # Stand in for the deployed Lambda: prepare both on the first poll, index one text-first on the second.
        polls.append(seconds)
        now[0] += seconds
        if len(polls) == 1:
            for upload_id in (a, b):
                await repository.update_file_preparation(USER, upload_id, preparation_record(status=STATUS_READY))
                await repository.update_file_kb_index(USER, upload_id, {"status": "indexing"})
        elif len(polls) == 2:
            await repository.update_file_kb_index(USER, a, {"status": "indexing", "textIndexedAt": "t"})
            await repository.update_file_kb_index(USER, b, {"status": "indexed"})

    states = asyncio.run(fx.wait_for_documents(USER, [a, b], timeout=900, poll=15, sleep=sleep, clock=lambda: now[0]))
    assert len(polls) == 2
    assert all(s["settled"] for s in states.values())
    assert states[a]["textIndexed"] and states[b]["kbIndex"] == "indexed" and states[b]["waitedSeconds"] == 30


def test_wait_gives_up_at_the_timeout_and_reports_what_it_saw(store):
    upload_id = asyncio.run(fx.register_reference_upload(USER, SESSION, "a.pdf", "application/pdf", b"a"))
    now = [0.0]

    async def sleep(seconds):
        now[0] += seconds

    states = asyncio.run(fx.wait_for_documents(USER, [upload_id], timeout=60, poll=15, sleep=sleep,
                                               clock=lambda: now[0]))
    assert states[upload_id]["settled"] is False and states[upload_id]["preparation"] == "pending"
    assert states[upload_id]["waitedSeconds"] == 60


def test_delete_removes_the_row_and_every_object_under_the_upload(store):
    s3, repository, _ = store
    upload_id = asyncio.run(fx.register_reference_upload(USER, SESSION, "a.pdf", "application/pdf", b"a"))
    prefix = f"user-files/{USER}/{SESSION}/{upload_id}/"
    s3.put_object(Bucket=BUCKET, Key=prefix + "_derived/text.jsonl", Body=b"{}")
    s3.put_object(Bucket=BUCKET, Key=f"user-files/{USER}/{SESSION}/other/keep.pdf", Body=b"k")
    assert asyncio.run(fx.delete_upload(USER, upload_id)) is True
    assert asyncio.run(repository.get_file(USER, upload_id)) is None
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]
    assert keys == [f"user-files/{USER}/{SESSION}/other/keep.pdf"]
    assert asyncio.run(fx.delete_upload(USER, upload_id)) is False


def _case(**extra):
    raw = {"id": "document_qa.x", "family": "document_qa", "turns": [{"prompt": "q"}], "expect": {"contains_any": ["1"]}}
    return {**raw, **extra}


def test_cases_name_only_known_large_documents():
    assert parse_case(_case(large_documents=["manual"]), family="document_qa").large_documents == ("manual",)
    assert parse_case(_case(), family="document_qa").large_documents == ()
    with pytest.raises(CaseError, match="unknown"):
        parse_case(_case(large_documents=["novel"]), family="document_qa")


@pytest.fixture
def builders():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "scripts"))
    from large_document_quality import fixtures as ldq

    return ldq


def test_the_known_documents_are_the_quality_harness_builders(builders):
    assert set(LARGE_DOCUMENTS) == set(builders.BUILDERS)


def test_every_large_case_asks_a_planted_question_and_expects_its_answer(builders):
    """A case drifting from its fixture would score a correct answer as wrong, or the reverse."""
    from pathlib import Path

    from apis.shared.evals.cases import load_cases

    questions = {
        name: [*getattr(builders, f"_{key}_QUESTIONS")]
        for name, key in (("manual", "MANUAL"), ("scanned", "SCANNED"), ("brochure", "LAYOUT"), ("handbook", "DOCX"))
    }
    cases_dir = Path(__file__).resolve().parents[4] / "evals" / "cases"
    large = [c for c in load_cases(cases_dir, families=["document_qa"]) if c.large_documents]
    assert len(large) >= 8
    for case in large:
        (doc,) = case.large_documents
        matches = [q for q in questions[doc] if q.question.lower() in case.turns[0].lower()]
        assert matches, f"{case.id} does not ask one of {doc}'s planted questions"
        question = matches[0]
        assert set(case.expect.contains_any) <= set(question.expected), case.id
        assert set(case.expect.forbidden) <= set(question.forbidden), case.id


def test_stale_uploads_are_judged_by_their_newest_object():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    old, fresh = now - timedelta(hours=3), now - timedelta(minutes=5)
    root = f"user-files/{USER}"
    listing = [
        {"Key": f"{root}/eval-a/up-1/manual.pdf", "LastModified": old},
        {"Key": f"{root}/eval-a/up-1/_derived/text.jsonl", "LastModified": old},
        # Original long ago, but the prepare Lambda wrote a sidecar minutes ago: still in use.
        {"Key": f"{root}/eval-b/up-2/brochure.pdf", "LastModified": old},
        {"Key": f"{root}/eval-b/up-2/_derived/pages.jsonl", "LastModified": fresh},
        {"Key": "user-files/someone-else/s/up-9/x.pdf", "LastModified": old},
        {"Key": f"{root}/stray.txt", "LastModified": old},
    ]
    assert fx.stale_uploads(listing, USER, now - timedelta(hours=1)) == [("eval-a", "up-1")]
