"""Managed knowledge base provisioning, retrieval, ingestion and deletion.

Every AWS interaction here is stubbed. The ``bedrock-agent`` and
``bedrock-agent-runtime`` clients are hand-rolled fakes (no network client is ever
constructed) and DynamoDB is moto, so the conditional writes the provisioning saga
depends on are exercised for real rather than mocked into always succeeding.
Nothing in this file can reach AWS: there is no ``boto3.client`` call for either
Bedrock service, and moto intercepts the DynamoDB ones.

Two things are asserted here that no other test in the suite can see:

* **Order.** The KB_Record must exist, in ``provisioning``, *before*
  ``CreateKnowledgeBase`` is called. The fake client reads the record from moto at
  the moment it is called, which turns "written first" from a code-reading
  exercise into an assertion.
* **Shape.** The managed API shapes contradict the documentation in several
  places — a 33-character ``clientToken`` minimum, a 10-document batch cap where
  the user guide says 25, a nested connector type, ``managedSearchConfiguration``
  instead of ``vectorSearchConfiguration``. Each is pinned by a test whose failure
  message says why, because each looks like a mistake to anyone who checks the
  docs.

Feature: managed-kb-migration
Requirements: 7.1-7.8, 8.1-8.8, 9.1-9.6, 11.1-11.5, 20.7, 24.2, 24.3, 24.11
"""

import ast
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import boto3
import pytest
from moto import mock_aws

from apis.shared.kb_backend import tags as kb_tags
from apis.shared.kb_backend import managed_backend as mb
from apis.shared.kb_backend import provisioning as p
from apis.shared.kb_backend import records as r
from apis.shared.kb_backend.protocol import DEFAULT_TOP_K, Chunk, DocumentSource
from apis.shared.kb_backend.protocol import KnowledgeBaseBackend

REGION = "us-east-1"
TABLE = "test-managed-kb"
ASSISTANT_ID = "ast-managed-001"
APP_KB_ID = ASSISTANT_ID  # App_KB_Id == assistant_id in this phase
OWNER = "owner-opaque-42"
ROLE_ARN = "arn:aws:iam::123456789012:role/test-managed-kb-role"
AWS_KB_ID = "KBAAAAAAAA"
AWS_DS_ID = "DSAAAAAAAA"


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class FakeBedrockAgent:
    """A ``bedrock-agent`` control-plane stub with real idempotency semantics.

    ``clientToken`` deduplication is modelled rather than ignored, because it is
    the mechanism the crash-recovery path relies on: a retry that reuses the token
    must receive the *same* knowledge base, not a second one. A stub that minted a
    fresh id per call would let a duplicate-creating bug pass.

    ``on_create`` runs at the moment ``create_knowledge_base`` is entered, which is
    how the record-before-AWS ordering is observed.
    """

    def __init__(
        self,
        *,
        on_create=None,
        create_failures: Optional[List[Exception]] = None,
    ) -> None:
        self.create_kb_calls: List[Dict[str, Any]] = []
        self.create_ds_calls: List[Dict[str, Any]] = []
        self.ingest_calls: List[Dict[str, Any]] = []
        self.delete_calls: List[Dict[str, Any]] = []
        self.start_ingestion_job_calls: List[Dict[str, Any]] = []
        self.thread_idents: List[int] = []
        self.observed_records: List[Optional[Dict[str, Any]]] = []
        self._by_token: Dict[str, str] = {}
        self._ds_by_token: Dict[str, str] = {}
        self._on_create = on_create
        self._create_failures = list(create_failures or [])
        self._counter = 0
        self._lock = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0

    # -- provisioning ------------------------------------------------------
    def create_knowledge_base(self, **kwargs):
        self.thread_idents.append(threading.get_ident())
        if self._on_create is not None:
            self.observed_records.append(self._on_create())
        self.create_kb_calls.append(kwargs)

        if self._create_failures:
            raise self._create_failures.pop(0)

        token = kwargs["clientToken"]
        if token not in self._by_token:
            self._counter += 1
            self._by_token[token] = f"KB{self._counter:08d}"
        return {"knowledgeBase": {"knowledgeBaseId": self._by_token[token], "status": "ACTIVE"}}

    def create_data_source(self, **kwargs):
        self.thread_idents.append(threading.get_ident())
        self.create_ds_calls.append(kwargs)
        token = kwargs["clientToken"]
        if token not in self._ds_by_token:
            self._ds_by_token[token] = f"DS{len(self._ds_by_token) + 1:08d}"
        return {"dataSource": {"dataSourceId": self._ds_by_token[token], "status": "AVAILABLE"}}

    @property
    def distinct_knowledge_base_ids(self) -> set:
        return set(self._by_token.values())

    # -- documents ---------------------------------------------------------
    def _enter(self):
        with self._lock:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)

    def _exit(self):
        with self._lock:
            self._in_flight -= 1

    def ingest_knowledge_base_documents(self, **kwargs):
        self._enter()
        try:
            self.thread_idents.append(threading.get_ident())
            self.ingest_calls.append(kwargs)
            # Real calls take time; without a pause every coroutine would finish
            # before the next started and the concurrency bound would be
            # untestable (max in flight would read 1 no matter what it was).
            threading.Event().wait(0.02)
            return {"documentDetails": []}
        finally:
            self._exit()

    def delete_knowledge_base_documents(self, **kwargs):
        self._enter()
        try:
            self.delete_calls.append(kwargs)
            threading.Event().wait(0.02)
            return {"documentDetails": []}
        finally:
            self._exit()

    def start_ingestion_job(self, **kwargs):  # pragma: no cover - must never run
        self.start_ingestion_job_calls.append(kwargs)
        raise AssertionError(
            "StartIngestionJob must never be called (Requirement 9.2): it is "
            "0.1 RPS account-wide and not adjustable"
        )


class FakeBedrockAgentRuntime:
    """A ``bedrock-agent-runtime`` stub returning canned ``Retrieve`` results."""

    def __init__(self, results: Optional[List[Dict[str, Any]]] = None) -> None:
        self.results = results if results is not None else []
        self.calls: List[Dict[str, Any]] = []
        self.thread_idents: List[int] = []

    def retrieve(self, **kwargs):
        self.thread_idents.append(threading.get_ident())
        self.calls.append(kwargs)
        return {"retrievalResults": self.results}


def _client_error(code: str, message: str):
    """A ``ClientError`` shaped exactly as botocore raises one."""
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": message}}, "CreateKnowledgeBase")


