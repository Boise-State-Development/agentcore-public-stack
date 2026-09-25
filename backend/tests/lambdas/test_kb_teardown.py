"""Tearing down a deleted agent's managed knowledge base (``teardown``).

Found validating Shared Projects on dev: purging a project whose first upload went
born-managed deleted every ``AST#`` row except ``KB#``, and the Bedrock knowledge
base it pointed at stayed ``ACTIVE`` and billing. ``DELETE /assistants`` had the
same hole. Nothing would ever clean it up: the reconciler never removes a record,
by design, and ships disarmed.

The fix has two halves, and both are tested here against moto DynamoDB and a
stubbed ``bedrock-agent`` (no test contacts AWS):

* app-api's half, ``records.request_teardown`` via ``queue_teardown``, puts the
  record on the migration work queue and bumps its generation, which fences any
  worker still running against it;
* the migration worker's half, ``run_step`` → ``teardown.run_teardown``, deletes
  the data source and knowledge base through the tombstoned saga under the worker
  lease, then removes the record. Every failure re-queues rather than failing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from apis.app_api.kb_migration import teardown as td
from apis.app_api.kb_migration import worker
from apis.shared.kb_backend import records as r
from apis.shared.kb_backend import tags as kb_tags
from apis.shared.kb_backend import tombstones as tb
from apis.shared.kb_backend.provisioning import _resource_name

REGION = "us-east-1"
TABLE = "test-kb-teardown"
ASSISTANT_ID = "ast-a1b2c3d4-0000-4000-8000-000000000001"
OWNER = "user-a1b2c3d4-0000-4000-8000-000000000002"
AWS_KB_ID = "KBTEST0001"
AWS_DS_ID = "DSTEST0001"


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    """Read per use, never at import: the full suite runs for minutes, and a
    timestamp fixed at collection drifts behind the ones the code writes."""
    return datetime.now(timezone.utc)


def _past() -> str:
    return _iso(_now() - timedelta(hours=1))


def _future() -> str:
    return _iso(_now() + timedelta(minutes=10))


@pytest.fixture()
def table(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE,
        kb_tags.ENV_TAG_VALUE_PREFIX: "testprefix",
        kb_tags.ENV_TAG_VALUE_ENVIRONMENT: "testenv",
    }.items():
        monkeypatch.setenv(key, value)

    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": n, "AttributeType": "S"}
                for n in ("PK", "SK", "GSI7_PK", "GSI7_SK")
            ],
            BillingMode="PAY_PER_REQUEST",
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
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """No CloudWatch, and no real polling waits."""
    monkeypatch.setattr(tb, "emit_count", lambda *a, **k: None)
    monkeypatch.setattr("apis.shared.kb_backend.metrics.emit_count", lambda *a, **k: None)
    monkeypatch.setattr(tb, "KB_DELETE_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(tb, "KB_DELETE_POLL_TIMEOUT_SECONDS", 0.05)


def _error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeBedrockAgent:
    """``bedrock-agent`` as the teardown sees it.

    ``delete_knowledge_base`` succeeds and the knowledge base keeps appearing in
    ``list_knowledge_bases`` for ``polls_before_gone`` more polls, the way AWS lists
    a ``DELETING`` knowledge base for minutes after accepting the delete.
    """

    def __init__(
        self,
        knowledge_bases: Optional[List[Dict]] = None,
        *,
        polls_before_gone: int = 0,
        kb_delete_error: Optional[str] = None,
        ds_delete_error: Optional[str] = None,
        stuck_status: Optional[str] = None,
    ):
        self.kbs = {kb["knowledgeBaseId"]: dict(kb) for kb in (knowledge_bases or [])}
        self.polls_before_gone = polls_before_gone
        self.kb_delete_error = kb_delete_error
        self.ds_delete_error = ds_delete_error
        self.stuck_status = stuck_status
        self.calls: List[tuple] = []
        self._deleted: Dict[str, int] = {}

    def delete_data_source(self, knowledgeBaseId, dataSourceId):
        self.calls.append(("delete_data_source", knowledgeBaseId, dataSourceId))
        if self.ds_delete_error:
            raise _error(self.ds_delete_error, "DeleteDataSource")
        return {"status": "DELETING"}

    def delete_knowledge_base(self, knowledgeBaseId):
        self.calls.append(("delete_knowledge_base", knowledgeBaseId))
        if self.kb_delete_error:
            raise _error(self.kb_delete_error, "DeleteKnowledgeBase")
        if knowledgeBaseId not in self.kbs:
            raise _error("ResourceNotFoundException", "DeleteKnowledgeBase")
        self.kbs[knowledgeBaseId]["status"] = self.stuck_status or "DELETING"
        self._deleted[knowledgeBaseId] = self.polls_before_gone
        return {"knowledgeBaseId": knowledgeBaseId, "status": "DELETING"}

    def list_knowledge_bases(self, maxResults=100, nextToken=None):
        self.calls.append(("list_knowledge_bases",))
        for kb_id, remaining in list(self._deleted.items()):
            if self.stuck_status:
                continue
            if remaining <= 0:
                self.kbs.pop(kb_id, None)
                del self._deleted[kb_id]
            else:
                self._deleted[kb_id] = remaining - 1
        return {"knowledgeBaseSummaries": list(self.kbs.values())}

    def aws_calls(self, operation: str) -> List[tuple]:
        return [c for c in self.calls if c[0] == operation]


def _summary(kb_id: str = AWS_KB_ID, name: Optional[str] = None, status: str = "ACTIVE") -> Dict:
    return {"knowledgeBaseId": kb_id, "name": name or f"other-{kb_id}", "status": status}


def _seed(table, **fields) -> Dict:
    """A KB# record like the one left behind on dev: born managed, built, done."""
    item = {
        "PK": r.kb_pk(ASSISTANT_ID),
        "SK": r.kb_sk(ASSISTANT_ID),
        "appKbId": ASSISTANT_ID,
        "ownerUserId": OWNER,
        "retrievalEngine": r.ENGINE_MANAGED,
        "provisioningState": r.ACTIVE,
        "awsKbId": AWS_KB_ID,
        "awsDataSourceId": AWS_DS_ID,
        "migrationState": r.RETAIN,
        "migrationGeneration": Decimal(0),
        "migrationLeaseUntil": _past(),
        "clientToken": "a" * 40,
    }
    item.update(fields)
    for key in [k for k, v in item.items() if v is None]:
        del item[key]
    table.put_item(Item=item)
    return item


