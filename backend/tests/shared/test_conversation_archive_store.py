"""Writing and deleting archived turns (moto S3 + SSM)."""

import json

import boto3
import pytest

from apis.shared.conversation_archive import (
    archive_bucket_name,
    build_turn,
    delete_session_archive,
    put_turns,
    reset_bucket_cache,
)

BUCKET = "test-conversation-archive"
REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _fresh_bucket_cache():
    reset_bucket_cache()
    yield
    reset_bucket_cache()


@pytest.fixture()
def s3(aws, monkeypatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(Bucket=BUCKET)
    return client


def _turn(session_id="s1", index=0, user_id="u1"):
    return build_turn(
        user_id=user_id,
        session_id=session_id,
        message_index=index,
        user_text=f"question {index}",
        assistant_messages=[{"role": "assistant", "content": [{"text": f"answer {index}"}]}],
        created_at="2026-10-07T00:00:00+00:00",
    )


def _keys(s3):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []))


# ── bucket resolution ─────────────────────────────────────────────────────


def test_env_var_wins(monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", "from-env")
    assert archive_bucket_name() == "from-env"


def test_runtime_resolves_the_bucket_from_ssm_once(aws, monkeypatch):
    """The runtime has no env slot for the bucket; it reads SSM under PROJECT_PREFIX."""
    monkeypatch.delenv("CONVERSATION_ARCHIVE_BUCKET_NAME", raising=False)
    monkeypatch.setenv("PROJECT_PREFIX", "pfx")
    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(Name="/pfx/conversations/archive-bucket-name", Value="from-ssm", Type="String")

    assert archive_bucket_name() == "from-ssm"
    ssm.delete_parameter(Name="/pfx/conversations/archive-bucket-name")
    assert archive_bucket_name() == "from-ssm", "resolved once per process"


def test_a_missing_parameter_means_no_archive_and_is_not_retried_every_turn(aws, monkeypatch):
    monkeypatch.delenv("CONVERSATION_ARCHIVE_BUCKET_NAME", raising=False)
    monkeypatch.setenv("PROJECT_PREFIX", "pfx")
    assert archive_bucket_name() is None

    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name="/pfx/conversations/archive-bucket-name", Value="late", Type="String"
    )
    assert archive_bucket_name() is None, "negative result is cached for a while"


def test_no_env_and_no_prefix_means_no_archive(monkeypatch):
    monkeypatch.delenv("CONVERSATION_ARCHIVE_BUCKET_NAME", raising=False)
    monkeypatch.delenv("PROJECT_PREFIX", raising=False)
    assert archive_bucket_name() is None
    assert put_turns([_turn()]) == 0
    assert delete_session_archive("u1", "s1") == 0


# ── put ───────────────────────────────────────────────────────────────────


def test_put_writes_one_json_object_per_turn_and_rewrites_idempotently(s3, monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)

    assert put_turns([_turn(index=0), _turn(index=2)]) == 2
    assert put_turns([_turn(index=2)]) == 1

    assert _keys(s3) == ["conversations/u1/s1/000000.json", "conversations/u1/s1/000002.json"]
    obj = s3.get_object(Bucket=BUCKET, Key="conversations/u1/s1/000002.json")
    assert obj["ContentType"] == "application/json"
    assert json.loads(obj["Body"].read())["assistantText"] == "answer 2"


def test_put_failure_is_swallowed(aws, monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", "no-such-bucket")
    assert put_turns([_turn()]) == 0


# ── delete ────────────────────────────────────────────────────────────────


def test_delete_removes_exactly_one_sessions_turns(s3, monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    put_turns([_turn("s1", 0), _turn("s1", 2), _turn("s10", 0), _turn("s1", 0, user_id="u2")])

    assert delete_session_archive("u1", "s1") == 2
    # "s10" shares the "s1" string prefix; the trailing slash keeps it safe.
    assert _keys(s3) == ["conversations/u1/s10/000000.json", "conversations/u2/s1/000000.json"]


def test_delete_is_not_gated_on_the_index_flag(s3, monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    monkeypatch.setenv("CONVERSATION_INDEX_ENABLED", "false")
    put_turns([_turn()])
    assert delete_session_archive("u1", "s1") == 1


def test_delete_pages_past_a_thousand_turns(s3, monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    for i in range(1003):
        s3.put_object(Bucket=BUCKET, Key=f"conversations/u1/s1/{i:06d}.json", Body=b"{}")
    assert delete_session_archive("u1", "s1") == 1003
    assert _keys(s3) == []


def test_delete_with_an_unusable_id_does_nothing(s3, monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    put_turns([_turn()])
    assert delete_session_archive("u1", "") == 0
    assert delete_session_archive("u1/..", "x") == 0
    assert len(_keys(s3)) == 1
