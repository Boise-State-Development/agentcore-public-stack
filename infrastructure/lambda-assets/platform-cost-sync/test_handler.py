"""Tests for the platform cost sync handler.

Run with: uv run pytest infrastructure/lambda-assets/platform-cost-sync/
(from repo root), or point pytest at this directory. The handler needs only
boto3, which is never actually called here -- the Cost Explorer client and the
DynamoDB table are both fakes, so nothing in this file touches AWS or spends a
cent of Cost Explorer's $0.01-per-request.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import pytest

_HANDLER_PATH = Path(__file__).parent / "handler.py"
_spec = importlib.util.spec_from_file_location("platform_cost_sync_handler", _HANDLER_PATH)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeCE:
    """Minimal Cost Explorer stand-in. Records the calls it was asked to make."""

    def __init__(self, pages: List[Dict[str, Any]]):
        self._pages = pages
        self.calls: List[Dict[str, Any]] = []

    def get_cost_and_usage(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return self._pages[len(self.calls) - 1]


def _page(services: Dict[str, str], next_token: str | None = None) -> Dict[str, Any]:
    page: Dict[str, Any] = {
        "ResultsByTime": [
            {
                "Groups": [
                    {"Keys": [name], "Metrics": {"UnblendedCost": {"Amount": amount}}}
                    for name, amount in services.items()
                ]
            }
        ]
    }
    if next_token:
        page["NextPageToken"] = next_token
    return page


class FakeBatch:
    def __init__(self, sink: List[Dict[str, Any]]):
        self._sink = sink

    def put_item(self, Item: Dict[str, Any]) -> None:  # noqa: N803 - boto3 casing
        self._sink.append(Item)

    def __enter__(self) -> "FakeBatch":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class FakeTable:
    def __init__(self) -> None:
        self.service_rows: List[Dict[str, Any]] = []
        self.summaries: List[Dict[str, Any]] = []

    def batch_writer(self, **_kwargs: Any) -> FakeBatch:
        return FakeBatch(self.service_rows)

    def put_item(self, Item: Dict[str, Any]) -> None:  # noqa: N803
        self.summaries.append(Item)


# The shape of a real prod response, trimmed. Numbers are the measured
# September 1-21 figures, so the assertions below are against reality.
PROD_SEPT = {
    "Claude Sonnet 5 (Amazon Bedrock Edition)": "913.52",
    "Amazon Bedrock AgentCore": "201.54",
    "Amazon Elastic Container Service": "117.95",
    "EC2 - Other": "91.12",
    "Claude Opus 5 (Amazon Bedrock Edition)": "66.74",
    "AWS Business Support+": "56.64",
    "Amazon Relational Database Service": "31.55",
    "Amazon Bedrock": "1.12",
    "AWS Cost Explorer": "0.01",
}


# --------------------------------------------------------------------------
# classify_service
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "service,expected",
    [
        ("Claude Sonnet 5 (Amazon Bedrock Edition)", "inference"),
        ("OpenAI GPT-5.6 Terra (Amazon Bedrock Edition)", "inference"),
        ("Claude 3 Haiku (Amazon Bedrock Edition)", "inference"),
        # No suffix: the non-token side of Bedrock (guardrails, knowledge
        # bases). Billing it as inference would double-count against our
        # ledger and break the reconciliation it feeds.
        ("Amazon Bedrock", "platform"),
        # AgentCore is the whole point of this sync -- the largest
        # non-inference line and previously invisible.
        ("Amazon Bedrock AgentCore", "platform"),
        ("Amazon Elastic Container Service", "platform"),
        ("Amazon Relational Database Service", "excluded"),
        ("AWS Business Support+", "excluded"),
        ("AWS Config", "excluded"),
        ("AWS Cost Explorer", "excluded"),
    ],
)
def test_classify_service(service: str, expected: str) -> None:
    assert mod.classify_service(service, mod.DEFAULT_EXCLUDED_SERVICES) == expected


# --------------------------------------------------------------------------
# Exclusion list resolution — the unset-env-var trap
# --------------------------------------------------------------------------


def test_unset_env_keeps_the_default_exclusions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLATFORM_COST_EXCLUDED_SERVICES", raising=False)
    assert mod._excluded_services() == frozenset(mod.DEFAULT_EXCLUDED_SERVICES)


@pytest.mark.parametrize("value", ["", "   ", ",", " , , "])
def test_empty_env_does_not_empty_the_list(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """An empty/whitespace/comma-only value must NOT clear the exclusions.

    This is the browser-blocklist failure mode: a workflow that forwards an
    unset variable sends an empty string, and a naive split would leave the
    list empty. Here that would bill another team's Aurora cluster to our
    users, and the total would merely look a bit high -- no error, no alarm.
    """
    monkeypatch.setenv("PLATFORM_COST_EXCLUDED_SERVICES", value)
    assert mod._excluded_services() == frozenset(mod.DEFAULT_EXCLUDED_SERVICES)


def test_env_overrides_rather_than_extends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLATFORM_COST_EXCLUDED_SERVICES", "Amazon Foo, Amazon Bar")
    assert mod._excluded_services() == frozenset({"Amazon Foo", "Amazon Bar"})


# --------------------------------------------------------------------------
# Month bounds
# --------------------------------------------------------------------------


def test_complete_month_uses_exclusive_first_of_next_month() -> None:
    start, end, partial = mod._month_bounds("2026-08", date(2026, 9, 21))
    assert (start, end, partial) == ("2026-08-01", "2026-09-01", False)


def test_december_rolls_the_year() -> None:
    start, end, partial = mod._month_bounds("2026-12", date(2027, 3, 1))
    assert (start, end, partial) == ("2026-12-01", "2027-01-01", False)


def test_in_progress_month_ends_tomorrow_and_is_flagged_partial() -> None:
    """CE rejects a future end date, and a month-to-date figure must say so."""
    start, end, partial = mod._month_bounds("2026-09", date(2026, 9, 21))
    assert (start, end, partial) == ("2026-09-01", "2026-09-22", True)


# --------------------------------------------------------------------------
# Period selection
# --------------------------------------------------------------------------


def test_default_syncs_current_and_previous_month() -> None:
    """Last month keeps re-syncing because its charges settle for days."""
    assert mod._periods_to_sync({}, date(2026, 9, 21)) == ["2026-09", "2026-08"]


def test_default_spans_the_year_boundary() -> None:
    assert mod._periods_to_sync({}, date(2027, 1, 3)) == ["2027-01", "2026-12"]


def test_explicit_periods_drive_a_backfill_and_are_capped() -> None:
    event = {"periods": [f"2026-{m:02d}" for m in range(1, 13)] + ["2025-12"]}
    assert len(mod._periods_to_sync(event, date(2026, 9, 21))) == 12


def test_non_dict_event_is_tolerated() -> None:
    assert mod._periods_to_sync([], date(2026, 9, 21)) == ["2026-09", "2026-08"]


# --------------------------------------------------------------------------
# sync_period — the real arithmetic
# --------------------------------------------------------------------------


def test_sync_period_splits_prod_september_into_three_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLATFORM_COST_EXCLUDED_SERVICES", raising=False)
    ce = FakeCE([_page(PROD_SEPT)])
    table = FakeTable()

    result = mod.sync_period(
        "2026-09",
        ce,
        table,
        today=date(2026, 9, 21),
        account_id="897729136999",
        min_service_cost=0.01,
    )

    # Inference = the two Bedrock Edition SKUs. Reconciliation input only.
    assert result["inferenceCost"] == pytest.approx(913.52 + 66.74, abs=0.01)
    # Platform = AgentCore + ECS + EC2-Other + plain "Amazon Bedrock".
    assert result["platformCost"] == pytest.approx(
        201.54 + 117.95 + 91.12 + 1.12, abs=0.01
    )
    # Excluded = another team's Aurora + Support + Cost Explorer itself.
    assert result["excludedCost"] == pytest.approx(56.64 + 31.55 + 0.01, abs=0.01)
    assert result["partialMonth"] is True


def test_summary_total_is_the_sum_of_all_three_buckets() -> None:
    ce = FakeCE([_page(PROD_SEPT)])
    table = FakeTable()
    mod.sync_period(
        "2026-09", ce, table,
        today=date(2026, 9, 21), account_id="", min_service_cost=0.01,
    )

    summary = table.summaries[0]
    assert summary["PK"] == "PLATFORM#MONTHLY"
    assert summary["SK"] == "2026-09"
    assert summary["totalCost"] == (
        summary["inferenceCost"] + summary["platformCost"] + summary["excludedCost"]
    )
    assert summary["totalCost"] == pytest.approx(
        Decimal(sum(float(v) for v in PROD_SEPT.values())), abs=Decimal("0.01")
    )


def test_service_rows_carry_their_category_and_period() -> None:
    ce = FakeCE([_page(PROD_SEPT)])
    table = FakeTable()
    mod.sync_period(
        "2026-09", ce, table,
        today=date(2026, 9, 21), account_id="", min_service_cost=0.01,
    )

    by_name = {row["SK"]: row for row in table.service_rows}
    assert by_name["Amazon Bedrock AgentCore"]["category"] == "platform"
    assert by_name["Amazon Relational Database Service"]["category"] == "excluded"
    assert by_name["Claude Sonnet 5 (Amazon Bedrock Edition)"]["category"] == "inference"
    assert all(row["PK"] == "PLATFORM#SERVICE#2026-09" for row in table.service_rows)
    assert all(row["period"] == "2026-09" for row in table.service_rows)


def test_costs_persist_as_decimal_not_float() -> None:
    """DynamoDB rejects floats; a float here fails at write time in cloud only."""
    ce = FakeCE([_page(PROD_SEPT)])
    table = FakeTable()
    mod.sync_period(
        "2026-09", ce, table,
        today=date(2026, 9, 21), account_id="", min_service_cost=0.01,
    )

    assert all(isinstance(row["cost"], Decimal) for row in table.service_rows)
    summary = table.summaries[0]
    for key in ("inferenceCost", "platformCost", "excludedCost", "totalCost"):
        assert isinstance(summary[key], Decimal), key


def test_subcent_services_count_in_totals_but_get_no_row() -> None:
    ce = FakeCE([_page({"Amazon Route 53": "1.00", "AWS Lambda": "0.004"})])
    table = FakeTable()
    result = mod.sync_period(
        "2026-09", ce, table,
        today=date(2026, 9, 21), account_id="", min_service_cost=0.01,
    )

    assert [row["SK"] for row in table.service_rows] == ["Amazon Route 53"]
    assert result["serviceCount"] == 1
    # Asserted against the PERSISTED figure, not the return value: the latter
    # is rounded to cents for the log line, which would hide exactly the
    # fraction this test exists to prove is still counted.
    assert table.summaries[0]["platformCost"] == Decimal("1.004")


def test_pagination_is_followed() -> None:
    ce = FakeCE(
        [
            _page({"Amazon Elastic Container Service": "100.00"}, next_token="t1"),
            _page({"Amazon Bedrock AgentCore": "200.00"}),
        ]
    )
    table = FakeTable()
    result = mod.sync_period(
        "2026-09", ce, table,
        today=date(2026, 9, 21), account_id="", min_service_cost=0.01,
    )

    assert len(ce.calls) == 2
    assert ce.calls[1]["NextPageToken"] == "t1"
    assert result["platformCost"] == pytest.approx(300.00, abs=0.01)


def test_duplicate_service_across_entries_is_summed_not_dropped() -> None:
    ce = FakeCE(
        [
            {
                "ResultsByTime": [
                    {"Groups": [{"Keys": ["EC2 - Other"], "Metrics": {"UnblendedCost": {"Amount": "10.00"}}}]},
                    {"Groups": [{"Keys": ["EC2 - Other"], "Metrics": {"UnblendedCost": {"Amount": "5.00"}}}]},
                ]
            }
        ]
    )
    table = FakeTable()
    result = mod.sync_period(
        "2026-09", ce, table,
        today=date(2026, 9, 21), account_id="", min_service_cost=0.01,
    )

    assert result["platformCost"] == pytest.approx(15.00, abs=0.01)
    assert len(table.service_rows) == 1


def test_unparseable_amount_is_skipped_not_fatal() -> None:
    ce = FakeCE([_page({"Amazon Bedrock AgentCore": "12.00", "Broken": "n/a"})])
    table = FakeTable()
    result = mod.sync_period(
        "2026-09", ce, table,
        today=date(2026, 9, 21), account_id="", min_service_cost=0.01,
    )
    assert result["platformCost"] == pytest.approx(12.00, abs=0.01)


def test_one_ce_call_per_page_only() -> None:
    """Cost control: CE bills per request, so the call count is a budget."""
    ce = FakeCE([_page(PROD_SEPT)])
    mod.sync_period(
        "2026-09", ce, FakeTable(),
        today=date(2026, 9, 21), account_id="", min_service_cost=0.01,
    )
    assert len(ce.calls) == 1


# --------------------------------------------------------------------------
# handler — guards
# --------------------------------------------------------------------------


def test_kill_switch_short_circuits_before_any_ce_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLATFORM_COST_SYNC_ENABLED", "false")
    monkeypatch.setenv("DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME", "t")

    def explode(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("must not construct an AWS client when disabled")

    monkeypatch.setattr(mod.boto3, "client", explode)
    assert mod.handler({}, None) == {"status": "disabled", "synced": []}


def test_missing_table_name_is_a_no_op_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLATFORM_COST_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME", raising=False)
    assert mod.handler({}, None)["status"] == "misconfigured"


def test_one_failing_period_does_not_abort_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An AccessDenied on Cost Explorer must degrade, never page anyone."""
    monkeypatch.delenv("PLATFORM_COST_SYNC_ENABLED", raising=False)
    monkeypatch.setenv("DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME", "t")
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: object())
    monkeypatch.setattr(
        mod.boto3, "resource", lambda *a, **k: type("R", (), {"Table": lambda s, n: FakeTable()})()
    )

    calls: List[str] = []

    def fake_sync(period: str, *_a: Any, **_k: Any) -> Dict[str, Any]:
        calls.append(period)
        if period == "2026-09":
            raise RuntimeError("AccessDeniedException")
        return {"period": period, "platformCost": 1.0}

    monkeypatch.setattr(mod, "sync_period", fake_sync)
    result = mod.handler({"periods": ["2026-09", "2026-08"]}, None)

    assert calls == ["2026-09", "2026-08"]
    assert result["status"] == "partial"
    assert [f["period"] for f in result["failures"]] == ["2026-09"]
    assert [s["period"] for s in result["synced"]] == ["2026-08"]


