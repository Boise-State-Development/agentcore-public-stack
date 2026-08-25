"""Tombstoned deletion sagas — ordering, confirmation, and refusal.

Feature: managed-kb-migration, task 10.1.
Requirements: 13.1-13.8, 24.11.

What these tests are actually defending
---------------------------------------
A managed knowledge base is a *billed* resource with no CloudFormation parent, so a
delete that half-finishes is not a crash — it is a recurring charge with no owner
and no error anywhere. Every assertion here exists because the natural
implementation of one step produces exactly that outcome:

* **Tombstone before AWS.** Asserted *causally*, not by call order in a mock: the
  stubbed client reads DynamoDB at the moment it is called and records whether the
  tombstone was already there. Reversing the two steps in the source makes that
  observation ``False`` and the test fails, which a plain "was write called before
  delete" assertion on two independent mocks would not reliably catch.
* **Clear only after confirmed absence.** Same technique from the other side: the
  stub records, on every list poll, whether the tombstone still exists. Clearing
  early makes one of those observations ``False``.
* **"Accepted" is not "gone".** The stub's ``delete_knowledge_base`` succeeds and
  then keeps listing the knowledge base, which is exactly what AWS does for 2-6
  minutes.

No test here contacts AWS. DynamoDB is moto; every ``bedrock-agent`` call goes to
:class:`FakeBedrockAgent` (Requirement 24.11).
"""

from datetime import datetime, timezone

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from apis.shared.kb_backend import tombstones as tomb

REGION = "us-east-1"
TABLE = "test-kb-tombstones"
ASSISTANT_ID = "ast-tomb01"
APP_KB_ID = ASSISTANT_ID
AWS_KB_ID = "KBAAAA1111"
AWS_DS_ID = "DSAAAA1111"
DOCUMENT_ID = "doc-tomb01"
ARN = f"arn:aws:bedrock:{REGION}:123456789012:knowledge-base/{AWS_KB_ID}"
PROJECT_TAGS = {"prefix": "testprefix", "env": "testenv", "appKbId": APP_KB_ID}


# ── Fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
    monkeypatch.setenv("PROJECT_PREFIX", "testprefix")
    monkeypatch.setenv("ENVIRONMENT", "testenv")

    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


@pytest.fixture(autouse=True)
def no_metrics(monkeypatch):
    """Metrics are observability; stub them so nothing reaches CloudWatch."""
    monkeypatch.setattr(tomb, "emit_count", lambda *a, **k: None)


def _not_found(operation):
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "not found"}},
        operation,
    )


