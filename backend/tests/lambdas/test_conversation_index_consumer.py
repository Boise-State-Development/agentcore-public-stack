"""The conversation-search index consumer (docs/specs/conversation-search.md §4, PR-2b).

S3 and DynamoDB are moto; Bedrock is a hand-written fake that keeps a document
store, so the assertions are about what ends up in the knowledge base — one
document per archived turn, replaced on re-ingest, gone when the turn is gone, and
filed under the user the archive key names — rather than only about call shapes.
"""

import json
from typing import Any, Dict, List, Optional

import boto3
import pytest

from apis.app_api.conversation_index import consumer as c
from apis.shared.conversation_archive.documents import ArchivedTurn, archive_key
from apis.shared.kb_backend import provisioning
from apis.shared.kb_backend import records as r
from apis.shared.kb_backend import tags as kb_tags
from apis.shared.kb_backend.reserved import CONVERSATIONS_KB_ID

REGION = "us-east-1"
BUCKET = "test-conversation-archive"
USER_A = "user-aaa"
USER_B = "user-bbb"
SESSION_A = "11111111-aaaa-4aaa-8aaa-111111111111"
SESSION_B = "22222222-bbbb-4bbb-8bbb-222222222222"
CREATED_AT = "2026-10-07T12:00:00Z"


# ── Bedrock fake ─────────────────────────────────────────────────────────────
class FakeBedrockAgent:
    """The control-plane calls the consumer and the provisioning saga make.

    Documents are held by ``customDocumentIdentifier``, so a second ingest of an
    id replaces the first exactly as the service does.
    """

    def __init__(self) -> None:
        self.documents: Dict[str, Dict[str, Any]] = {}
        self.ingest_calls: List[Dict[str, Any]] = []
        self.delete_calls: List[Dict[str, Any]] = []
        self.created_kbs: List[Dict[str, Any]] = []
        self.created_data_sources: List[Dict[str, Any]] = []
        self.fail_ingest: Optional[Exception] = None
        self.before_ingest_returns = None

    # provisioning
    def create_knowledge_base(self, **kwargs):
        self.created_kbs.append(kwargs)
        return {"knowledgeBase": {"knowledgeBaseId": "KBCONV0001", "status": "CREATING"}}

    def get_knowledge_base(self, **kwargs):
        return {"knowledgeBase": {"knowledgeBaseId": kwargs["knowledgeBaseId"], "status": "ACTIVE"}}

    def create_data_source(self, **kwargs):
        self.created_data_sources.append(kwargs)
        return {"dataSource": {"dataSourceId": "DSCONV0001"}}

    # documents
    def ingest_knowledge_base_documents(self, **kwargs):
        self.ingest_calls.append(kwargs)
        if self.fail_ingest is not None:
            raise self.fail_ingest
        ids = [d["content"]["custom"]["customDocumentIdentifier"]["id"] for d in kwargs["documents"]]
        if len(ids) != len(set(ids)):
            # What the service answers (dev, 2026-10-07): the whole call is refused.
            raise ValueError("ValidationException: You provided duplicate documents in the request.")
        for document in kwargs["documents"]:
            custom = document["content"]["custom"]
            self.documents[custom["customDocumentIdentifier"]["id"]] = document
        if self.before_ingest_returns is not None:
            self.before_ingest_returns()
        return {"documentDetails": []}

    def delete_knowledge_base_documents(self, **kwargs):
        self.delete_calls.append(kwargs)
        for identifier in kwargs["documentIdentifiers"]:
            self.documents.pop(identifier["custom"]["id"], None)
        return {"documentDetails": []}

    # a retrieval stand-in: the search route filters on `user_id equals`
    def visible_to(self, user_id: str) -> List[str]:
        out = []
        for doc_id, document in self.documents.items():
            attrs = {a["key"]: a["value"]["stringValue"] for a in document["metadata"]["inlineAttributes"]}
            if attrs.get("user_id") == user_id:
                out.append(doc_id)
        return sorted(out)


# ── Fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def env(assistants_table, monkeypatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("CONVERSATION_INDEX_ENABLED", "true")
    monkeypatch.setenv("CONVERSATION_ARCHIVE_BUCKET_NAME", BUCKET)
    monkeypatch.setenv("MANAGED_KB_SERVICE_ROLE_ARN", "arn:aws:iam::123456789012:role/kb-service")
    monkeypatch.setenv("MANAGED_KB_TAG_VALUE_PREFIX", "test-project")
    monkeypatch.setenv("MANAGED_KB_TAG_VALUE_ENVIRONMENT", "dev")
    boto3.client("s3", region_name=REGION).create_bucket(Bucket=BUCKET)

    fake = FakeBedrockAgent()
    monkeypatch.setattr(c, "bedrock_agent_client", lambda: fake)
    return fake