def _result(document_id: str, score: float, text: str = "passage") -> Dict[str, Any]:
    """One ``Retrieve`` result, as the service returns it for a CUSTOM connector."""
    return {
        "content": {"text": text},
        "score": score,
        "location": {
            "type": "CUSTOM",
            "customDocumentLocation": {"id": document_id},
        },
        "metadata": {"filename": f"{document_id}.pdf"},
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def table(monkeypatch):
    """The assistants table, including the GSI7 work index.

    Built here rather than taken from the shared ``assistants_table`` fixture,
    which predates GSI7 — the same reason ``test_kb_records.py`` builds its own.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
    monkeypatch.setenv(kb_tags.ENV_TAG_VALUE_PREFIX, "test-prefix")
    monkeypatch.setenv(kb_tags.ENV_TAG_VALUE_ENVIRONMENT, "test")

    with mock_aws():
        ddb = boto3.client("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI7_PK", "AttributeType": "S"},
                {"AttributeName": "GSI7_SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "KbWorkIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI7_PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI7_SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


def _record(table) -> Optional[Dict[str, Any]]:
    return table.get_item(
        Key={"PK": r.kb_pk(ASSISTANT_ID), "SK": r.kb_sk(APP_KB_ID)}
    ).get("Item")


async def _no_sleep(_seconds: float) -> None:
    """Retry backoff, without the wait."""
    return None


async def _provision(client, **overrides):
    kwargs: Dict[str, Any] = dict(
        assistant_id=ASSISTANT_ID,
        app_kb_id=APP_KB_ID,
        owner_user_id=OWNER,
        role_arn=ROLE_ARN,
        client=client,
        region=REGION,
        sleep=_no_sleep,
    )
    kwargs.update(overrides)
    return await p.provision_managed_kb(**kwargs)


def _locator(kb_id: str = AWS_KB_ID, ds_id: str = AWS_DS_ID):
    def _locate(_kb_ref: str) -> Tuple[str, str]:
        return kb_id, ds_id

    return _locate


# ===========================================================================
# 8.1 — clientToken construction
# ===========================================================================


class TestClientToken:
    """The 33-character minimum, which the natural template silently violates."""

    def test_built_token_clears_the_minimum(self):
        token = p.build_client_token(APP_KB_ID, "knowledge-base")
        assert len(token) >= p.CLIENT_TOKEN_MIN_LENGTH

    def test_the_natural_interpolated_template_is_too_short_and_is_rejected(self):
        """This is the bug the builder exists to prevent, spelled out.

        ``{id}-{variant}-kb`` reads as obviously fine and is 31 characters for a
        realistic id, four short of the API's minimum. It fails in botocore's
        client-side validation, so there is no service error and no request id to
        search for.
        """
        naive = f"{APP_KB_ID}-managed-kb"
        assert len(naive) < p.CLIENT_TOKEN_MIN_LENGTH, (
            "the premise of this test is that the natural template is too short; "
            f"{naive!r} is {len(naive)} characters"
        )
        with pytest.raises(ValueError, match="at least 33 characters"):
            p.validate_client_token(naive)

    @pytest.mark.parametrize("app_kb_id", ["a", "ast-1", "x" * 300, "ast_with_underscores"])
    def test_tokens_are_valid_for_any_input_length(self, app_kb_id):
        """Short, long and illegal-alphabet inputs all produce a legal token."""
        token = p.build_client_token(app_kb_id, "knowledge-base")
        p.validate_client_token(token)  # raises if not
        assert p.CLIENT_TOKEN_MIN_LENGTH <= len(token) <= p.CLIENT_TOKEN_MAX_LENGTH
        assert p.CLIENT_TOKEN_PATTERN.match(token)

    def test_token_is_deterministic_so_a_retry_reuses_it(self):
        """Determinism is what makes AWS deduplicate a retried create."""
        assert p.build_client_token(APP_KB_ID, "knowledge-base") == p.build_client_token(
            APP_KB_ID, "knowledge-base"
        )

    def test_different_knowledge_bases_get_different_tokens(self):
        assert p.build_client_token("ast-a", "knowledge-base") != p.build_client_token(
            "ast-b", "knowledge-base"
        )

    def test_kb_and_data_source_tokens_differ(self):
        """Two idempotency scopes, two tokens; sharing one would conflate them."""
        assert p.build_client_token(APP_KB_ID, "knowledge-base") != p.build_client_token(
            APP_KB_ID, "data-source"
        )

    def test_a_token_may_not_end_with_a_hyphen(self):
        with pytest.raises(ValueError, match="pattern"):
            p.validate_client_token("a" * 40 + "-")

    def test_an_over_long_token_is_rejected(self):
        with pytest.raises(ValueError, match="at most 256"):
            p.validate_client_token("a" * 257)


# ===========================================================================
# 8.1 / 8.2 — payload shapes
# ===========================================================================


class TestKnowledgeBasePayload:
    def _payload(self):
        return p.knowledge_base_payload(
            name="test-prefix-kb-ast",
            role_arn=ROLE_ARN,
            client_token=p.build_client_token(APP_KB_ID, "knowledge-base"),
            region=REGION,
        )

    def test_type_is_managed(self):
        assert self._payload()["knowledgeBaseConfiguration"]["type"] == "MANAGED"

    def test_managed_configuration_is_present(self):
        assert "managedKnowledgeBaseConfiguration" in self._payload()["knowledgeBaseConfiguration"]

    def test_storage_configuration_is_omitted_entirely(self):
        """Requirement 8.2. There is no vector store, and sending one is rejected."""
        assert "storageConfiguration" not in self._payload()

    def test_role_arn_is_passed(self):
        assert self._payload()["roleArn"] == ROLE_ARN

    def test_embedding_is_pinned_to_titan_v2_float32_1024(self):
        """Requirement 8.5, and immutable from here on (8.8).

        A drift in any of these three values is not a migration but a rebuild, so
        the numbers are asserted rather than merely present.
        """
        managed = self._payload()["knowledgeBaseConfiguration"][
            "managedKnowledgeBaseConfiguration"
        ]
        assert managed["embeddingModelType"] == "CUSTOM"
        assert managed["embeddingModelArn"].endswith("amazon.titan-embed-text-v2:0")
        bedrock_config = managed["embeddingModelConfiguration"][
            "bedrockEmbeddingModelConfiguration"
        ]
        assert bedrock_config["dimensions"] == 1024
        assert bedrock_config["embeddingDataType"] == "FLOAT32"

    def test_kms_key_is_only_sent_when_supplied(self):
        assert "serverSideEncryptionConfiguration" not in self._payload()[
            "knowledgeBaseConfiguration"
        ]["managedKnowledgeBaseConfiguration"]

        with_kms = p.knowledge_base_payload(
            name="n",
            role_arn=ROLE_ARN,
            client_token=p.build_client_token(APP_KB_ID, "kb"),
            kms_key_arn="arn:aws:kms:us-east-1:123456789012:key/abc",
        )
        assert (
            with_kms["knowledgeBaseConfiguration"]["managedKnowledgeBaseConfiguration"][
                "serverSideEncryptionConfiguration"
            ]["kmsKeyArn"]
            == "arn:aws:kms:us-east-1:123456789012:key/abc"
        )

    def test_a_short_client_token_is_refused_before_the_request_is_built(self):
        with pytest.raises(ValueError, match="at least 33"):
            p.knowledge_base_payload(name="n", role_arn=ROLE_ARN, client_token="short")


class TestDataSourcePayload:
    def _payload(self):
        return p.data_source_payload(
            knowledge_base_id=AWS_KB_ID,
            name="test-prefix-kb-ast",
            client_token=p.build_client_token(APP_KB_ID, "data-source"),
        )

    def test_data_source_type_is_the_managed_connector(self):
        """Requirement 8.3. A top-level ``CUSTOM`` is rejected outright."""
        config = self._payload()["dataSourceConfiguration"]
        assert config["type"] == "MANAGED_KNOWLEDGE_BASE_CONNECTOR"
        assert config["type"] != "CUSTOM", (
            "the classic top-level CUSTOM/S3/WEB types are rejected for managed "
            "knowledge bases with 'Unsupported data source type'"
        )

    def test_the_real_connector_type_is_nested_in_connector_parameters(self):
        """Requirement 8.4 — ``CUSTOM`` lives one level down, not at the top."""
        connector = self._payload()["dataSourceConfiguration"][
            "managedKnowledgeBaseConnectorConfiguration"
        ]
        assert connector["connectorParameters"] == {"type": "CUSTOM", "version": "1"}

    def test_image_extraction_is_enabled(self):
        """Requirement 8.6. Opt-in: left default, no image or chart content is
        indexed at all, silently, while the bill is unchanged."""
        connector = self._payload()["dataSourceConfiguration"][
            "managedKnowledgeBaseConnectorConfiguration"
        ]
        status = connector["mediaExtractionConfiguration"]["imageExtractionConfiguration"][
            "imageExtractionStatus"
        ]
        assert status == "ENABLED"

    def test_data_deletion_policy_is_retain_at_creation(self):
        """Requirement 8.7 — the documented remedy for DELETE_UNSUCCESSFUL, and it
        must be set at creation because it cannot rescue a stuck knowledge base
        afterwards. The dev account has one stuck since 2025-11-24."""
        assert self._payload()["dataDeletionPolicy"] == "RETAIN"

    def test_a_short_client_token_is_refused(self):
        with pytest.raises(ValueError, match="at least 33"):
            p.data_source_payload(knowledge_base_id=AWS_KB_ID, name="n", client_token="short")


class TestTags:
    def test_the_resource_name_uses_the_same_prefix_as_the_tags(self, monkeypatch):
        """A name that says one deployment while the tag says another is the kind
        of thing an operator reads once and trusts.

        Every filter in this feature matches on tags, so the name is only a
        convention — but it is resolved through the same helper precisely so the
        two cannot disagree. Asserted because the docstring claims it.
        """
        monkeypatch.setenv(kb_tags.ENV_TAG_VALUE_PREFIX, "from-tag-var")
        monkeypatch.delenv("PROJECT_PREFIX", raising=False)

        name = p._resource_name("ast-1")
        tags = p.build_tags("ast-1", "u-1")

        assert name.startswith(f"{tags[kb_tags.TAG_KEY_PREFIX]}-kb-")
        assert name == "from-tag-var-kb-ast-1"

    def test_tags_carry_prefix_env_kb_and_owner(self):
        tags = p.build_tags(APP_KB_ID, OWNER, project_prefix="pfx", environment="dev")
        assert tags == {
            kb_tags.TAG_KEY_PREFIX: "pfx",
            kb_tags.TAG_KEY_ENVIRONMENT: "dev",
            kb_tags.TAG_KEY_APP_KB_ID: APP_KB_ID,
            kb_tags.TAG_KEY_OWNER_USER_ID: OWNER,
        }

    def test_an_email_owner_tag_is_refused(self):
        """Requirement 20.12. Tags are readable by anyone with ListKnowledgeBases."""
        with pytest.raises(ValueError, match="opaque identifier"):
            p.build_tags(APP_KB_ID, "student@example.edu")


# ===========================================================================
# 8.1 — the saga
# ===========================================================================


class TestProvisioningSaga:
    @pytest.mark.asyncio
    async def test_happy_path_creates_and_attaches(self, table):
        client = FakeBedrockAgent()
        result = await _provision(client)

        assert result.created is True
        assert len(client.create_kb_calls) == 1
        assert len(client.create_ds_calls) == 1

        item = _record(table)
        assert item["awsKbId"] == result.aws_kb_id
        assert item["awsDataSourceId"] == result.aws_data_source_id
        assert item["provisioningState"] == r.ACTIVE

    @pytest.mark.asyncio
    async def test_the_record_exists_in_provisioning_before_aws_is_called(self, table):
        """Requirement 7.3, and the reason this whole ordering exists.

        The fake client reads the record at the instant ``CreateKnowledgeBase`` is
        entered. Written afterwards instead, a crash in between would leave a
        billed knowledge base that no record points at and nothing can find — no
        exception, no alarm, just an invoice.
        """
        client = FakeBedrockAgent(on_create=lambda: _record(table))
        await _provision(client)

        assert client.observed_records, "the create hook never ran"
        seen = client.observed_records[0]
        assert seen is not None, (
            "CreateKnowledgeBase was called before the KB_Record existed: a crash "
            "at that point would strand an untraceable paying resource"
        )
        assert seen["provisioningState"] == r.PROVISIONING
        assert "awsKbId" not in seen

    @pytest.mark.asyncio
    async def test_the_client_token_is_persisted_and_is_the_one_sent(self, table):
        """Persisted so a *later* process, with no memory of this one, can retry."""
        client = FakeBedrockAgent()
        await _provision(client)

        stored = _record(table)["clientToken"]
        assert len(stored) >= p.CLIENT_TOKEN_MIN_LENGTH
        assert client.create_kb_calls[0]["clientToken"] == stored

    @pytest.mark.asyncio
    async def test_tags_are_applied_at_creation(self, table):
        client = FakeBedrockAgent()
        await _provision(client)
        tags = client.create_kb_calls[0]["tags"]
        assert tags[kb_tags.TAG_KEY_APP_KB_ID] == APP_KB_ID
        assert tags[kb_tags.TAG_KEY_OWNER_USER_ID] == OWNER

    @pytest.mark.asyncio
    async def test_a_second_call_creates_nothing(self, table):
        """Requirement 7.4 — idempotent once the record carries both identifiers."""
        client = FakeBedrockAgent()
        first = await _provision(client)
        second = await _provision(client)

        assert second.created is False
        assert (second.aws_kb_id, second.aws_data_source_id) == (
            first.aws_kb_id,
            first.aws_data_source_id,
        )
        assert len(client.create_kb_calls) == 1

    @pytest.mark.asyncio
    async def test_the_loser_of_the_enrolment_race_does_not_create_a_second_kb(self, table):
        """Requirement 7.4. The loser must wait, not proceed.

        A loser that carried on with its own token would create a second knowledge
        base, and only one of the two could ever be recorded — the other would be
        billed forever with nothing pointing at it.
        """
        r.create_provisioning(
            ASSISTANT_ID,
            r.KbRecord(app_kb_id=APP_KB_ID, owner_user_id=OWNER, client_token="z" * 40),
        )
        # Simulate the race: the winner's record is already there, so the loser's
        # own create_provisioning is rejected by the attribute_not_exists guard.
        client = FakeBedrockAgent()
        table.delete_item(Key={"PK": r.kb_pk(ASSISTANT_ID), "SK": r.kb_sk(APP_KB_ID)})

        original = r.create_provisioning

        def _lose(assistant_id, record):
            original(assistant_id, record)  # the "winner" writes first
            raise r.TransitionLost("simulated race loss")

        r.create_provisioning = _lose
        try:
            with pytest.raises(p.ProvisioningInProgress):
                await _provision(client)
        finally:
            r.create_provisioning = original

        assert client.create_kb_calls == [], (
            "the losing worker called CreateKnowledgeBase anyway, which is how a "
            "second knowledge base gets created for one record"
        )

    @pytest.mark.asyncio
    async def test_provisioning_requires_a_service_role(self, table):
        with pytest.raises(p.ProvisioningError, match="service role"):
            await _provision(FakeBedrockAgent(), role_arn=None)


class TestRetryableEmbeddingVerification:
    """Requirement 7.7 — the failure that looks fatal and is not."""

    @pytest.mark.asyncio
    async def test_embedding_verification_failure_is_retried_and_succeeds(self, table):
        """It is IAM eventual consistency against a model confirmed ACTIVE.

        Treated as fatal, lazy provisioning fails intermittently and the message
        sends the operator to check a model that is demonstrably fine.
        """
        client = FakeBedrockAgent(
            create_failures=[
                _client_error(
                    "ValidationException",
                    "Unable to verify the specified embedding model",
                )
            ]
        )
        result = await _provision(client)

        assert result.created is True
        assert len(client.create_kb_calls) == 2, (
            "the embedding-verification failure was not retried"
        )
        assert _record(table)["provisioningState"] == r.ACTIVE

    @pytest.mark.asyncio
    async def test_retries_reuse_the_same_client_token(self, table):
        """Otherwise a retry is a second create, not a retry."""
        client = FakeBedrockAgent(
            create_failures=[
                _client_error("ValidationException", "unable to verify the specified embedding model")
            ]
        )
        await _provision(client)
        tokens = {call["clientToken"] for call in client.create_kb_calls}
        assert len(tokens) == 1
        assert client.distinct_knowledge_base_ids == {"KB00000001"}

    @pytest.mark.asyncio
    async def test_a_genuine_validation_error_is_not_retried(self, table):
        """A malformed request must fail fast: retrying only delays the report."""
        client = FakeBedrockAgent(
            create_failures=[_client_error("ValidationException", "roleArn is invalid")]
        )
        with pytest.raises(Exception, match="roleArn is invalid"):
            await _provision(client)
        assert len(client.create_kb_calls) == 1

    def test_classification_of_the_embedding_message(self):
        assert p.is_retryable_error(
            _client_error("ValidationException", "Unable to verify the specified embedding model")
        )
        assert p.is_retryable_error(_client_error("ThrottlingException", "slow down"))
        assert not p.is_retryable_error(_client_error("AccessDeniedException", "no"))


# ===========================================================================
# 8.6 — crash between the AWS create and the record update
# ===========================================================================


class TestCrashBetweenCreateAndRecordUpdate:
    """Requirements 7.8, 24.3.

    The window that record-first ordering exists to make survivable: the AWS
    knowledge base exists, the record does not yet name it.
    """

    @pytest.mark.asyncio
    async def test_the_record_survives_as_a_discoverable_retry_anchor(self, table):
        client = FakeBedrockAgent()
        crashed = []

        def _crash(*_args, **_kwargs):
            crashed.append(True)
            raise RuntimeError("worker died before attaching identifiers")

        original = r.attach_aws_ids
        r.attach_aws_ids = _crash
        try:
            with pytest.raises(RuntimeError, match="worker died"):
                await _provision(client)
        finally:
            r.attach_aws_ids = original

        assert crashed, "the crash never happened; the test proves nothing"
        assert len(client.create_kb_calls) == 1

        anchor = _record(table)
        assert anchor is not None, (
            "the KB_Record vanished, so the created knowledge base is now an "
            "orphan nothing can find (Requirement 7.8)"
        )
        assert anchor["provisioningState"] == r.PROVISIONING
        assert "awsKbId" not in anchor
        assert anchor["clientToken"], (
            "the anchor carries no clientToken, so a retry cannot be deduplicated "
            "and would create a second knowledge base"
        )

    @pytest.mark.asyncio
    async def test_the_retry_does_not_create_a_second_knowledge_base(self, table):
        """The whole point: one knowledge base across a crash and a retry."""
        client = FakeBedrockAgent()

        original = r.attach_aws_ids
        r.attach_aws_ids = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash"))
        try:
            with pytest.raises(RuntimeError):
                await _provision(client)
        finally:
            r.attach_aws_ids = original

        result = await _provision(client)  # the retry

        assert len(client.create_kb_calls) == 2, "the retry did not re-issue the create"
        assert client.distinct_knowledge_base_ids == {"KB00000001"}, (
            "the retry created a SECOND knowledge base: the persisted clientToken "
            "was not reused, so AWS did not deduplicate"
        )
        assert result.aws_kb_id == "KB00000001"
        assert _record(table)["provisioningState"] == r.ACTIVE

    @pytest.mark.asyncio
    async def test_a_crash_after_the_data_source_still_converges(self, table):
        """Both AWS resources exist; only the final write was lost."""
        client = FakeBedrockAgent()

        original = r.attach_aws_ids
        r.attach_aws_ids = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash"))
        try:
            with pytest.raises(RuntimeError):
                await _provision(client)
        finally:
            r.attach_aws_ids = original

        assert len(client.create_ds_calls) == 1
        await _provision(client)

        # One data source, because the data-source token is reused too.
        assert len({call["clientToken"] for call in client.create_ds_calls}) == 1
        item = _record(table)
        assert item["awsDataSourceId"] == "DS00000001"


# ===========================================================================
# 20.7 — synchronous boto3 calls run off the event loop
# ===========================================================================


class TestOffEventLoop:
    @pytest.mark.asyncio
    async def test_create_knowledge_base_runs_in_a_worker_thread(self, table):
        """``CreateKnowledgeBase`` blocks for 47-124 s; on the loop thread that
        would stall every other coroutine, including the health check."""
        client = FakeBedrockAgent()
        loop_thread = threading.get_ident()
        await _provision(client)

        assert client.thread_idents, "no AWS call was recorded"
        assert all(ident != loop_thread for ident in client.thread_idents)

    @pytest.mark.asyncio
    async def test_retrieve_runs_in_a_worker_thread(self):
        runtime = FakeBedrockAgentRuntime([_result("doc-a", 0.9)])
        backend = mb.ManagedKbBackend(runtime_client=runtime, locator=_locator())
        loop_thread = threading.get_ident()

        await backend.search(APP_KB_ID, "query")

        assert runtime.thread_idents and runtime.thread_idents[0] != loop_thread

    @pytest.mark.asyncio
    async def test_ingest_runs_in_a_worker_thread(self):
        agent = FakeBedrockAgent()
        backend = mb.ManagedKbBackend(agent_client=agent, locator=_locator())
        loop_thread = threading.get_ident()

        await backend.ingest(APP_KB_ID, DocumentSource("doc-a", "a.pdf", chunks=["x"]))

        assert agent.thread_idents and agent.thread_idents[0] != loop_thread


# ===========================================================================
# 8.3 — managed retrieval
# ===========================================================================


class TestRetrievalConfiguration:
    def test_uses_managed_search_configuration(self):
        """Requirement 11.1. ``vectorSearchConfiguration`` is a valid *member* of
        the request shape, so it passes client-side validation and is then rejected
        by the service for managed knowledge bases — every retrieval fails."""
        config = mb.retrieval_configuration()
        assert "managedSearchConfiguration" in config
        assert "vectorSearchConfiguration" not in config

    def test_number_of_results_defaults_to_the_parity_five(self):
        assert mb.retrieval_configuration()["managedSearchConfiguration"][
            "numberOfResults"
        ] == 5
        assert DEFAULT_TOP_K == 5

    def test_reranking_is_managed_not_none(self):
        """Requirement 11.2. Managed reranking separates the scores
        (0.89/0.38/0.25/0.21/0.19 versus a nearly flat 1.00/0.84/0.78/0.77/0.77),
        and that separation is what makes the 2,000-character cap defensible."""
        managed = mb.retrieval_configuration()["managedSearchConfiguration"]
        assert managed["rerankingModelType"] == "MANAGED"
        assert managed["rerankingModelType"] != "NONE"

    def test_no_hybrid_search_key_is_sent(self):
        """Requirement 11.3 — not toggleable, and not attempted."""
        managed = mb.retrieval_configuration()["managedSearchConfiguration"]
        assert not any("hybrid" in key.lower() or key == "overrideSearchType" for key in managed)

    def test_top_k_is_honoured(self):
        assert mb.retrieval_configuration(3)["managedSearchConfiguration"][
            "numberOfResults"
        ] == 3

    @pytest.mark.parametrize("operator", ["equals", "in"])
    def test_exact_match_filters_are_permitted(self, operator):
        config = mb.retrieval_configuration(
            5, {operator: {"key": "assistant_id", "value": APP_KB_ID}}
        )
        assert operator in config["managedSearchConfiguration"]["filter"]

    @pytest.mark.parametrize("operator", ["startsWith", "stringContains", "notEquals"])
    def test_non_exact_filters_are_refused(self, operator):
        """Requirement 11.5. A prefix filter isolating 'ast-1' also admits
        'ast-10', and the over-match looks like an ordinary result."""
        with pytest.raises(mb.UnsafeFilterOperator, match=operator):
            mb.retrieval_configuration(5, {operator: {"key": "k", "value": "v"}})

    def test_an_unsafe_operator_nested_in_a_compound_filter_is_refused(self):
        """A compound filter is only as safe as its least safe leaf."""
        with pytest.raises(mb.UnsafeFilterOperator, match="startsWith"):
            mb.retrieval_configuration(
                5,
                {
                    "andAll": [
                        {"equals": {"key": "a", "value": "1"}},
                        {"orAll": [{"startsWith": {"key": "b", "value": "2"}}]},
                    ]
                },
            )


class TestManagedSearch:
    @pytest.mark.asyncio
    async def test_score_is_relevance_and_is_not_converted(self):
        """The inversion that raises nothing and just makes answers worse.

        ``Retrieve`` already returns higher-is-better, which is the protocol's
        direction, so this adapter must pass the score through. Negating it here —
        copying the legacy adapter's conversion — would reverse the ranking with
        no error anywhere.
        """
        runtime = FakeBedrockAgentRuntime(
            [_result("doc-best", 0.9), _result("doc-mid", 0.4), _result("doc-worst", 0.1)]
        )
        backend = mb.ManagedKbBackend(runtime_client=runtime, locator=_locator())

        chunks = await backend.search(APP_KB_ID, "query")

        assert [c.relevance for c in chunks] == [0.9, 0.4, 0.1]
        assert chunks[0].document_id == "doc-best"
        assert chunks[0].relevance > chunks[-1].relevance, (
            "relevance is inverted: the best chunk now scores lowest"
        )

    @pytest.mark.asyncio
    async def test_document_id_comes_from_the_custom_document_identifier(self):
        """Requirement 9.4 — the join key the status filter needs."""
        runtime = FakeBedrockAgentRuntime([_result("doc-platform-id", 0.5)])
        backend = mb.ManagedKbBackend(runtime_client=runtime, locator=_locator())

        chunks = await backend.search(APP_KB_ID, "query")
        assert chunks[0].document_id == "doc-platform-id"

    @pytest.mark.asyncio
    async def test_the_service_document_id_is_not_used_as_the_platform_id(self):
        """``documentId`` is a GetDocumentContent handle, not our id.

        Using it would produce a chunk whose ``document_id`` looks plausible,
        joins against no ``DOC#`` record, and is dropped by the fail-closed status
        filter — results disappearing two layers from the cause.
        """
        result = {
            "content": {"text": "t"},
            "score": 0.5,
            "documentId": "service-assigned-handle",
            "metadata": {},
        }
        runtime = FakeBedrockAgentRuntime([result])
        backend = mb.ManagedKbBackend(runtime_client=runtime, locator=_locator())

        chunks = await backend.search(APP_KB_ID, "query")
        assert chunks[0].document_id != "service-assigned-handle"

    @pytest.mark.asyncio
    async def test_metadata_carries_text_and_document_id_for_consumers_above_the_seam(self):
        runtime = FakeBedrockAgentRuntime([_result("doc-a", 0.5, text="the passage")])
        backend = mb.ManagedKbBackend(runtime_client=runtime, locator=_locator())

        chunk = (await backend.search(APP_KB_ID, "query"))[0]
        assert chunk.metadata["text"] == "the passage"
        assert chunk.metadata["document_id"] == "doc-a"

    @pytest.mark.asyncio
    async def test_a_missing_score_stays_none(self):
        """Not 0.0: a fabricated score is indistinguishable from a real one, and
        on this backend it would rank the chunk last while on legacy it ranks
        first."""
        runtime = FakeBedrockAgentRuntime([{"content": {"text": "t"}, "metadata": {}}])
        backend = mb.ManagedKbBackend(runtime_client=runtime, locator=_locator())

        assert (await backend.search(APP_KB_ID, "q"))[0].relevance is None

    @pytest.mark.asyncio
    async def test_the_aws_kb_id_is_resolved_not_taken_from_the_caller(self):
        runtime = FakeBedrockAgentRuntime([])
        backend = mb.ManagedKbBackend(runtime_client=runtime, locator=_locator("KB-RESOLVED"))

        await backend.search(APP_KB_ID, "query")
        assert runtime.calls[0]["knowledgeBaseId"] == "KB-RESOLVED"

    @pytest.mark.asyncio
    async def test_an_unprovisioned_knowledge_base_raises(self):
        backend = mb.ManagedKbBackend(
            runtime_client=FakeBedrockAgentRuntime([]), locator=lambda _ref: (None, None)
        )
        with pytest.raises(mb.ManagedKbNotProvisioned):
            await backend.search(APP_KB_ID, "query")

    @pytest.mark.asyncio
    async def test_the_kb_record_supplies_the_ids_when_no_locator_is_given(self, table):
        """The default path: resolve from the record, never from the caller."""
        r.create_provisioning(
            ASSISTANT_ID, r.KbRecord(app_kb_id=APP_KB_ID, owner_user_id=OWNER)
        )
        r.attach_aws_ids(ASSISTANT_ID, APP_KB_ID, AWS_KB_ID, AWS_DS_ID, "2026-01-01T00:00:00Z")

        runtime = FakeBedrockAgentRuntime([])
        backend = mb.ManagedKbBackend(runtime_client=runtime)
        await backend.search(APP_KB_ID, "query")

        assert runtime.calls[0]["knowledgeBaseId"] == AWS_KB_ID


# ===========================================================================
# 8.4 — direct ingestion and deletion
# ===========================================================================


class TestBatching:
    def test_the_limit_is_ten(self):
        """Requirement 9.3. Server-enforced; the service model's document list
        carries ``max: 10``. AWS's user guide says 25 and is wrong for managed
        knowledge bases."""
        assert mb.MAX_DOCUMENTS_PER_CALL == 10

    def test_batches_never_exceed_the_limit(self):
        batches = mb.batched(list(range(25)))
        assert [len(b) for b in batches] == [10, 10, 5]
        assert all(len(b) <= 10 for b in batches)

    def test_a_batch_size_above_the_limit_is_refused(self):
        """25 is the number the user guide gives. An 11-document call fails as a
        whole, so the other ten documents are lost with the eleventh."""
        with pytest.raises(ValueError, match="exceeds the server-enforced maximum"):
            mb.batched(list(range(25)), 25)

    def test_an_exact_multiple_produces_no_empty_batch(self):
        assert [len(b) for b in mb.batched(list(range(20)))] == [10, 10]

    def test_no_items_means_no_batches(self):
        assert mb.batched([]) == []


class TestDocumentPayload:
    def test_custom_document_identifier_is_the_platform_document_id(self):
        """Requirement 9.4, verbatim — it is also the deletion handle and the
        status-filter join key, so any transformation here needs undoing twice."""
        payload = mb.document_payload(DocumentSource("doc-42", "a.pdf", chunks=["x"]))
        custom = payload["content"]["custom"]
        assert custom["customDocumentIdentifier"] == {"id": "doc-42"}

    def test_content_data_source_type_is_custom(self):
        payload = mb.document_payload(DocumentSource("doc-42", "a.pdf", chunks=["x"]))
        assert payload["content"]["dataSourceType"] == "CUSTOM"

    def test_an_s3_backed_source_points_at_the_object(self):
        payload = mb.document_payload(
            DocumentSource("doc-42", "a.pdf", s3_key="assistants/ast/documents/doc-42/a.pdf"),
            bucket="docs-bucket",
        )
        custom = payload["content"]["custom"]
        assert custom["sourceType"] == "S3_LOCATION"
        assert custom["s3Location"]["uri"] == (
            "s3://docs-bucket/assistants/ast/documents/doc-42/a.pdf"
        )

    def test_a_chunked_source_is_sent_inline(self):
        payload = mb.document_payload(DocumentSource("doc-42", "a.pdf", chunks=["a", "b"]))
        custom = payload["content"]["custom"]
        assert custom["sourceType"] == "IN_LINE"
        assert custom["inlineContent"]["textContent"]["data"] == "a\n\nb"

    def test_an_empty_source_is_refused(self):
        with pytest.raises(mb.ManagedKbError, match="nothing to ingest"):
            mb.document_payload(DocumentSource("doc-42", "a.pdf"))

    def test_metadata_is_string_valued(self):
        payload = mb.document_payload(
            DocumentSource("doc-42", "a.pdf", chunks=["x"], metadata={"pages": 3})
        )
        attributes = {a["key"]: a["value"] for a in payload["metadata"]["inlineAttributes"]}
        assert attributes["pages"] == {"type": "STRING", "stringValue": "3"}
        assert attributes["document_id"]["stringValue"] == "doc-42"

    def test_no_chunk_index_appears_anywhere_in_the_payload(self):
        """Requirement 9.6 — the ``{doc_id}#{chunk_index}`` scheme is retired on
        this path, along with ``delete_vector_tail`` and the shrinkage stash."""
        payload = mb.document_payload(DocumentSource("doc-42", "a.pdf", chunks=["a", "b"]))
        assert "#" not in repr(payload)


class TestInlineAttributeCap:
    """Bedrock caps inlineAttributes at 50; caller metadata is unbounded.

    The failure this guards is disproportionate: metadata is per-document but the
    API call is per-batch, so one over-decorated document fails the ingestion of
    the nine innocent documents travelling with it.
    """

    def test_the_cap_is_the_literal_service_limit(self):
        """Pinned to 50 as a literal, not to the constant, so moving the constant
        cannot satisfy this. 50 is `{'min': 1, 'max': 50}` in the packaged service
        model — a property of Bedrock, not a tuning knob."""
        assert mb.MAX_INLINE_ATTRIBUTES == 50

    def test_metadata_is_truncated_to_the_cap(self):
        payload = mb.document_payload(
            DocumentSource(
                "doc-42", "a.pdf", chunks=["x"],
                metadata={f"key_{i:03d}": i for i in range(200)},
            )
        )
        attributes = payload["metadata"]["inlineAttributes"]
        assert len(attributes) == mb.MAX_INLINE_ATTRIBUTES

    def test_reserved_keys_survive_truncation(self):
        """``document_id`` is the status filter's join key, and it sorts after
        plenty of plausible caller keys — so alphabetical truncation would drop the
        one attribute the platform cannot do without, and every chunk of that
        document would then be discarded as unverifiable.
        """
        payload = mb.document_payload(
            DocumentSource(
                "doc-42", "a.pdf", chunks=["x"],
                # All sort BEFORE "document_id", so a plain sorted() truncation
                # would evict it.
                metadata={f"aaa_{i:03d}": i for i in range(200)},
            )
        )
        keys = [a["key"] for a in payload["metadata"]["inlineAttributes"]]
        assert "document_id" in keys, "truncation dropped the status-filter join key"
        assert "filename" in keys
        assert len(keys) == mb.MAX_INLINE_ATTRIBUTES

    def test_a_caller_cannot_override_the_reserved_keys(self):
        """Otherwise a caller could point document_id at someone else's document."""
        payload = mb.document_payload(
            DocumentSource(
                "doc-42", "a.pdf", chunks=["x"],
                metadata={"document_id": "doc-other"},
            )
        )
        attributes = {a["key"]: a["value"]["stringValue"] for a in payload["metadata"]["inlineAttributes"]}
        assert attributes["document_id"] == "doc-42"

    def test_metadata_under_the_cap_is_untouched(self):
        payload = mb.document_payload(
            DocumentSource("doc-42", "a.pdf", chunks=["x"], metadata={"pages": 3})
        )
        keys = {a["key"] for a in payload["metadata"]["inlineAttributes"]}
        assert keys == {"document_id", "filename", "pages"}


class TestIngestion:
    @pytest.mark.asyncio
    async def test_a_single_document_is_one_call(self):
        agent = FakeBedrockAgent()
        backend = mb.ManagedKbBackend(agent_client=agent, locator=_locator())

        await backend.ingest(APP_KB_ID, DocumentSource("doc-a", "a.pdf", chunks=["x"]))

        assert len(agent.ingest_calls) == 1
        call = agent.ingest_calls[0]
        assert call["knowledgeBaseId"] == AWS_KB_ID
        assert call["dataSourceId"] == AWS_DS_ID
        assert len(call["documents"]) == 1

    @pytest.mark.asyncio
    async def test_twenty_five_documents_are_split_into_batches_of_ten(self):
        """Requirement 9.3. The interesting number is 25, because that is what the
        user guide claims is allowed."""
        agent = FakeBedrockAgent()
        backend = mb.ManagedKbBackend(agent_client=agent, locator=_locator())
        sources = [DocumentSource(f"doc-{i}", f"{i}.pdf", chunks=["x"]) for i in range(25)]

        await backend.ingest_documents(APP_KB_ID, sources)

        sizes = sorted(len(call["documents"]) for call in agent.ingest_calls)
        assert sizes == [5, 10, 10]
        assert all(len(call["documents"]) <= 10 for call in agent.ingest_calls)

    @pytest.mark.asyncio
    async def test_every_document_is_sent_exactly_once(self):
        agent = FakeBedrockAgent()
        backend = mb.ManagedKbBackend(agent_client=agent, locator=_locator())
        sources = [DocumentSource(f"doc-{i}", f"{i}.pdf", chunks=["x"]) for i in range(23)]

        await backend.ingest_documents(APP_KB_ID, sources)

        sent = [
            document["content"]["custom"]["customDocumentIdentifier"]["id"]
            for call in agent.ingest_calls
            for document in call["documents"]
        ]
        assert sorted(sent) == sorted(f"doc-{i}" for i in range(23))
        assert len(sent) == len(set(sent))

    @pytest.mark.asyncio
    async def test_start_ingestion_job_is_never_called(self):
        """Requirement 9.2 — 0.1 RPS account-wide, not adjustable: one document
        every ten seconds for the entire account."""
        agent = FakeBedrockAgent()
        backend = mb.ManagedKbBackend(agent_client=agent, locator=_locator())

        await backend.ingest_documents(
            APP_KB_ID, [DocumentSource(f"doc-{i}", "a.pdf", chunks=["x"]) for i in range(12)]
        )

        assert agent.start_ingestion_job_calls == []

    def test_the_source_never_mentions_start_ingestion_job(self):
        """A stronger guard than the call assertion: the code cannot call it."""
        source = Path(mb.__file__).read_text(encoding="utf-8")
        assert "start_ingestion_job" not in source

    @pytest.mark.asyncio
    async def test_no_client_token_is_sent_on_ingest(self):
        """Idempotency comes from ``customDocumentIdentifier`` being 1:1, so a
        re-ingest replaces. A content-blind token would look like extra safety and
        instead silently swallow a legitimate re-upload."""
        agent = FakeBedrockAgent()
        backend = mb.ManagedKbBackend(agent_client=agent, locator=_locator())

        await backend.ingest(APP_KB_ID, DocumentSource("doc-a", "a.pdf", chunks=["x"]))
        assert "clientToken" not in agent.ingest_calls[0]

    @pytest.mark.asyncio
    async def test_concurrency_is_bounded_at_ten(self):
        """Requirement 9.5. Ingest and delete share one account-wide budget of 10
        concurrent document operations."""
        agent = FakeBedrockAgent()
        backend = mb.ManagedKbBackend(agent_client=agent, locator=_locator())
        # 150 documents ⇒ 15 batches, more than the bound permits at once.
        sources = [DocumentSource(f"doc-{i}", "a.pdf", chunks=["x"]) for i in range(150)]

        await backend.ingest_documents(APP_KB_ID, sources)

        assert len(agent.ingest_calls) == 15
        # The literal 10, deliberately, not ``mb.MAX_CONCURRENT_DOCUMENT_OPERATIONS``:
        # comparing observed concurrency against the constant that produced it is
        # tautological, and would pass just as happily with the bound raised to 100.
        assert agent.max_in_flight <= 10
        assert agent.max_in_flight > 1, "the batches ran serially; nothing was bounded"

    @pytest.mark.asyncio
    async def test_ingesting_nothing_calls_nothing(self):
        agent = FakeBedrockAgent()
        backend = mb.ManagedKbBackend(agent_client=agent, locator=_locator())
        await backend.ingest_documents(APP_KB_ID, [])
        assert agent.ingest_calls == []

    @pytest.mark.asyncio
    async def test_a_failed_batch_surfaces(self):
        agent = FakeBedrockAgent()

        def _boom(**_kwargs):
            raise RuntimeError("ingest rejected")

        agent.ingest_knowledge_base_documents = _boom
        backend = mb.ManagedKbBackend(agent_client=agent, locator=_locator())

        with pytest.raises(RuntimeError, match="ingest rejected"):
            await backend.ingest(APP_KB_ID, DocumentSource("doc-a", "a.pdf", chunks=["x"]))

    @pytest.mark.asyncio
    async def test_ingest_requires_a_data_source(self):
        backend = mb.ManagedKbBackend(
            agent_client=FakeBedrockAgent(), locator=lambda _ref: (AWS_KB_ID, None)
        )
        with pytest.raises(mb.ManagedKbNotProvisioned, match="awsDataSourceId"):
            await backend.ingest(APP_KB_ID, DocumentSource("doc-a", "a.pdf", chunks=["x"]))


class TestDeletion:
    @pytest.mark.asyncio
    async def test_delete_is_by_platform_document_id(self):
        agent = FakeBedrockAgent()
        backend = mb.ManagedKbBackend(agent_client=agent, locator=_locator())

        await backend.delete_document(APP_KB_ID, "doc-42")

        identifiers = agent.delete_calls[0]["documentIdentifiers"]
        assert identifiers == [{"dataSourceType": "CUSTOM", "custom": {"id": "doc-42"}}]

    @pytest.mark.asyncio
    async def test_deletes_are_batched_at_ten(self):
        agent = FakeBedrockAgent()
        backend = mb.ManagedKbBackend(agent_client=agent, locator=_locator())

        await backend.delete_documents(APP_KB_ID, [f"doc-{i}" for i in range(25)])

        sizes = sorted(len(call["documentIdentifiers"]) for call in agent.delete_calls)
        assert sizes == [5, 10, 10]

    @pytest.mark.asyncio
    async def test_deletes_share_the_ingest_concurrency_budget(self):
        agent = FakeBedrockAgent()
        backend = mb.ManagedKbBackend(agent_client=agent, locator=_locator())

        await backend.delete_documents(APP_KB_ID, [f"doc-{i}" for i in range(150)])

        # Literal, for the same reason as the ingest case above.
        assert agent.max_in_flight <= 10
        assert agent.max_in_flight > 1, "the batches ran serially; nothing was bounded"

    @pytest.mark.asyncio
    async def test_deleting_nothing_calls_nothing(self):
        agent = FakeBedrockAgent()
        backend = mb.ManagedKbBackend(agent_client=agent, locator=_locator())
        await backend.delete_documents(APP_KB_ID, [])
        await backend.delete_documents(APP_KB_ID, ["", None])
        assert agent.delete_calls == []


# ===========================================================================
# Seam conformance and import weight
# ===========================================================================


class TestSeamConformance:
    def test_the_managed_backend_satisfies_the_protocol(self):
        assert isinstance(mb.ManagedKbBackend(), KnowledgeBaseBackend)

    @pytest.mark.asyncio
    async def test_search_returns_protocol_chunks(self):
        runtime = FakeBedrockAgentRuntime([_result("doc-a", 0.5)])
        backend = mb.ManagedKbBackend(runtime_client=runtime, locator=_locator())
        chunks = await backend.search(APP_KB_ID, "q")
        assert all(isinstance(chunk, Chunk) for chunk in chunks)

    def test_constructing_the_backend_creates_no_aws_client(self):
        """Lazy clients: importing and constructing must not touch credentials."""
        backend = mb.ManagedKbBackend()
        assert backend._runtime_client is None
        assert backend._agent_client is None


class TestImportWeight:
    """The new modules obey the package's import boundary.

    Run in a fresh interpreter because by now the suite has imported boto3
    already, so an in-process check would pass regardless.
    """

    @staticmethod
    def _loaded(module: str) -> List[str]:
        backend_root = Path(__file__).resolve().parents[2]
        program = (
            "import sys\n"
            f"import {module}\n"
            "forbidden = ('apis.shared.assistants', 'boto3')\n"
            "print(','.join(sorted({n for n in sys.modules "
            "if any(n == f or n.startswith(f + '.') for f in forbidden)})))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            cwd=str(backend_root),
            env={"PYTHONPATH": str(backend_root / "src"), "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, result.stderr
        return [name for name in result.stdout.strip().split(",") if name]

    @pytest.mark.parametrize(
        "module",
        [
            "apis.shared.kb_backend.provisioning",
            "apis.shared.kb_backend.managed_backend",
        ],
    )
    def test_import_pulls_in_neither_boto3_nor_assistants(self, module):
        assert self._loaded(module) == [], (
            f"importing {module} loaded a forbidden module; keep boto3 and "
            "anything from apis.shared.assistants function-local"
        )


class TestNoLiveAws:
    """No test in this file constructs a Bedrock client.

    Both service clients are hand-rolled fakes and DynamoDB is moto. Checked by
    walking this file's own AST rather than by substring, because a substring
    search for ``boto3.client("bedrock`` finds the assertion that looks for it and
    reports a failure that is only ever itself.
    """

    #: Anything that would hand back a real Bedrock client.
    _FORBIDDEN_CALLS = frozenset(
        {"bedrock_agent_client", "bedrock_agent_runtime_client"}
    )

    def test_this_file_never_creates_a_bedrock_client(self):
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"), filename=__file__)

        offenders: List[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "client"
                and isinstance(func.value, ast.Name)
                and func.value.id == "boto3"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and str(node.args[0].value).startswith("bedrock")
            ):
                offenders.append(f"line {node.lineno}: boto3.client('bedrock...')")
            elif isinstance(func, ast.Name) and func.id in self._FORBIDDEN_CALLS:
                offenders.append(f"line {node.lineno}: {func.id}()")
            elif isinstance(func, ast.Attribute) and func.attr in self._FORBIDDEN_CALLS:
                offenders.append(f"line {node.lineno}: {func.attr}()")

        assert offenders == [], (
            "this test file would construct a real Bedrock client:\n"
            + "\n".join(offenders)
        )

    def test_the_only_boto3_clients_are_dynamodb_under_moto(self):
        """The DynamoDB access is real botocore, intercepted by moto."""
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"), filename=__file__)
        services = {
            str(node.args[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("client", "resource")
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "boto3"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        assert services == {"dynamodb"}, f"unexpected AWS services used: {services}"
        assert "mock_aws" in Path(__file__).read_text(encoding="utf-8")


def test_asyncio_gather_is_used_rather_than_ensure_future():
    """Requirement 10.8's spirit at this layer: no fire-and-forget orchestration.

    ``asyncio.ensure_future`` without an await loses the failure entirely — the
    task is garbage collected and the exception is reported, at best, as an
    unretrieved-exception warning nobody reads.
    """
    source = Path(mb.__file__).read_text(encoding="utf-8")
    assert "ensure_future" not in source
    assert "asyncio.gather" in source


def test_the_module_constants_match_the_verified_api_limits():
    """One place to look when a doc page disagrees with reality."""
    assert p.CLIENT_TOKEN_MIN_LENGTH == 33
    assert p.CLIENT_TOKEN_MAX_LENGTH == 256
    assert mb.MAX_DOCUMENTS_PER_CALL == 10
    assert mb.MAX_CONCURRENT_DOCUMENT_OPERATIONS == 10
    assert p.DATA_DELETION_POLICY == "RETAIN"
    assert p.IMAGE_EXTRACTION_STATUS == "ENABLED"
    assert p.EMBEDDING_MODEL_ID == "amazon.titan-embed-text-v2:0"
    assert p.EMBEDDING_DIMENSIONS == 1024
    assert mb.RERANKING_MODEL_TYPE == "MANAGED"