class FakeBedrockAgent:
    """A ``bedrock-agent`` stand-in that behaves the way the real API does.

    Two behaviours are modelled deliberately because they are what the sagas are
    built against:

    * ``delete_knowledge_base`` **succeeds** and the knowledge base keeps
      appearing in ``list_knowledge_bases`` for ``polls_before_gone`` further
      polls, mirroring the measured 2-6 minute asynchronous deletion.
    * every call runs ``probe`` first, so a test can capture the state of
      DynamoDB *at the moment of the AWS call* and assert ordering causally
      rather than by mock call sequence.
    """

    def __init__(
        self,
        knowledge_bases=None,
        tags=None,
        polls_before_gone=0,
        probe=None,
        page_size=None,
        delete_raises=None,
        document_statuses=None,
    ):
        self.kbs = {kb["knowledgeBaseId"]: dict(kb) for kb in (knowledge_bases or [])}
        self.tags = dict(tags or {})
        self.polls_before_gone = polls_before_gone
        self.probe = probe
        self.page_size = page_size
        self.delete_raises = delete_raises

        #: Successive ``GetKnowledgeBaseDocuments`` statuses to hand back, so a
        #: test can model DELETING → NOT_FOUND.
        self.document_statuses = list(document_statuses or ["NOT_FOUND"])

        self.list_calls = 0
        self.delete_calls = []
        self.delete_document_calls = []
        self.get_document_calls = 0
        #: One entry per AWS call: (operation, probe_result).
        self.observations = []
        self._pending = {}

    # ── probe plumbing ──────────────────────────────────────────────────────
    def _observe(self, operation):
        result = self.probe() if self.probe else None
        self.observations.append((operation, result))
        return result

    def probes_for(self, operation):
        return [result for op, result in self.observations if op == operation]

    # ── control plane ───────────────────────────────────────────────────────
    def list_knowledge_bases(self, **kwargs):
        self._observe("list_knowledge_bases")
        self.list_calls += 1

        # Age out anything whose delete has been accepted.
        for kb_id in list(self._pending):
            self._pending[kb_id] -= 1
            if self._pending[kb_id] <= 0:
                self._pending.pop(kb_id)
                self.kbs.pop(kb_id, None)

        summaries = [
            {
                "knowledgeBaseId": kb["knowledgeBaseId"],
                "name": kb.get("name", ""),
                "status": kb.get("status", "ACTIVE"),
                "updatedAt": kb.get("updatedAt", datetime.now(timezone.utc)),
            }
            for kb in self.kbs.values()
        ]

        page_size = self.page_size or kwargs.get("maxResults") or len(summaries) or 1
        start = int(kwargs.get("nextToken") or 0)
        page = summaries[start : start + page_size]
        response = {"knowledgeBaseSummaries": page}
        if start + page_size < len(summaries):
            response["nextToken"] = str(start + page_size)
        return response

    def get_knowledge_base(self, knowledgeBaseId):  # noqa: N803 - AWS parameter name
        self._observe("get_knowledge_base")
        kb = self.kbs.get(knowledgeBaseId)
        if kb is None:
            raise _not_found("GetKnowledgeBase")
        return {"knowledgeBase": kb}

    def list_tags_for_resource(self, resourceArn):  # noqa: N803 - AWS parameter name
        self._observe("list_tags_for_resource")
        return {"tags": dict(self.tags.get(resourceArn, {}))}

    def delete_knowledge_base(self, knowledgeBaseId):  # noqa: N803 - AWS parameter name
        self._observe("delete_knowledge_base")
        self.delete_calls.append(knowledgeBaseId)
        if self.delete_raises is not None:
            raise self.delete_raises
        if knowledgeBaseId not in self.kbs:
            raise _not_found("DeleteKnowledgeBase")
        if self.polls_before_gone <= 0:
            self.kbs.pop(knowledgeBaseId, None)
        else:
            # A knowledge base already in DELETE_UNSUCCESSFUL stays there: a second
            # delete call is accepted and changes nothing, which is exactly why the
            # state needs an operator rather than a retry.
            if self.kbs[knowledgeBaseId].get("status") != "DELETE_UNSUCCESSFUL":
                self.kbs[knowledgeBaseId]["status"] = "DELETING"
            self._pending[knowledgeBaseId] = self.polls_before_gone
        return {"knowledgeBaseId": knowledgeBaseId, "status": "DELETING"}

    # ── documents ───────────────────────────────────────────────────────────
    def delete_knowledge_base_documents(self, **kwargs):
        self._observe("delete_knowledge_base_documents")
        self.delete_document_calls.append(kwargs)
        return {"documentDetails": [{"status": "DELETING"}]}

    def get_knowledge_base_documents(self, **kwargs):
        self._observe("get_knowledge_base_documents")
        index = min(self.get_document_calls, len(self.document_statuses) - 1)
        self.get_document_calls += 1
        status = self.document_statuses[index]
        return {"documentDetails": [{"status": status, "identifier": {}}]}