def _s3():
    return boto3.client("s3", region_name=REGION)


def _turn(user_id=USER_A, session_id=SESSION_A, index=0, user="What is a window function?",
          assistant="A window function computes over a frame of rows.", **extra) -> ArchivedTurn:
    return ArchivedTurn(
        user_id=user_id,
        session_id=session_id,
        message_index=index,
        user_text=user,
        assistant_text=assistant,
        created_at=CREATED_AT,
        **extra,
    )


def _archive(turn: ArchivedTurn) -> str:
    _s3().put_object(Bucket=BUCKET, Key=turn.key, Body=turn.to_json())
    return turn.key


def _event(key: str, detail_type="Object Created", reason: Optional[str] = None, bucket=BUCKET) -> Dict[str, Any]:
    detail: Dict[str, Any] = {"bucket": {"name": bucket}, "object": {"key": key}}
    if reason:
        detail["reason"] = reason
    return {"source": "aws.s3", "detail-type": detail_type, "detail": detail}


def _sqs(*events, ids=None) -> Dict[str, Any]:
    return {
        "Records": [
            {"messageId": (ids[i] if ids else f"m{i}"), "body": json.dumps(e) if not isinstance(e, str) else e}
            for i, e in enumerate(events)
        ]
    }


def _provision(fake: FakeBedrockAgent):
    """Run one ingest so the knowledge base exists, then forget its calls."""
    key = _archive(_turn(session_id="seed-session", index=0))
    c.lambda_handler(_sqs(_event(key)), None)
    _s3().delete_object(Bucket=BUCKET, Key=key)
    fake.documents.clear()
    fake.ingest_calls.clear()
    fake.created_kbs.clear()
    fake.created_data_sources.clear()


def _attrs(document) -> Dict[str, str]:
    return {a["key"]: a["value"]["stringValue"] for a in document["metadata"]["inlineAttributes"]}


# ── Ingest ───────────────────────────────────────────────────────────────────
class TestIngest:
    def test_created_turn_becomes_one_inline_document(self, env):
        key = _archive(_turn(index=4, project_id="proj-1", assistant_id="ast-1"))

        assert c.lambda_handler(_sqs(_event(key)), None) == {"batchItemFailures": []}

        [call] = env.ingest_calls
        assert call["knowledgeBaseId"] == "KBCONV0001"
        assert call["dataSourceId"] == "DSCONV0001"
        [document] = call["documents"]
        custom = document["content"]["custom"]
        assert custom["customDocumentIdentifier"] == {"id": f"conv#{USER_A}#{SESSION_A}#4"}
        # IN_LINE text, never S3_LOCATION: the KB service role cannot read this bucket.
        assert custom["sourceType"] == "IN_LINE"
        assert "s3Location" not in custom
        assert custom["inlineContent"]["textContent"]["data"] == (
            "What is a window function?\n\nA window function computes over a frame of rows."
        )

    def test_attributes_are_the_turns_identity(self, env):
        key = _archive(_turn(index=4, project_id="proj-1", assistant_id="ast-1"))
        c.lambda_handler(_sqs(_event(key)), None)

        attrs = _attrs(env.ingest_calls[0]["documents"][0])
        assert attrs["user_id"] == USER_A
        assert attrs["session_id"] == SESSION_A
        assert attrs["message_index"] == "4"
        assert attrs["created_at"] == CREATED_AT
        assert attrs["project_id"] == "proj-1"
        assert attrs["assistant_id"] == "ast-1"
        # The shared builder's two, and nothing else.
        assert attrs["document_id"] == f"conv#{USER_A}#{SESSION_A}#4"
        assert set(attrs) == {
            "user_id", "session_id", "message_index", "created_at",
            "project_id", "assistant_id", "document_id", "filename",
        }

    def test_unscoped_turn_has_no_project_or_assistant_attribute(self, env):
        key = _archive(_turn())
        c.lambda_handler(_sqs(_event(key)), None)

        attrs = _attrs(env.ingest_calls[0]["documents"][0])
        assert "project_id" not in attrs
        assert "assistant_id" not in attrs

    def test_reingest_replaces_the_document(self, env):
        key = _archive(_turn(assistant="first answer"))
        c.lambda_handler(_sqs(_event(key)), None)
        _archive(_turn(assistant="resumed and finished answer"))
        c.lambda_handler(_sqs(_event(key)), None)

        assert list(env.documents) == [f"conv#{USER_A}#{SESSION_A}#0"]
        text = env.documents[f"conv#{USER_A}#{SESSION_A}#0"]["content"]["custom"]["inlineContent"]["textContent"]["data"]
        assert text.endswith("resumed and finished answer")

    def test_a_batch_is_one_call_of_up_to_ten(self, env):
        _provision(env)
        keys = [_archive(_turn(index=i * 2)) for i in range(10)]

        c.lambda_handler(_sqs(*[_event(k) for k in keys]), None)

        assert len(env.ingest_calls) == 1
        assert len(env.ingest_calls[0]["documents"]) == 10

    def test_duplicate_events_for_one_key_ingest_once(self, env):
        key = _archive(_turn())
        c.lambda_handler(_sqs(_event(key), _event(key)), None)

        assert len(env.ingest_calls[0]["documents"]) == 1