# --------------------------------------------------------------------------
# Deployment scoping — "only this app's charges in the account"
# --------------------------------------------------------------------------


DEPLOYMENT_ONLY = {
    "Amazon Bedrock AgentCore": "201.54",
    "Amazon Elastic Container Service": "117.95",
}


def test_tag_filter_is_sent_when_a_project_tag_is_configured() -> None:
    ce = FakeCE([_page(DEPLOYMENT_ONLY)])
    rows, scope = mod.resolve_scoped_costs(ce, "2026-09-01", "2026-09-22", "boisestateai-v2")

    assert scope == "deployment"
    assert ce.calls[0]["Filter"] == {
        "Tags": {"Key": "Project", "Values": ["boisestateai-v2"]}
    }
    assert len(rows) == 2


def test_no_project_tag_means_account_scope_and_no_filter() -> None:
    ce = FakeCE([_page(PROD_SEPT)])
    rows, scope = mod.resolve_scoped_costs(ce, "2026-09-01", "2026-09-22", None)

    assert scope == "account"
    assert "Filter" not in ce.calls[0]


def test_inactive_tag_returns_empty_and_falls_back_to_the_account() -> None:
    """An unactivated cost allocation tag is not an error — it matches nothing.

    Cost Explorer will not filter by a tag until it has been activated in the
    payer account, and reports that as an empty result. A fork whose deployer
    cannot perform that action (a linked account cannot even list the tags)
    must still get a working dashboard.
    """
    ce = FakeCE([_page({}), _page(PROD_SEPT)])
    rows, scope = mod.resolve_scoped_costs(ce, "2026-09-01", "2026-09-22", "boisestateai-v2")

    assert scope == "account"
    assert len(ce.calls) == 2
    assert ce.calls[0].get("Filter") is not None   # tried scoped first
    assert "Filter" not in ce.calls[1]             # then fell back
    assert len(rows) == len(PROD_SEPT)


