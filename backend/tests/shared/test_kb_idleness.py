"""
Idleness and the fleet gauges: measuring "nobody needs this" without lying.

Requirements 22.1, 22.5, 22.6. Two rules carry real risk and both are tested here
against the mistake they exist to prevent, not merely for their happy path.

**Idleness is not retrieval (22.5).** The tempting implementation reads
``lastRetrievedAt`` and stops. It is wrong in the one direction that destroys data:
an agent can be invoked hundreds of times a day and retrieve nothing, because
retrieval only fires when the query matches. So a corpus judged by retrieval alone
looks most abandoned exactly when its agent is busiest with questions its documents
do not answer — and the follow-up spec's eviction pass would delete it.

**Never a write per retrieval (22.6).** The write is conditional on a throttle
floor, so at most one lands per window however many turns race.

Also asserted: an unmeasured knowledge base is **not** counted as idle. Reporting
"very idle" for every freshly provisioned corpus is precisely the training signal
that makes operators stop reading a metric.

Feature: managed-kb-migration
Requirements: 22.1, 22.5, 22.6
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from apis.shared.kb_backend import idleness
from apis.shared.kb_backend.metrics import (
    BYTES_PER_GB,
    IDLE_THRESHOLD_DAYS,
    METRIC_KB_COUNT,
    METRIC_KB_IDLE_GB,
    METRIC_KB_STORAGE_GB,
    emit_fleet_gauges,
)

ASSISTANT_ID = "ast-idle-001"
NOW = "2026-08-25T12:00:00Z"
TABLE = "test-assistants"
ENV = {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE, "AWS_REGION": "us-west-2"}


def _days_ago(days: int) -> str:
    from datetime import datetime, timedelta, timezone

    moment = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── The rule that stops a busy agent's corpus looking abandoned ──────────────
class TestIdlenessIsNotRetrievalAlone:
    def test_agent_use_counts_even_when_nothing_was_ever_retrieved(self):
        """Requirement 22.5, stated as the failure it prevents.

        The knowledge base has never served a retrieval — no ``lastRetrievedAt`` at
        all — but its agent was used today. Judged by retrieval alone this is
        maximally idle; judged correctly it is active.
        """
        days = idleness.idle_days(
            ASSISTANT_ID, record={}, agent_timestamps=[_days_ago(0)], now=NOW
        )

        assert days == pytest.approx(0.0, abs=0.01)

    def test_the_more_recent_of_the_two_wins(self):
        record = {idleness.LAST_RETRIEVED_ATTR: _days_ago(90)}

        days = idleness.idle_days(
            ASSISTANT_ID, record=record, agent_timestamps=[_days_ago(2)], now=NOW
        )

        assert days == pytest.approx(2.0, abs=0.01)

    def test_retrieval_wins_when_it_is_the_more_recent(self):
        record = {idleness.LAST_RETRIEVED_ATTR: _days_ago(1)}

        days = idleness.idle_days(
            ASSISTANT_ID, record=record, agent_timestamps=[_days_ago(200)], now=NOW
        )

        assert days == pytest.approx(1.0, abs=0.01)

    def test_a_genuinely_idle_knowledge_base_reads_as_idle(self):
        """The permissive cases above would all pass if this returned 0 for
        everything, so the negative case is what makes them mean something."""
        record = {idleness.LAST_RETRIEVED_ATTR: _days_ago(120)}

        days = idleness.idle_days(
            ASSISTANT_ID, record=record, agent_timestamps=[_days_ago(115)], now=NOW
        )

        assert days == pytest.approx(115.0, abs=0.01)
        assert days >= IDLE_THRESHOLD_DAYS

    def test_no_signal_at_all_is_unknown_not_ancient(self):
        """A knowledge base provisioned an hour ago looks exactly like this. If it
        returned a large number, every new corpus would report as idle."""
        assert idleness.idle_days(ASSISTANT_ID, record={}, agent_timestamps=[], now=NOW) is None
        assert (
            idleness.idle_days(ASSISTANT_ID, record={}, agent_timestamps=[None], now=NOW) is None
        )

    def test_activity_is_a_maximum_over_a_set_of_agents(self):
        """One bound agent this phase, written as a maximum over a set so F4 making
        the set larger is not a rewrite of the module whose whole job is not to
        under-report activity."""
        latest = idleness.last_activity_at(
            ASSISTANT_ID,
            record={idleness.LAST_RETRIEVED_ATTR: _days_ago(50)},
            agent_timestamps=[_days_ago(40), _days_ago(3), _days_ago(60)],
        )

        assert latest == _days_ago(3)

    def test_the_bound_agent_this_phase_is_the_assistant_itself(self):
        assert idleness.bound_agent_ids(ASSISTANT_ID) == [ASSISTANT_ID]

    def test_an_unparseable_timestamp_is_unknown_rather_than_ancient(self):
        record = {idleness.LAST_RETRIEVED_ATTR: "last Tuesday"}
        assert idleness.idle_days(ASSISTANT_ID, record=record, agent_timestamps=[], now=NOW) is None

    def test_an_iso_offset_timestamp_still_parses(self):
        """The table holds timestamps written by several generations of code."""
        record = {idleness.LAST_RETRIEVED_ATTR: "2026-08-23T12:00:00+00:00"}
        days = idleness.idle_days(ASSISTANT_ID, record=record, agent_timestamps=[], now=NOW)
        assert days == pytest.approx(2.0, abs=0.01)

    def test_a_future_timestamp_clamps_to_zero_rather_than_going_negative(self):
        record = {idleness.LAST_RETRIEVED_ATTR: _days_ago(-5)}
        days = idleness.idle_days(ASSISTANT_ID, record=record, agent_timestamps=[], now=NOW)
        assert days == 0.0

    def test_the_agent_timestamp_falls_back_through_updated_and_created(self):
        """An assistant that has never been used still has a creation date, which is
        a better idleness floor than nothing."""
        table = MagicMock()
        table.get_item.return_value = {"Item": {"createdAt": _days_ago(10)}}
        resource = MagicMock()
        resource.Table.return_value = table

        with patch.dict("os.environ", ENV, clear=True), patch(
            "boto3.resource", return_value=resource
        ):
            assert idleness.agent_last_used_at(ASSISTANT_ID) == _days_ago(10)


# ── The throttled write ──────────────────────────────────────────────────────
class TestTheWriteIsThrottled:
    def _table(self, error_code=None):
        table = MagicMock()
        if error_code:
            table.update_item.side_effect = _client_error(error_code)
        resource = MagicMock()
        resource.Table.return_value = table
        return resource, table

    def test_the_write_is_conditional_on_a_freshness_floor(self):
        """Requirement 22.6. The condition is what makes calling this on every
        retrieval acceptable: a conditional write that loses is not a write."""
        resource, table = self._table()

        with patch.dict("os.environ", ENV, clear=True), patch(
            "boto3.resource", return_value=resource
        ):
            assert idleness.touch_last_retrieved(ASSISTANT_ID, ASSISTANT_ID) is True

        kwargs = table.update_item.call_args.kwargs
        assert idleness.LAST_RETRIEVED_ATTR in kwargs["ConditionExpression"]
        assert ":floor" in kwargs["ConditionExpression"]
        assert ":floor" in kwargs["ExpressionAttributeValues"]

    def test_the_condition_refuses_to_create_a_record(self):
        """A legacy knowledge base has no KB_Record and must keep having none: the
        migration's zero-backfill property across 1,692 rows is that nothing writes
        to them until their owner opts in. A metrics side effect that created rows
        would break it while looking harmless."""
        resource, table = self._table()

        with patch.dict("os.environ", ENV, clear=True), patch(
            "boto3.resource", return_value=resource
        ):
            idleness.touch_last_retrieved(ASSISTANT_ID, ASSISTANT_ID)

        condition = table.update_item.call_args.kwargs["ConditionExpression"]
        assert "attribute_exists(SK)" in condition

    def test_a_rejected_write_is_not_an_error(self):
        resource, _ = self._table(error_code="ConditionalCheckFailedException")

        with patch.dict("os.environ", ENV, clear=True), patch(
            "boto3.resource", return_value=resource
        ):
            assert idleness.touch_last_retrieved(ASSISTANT_ID, ASSISTANT_ID) is False

    def test_any_other_failure_is_swallowed(self):
        resource, _ = self._table(error_code="ProvisionedThroughputExceededException")

        with patch.dict("os.environ", ENV, clear=True), patch(
            "boto3.resource", return_value=resource
        ):
            assert idleness.touch_last_retrieved(ASSISTANT_ID, ASSISTANT_ID) is False

    def test_no_table_configured_is_a_no_op(self):
        with patch.dict("os.environ", {}, clear=True):
            assert idleness.touch_last_retrieved(ASSISTANT_ID, ASSISTANT_ID) is False

    def test_the_throttle_window_is_resolved_at_call_time(self):
        """Never bound as a default argument, which is captured once at import and
        makes a test's override silently ineffective."""
        with patch.dict("os.environ", {}, clear=True):
            assert idleness.throttle_hours() == idleness.THROTTLE_HOURS
        with patch.dict("os.environ", {"KB_LAST_RETRIEVED_THROTTLE_HOURS": "6"}, clear=True):
            assert idleness.throttle_hours() == 6
        with patch.dict("os.environ", {"KB_LAST_RETRIEVED_THROTTLE_HOURS": "0"}, clear=True):
            # Zero would mean a write per retrieval, which is the thing forbidden.
            assert idleness.throttle_hours() == 1

    @pytest.mark.asyncio
    async def test_the_touch_is_detached_from_the_caller(self):
        """Retrieval must wait for neither the write nor its rejection — and
        rejection is the common case."""
        import asyncio

        started = asyncio.Event()

        def _slow(assistant_id, app_kb_id):
            started.set()
            return False

        with patch.object(idleness, "touch_last_retrieved", side_effect=_slow):
            idleness.schedule_activity_touch(ASSISTANT_ID, ASSISTANT_ID)
            # Nothing was awaited above; the work happens after we yield.
            assert started.is_set() is False
            for _ in range(100):
                await asyncio.sleep(0)
                if started.is_set():
                    break

        assert started.is_set() is True

    def test_scheduling_outside_an_event_loop_does_nothing(self):
        """A missing idleness sample is a gap in a baseline metric; an exception
        would be a failed retrieval."""
        idleness.schedule_activity_touch(ASSISTANT_ID, ASSISTANT_ID)