# ── Delete and expiry ────────────────────────────────────────────────────────
class TestDelete:
    def test_user_delete_removes_the_document(self, env):
        key = _archive(_turn(index=2))
        c.lambda_handler(_sqs(_event(key)), None)
        _s3().delete_object(Bucket=BUCKET, Key=key)

        c.lambda_handler(_sqs(_event(key, "Object Deleted", reason="DeleteObject")), None)

        [call] = env.delete_calls
        assert call["documentIdentifiers"] == [
            {"dataSourceType": "CUSTOM", "custom": {"id": f"conv#{USER_A}#{SESSION_A}#2"}}
        ]
        assert env.documents == {}

    def test_lifecycle_expiration_takes_the_same_path(self, env):
        key = _archive(_turn(index=2))
        c.lambda_handler(_sqs(_event(key)), None)
        _s3().delete_object(Bucket=BUCKET, Key=key)

        result = c.lambda_handler(
            _sqs(_event(key, "Object Deleted", reason="Lifecycle Expiration")), None
        )

        assert result == {"batchItemFailures": []}
        assert env.documents == {}

    def test_a_session_delete_is_batched(self, env):
        keys = [_archive(_turn(index=i * 2)) for i in range(10)]
        c.lambda_handler(_sqs(*[_event(k) for k in keys]), None)
        _s3().delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": k} for k in keys]})

        c.lambda_handler(_sqs(*[_event(k, "Object Deleted") for k in keys]), None)

        assert len(env.delete_calls) == 1
        assert len(env.delete_calls[0]["documentIdentifiers"]) == 10
        assert env.documents == {}

    def test_delete_never_provisions(self, env, assistants_table):
        key = archive_key(USER_A, SESSION_A, 0)

        result = c.lambda_handler(_sqs(_event(key, "Object Deleted")), None)

        assert result == {"batchItemFailures": []}
        assert env.created_kbs == []
        assert env.delete_calls == []
        assert r.get_kb_record(CONVERSATIONS_KB_ID, CONVERSATIONS_KB_ID) is None

    def test_a_stale_created_event_does_not_resurrect_a_deleted_turn(self, env):
        _provision(env)
        key = archive_key(USER_A, SESSION_A, 0)  # written and deleted before we ran

        c.lambda_handler(_sqs(_event(key, "Object Created")), None)

        assert env.ingest_calls == []
        assert env.documents == {}
        assert len(env.delete_calls) == 1

    def test_a_delete_racing_the_ingest_still_wins(self, env):
        key = _archive(_turn())
        # The user deletes the conversation while the ingest call is in flight.
        env.before_ingest_returns = lambda: _s3().delete_object(Bucket=BUCKET, Key=key)

        c.lambda_handler(_sqs(_event(key)), None)

        assert env.documents == {}

    def test_a_forbidden_read_is_a_failure_not_a_delete(self, env, monkeypatch):
        _provision(env)
        key = _archive(_turn())
        c.lambda_handler(_sqs(_event(key)), None)

        from botocore.exceptions import ClientError

        def forbidden(*args, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "GetObject")

        monkeypatch.setattr(c, "read_archive_object", forbidden)
        result = c.lambda_handler(_sqs(_event(key, "Object Deleted"), ids=["gone"]), None)

        assert result == {"batchItemFailures": [{"itemIdentifier": "gone"}]}
        assert f"conv#{USER_A}#{SESSION_A}#0" in env.documents