def test_successful_scoping_costs_only_one_ce_call() -> None:
    """The fallback probe is skipped once the tag works — CE bills per call."""
    ce = FakeCE([_page(DEPLOYMENT_ONLY)])
    mod.resolve_scoped_costs(ce, "2026-09-01", "2026-09-22", "boisestateai-v2")
    assert len(ce.calls) == 1


def test_scope_is_persisted_on_the_summary_row() -> None:
    """The UI must be able to say which question the number answers."""
    ce = FakeCE([_page(DEPLOYMENT_ONLY)])
    table = FakeTable()
    result = mod.sync_period(
        "2026-09", ce, table,
        today=date(2026, 9, 21), account_id="", min_service_cost=0.01,
        project_tag="boisestateai-v2",
    )

    assert result["scope"] == "deployment"
    assert table.summaries[0]["scope"] == "deployment"
    assert table.summaries[0]["projectTag"] == "boisestateai-v2"


def test_account_scope_is_labelled_as_such_on_the_row() -> None:
    ce = FakeCE([_page({}), _page(PROD_SEPT)])
    table = FakeTable()
    result = mod.sync_period(
        "2026-09", ce, table,
        today=date(2026, 9, 21), account_id="", min_service_cost=0.01,
        project_tag="boisestateai-v2",
    )

    assert result["scope"] == "account"
    assert table.summaries[0]["scope"] == "account"