def _record(table) -> Optional[Dict]:
    return r.get_kb_record(ASSISTANT_ID, ASSISTANT_ID)


def _partition(table) -> List[str]:
    items = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(r.kb_pk(ASSISTANT_ID))
    )["Items"]
    return sorted(item["SK"] for item in items)


def _run(client: FakeBedrockAgent) -> worker.StepResult:
    """One worker invocation, through ``run_step`` so the lease and the state
    dispatch are exercised too, with ``client`` as the ``bedrock-agent`` client."""
    with patch("apis.shared.kb_backend.managed_backend.bedrock_agent_client", return_value=client):
        return asyncio.run(worker.run_step(ASSISTANT_ID, ASSISTANT_ID))


# ── app-api's half: queuing ──────────────────────────────────────────────────
class TestRequestTeardown:
    def test_an_agent_without_a_record_queues_nothing(self, table):
        """Every legacy agent: no KB# record, and none may be conjured."""
        assert asyncio.run(td.queue_teardown(ASSISTANT_ID)) is False
        assert _partition(table) == []

    def test_a_built_knowledge_base_is_put_on_the_work_queue(self, table):
        _seed(table)

        assert asyncio.run(td.queue_teardown(ASSISTANT_ID)) is True

        record = _record(table)
        assert record["migrationState"] == r.TEARDOWN
        assert record["GSI7_PK"] == r.work_pk(r.TEARDOWN)
        assert record["teardownRequestedAt"]
        # The dispatcher finds it, which is what makes it actually happen.
        due = r.query_due_work(r.TEARDOWN, _iso(_now() + timedelta(minutes=1)))
        assert [d["appKbId"] for d in due] == [ASSISTANT_ID]

    def test_the_generation_bump_fences_a_worker_still_running(self, table):
        """A born-managed or migration step that read generation 0 must lose its
        next write, rather than finish provisioning a knowledge base being deleted."""
        _seed(table, migrationState=r.BORN_MANAGED, provisioningState=r.PROVISIONING,
              awsKbId=None, awsDataSourceId=None, migrationLeaseUntil=_future())

        asyncio.run(td.queue_teardown(ASSISTANT_ID))

        assert int(_record(table)["migrationGeneration"]) == 1
        with pytest.raises(r.TransitionLost):
            r.set_migration_state(ASSISTANT_ID, ASSISTANT_ID, r.RETAIN, 0)

    def test_the_lease_and_provisioning_state_are_left_alone(self, table):
        """So a create that is mid-flight can still record ``awsKbId`` for the
        teardown to find, and the teardown waits for that worker's lease."""
        lease = _future()
        _seed(table, migrationState=r.BORN_MANAGED, provisioningState=r.PROVISIONING,
              awsKbId=None, awsDataSourceId=None, migrationLeaseUntil=lease)

        asyncio.run(td.queue_teardown(ASSISTANT_ID))
        r.attach_knowledge_base_id(ASSISTANT_ID, ASSISTANT_ID, AWS_KB_ID, _iso(_now()))

        record = _record(table)
        assert record["migrationLeaseUntil"] == lease
        assert record["awsKbId"] == AWS_KB_ID

    def test_a_retried_delete_does_not_requeue_or_refence(self, table):
        _seed(table)
        asyncio.run(td.queue_teardown(ASSISTANT_ID))
        first = _record(table)

        assert asyncio.run(td.queue_teardown(ASSISTANT_ID)) is True

        again = _record(table)
        assert again["migrationGeneration"] == first["migrationGeneration"]
        assert again["GSI7_SK"] == first["GSI7_SK"]