# ── Isolation ────────────────────────────────────────────────────────────────
class TestIsolation:
    def test_two_users_documents_never_cross(self, env):
        a = _archive(_turn(USER_A, SESSION_A, 0, user="alpha question", assistant="alpha answer"))
        b = _archive(_turn(USER_B, SESSION_B, 0, user="beta question", assistant="beta answer"))

        c.lambda_handler(_sqs(_event(a), _event(b)), None)

        assert env.visible_to(USER_A) == [f"conv#{USER_A}#{SESSION_A}#0"]
        assert env.visible_to(USER_B) == [f"conv#{USER_B}#{SESSION_B}#0"]

    def test_two_users_with_the_same_session_id_keep_separate_documents(self, env):
        """Session ids are not unique across users. Under the first id format both
        turns were one document: a batch holding both was refused as duplicates
        (dev, 2026-10-07), and apart, one replaced the other."""
        a = _archive(_turn(USER_A, SESSION_A, 0, user="alpha question", assistant="alpha answer"))
        b = _archive(_turn(USER_B, SESSION_A, 0, user="beta question", assistant="beta answer"))

        result = c.lambda_handler(_sqs(_event(a), _event(b)), None)

        assert result == {"batchItemFailures": []}
        assert env.visible_to(USER_A) == [f"conv#{USER_A}#{SESSION_A}#0"]
        assert env.visible_to(USER_B) == [f"conv#{USER_B}#{SESSION_A}#0"]

        _s3().delete_object(Bucket=BUCKET, Key=b)
        c.lambda_handler(_sqs(_event(b, "Object Deleted")), None)

        assert env.visible_to(USER_A) == [f"conv#{USER_A}#{SESSION_A}#0"]
        assert env.visible_to(USER_B) == []

    def test_a_body_naming_another_user_is_refused(self, env):
        # Stored under USER_B's key, but the body says USER_A wrote it.
        key = archive_key(USER_B, SESSION_A, 0)
        _s3().put_object(Bucket=BUCKET, Key=key, Body=_turn(USER_A, SESSION_A, 0).to_json())

        result = c.lambda_handler(_sqs(_event(key)), None)

        assert result == {"batchItemFailures": []}
        assert env.ingest_calls == []
        assert env.documents == {}


# ── What is ignored ──────────────────────────────────────────────────────────
class TestIgnored:
    def test_flag_off_does_nothing(self, env, monkeypatch):
        monkeypatch.setenv("CONVERSATION_INDEX_ENABLED", "")
        key = _archive(_turn())

        def must_not_read(*args, **kwargs):
            raise AssertionError("read the archive with the flag off")

        monkeypatch.setattr(c, "read_archive_object", must_not_read)
        result = c.lambda_handler(_sqs(_event(key), _event(key, "Object Deleted")), None)

        assert result == {"batchItemFailures": []}
        assert env.ingest_calls == [] and env.delete_calls == [] and env.created_kbs == []

    @pytest.mark.parametrize(
        "key",
        [
            "conversations/user-aaa/000001.json",  # missing the session segment
            "conversations/user-aaa/sess/extra/000001.json",  # one segment too many
            "conversations/user-aaa/sess/1.json",  # not zero-padded
            "conversations//sess/000001.json",  # empty user
            "other/user-aaa/sess/000001.json",  # outside the prefix
        ],
    )
    def test_a_malformed_key_is_acknowledged_untouched(self, env, key):
        _s3().put_object(Bucket=BUCKET, Key=key, Body=_turn().to_json())

        assert c.lambda_handler(_sqs(_event(key)), None) == {"batchItemFailures": []}
        assert env.ingest_calls == [] and env.delete_calls == []

    def test_another_bucket_is_ignored(self, env):
        key = _archive(_turn())
        assert c.lambda_handler(_sqs(_event(key, bucket="someone-else")), None) == {
            "batchItemFailures": []
        }
        assert env.ingest_calls == []

    def test_a_non_json_body_is_acknowledged(self, env):
        assert c.lambda_handler(_sqs("not json"), None) == {"batchItemFailures": []}

    def test_an_oversized_object_is_skipped_not_retried(self, env):
        key = archive_key(USER_A, SESSION_A, 0)
        _s3().put_object(Bucket=BUCKET, Key=key, Body=b"x" * (c.MAX_OBJECT_BYTES + 1))

        assert c.lambda_handler(_sqs(_event(key)), None) == {"batchItemFailures": []}
        assert env.ingest_calls == [] and env.delete_calls == []

    def test_a_malformed_object_is_skipped_not_retried(self, env):
        key = archive_key(USER_A, SESSION_A, 0)
        _s3().put_object(Bucket=BUCKET, Key=key, Body=b'{"schemaVersion": 99}')

        assert c.lambda_handler(_sqs(_event(key)), None) == {"batchItemFailures": []}
        assert env.ingest_calls == []


