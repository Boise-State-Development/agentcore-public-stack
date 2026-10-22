"""Re-indexing the conversation archive (scripts/reindex_conversation_archive.py).

S3 and SQS are moto. The point is that every archived turn reaches the
consumer's queue as an event the consumer accepts, and that nothing in the
archive changes.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, List

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import reindex_conversation_archive as reindex  # noqa: E402

from apis.shared.conversation_archive import ArchivedTurn  # noqa: E402

REGION = "us-east-1"
PREFIX = "test-prefix"
BUCKET = f"{PREFIX}-conversation-archive"


@pytest.fixture()
def aws(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)
        queue_url = boto3.client("sqs", region_name=REGION).create_queue(QueueName=f"{PREFIX}-conversation-index")["QueueUrl"]
        yield s3, queue_url


def _archive(s3, user_id: str, session_id: str, index: int) -> str:
    turn = ArchivedTurn(user_id, session_id, index, "q", "a", "2026-10-07T00:00:00+00:00")
    s3.put_object(Bucket=BUCKET, Key=turn.key, Body=turn.to_json())
    return turn.key


def _drain(queue_url: str) -> List[Dict]:
    sqs = boto3.client("sqs", region_name=REGION)
    out = []
    while True:
        page = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10)
        messages = page.get("Messages") or []
        if not messages:
            return out
        for message in messages:
            out.append(json.loads(message["Body"]))
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])


def _snapshot(s3) -> Dict[str, str]:
    page = s3.list_objects_v2(Bucket=BUCKET)
    return {o["Key"]: o["ETag"] + o["LastModified"].isoformat() for o in page.get("Contents", [])}


def test_a_dry_run_counts_and_sends_nothing(aws):
    s3, queue_url = aws
    _archive(s3, "u1", "s1", 0)
    _archive(s3, "u1", "s1", 2)

    assert reindex.main(["--project-prefix", PREFIX]) == 0
    assert _drain(queue_url) == []


def test_apply_queues_one_event_per_turn_and_leaves_the_archive_alone(aws):
    s3, queue_url = aws
    keys = [_archive(s3, "u1", "s1", i) for i in range(0, 24, 2)]
    s3.put_object(Bucket=BUCKET, Key="conversations/not-a-turn.txt", Body=b"x")
    before = _snapshot(s3)

    assert reindex.main(["--project-prefix", PREFIX, "--apply", "--confirm-prefix", PREFIX, "--sleep", "0"]) == 0

    events = _drain(queue_url)
    assert sorted(e["detail"]["object"]["key"] for e in events) == sorted(keys)
    assert _snapshot(s3) == before


def test_the_consumer_accepts_the_event_it_sends():
    from apis.app_api.conversation_index import consumer

    key = "conversations/u1/s1/000002.json"
    message = {"body": json.dumps(reindex.object_created_event(BUCKET, key))}

    assert consumer._keys_of(message, BUCKET) == [(key, "Object Created")]


def test_user_limits_it_to_one_owner(aws):
    s3, queue_url = aws
    _archive(s3, "u1", "s1", 0)
    mine = _archive(s3, "u2", "s1", 0)

    reindex.main(["--project-prefix", PREFIX, "--user", "u2", "--apply", "--confirm-prefix", PREFIX, "--sleep", "0"])

    assert [e["detail"]["object"]["key"] for e in _drain(queue_url)] == [mine]


def test_batches_of_ten_with_a_pause_between_and_failures_counted():
    sent: List[int] = []
    sleeps: List[float] = []

    def send(entries):
        sent.append(len(entries))
        return [entries[0]["Id"]] if len(sent) == 2 else []

    report = reindex.run(
        keys=iter(f"conversations/u/s/{i:06d}.json" for i in range(23)),
        bucket=BUCKET,
        send=send,
        apply=True,
        sleep_seconds=1.0,
        sleep=sleeps.append,
    )

    assert sent == [10, 10, 3]
    assert sleeps == [1.0, 1.0]
    assert (report.turns, report.sent, report.failed) == (23, 22, 1)


def test_apply_requires_the_confirm_prefix():
    with pytest.raises(SystemExit):
        reindex.main(["--project-prefix", PREFIX, "--apply"])
