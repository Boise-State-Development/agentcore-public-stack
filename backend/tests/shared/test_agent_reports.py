"""Agent Marketplace Phase 8 — problem reports (D15).

These run against a real (moto) table with the sparse GSI6, because the properties worth
testing are all properties of the *keys*, not of the Python:

* **The queue is sparse, not filtered.** A resolved report leaves the admin queue by
  losing its index key. A test that asserted on a filtered list would still pass if
  someone replaced the sparse write with a state filter — and would then miss the day a
  reader forgot the filter.
* **One open report per reporter (D15.4).** Enforced by three conditional writes on a
  deterministic key. Every case below is a way that can silently become "stacks a second
  report", which is the failure that makes the queue floodable and the nav count
  meaningless.
* **Reports are child rows that never outlive their Agent.** An orphaned *open* report
  keeps its index key, so it would sit in the queue forever pointing at nothing.
"""

import boto3
import pytest
from moto import mock_aws

from apis.shared.assistants.reports import (
    count_open_reports,
    delete_reports_for_agent,
    list_open_reports,
    list_reports_for_agent,
    report_id_for,
    resolve_report,
    submit_report,
)

REGION = "us-east-1"
TABLE = "test-rag-assistants"
AGENT = "ast-001"
REPORTER = "user-reporter"


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_ASSISTANTS_TABLE_NAME", TABLE)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI6_PK", "AttributeType": "S"},
                {"AttributeName": "GSI6_SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "AgentReportsIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI6_PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI6_SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb.Table(TABLE)


async def _file(agent_id=AGENT, reporter=REPORTER, reason="broken", note="It errors out"):
    return await submit_report(
        agent_id,
        reporter_id=reporter,
        reporter_name=f"Name of {reporter}",
        reason=reason,
        note=note,
    )


# ── the stored shape ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_report_is_a_child_row_of_the_agent_it_concerns(table):
    """PK is the Agent's, so the report is deleted with it and never outlives it."""
    await _file()

    item = table.get_item(
        Key={"PK": f"AST#{AGENT}", "SK": f"REPORT#{report_id_for(AGENT, REPORTER)}"}
    )["Item"]
    assert item["reporterId"] == REPORTER
    assert item["reason"] == "broken"
    assert item["state"] == "open"


@pytest.mark.asyncio
async def test_the_sort_key_does_not_carry_the_timestamp(table):
    """The key has to be derivable from (agent, reporter) alone (D15.4).

    The spec sketched ``REPORT#{created_at}#{report_id}``, which cannot be conditionally
    updated without first reading it — the very lookup D15.4 exists to avoid. Chronology
    lives in GSI6_SK instead, which is the only place anything reads it.
    """
    report, _ = await _file()

    item = table.get_item(
        Key={"PK": f"AST#{AGENT}", "SK": f"REPORT#{report_id_for(AGENT, REPORTER)}"}
    )["Item"]
    assert item["SK"] == f"REPORT#{report.report_id}"
    assert report.created_at not in item["SK"]
    assert item["GSI6_SK"] == f"CREATED#{report.created_at}"


@pytest.mark.asyncio
async def test_the_report_id_does_not_expose_the_reporter(table):
    """The reporter is admin-visible in the *item* (D15.2), not enumerable from the key."""
    assert REPORTER not in report_id_for(AGENT, REPORTER)


@pytest.mark.asyncio
async def test_the_same_reporter_gets_different_ids_on_different_agents(table):
    assert report_id_for("ast-001", REPORTER) != report_id_for("ast-002", REPORTER)


# ── one open report per reporter (D15.4) ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_second_report_while_the_first_is_open_updates_it(table):
    first, replaced_first = await _file(reason="broken", note="It errors out")
    second, replaced_second = await _file(reason="inappropriate", note="Worse than I thought")

    assert replaced_first is False
    assert replaced_second is True

    reports = await list_reports_for_agent(AGENT)
    assert len(reports) == 1, "a second submission must update, not stack"
    assert reports[0].reason == "inappropriate"
    assert reports[0].note == "Worse than I thought"


@pytest.mark.asyncio
async def test_updating_an_open_report_does_not_move_it_up_the_queue(table):
    """``createdAt`` and the index key survive, so amending is not a way to jump ahead."""
    first, _ = await _file()
    second, _ = await _file(reason="other", note="amended")

    assert second.created_at == first.created_at


@pytest.mark.asyncio
async def test_clearing_the_note_on_an_amended_report_drops_the_old_text(table):
    await _file(note="It errors out")
    await _file(note=None)

    assert (await list_reports_for_agent(AGENT))[0].note is None


@pytest.mark.asyncio
async def test_two_reporters_each_get_their_own_report(table):
    await _file(reporter="user-a")
    await _file(reporter="user-b")

    assert len(await list_reports_for_agent(AGENT)) == 2
    assert await count_open_reports() == 2


@pytest.mark.asyncio
async def test_reporting_again_after_a_resolution_files_a_new_report(table):
    """The one-open rule is about *open* reports; a closed one is not a permanent gag."""
    first, _ = await _file()
    await resolve_report(
        AGENT, first.report_id, state="resolved", resolved_by="Admin", note="Fixed"
    )

    fresh, replaced = await _file(reason="broken", note="It is back")

    assert replaced is False, "a closed report must not be reported as merely amended"
    assert fresh.state == "open"
    assert fresh.created_at >= first.created_at

    stored = (await list_reports_for_agent(AGENT))[0]
    assert stored.state == "open"
    assert stored.resolved_at is None, "a fresh report must not inherit the old verdict"
    assert stored.resolution_note is None


# ── the queue is sparse, not filtered ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_open_report_is_in_the_queue(table):
    await _file()
    assert [r.report_id for r in await list_open_reports()] == [report_id_for(AGENT, REPORTER)]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["resolved", "dismissed"])
