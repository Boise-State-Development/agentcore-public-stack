"""Writing and deleting archived turns (moto S3 + SSM)."""

import json

import boto3
import pytest

from apis.shared.conversation_archive import (
    archive_bucket_name,
    build_turn,
    delete_session_archive,
    put_turn_guarded,
    put_turns,
    read_session_turns,
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


# ── the live path's guarded put ───────────────────────────────────────────


def _live_turn(index=24, user_text="plan my week", assistant_text="One question first."):
    return build_turn(
        user_id="u1",
        session_id="s1",
        message_index=index,
        user_text=user_text,
        assistant_messages=[{"role": "assistant", "content": [{"text": assistant_text}]}],
        created_at="2026-10-09T00:00:00+00:00",
    )


def _body(s3, index):
    return json.loads(s3.get_object(Bucket=BUCKET, Key=f"conversations/u1/s1/{index:06d}.json")["Body"].read())


def test_a_new_turn_is_written_create_only(s3, monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    assert put_turn_guarded(_live_turn(22, "previous", "previous answer"), expect_existing=False)
    assert _body(s3, 22)["userText"] == "previous"

    # A misplaced turn aimed at that key must not replace it.
    assert not put_turn_guarded(_live_turn(22), expect_existing=False)
    assert _body(s3, 22)["userText"] == "previous"


def test_a_resumed_turn_replaces_its_paused_copy(s3, monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    assert put_turn_guarded(_live_turn(24), expect_existing=False)

    resumed = _live_turn(24, assistant_text="One question first.\n\nHere is the plan.")
    assert put_turn_guarded(resumed, expect_existing=True)
    assert _body(s3, 24)["assistantText"] == "One question first.\n\nHere is the plan."


def test_a_resumed_turn_that_added_no_text_still_rewrites_its_own_key(s3, monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    assert put_turn_guarded(_live_turn(24), expect_existing=False)
    assert put_turn_guarded(_live_turn(24), expect_existing=True)


@pytest.mark.parametrize(
    "user_text,assistant_text",
    [
        ("a different question", "One question first."),  # another turn's words
        ("plan my week", "Something else entirely."),       # same words, a reply that is not an extension
    ],
)
def test_a_re_archive_never_replaces_a_different_turn(s3, monkeypatch, user_text, assistant_text):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    assert put_turn_guarded(_live_turn(22), expect_existing=False)

    assert not put_turn_guarded(_live_turn(22, user_text, assistant_text), expect_existing=True)
    assert _body(s3, 22)["userText"] == "plan my week"
    assert _body(s3, 22)["assistantText"] == "One question first."


def test_a_re_archive_with_nothing_there_creates_the_object(s3, monkeypatch):
    """The pause's own write may have failed; the resume still lands."""
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    assert put_turn_guarded(_live_turn(24), expect_existing=True)
    assert _body(s3, 24)["userText"] == "plan my week"


def test_a_re_archive_whose_read_fails_falls_back_to_create_only(s3, monkeypatch):
    """Without a successful read the guard never overwrites (e.g. before the
    Runtime role gains s3:GetObject, every read is a 403)."""
    from botocore.exceptions import ClientError

    from apis.shared.conversation_archive import store

    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    assert put_turn_guarded(_live_turn(24), expect_existing=False)

    real_get_client = store.get_client

    class _DeniedReads:
        def __init__(self, client):
            self._client = client

        def get_object(self, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "GetObject")

        def __getattr__(self, name):
            return getattr(self._client, name)

    monkeypatch.setattr(store, "get_client", lambda *a, **k: _DeniedReads(real_get_client(*a, **k)))
    resumed = _live_turn(24, assistant_text="One question first.\n\nHere is the plan.")
    assert not put_turn_guarded(resumed, expect_existing=True)
    assert _body(s3, 24)["assistantText"] == "One question first."

    assert put_turn_guarded(_live_turn(26), expect_existing=True), "a missing key is still created"


def test_guarded_put_failure_is_swallowed(aws, monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", "no-such-bucket")
    assert not put_turn_guarded(_live_turn(), expect_existing=False)
    assert not put_turn_guarded(_live_turn(), expect_existing=True)


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


# ── reading a session back (the messages-route fallback) ─────────────────


def test_read_returns_one_sessions_turns_in_order(s3, monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    put_turns([_turn("s1", 12), _turn("s1", 0), _turn("s1", 4), _turn("s10", 0), _turn("s1", 0, user_id="u2")])

    turns = read_session_turns("u1", "s1")

    assert [t.message_index for t in turns] == [0, 4, 12]
    assert turns[1].user_text == "question 4"


def test_read_skips_an_object_whose_body_names_another_owner(s3, monkeypatch):
    """Same refusal as the index consumer: a body that disagrees with its key is never shown."""
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    put_turns([_turn("s1", 0)])
    s3.put_object(Bucket=BUCKET, Key="conversations/u1/s1/000002.json", Body=_turn("s1", 2, user_id="u2").to_json())
    s3.put_object(Bucket=BUCKET, Key="conversations/u1/s1/000004.json", Body=b"not json")

    assert [t.message_index for t in read_session_turns("u1", "s1")] == [0]


def test_read_without_a_bucket_or_with_a_bad_id_is_empty(s3, monkeypatch):
    monkeypatch.delenv("CONVERSATION_ARCHIVE_BUCKET_NAME", raising=False)
    monkeypatch.delenv("PROJECT_PREFIX", raising=False)
    assert read_session_turns("u1", "s1") == []
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    assert read_session_turns("u1/..", "s1") == []


def test_read_failure_is_swallowed(aws, monkeypatch):
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", "no-such-bucket")
    monkeypatch.setenv("AWS_REGION", REGION)
    assert read_session_turns("u1", "s1") == []