# ── the worker's half: deleting ──────────────────────────────────────────────
class TestRunTeardown:
    def test_deletes_data_source_then_knowledge_base_then_every_row(self, table):
        """The dev repro, end to end: nothing of the agent survives, in DynamoDB or AWS."""
        _seed(table)
        table.put_item(Item={
            "PK": r.kb_pk(ASSISTANT_ID), "SK": r.document_tombstone_sk(ASSISTANT_ID, "doc-1"),
            "intent": tb.INTENT_DELETE_DOCUMENT, "documentId": "doc-1",
        })
        client = FakeBedrockAgent([_summary()], polls_before_gone=2)
        asyncio.run(td.queue_teardown(ASSISTANT_ID))

        result = _run(client)

        assert result.converged and result.to_state is None
        deletes = [c[0] for c in client.calls if c[0].startswith("delete_")]
        assert deletes == ["delete_data_source", "delete_knowledge_base"]
        assert client.kbs == {}
        assert _partition(table) == []

    def test_waits_for_a_lease_a_worker_still_holds(self, table):
        """A provisioning step mid-``CreateKnowledgeBase`` holds the lease: the
        teardown steps aside (the dispatcher hands it back next tick)."""
        _seed(table, migrationLeaseUntil=_future())
        asyncio.run(td.queue_teardown(ASSISTANT_ID))
        client = FakeBedrockAgent([_summary()])

        with pytest.raises(worker.LeaseLost):
            _run(client)

        assert client.calls == []
        assert _record(table)["migrationState"] == r.TEARDOWN

    def test_a_record_with_nothing_in_aws_is_simply_removed(self, table):
        _seed(table, awsKbId=None, awsDataSourceId=None, clientToken=None,
              retrievalEngine=None, migrationState=r.MIGRATION_FAILED)
        asyncio.run(td.queue_teardown(ASSISTANT_ID))
        client = FakeBedrockAgent()

        result = _run(client)

        assert result.converged
        assert client.calls == []
        assert _partition(table) == []

    def test_a_create_whose_id_was_never_recorded_is_found_by_name(self, table):
        name = _resource_name(ASSISTANT_ID)
        _seed(table, awsKbId=None, awsDataSourceId=None,
              migrationState=r.BORN_MANAGED, provisioningState=r.PROVISIONING)
        asyncio.run(td.queue_teardown(ASSISTANT_ID))
        client = FakeBedrockAgent([_summary("KBOTHER001"), _summary(AWS_KB_ID, name=name)])

        _run(client)

        assert client.aws_calls("delete_knowledge_base") == [("delete_knowledge_base", AWS_KB_ID)]
        assert set(client.kbs) == {"KBOTHER001"}
        assert _partition(table) == []

    def test_knowledge_base_and_data_source_already_gone_is_success(self, table):
        """A retry after AWS finished, or someone deleted it by hand."""
        _seed(table)
        asyncio.run(td.queue_teardown(ASSISTANT_ID))
        client = FakeBedrockAgent([], ds_delete_error="ResourceNotFoundException")

        result = _run(client)

        assert result.converged
        assert _partition(table) == []

    def test_a_refused_delete_is_requeued_not_failed(self, table):
        """AWS can refuse the knowledge base while its data source is still going.
        ``failed`` is terminal, and a teardown that stops trying is the bill this
        exists to end, so the record stays queued, due later, with the reason."""
        _seed(table)
        asyncio.run(td.queue_teardown(ASSISTANT_ID))
        client = FakeBedrockAgent([_summary()], kb_delete_error="ConflictException")

        result = _run(client)

        record = _record(table)
        assert result.to_state == r.TEARDOWN and not result.converged
        assert record["migrationState"] == r.TEARDOWN
        assert record["GSI7_PK"] == r.work_pk(r.TEARDOWN)
        assert record["GSI7_SK"] > _iso(_now() + timedelta(minutes=10))
        assert "ConflictException" in record["migrationError"]
        # The tombstone is the durable work item, and it survives.
        assert r.kb_tombstone_sk(ASSISTANT_ID) in _partition(table)

        # Next tick, once AWS lets go, it finishes.
        client.kb_delete_error = None
        table.update_item(
            Key={"PK": r.kb_pk(ASSISTANT_ID), "SK": r.kb_sk(ASSISTANT_ID)},
            UpdateExpression="SET migrationLeaseUntil = :past",
            ExpressionAttributeValues={":past": _past()},
        )
        assert _run(client).converged
        assert _partition(table) == []

    def test_delete_unsuccessful_is_rechecked_daily_and_keeps_the_evidence(self, table):
        _seed(table)
        asyncio.run(td.queue_teardown(ASSISTANT_ID))
        client = FakeBedrockAgent([_summary()], stuck_status=tb.KB_STATUS_DELETE_UNSUCCESSFUL)

        _run(client)

        record = _record(table)
        assert record["migrationState"] == r.TEARDOWN
        assert record["GSI7_SK"] > _iso(_now() + timedelta(hours=23))
        tombstone = table.get_item(
            Key={"PK": r.kb_pk(ASSISTANT_ID), "SK": r.kb_tombstone_sk(ASSISTANT_ID)}
        )["Item"]
        assert tombstone["awsStatus"] == tb.KB_STATUS_DELETE_UNSUCCESSFUL

    def test_the_teardown_state_is_work_eligible_and_never_terminal(self):
        assert r.TEARDOWN in r.WORK_ELIGIBLE_STATES
        assert r.TEARDOWN in r.ALL_MIGRATION_STATES
        assert r.TEARDOWN not in r.TERMINAL_STATES


class TestDispatcherGating:
    def test_teardown_is_swept_under_either_flag(self, monkeypatch):
        from apis.app_api.kb_migration import dispatcher as d

        for flag in (d.FLAG_NEW_DEFAULT, d.FLAG_MIGRATION_ENABLED):
            monkeypatch.delenv(d.FLAG_NEW_DEFAULT, raising=False)
            monkeypatch.delenv(d.FLAG_MIGRATION_ENABLED, raising=False)
            monkeypatch.setenv(flag, "true")
            assert r.TEARDOWN in d._enabled_work_states()