async def test_triage_removes_the_index_key_rather_than_filtering(table, state):
    report, _ = await _file()
    await resolve_report(AGENT, report.report_id, state=state, resolved_by="Admin", note=None)

    assert await list_open_reports() == []
    assert await count_open_reports() == 0

    item = table.get_item(Key={"PK": f"AST#{AGENT}", "SK": f"REPORT#{report.report_id}"})["Item"]
    assert "GSI6_PK" not in item, "the report must leave the queue by losing its key"
    assert "GSI6_SK" not in item
    assert item["state"] == state


@pytest.mark.asyncio
async def test_triage_records_who_decided_and_what_they_wrote(table):
    report, _ = await _file()
    resolved = await resolve_report(
        AGENT, report.report_id, state="resolved", resolved_by="Ada Admin", note="Asked for changes"
    )

    assert resolved.resolved_by == "Ada Admin"
    assert resolved.resolution_note == "Asked for changes"
    assert resolved.resolved_at is not None


@pytest.mark.asyncio
async def test_resolving_a_report_that_is_gone_is_a_not_found(table):
    with pytest.raises(ValueError):
        await resolve_report(AGENT, "nope", state="resolved", resolved_by="Admin", note=None)


@pytest.mark.asyncio
async def test_the_queue_is_oldest_first(table):
    """It is a work queue, not a feed: the report that has waited longest is next."""
    await _file(reporter="user-a")
    await _file(reporter="user-b")
    await _file(reporter="user-c")

    ordered = await list_open_reports()
    assert [r.created_at for r in ordered] == sorted(r.created_at for r in ordered)


# ── reports never outlive their Agent ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_deleting_an_agent_clears_its_reports_from_the_queue(table):
    await _file(reporter="user-a")
    await _file(reporter="user-b")

    assert await delete_reports_for_agent(AGENT) == 2
    assert await list_reports_for_agent(AGENT) == []
    assert await count_open_reports() == 0, "an orphaned open report would haunt the queue"


@pytest.mark.asyncio
async def test_deleting_reports_leaves_other_agents_alone(table):
    await _file(agent_id="ast-001")
    await _file(agent_id="ast-002")

    await delete_reports_for_agent("ast-001")

    assert len(await list_reports_for_agent("ast-002")) == 1
    assert await count_open_reports() == 1


@pytest.mark.asyncio
async def test_deleting_reports_on_an_agent_with_none_is_a_no_op(table):
    assert await delete_reports_for_agent("ast-never-reported") == 0