def _kb(kb_id=AWS_KB_ID, status="ACTIVE", name="testprefix-kb-x", role_arn="role/a"):
    return {
        "knowledgeBaseId": kb_id,
        "name": name,
        "status": status,
        "knowledgeBaseArn": f"arn:aws:bedrock:{REGION}:123456789012:knowledge-base/{kb_id}",
        "roleArn": role_arn,
        "createdAt": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def _tomb_item(table, app_kb_id=APP_KB_ID):
    return table.get_item(
        Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KBTOMB#{app_kb_id}"}
    ).get("Item")


def _tomb_exists(table, app_kb_id=APP_KB_ID):
    return _tomb_item(table, app_kb_id) is not None


# ── Requirement 13.1: tombstone before AWS ───────────────────────────────────
class TestTombstoneIsWrittenBeforeAws:
    def test_tombstone_already_exists_when_aws_delete_is_called(self, table):
        """The tombstone must be durable *before* the delete call, not after.

        Asserted from inside the AWS call itself. If the saga were reordered to
        call AWS first, the probe recorded at ``delete_knowledge_base`` would be
        ``False`` — a crash at that instant would leave a billed knowledge base
        that no record and no tombstone points at.
        """
        client = FakeBedrockAgent(
            knowledge_bases=[_kb()], probe=lambda: _tomb_exists(table)
        )

        tomb.delete_knowledge_base(
            ASSISTANT_ID, APP_KB_ID, AWS_KB_ID, AWS_DS_ID, client=client
        )

        probes = client.probes_for("delete_knowledge_base")
        assert probes == [True], (
            "the tombstone was not present in DynamoDB at the moment "
            "DeleteKnowledgeBase was called"
        )

    def test_tombstone_carries_the_aws_identifiers_and_intent(self, table):
        tomb.write_kb_tombstone(ASSISTANT_ID, APP_KB_ID, AWS_KB_ID, AWS_DS_ID)

        item = _tomb_item(table)
        assert item["intent"] == tomb.INTENT_DELETE_KB
        assert item["awsKbId"] == AWS_KB_ID
        assert item["awsDataSourceId"] == AWS_DS_ID
        assert item["appKbId"] == APP_KB_ID
        assert item["createdAt"]

    def test_retry_preserves_created_at_and_counts_attempts(self, table):
        """A retried saga must not restart the clock on a stuck delete."""
        tomb.write_kb_tombstone(ASSISTANT_ID, APP_KB_ID, AWS_KB_ID, AWS_DS_ID)
        first = _tomb_item(table)

        tomb.write_kb_tombstone(ASSISTANT_ID, APP_KB_ID, AWS_KB_ID, AWS_DS_ID)
        second = _tomb_item(table)

        assert second["createdAt"] == first["createdAt"]
        assert int(second["attempts"]) == 2


# ── No TTL, ever (Requirement 13.6) ──────────────────────────────────────────
class TestTombstonesHaveNoTtl:
    def test_kb_tombstone_carries_no_expiry_attribute(self, table):
        """A TTL would let DynamoDB delete the evidence of an unfinished delete.

        That is the precise silent-leak class this design closes, so the absence
        of *any* expiry-shaped attribute is asserted rather than assumed.
        """
        tomb.write_kb_tombstone(ASSISTANT_ID, APP_KB_ID, AWS_KB_ID, AWS_DS_ID)

        item = _tomb_item(table)
        expiry_attrs = {"ttl", "TTL", "expiresAt", "expires_at", "expireAt", "ttlEpoch"}
        found = expiry_attrs & set(item)
        assert not found, (
            f"tombstone carries expiry attribute(s) {sorted(found)}; a tombstone "
            f"must be cleared by confirmed deletion or persist as a work item"
        )

    def test_document_tombstone_carries_no_expiry_attribute(self, table):
        tomb.write_document_tombstone(
            ASSISTANT_ID, APP_KB_ID, DOCUMENT_ID, AWS_KB_ID, AWS_DS_ID
        )

        item = table.get_item(
            Key={
                "PK": f"AST#{ASSISTANT_ID}",
                "SK": f"KBTOMB#{APP_KB_ID}#DOC#{DOCUMENT_ID}",
            }
        )["Item"]
        expiry_attrs = {"ttl", "TTL", "expiresAt", "expires_at", "expireAt", "ttlEpoch"}
        assert not (expiry_attrs & set(item))


# ── Requirement 13.2, 13.3, 13.4: confirmation by polling ────────────────────
class TestClearOnlyAfterConfirmedAbsence:
    def test_tombstone_survives_every_poll_and_is_gone_only_at_the_end(self, table):
        """The tombstone must outlive every poll in which AWS still lists the KB.

        ``polls_before_gone=3`` models the measured asynchronous deletion. The
        probe on each list call captures whether the tombstone was still there;
        clearing on the accepted delete call instead would make the later
        observations ``False``.
        """
        client = FakeBedrockAgent(
            knowledge_bases=[_kb()],
            polls_before_gone=3,
            probe=lambda: _tomb_exists(table),
        )
        slept = []

        outcome = tomb.delete_knowledge_base(
            ASSISTANT_ID,
            APP_KB_ID,
            AWS_KB_ID,
            AWS_DS_ID,
            client=client,
            interval_seconds=0.0,
            sleep=slept.append,
        )

        list_probes = client.probes_for("list_knowledge_bases")
        assert list_probes, "the saga never polled ListKnowledgeBases"
        assert all(list_probes), (
            f"the tombstone was cleared before AWS confirmed absence; "
            f"per-poll presence was {list_probes}"
        )
        assert outcome.confirmed is True
        assert outcome.tombstone_cleared is True
        assert outcome.polls == 3
        assert not _tomb_exists(table), "tombstone survived a confirmed deletion"
        assert len(slept) == 2

    def test_accepted_delete_alone_does_not_clear_the_tombstone(self, table):
        """A knowledge base that never disappears must leave a work item.

        The stub's delete call succeeds — exactly like the real API — so anything
        that treated the accepted call as completion would pass. The knowledge
        base is still listed, so the saga must refuse to confirm.
        """
        client = FakeBedrockAgent(knowledge_bases=[_kb()], polls_before_gone=10_000)

        with pytest.raises(tomb.DeleteNotConfirmed):
            tomb.delete_knowledge_base(
                ASSISTANT_ID,
                APP_KB_ID,
                AWS_KB_ID,
                AWS_DS_ID,
                client=client,
                timeout_seconds=0.0,
                interval_seconds=0.0,
                sleep=lambda _s: None,
            )

        assert client.delete_calls == [AWS_KB_ID], "the delete call was never made"
        item = _tomb_item(table)
        assert item is not None, "an unconfirmed delete cleared its tombstone"
        assert "still present" in item["lastError"]

    def test_clear_refuses_without_confirmation(self, table):
        tomb.write_kb_tombstone(ASSISTANT_ID, APP_KB_ID, AWS_KB_ID, AWS_DS_ID)

        with pytest.raises(tomb.TombstoneError, match="has not confirmed"):
            tomb.clear_kb_tombstone(ASSISTANT_ID, APP_KB_ID, False)

        assert _tomb_exists(table), "the refused clear removed the tombstone anyway"

    def test_clear_document_tombstone_refuses_without_confirmation(self, table):
        tomb.write_document_tombstone(ASSISTANT_ID, APP_KB_ID, DOCUMENT_ID)

        with pytest.raises(tomb.TombstoneError):
            tomb.clear_document_tombstone(ASSISTANT_ID, APP_KB_ID, DOCUMENT_ID, False)

    def test_poll_window_tolerates_at_least_six_minutes(self):
        """Requirement 13.4. Deletion was measured at 2-6 minutes."""
        assert tomb.KB_DELETE_POLL_TIMEOUT_SECONDS >= 360.0, (
            f"the poll window is {tomb.KB_DELETE_POLL_TIMEOUT_SECONDS}s, below the "
            f"360s floor Requirement 13.4 sets from measured deletions"
        )

    def test_timeout_is_read_at_call_time_not_bound_at_import(self, monkeypatch):
        """Patching the module constant must actually change the behaviour.

        A default argument is evaluated once at import, so binding the timeout
        that way makes it unpatchable: a test that shortens it appears to pass
        while waiting the full production window. Here the constant is lowered to
        zero, so a knowledge base that never disappears must fail immediately and
        without sleeping.
        """
        monkeypatch.setattr(tomb, "KB_DELETE_POLL_TIMEOUT_SECONDS", 0.0)
        client = FakeBedrockAgent(knowledge_bases=[_kb()], polls_before_gone=10_000)
        slept = []

        with pytest.raises(tomb.DeleteNotConfirmed):
            tomb.confirm_knowledge_base_absent(client, AWS_KB_ID, sleep=slept.append)

        assert slept == [], "the patched timeout was ignored; the poll slept anyway"
        assert client.list_calls == 1

    def test_already_absent_is_a_completed_delete(self, table):
        """``ResourceNotFoundException`` means an earlier attempt finally landed."""
        client = FakeBedrockAgent(knowledge_bases=[])

        outcome = tomb.delete_knowledge_base(
            ASSISTANT_ID, APP_KB_ID, AWS_KB_ID, AWS_DS_ID, client=client
        )

        assert outcome.already_absent is True
        assert outcome.confirmed is True
        assert not _tomb_exists(table)


# ── Requirement 13.7: DELETE_UNSUCCESSFUL ────────────────────────────────────
class TestDeleteUnsuccessfulIsAnOperatorState:
    def test_delete_unsuccessful_raises_and_keeps_the_tombstone(self, table):
        """The dev account has held one of these since 2025-11-24.

        It does not clear by waiting and it does not clear by retrying, so it must
        surface as its own state rather than as a timeout or a success.
        """
        client = FakeBedrockAgent(
            knowledge_bases=[_kb(status="DELETE_UNSUCCESSFUL")],
            polls_before_gone=10_000,
        )

        with pytest.raises(tomb.DeleteUnsuccessful):
            tomb.delete_knowledge_base(
                ASSISTANT_ID,
                APP_KB_ID,
                AWS_KB_ID,
                AWS_DS_ID,
                client=client,
                interval_seconds=0.0,
                sleep=lambda _s: None,
            )

        item = _tomb_item(table)
        assert item is not None
        assert item["awsStatus"] == tomb.KB_STATUS_DELETE_UNSUCCESSFUL

    def test_delete_unsuccessful_is_not_a_timeout(self, table):
        """It must stop polling at once, not burn the window and misreport."""
        client = FakeBedrockAgent(
            knowledge_bases=[_kb(status="DELETE_UNSUCCESSFUL")],
            polls_before_gone=10_000,
        )

        with pytest.raises(tomb.DeleteUnsuccessful):
            tomb.confirm_knowledge_base_absent(
                client, AWS_KB_ID, interval_seconds=0.0, sleep=lambda _s: None
            )

        assert client.list_calls == 1


# ── Requirement 13.6: the record outlives the delete ─────────────────────────
class TestRecordRemoval:
    def test_refuses_to_remove_the_record_without_confirmation(self, table):
        table.put_item(
            Item={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{APP_KB_ID}", "appKbId": APP_KB_ID}
        )

        with pytest.raises(tomb.RecordRemovalRefused):
            tomb.remove_kb_record(ASSISTANT_ID, APP_KB_ID, False)

        assert table.get_item(
            Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{APP_KB_ID}"}
        ).get("Item")

    def test_removes_the_record_once_confirmed(self, table):
        table.put_item(
            Item={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{APP_KB_ID}", "appKbId": APP_KB_ID}
        )

        tomb.remove_kb_record(ASSISTANT_ID, APP_KB_ID, True)

        assert not table.get_item(
            Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{APP_KB_ID}"}
        ).get("Item")

    def test_saga_removes_the_record_only_after_confirmation(self, table):
        table.put_item(
            Item={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{APP_KB_ID}", "appKbId": APP_KB_ID}
        )
        client = FakeBedrockAgent(knowledge_bases=[_kb()], polls_before_gone=2)

        tomb.delete_knowledge_base(
            ASSISTANT_ID,
            APP_KB_ID,
            AWS_KB_ID,
            AWS_DS_ID,
            client=client,
            remove_record=True,
            interval_seconds=0.0,
            sleep=lambda _s: None,
        )

        assert not table.get_item(
            Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{APP_KB_ID}"}
        ).get("Item")

    def test_unconfirmed_saga_leaves_the_record_alone(self, table):
        table.put_item(
            Item={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{APP_KB_ID}", "appKbId": APP_KB_ID}
        )
        client = FakeBedrockAgent(knowledge_bases=[_kb()], polls_before_gone=10_000)

        with pytest.raises(tomb.DeleteNotConfirmed):
            tomb.delete_knowledge_base(
                ASSISTANT_ID,
                APP_KB_ID,
                AWS_KB_ID,
                AWS_DS_ID,
                client=client,
                remove_record=True,
                timeout_seconds=0.0,
                interval_seconds=0.0,
                sleep=lambda _s: None,
            )

        assert table.get_item(
            Key={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{APP_KB_ID}"}
        ).get("Item"), "an unconfirmed delete removed the KB_Record"


# ── Requirement 13.8: a survivor is discoverable work ────────────────────────
class TestSurvivingTombstonesAreDiscoverable:
    def test_iter_tombstones_returns_kb_and_document_survivors(self, table):
        tomb.write_kb_tombstone(ASSISTANT_ID, APP_KB_ID, AWS_KB_ID, AWS_DS_ID)
        tomb.write_document_tombstone(ASSISTANT_ID, APP_KB_ID, DOCUMENT_ID)
        table.put_item(
            Item={"PK": f"AST#{ASSISTANT_ID}", "SK": f"KB#{APP_KB_ID}", "appKbId": APP_KB_ID}
        )

        found = tomb.iter_tombstones(ASSISTANT_ID)

        assert [item["SK"] for item in found] == [
            f"KBTOMB#{APP_KB_ID}",
            f"KBTOMB#{APP_KB_ID}#DOC#{DOCUMENT_ID}",
        ]

    def test_confirmed_delete_leaves_no_work_item(self, table):
        client = FakeBedrockAgent(knowledge_bases=[_kb()])

        tomb.delete_knowledge_base(
            ASSISTANT_ID, APP_KB_ID, AWS_KB_ID, AWS_DS_ID, client=client
        )

        assert tomb.iter_tombstones(ASSISTANT_ID) == []


# ── Requirement 13.5: the service role goes last ─────────────────────────────
class TestServiceRoleGuard:
    def test_refuses_while_a_knowledge_base_still_uses_the_role(self, table):
        client = FakeBedrockAgent(
            knowledge_bases=[_kb(role_arn="arn:aws:iam::1:role/kb")],
            tags={ARN: PROJECT_TAGS},
        )

        with pytest.raises(tomb.ServiceRoleStillInUse, match=AWS_KB_ID):
            tomb.assert_service_role_deletable(client, "arn:aws:iam::1:role/kb")

    def test_a_deleting_knowledge_base_still_blocks_the_role(self, table):
        """Mid-``DELETING`` counts as present: it needs the role to finish."""
        client = FakeBedrockAgent(
            knowledge_bases=[_kb(status="DELETING", role_arn="arn:aws:iam::1:role/kb")],
            tags={ARN: PROJECT_TAGS},
        )

        with pytest.raises(tomb.ServiceRoleStillInUse):
            tomb.assert_service_role_deletable(client, "arn:aws:iam::1:role/kb")

    def test_allows_deletion_once_all_are_absent(self, table):
        client = FakeBedrockAgent(knowledge_bases=[], tags={})

        tomb.assert_service_role_deletable(client, "arn:aws:iam::1:role/kb")

    def test_another_projects_knowledge_base_does_not_block_the_role(self, table):
        """The guard is scoped by tag, like everything else here."""
        client = FakeBedrockAgent(
            knowledge_bases=[_kb(role_arn="arn:aws:iam::1:role/kb")],
            tags={ARN: {"prefix": "someone-else", "env": "prod"}},
        )

        tomb.assert_service_role_deletable(client, "arn:aws:iam::1:role/kb")


# ── Requirement 14.1: paginated, tag-filtered listing ────────────────────────
class TestListingIsPaginatedAndTagFiltered:
    def test_every_page_is_read(self, table):
        kbs = [_kb(kb_id=f"KB{i:04d}") for i in range(7)]
        client = FakeBedrockAgent(knowledge_bases=kbs, page_size=2)

        seen = [s["knowledgeBaseId"] for s in tomb.iter_knowledge_base_summaries(client)]

        assert len(seen) == 7, f"paging stopped early: {seen}"
        assert client.list_calls == 4

    def test_tag_filter_keeps_ours_and_drops_everything_else(self, table):
        mine, theirs, untagged = _kb("KBMINE"), _kb("KBTHEIRS"), _kb("KBNOTAGS")
        client = FakeBedrockAgent(
            knowledge_bases=[mine, theirs, untagged],
            tags={
                mine["knowledgeBaseArn"]: PROJECT_TAGS,
                theirs["knowledgeBaseArn"]: {"prefix": "other", "env": "testenv"},
            },
        )

        found = list(tomb.iter_project_knowledge_bases(client))

        assert [f.kb_id for f in found] == ["KBMINE"]

    def test_created_at_is_carried_through_from_aws(self, table):
        """The Reconciler's age gate depends on this provenance."""
        created = datetime(2025, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
        kb = _kb()
        kb["createdAt"] = created
        client = FakeBedrockAgent(knowledge_bases=[kb], tags={ARN: PROJECT_TAGS})

        found = list(tomb.iter_project_knowledge_bases(client))

        assert found[0].created_at == created

    def test_untagged_never_matches(self):
        assert tomb.matches_project_tags(None, {"prefix": "p"}) is False
        assert tomb.matches_project_tags({}, {"prefix": "p"}) is False
        assert tomb.matches_project_tags({"prefix": "p"}, {"prefix": "p"}) is True
        assert tomb.matches_project_tags({"prefix": "q"}, {"prefix": "p"}) is False

    def test_a_knowledge_base_that_vanishes_between_list_and_describe_is_skipped(self, table):
        client = FakeBedrockAgent(knowledge_bases=[_kb()], tags={ARN: PROJECT_TAGS})
        # Model the race: the summary is produced, then the resource is gone.
        summaries = list(tomb.iter_knowledge_base_summaries(client))
        assert summaries
        client.kbs.clear()

        assert list(tomb.iter_project_knowledge_bases(client)) == []


# ── Document saga ────────────────────────────────────────────────────────────
class TestDocumentSaga:
    def test_tombstone_precedes_the_aws_document_delete(self, table):
        def probe():
            return (
                table.get_item(
                    Key={
                        "PK": f"AST#{ASSISTANT_ID}",
                        "SK": f"KBTOMB#{APP_KB_ID}#DOC#{DOCUMENT_ID}",
                    }
                ).get("Item")
                is not None
            )

        client = FakeBedrockAgent(probe=probe, document_statuses=["NOT_FOUND"])

        tomb.delete_document(
            ASSISTANT_ID, APP_KB_ID, DOCUMENT_ID, AWS_KB_ID, AWS_DS_ID, client=client
        )

        assert client.probes_for("delete_knowledge_base_documents") == [True]

    def test_deleting_status_is_not_absent(self, table):
        """``DELETING`` is present. Treating it as gone is the document-scale
        version of trusting the accepted delete call."""
        client = FakeBedrockAgent(document_statuses=["DELETING"])

        with pytest.raises(tomb.DeleteNotConfirmed):
            tomb.delete_document(
                ASSISTANT_ID,
                APP_KB_ID,
                DOCUMENT_ID,
                AWS_KB_ID,
                AWS_DS_ID,
                client=client,
                timeout_seconds=0.0,
                interval_seconds=0.0,
                sleep=lambda _s: None,
            )

        assert table.get_item(
            Key={
                "PK": f"AST#{ASSISTANT_ID}",
                "SK": f"KBTOMB#{APP_KB_ID}#DOC#{DOCUMENT_ID}",
            }
        ).get("Item"), "an unconfirmed document delete cleared its tombstone"

    def test_cleared_once_not_found(self, table):
        client = FakeBedrockAgent(document_statuses=["DELETING", "NOT_FOUND"])

        outcome = tomb.delete_document(
            ASSISTANT_ID,
            APP_KB_ID,
            DOCUMENT_ID,
            AWS_KB_ID,
            AWS_DS_ID,
            client=client,
            interval_seconds=0.0,
            sleep=lambda _s: None,
        )

        assert outcome.confirmed is True
        assert outcome.polls == 2
        assert not table.get_item(
            Key={
                "PK": f"AST#{ASSISTANT_ID}",
                "SK": f"KBTOMB#{APP_KB_ID}#DOC#{DOCUMENT_ID}",
            }
        ).get("Item")


# ── Failure annotation ───────────────────────────────────────────────────────
class TestFailureAnnotation:
    def test_a_transport_failure_leaves_an_annotated_tombstone(self, table):
        client = FakeBedrockAgent(
            knowledge_bases=[_kb()],
            delete_raises=ClientError(
                {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                "DeleteKnowledgeBase",
            ),
        )

        with pytest.raises(ClientError):
            tomb.delete_knowledge_base(
                ASSISTANT_ID, APP_KB_ID, AWS_KB_ID, AWS_DS_ID, client=client
            )

        item = _tomb_item(table)
        assert item is not None
        assert "Throttling" in item["lastError"]