# ── Partial batch failure ────────────────────────────────────────────────────
class TestPartialFailure:
    def test_only_the_failed_messages_are_retried(self, env):
        _provision(env)
        created = _archive(_turn(index=0))
        gone = archive_key(USER_A, SESSION_A, 2)
        env.fail_ingest = RuntimeError("ThrottlingException")

        result = c.lambda_handler(
            _sqs(_event(created), _event(gone, "Object Deleted"), ids=["ingest", "delete"]), None
        )

        assert result == {"batchItemFailures": [{"itemIdentifier": "ingest"}]}
        assert len(env.delete_calls) == 1


# ── Provisioning ─────────────────────────────────────────────────────────────
class TestProvisioning:
    def test_first_ingest_provisions_the_shared_kb_once(self, env):
        c.lambda_handler(_sqs(_event(_archive(_turn(index=0)))), None)
        c.lambda_handler(_sqs(_event(_archive(_turn(index=2)))), None)

        assert len(env.created_kbs) == 1
        assert len(env.created_data_sources) == 1
        record = r.get_kb_record(CONVERSATIONS_KB_ID, CONVERSATIONS_KB_ID)
        assert record["PK"] == "AST#conversations"
        assert record["SK"] == "KB#conversations"
        assert record["awsKbId"] == "KBCONV0001"
        assert record["awsDataSourceId"] == "DSCONV0001"
        assert record["ownerUserId"] == c.KB_OWNER

    def test_the_kb_carries_the_contract_tags_and_its_purpose(self, env):
        c.lambda_handler(_sqs(_event(_archive(_turn()))), None)

        tags = env.created_kbs[0]["tags"]
        assert tags["purpose"] == "conversation-search"
        assert tags[kb_tags.TAG_KEY_APP_KB_ID] == CONVERSATIONS_KB_ID
        assert tags[kb_tags.TAG_KEY_PREFIX] == "test-project"
        assert tags[kb_tags.TAG_KEY_ENVIRONMENT] == "dev"

    def test_a_kb_being_provisioned_elsewhere_is_waited_for(self, env, assistants_table):
        # Another invocation won the record and is mid-create.
        r.create_provisioning(
            CONVERSATIONS_KB_ID,
            provisioning.new_managed_kb_record(CONVERSATIONS_KB_ID, c.KB_OWNER, client_token="t" * 40),
        )
        polls = []

        async def other_invocation_finishes(seconds):
            polls.append(seconds)
            r.attach_aws_ids(CONVERSATIONS_KB_ID, CONVERSATIONS_KB_ID, "KBOTHER", "DSOTHER", CREATED_AT)

        import asyncio

        asyncio.run(c.ensure_conversations_kb(env, sleep=other_invocation_finishes))

        assert polls  # it waited
        assert env.created_kbs == []  # and did not create a second knowledge base

    def test_losing_the_record_race_waits_for_the_winner(self, env, monkeypatch):
        import asyncio

        async def lost(*args, **kwargs):
            raise provisioning.ProvisioningInProgress("another worker")

        async def winner_finishes(seconds):
            if r.get_kb_record(CONVERSATIONS_KB_ID, CONVERSATIONS_KB_ID) is None:
                r.create_provisioning(
                    CONVERSATIONS_KB_ID,
                    provisioning.new_managed_kb_record(CONVERSATIONS_KB_ID, c.KB_OWNER, client_token="t" * 40),
                )
                r.attach_aws_ids(CONVERSATIONS_KB_ID, CONVERSATIONS_KB_ID, "KBWIN", "DSWIN", CREATED_AT)

        monkeypatch.setattr(provisioning, "provision_managed_kb", lost)
        asyncio.run(c.ensure_conversations_kb(env, sleep=winner_finishes))

        assert r.get_kb_record(CONVERSATIONS_KB_ID, CONVERSATIONS_KB_ID)["awsKbId"] == "KBWIN"

    def test_a_kb_that_never_finishes_fails_the_batch(self, env, monkeypatch):
        import asyncio

        async def lost(*args, **kwargs):
            raise provisioning.ProvisioningInProgress("another worker")

        async def no_progress(seconds):
            return None

        monkeypatch.setattr(provisioning, "provision_managed_kb", lost)
        with pytest.raises(provisioning.ProvisioningInProgress):
            asyncio.run(
                c.ensure_conversations_kb(env, sleep=no_progress, wait_seconds=10, poll_seconds=5)
            )