def test_deployment_scope_drops_the_foreign_aurora_entirely() -> None:
    """The point of scoping: another team's database is not merely excluded
    from the total, it never appears. Under account scope it does."""
    scoped = FakeCE([_page(DEPLOYMENT_ONLY)])
    scoped_table = FakeTable()
    mod.sync_period(
        "2026-09", scoped, scoped_table,
        today=date(2026, 9, 21), account_id="", min_service_cost=0.01,
        project_tag="boisestateai-v2",
    )
    assert not any(
        r["SK"] == "Amazon Relational Database Service" for r in scoped_table.service_rows
    )

    account = FakeCE([_page(PROD_SEPT)])
    account_table = FakeTable()
    mod.sync_period(
        "2026-09", account, account_table,
        today=date(2026, 9, 21), account_id="", min_service_cost=0.01,
        project_tag=None,
    )
    assert any(
        r["SK"] == "Amazon Relational Database Service" for r in account_table.service_rows
    )


def test_handler_reads_the_project_tag_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLATFORM_COST_SYNC_ENABLED", raising=False)
    monkeypatch.setenv("DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME", "t")
    monkeypatch.setenv("PLATFORM_COST_PROJECT_TAG", "boisestateai-v2")
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: object())
    monkeypatch.setattr(
        mod.boto3, "resource",
        lambda *a, **k: type("R", (), {"Table": lambda s, n: FakeTable()})(),
    )

    seen: List[Any] = []

    def fake_sync(period: str, *_a: Any, **kw: Any) -> Dict[str, Any]:
        seen.append(kw.get("project_tag"))
        return {"period": period, "scope": "deployment"}

    monkeypatch.setattr(mod, "sync_period", fake_sync)
    mod.handler({"periods": ["2026-09"]}, None)

    assert seen == ["boisestateai-v2"]


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_project_tag_means_account_scope_not_a_filter_on_empty_string(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """An empty env var must disable scoping, never filter on Project=''.

    Filtering on an empty value matches nothing, and the fallback would mask
    it — so the account total would be reported as if it were scoped.
    """
    monkeypatch.delenv("PLATFORM_COST_SYNC_ENABLED", raising=False)
    monkeypatch.setenv("DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME", "t")
    monkeypatch.setenv("PLATFORM_COST_PROJECT_TAG", value)
    monkeypatch.setattr(mod.boto3, "client", lambda *a, **k: object())
    monkeypatch.setattr(
        mod.boto3, "resource",
        lambda *a, **k: type("R", (), {"Table": lambda s, n: FakeTable()})(),
    )

    seen: List[Any] = []

    def fake_sync(period: str, *_a: Any, **kw: Any) -> Dict[str, Any]:
        seen.append(kw.get("project_tag"))
        return {"period": period, "scope": "account"}

    monkeypatch.setattr(mod, "sync_period", fake_sync)
    mod.handler({"periods": ["2026-09"]}, None)

    assert seen == [None]