# ── The gauges ───────────────────────────────────────────────────────────────
class TestFleetGauges:
    def _emit(self, **kwargs):
        records = []
        with patch("apis.shared.observability.emf._emf_logger") as raw:
            emit_fleet_gauges(**kwargs)
            for call in raw.info.call_args_list:
                records.append(json.loads(call.args[0]))
        return records

    def test_all_three_gauges_are_emitted_with_storage_units(self):
        records = self._emit(kb_count=4, stored_bytes=2 * BYTES_PER_GB, idle_bytes=BYTES_PER_GB)

        assert len(records) == 1
        record = records[0]
        assert record[METRIC_KB_COUNT] == 4
        assert record[METRIC_KB_STORAGE_GB] == 2.0
        assert record[METRIC_KB_IDLE_GB] == 1.0

        units = {
            metric["Name"]: metric["Unit"]
            for metric in record["_aws"]["CloudWatchMetrics"][0]["Metrics"]
        }
        assert units[METRIC_KB_STORAGE_GB] == "Gigabytes"
        assert units[METRIC_KB_IDLE_GB] == "Gigabytes"
        assert units[METRIC_KB_COUNT] == "Count"

    def test_the_namespace_is_the_one_the_iam_grant_allows(self):
        with patch.dict("os.environ", {"PROJECT_PREFIX": "bsu-agentcore"}, clear=True):
            records = self._emit(kb_count=1, stored_bytes=0, idle_bytes=0)

        namespace = records[0]["_aws"]["CloudWatchMetrics"][0]["Namespace"]
        assert namespace == "bsu-agentcore/ManagedKb"
        assert not namespace.startswith("AWS"), (
            "CloudWatch reserves every namespace beginning with AWS and rejects "
            "writes to them, so this would publish nothing forever"
        )

    def test_gigabytes_are_decimal_so_a_dashboard_matches_an_invoice(self):
        """AWS bills $5.00/GB-month on decimal gigabytes. Using 2**30 here would
        make every dashboard number 7% smaller than the bill it is meant to
        explain."""
        records = self._emit(kb_count=1, stored_bytes=1_500_000_000, idle_bytes=0)
        assert records[0][METRIC_KB_STORAGE_GB] == 1.5

    def test_unmeasured_rides_along_as_a_property_not_a_metric(self):
        """Legitimately large the day this ships and near zero a month later, so an
        alarm on it would be noise. Context for reading KbIdleGB, not a target."""
        records = self._emit(kb_count=3, stored_bytes=0, idle_bytes=0, unmeasured=2)

        record = records[0]
        assert record["unmeasuredKnowledgeBases"] == 2
        names = {m["Name"] for m in record["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
        assert "unmeasuredKnowledgeBases" not in names

    def test_no_dimensions_so_the_alarm_target_is_one_fleet_sum(self):
        records = self._emit(kb_count=1, stored_bytes=0, idle_bytes=0)
        assert records[0]["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[]]

    def test_a_reclaimed_gigabytes_metric_is_never_emitted(self):
        """Requirement 22.1 forbids it: nothing reclaims in this phase, and a
        structurally-always-zero metric trains operators to ignore the board."""
        records = self._emit(kb_count=1, stored_bytes=0, idle_bytes=0)
        assert not any("Reclaim" in key for key in records[0])

    def test_emission_never_raises(self):
        with patch(
            "apis.shared.observability.emf.emit_emf_metrics",
            side_effect=RuntimeError("logging broke"),
        ):
            emit_fleet_gauges(kb_count=1, stored_bytes=0, idle_bytes=0)


def _client_error(code: str):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code}}, "UpdateItem")
