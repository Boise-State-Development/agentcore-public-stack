"""Quality runs published into the release ledger (release-ledger spec §5.2, PR-5).

``apis.shared.costs.ledger_publish`` builds the ``LEDGER#EVAL`` and
``LEDGER#SMOKE`` rows. What these tests pin: the row is keyed by the build
that served the turns (the Runtime's, from the run's own cost rows, else its
image tag, never app-api's), a publish is a put (twice is once), nothing from
an answer or an id of the run's own survives into the row, and the ledger
reader reads the row back whole.
"""

from __future__ import annotations

import json
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from apis.shared.costs import ledger_publish as lp
from apis.shared.evals.report import JudgeComparison
from apis.shared.evals.score import FamilyComparison

SESSION = "eval-tool_selection-0123456789abcdef"
USER = "c1a2b3c4-d5e6-7890-abcd-ef0123456789"
RUNTIME_BUILD = "d5ead33e05f3e229"
APP_API_BUILD = "0a0b0c0d0e0f1011"

META = {
    "model_id": "us.anthropic.claude-haiku-5-5",
    "k": 1,
    "startedAt": "2026-11-07T04:00:00.123456Z",
    "target": "runtime:dev-boisestateai-v2",
    "prefix": "dev-boisestateai-v2",
    "user_id": USER,
    "gitCommit": "8f676bb2e1a4c0d2" * 2 + "abcdef01",
    # app-api's /health, the wrong build for the ledger (it does not serve turns).
    "appVersion": "1.33.0",
    "build": APP_API_BUILD,
    "imageTags": {"inference-api": RUNTIME_BUILD, "app-api": APP_API_BUILD},
}

RESULTS = [
    {"case_id": "tool_selection.a", "family": "tool_selection", "sample": 0, "session_id": SESSION,
     "passed": True, "final_message": "The product is 41,000,213,950. Session secret words here.",
     "failures": [], "finished_at": "2026-11-07T04:01:30.123456Z"},
]


def _comparison(**overrides):
    base = dict(family="tool_selection", n=9, base_pass=9, run_pass=8, gains=0, losses=1, p_value=1.0,
                gating=True, failed=False, reason="lost 1 of 9 with no gains; not significant",
                missing_in_run=("tool_selection.z",), new_in_run=())
    base.update(overrides)
    return FamilyComparison(**base)


def _call(build: str = RUNTIME_BUILD, version: str = "1.33.0", cost: str = "0.0021", **extra):
    return {"sessionId": SESSION, "timestamp": "2026-11-07T04:00:05Z", "appVersion": version, "build": build,
            "cost": {"total": Decimal(cost)}, "tokenUsage": {"cacheReadInputTokens": 0, "cacheWriteInputTokens": 0},
            **extra}


# --------------------------------------------------------------------------- served build


class TestServedBuild:
    def test_the_cost_rows_name_the_build(self):
        served = lp.served_build([_call(), _call()], image_tag="other", app_version="1.32.0")
        assert (served.app_version, served.build, served.source) == ("1.33.0", RUNTIME_BUILD, "cost_rows")
        assert served.other_build_calls == 0

    def test_a_deploy_mid_run_keys_on_the_majority_and_counts_the_rest(self):
        served = lp.served_build_from_calls([_call(), _call(), _call(build="newbuild00000001")])
        assert served.build == RUNTIME_BUILD and served.other_build_calls == 1

    def test_unstamped_rows_fall_back_to_the_runtime_image_tag(self):
        served = lp.served_build([{"cost": {"total": 1}}], image_tag=RUNTIME_BUILD, app_version="1.33.0")
        assert (served.build, served.source) == (RUNTIME_BUILD, "image_tag")

    def test_nothing_to_key_by_is_none(self):
        assert lp.served_build([], image_tag=None, app_version="1.33.0") is None
        assert lp.served_build([], image_tag=RUNTIME_BUILD, app_version=None) is None

    def test_the_image_tag_is_the_runtimes_never_app_apis(self):
        assert lp.runtime_image_tag(META["imageTags"]) == RUNTIME_BUILD
        assert lp.runtime_image_tag({"app-api": APP_API_BUILD}) is None
        assert lp.runtime_image_tag(None) is None


# --------------------------------------------------------------------------- eval row


def _eval_row(**kwargs):
    served = lp.ServedBuild("1.33.0", RUNTIME_BUILD, lp.BUILD_FROM_COST_ROWS)
    return lp.eval_ledger_row(META, [_comparison()], served=served, result_rows=RESULTS,
                              published_at="2026-11-07T04:05:00Z", **kwargs)


class TestEvalRow:
    def test_is_the_section_8_table_keyed_by_the_served_build(self):
        row = _eval_row(call_rows=[_call(), _call(cost="0.0009")])
        assert row["PK"] == "LEDGER#EVAL" and row["SK"] == META["startedAt"]
        assert (row["appVersion"], row["build"], row["buildSource"]) == ("1.33.0", RUNTIME_BUILD, "cost_rows")
        assert row["build"] != META["build"]
        assert row["families"] == {"tool_selection": {
            "n": 9, "base": 9, "run": 8, "gained": 0, "lost": 1, "p": 1.0, "gate": "pass", "gating": True,
        }}
        assert row["gateFailed"] is False
        assert (row["modelId"], row["k"], row["target"]) == (META["model_id"], 1, META["target"])
        assert row["calls"] == 2 and row["costUsd"] == pytest.approx(0.003)
        assert row["durationS"] == 90.0 and row["samples"] == 1 and row["date"] == "2026-11-07"

    def test_gate_reads_fail_pass_or_advisory(self):
        served = lp.ServedBuild("1.33.0", RUNTIME_BUILD, "cost_rows")
        rows = [_comparison(family="tool_selection", failed=True),
                _comparison(family="refusal"), _comparison(family="general_chat", gating=False)]
        row = lp.eval_ledger_row(META, rows, served=served)
        assert {f: v["gate"] for f, v in row["families"].items()} == {
            "tool_selection": "fail", "refusal": "pass", "general_chat": "advisory"}
        assert row["gateFailed"] is True

    def test_judge_means_by_family_and_evaluator(self):
        judges = [JudgeComparison("general_chat", "helpfulness", 6, 0.81234, 0.79)]
        row = _eval_row(judge_comparisons=judges, judge_model="us.anthropic.claude-sonnet-4-6")
        assert row["judgeMeans"] == {"general_chat": {"helpfulness": {"n": 6, "base": 0.8123, "run": 0.79}}}
        assert row["judgeModel"] == "us.anthropic.claude-sonnet-4-6"

    def test_carries_nothing_from_an_answer_and_passes_the_guard(self):
        row = _eval_row(call_rows=[_call()])
        text = json.dumps(row, default=str)
        assert "41,000" not in text and "not significant" not in text and "tool_selection.z" not in text
        assert SESSION not in text and USER not in text
        lp.assert_content_free(row, lp.EVAL_ROW_FIELDS, forbidden=[SESSION, USER])

    def test_carries_each_cases_outcome_and_the_track_and_passes_the_guard(self):
        cases = {"tool_selection.a": {"pass": False, "passes": 0, "samples": 1, "base": True,
                                      "codes": ["tool_missing"], "names": ["calculator"]}}
        row = _eval_row(call_rows=[_call()], cases=cases, track="nightly")
        assert row["cases"] == cases and row["track"] == "nightly"
        lp.assert_content_free(row, lp.EVAL_ROW_FIELDS, forbidden=[SESSION, USER])

    def test_an_unknown_track_is_refused(self):
        with pytest.raises(lp.PublishError):
            _eval_row(track="hourly")

    def test_without_cases_or_track_the_row_is_as_before(self):
        row = _eval_row()
        assert "cases" not in row and "track" not in row

    def test_the_guard_still_refuses_prose_inside_a_case_entry(self):
        row = _eval_row(cases={"tool_selection.a": {"pass": False, "codes": ["did not call calculator"]}})
        with pytest.raises(lp.PublishError):
            lp.assert_content_free(row, lp.EVAL_ROW_FIELDS)

    def test_a_run_without_started_at_cannot_be_keyed(self):
        with pytest.raises(lp.PublishError):
            lp.eval_ledger_row({**META, "startedAt": None}, [], served=lp.ServedBuild("1", "b", "cost_rows"))


# --------------------------------------------------------------------------- smoke row


def _smoke_results():
    return [
        {"row": "plain_first_turn", "status": "PASS", "data": {"firstTokenMs": 900}},
        {"row": "second_turn_cache", "status": "PASS", "data": {}},
        {"row": "ask_user_question_resume", "status": "SKIP", "data": {}},
        {"row": "steer_mid_turn", "status": "OBSERVED", "data": {}},
        {"row": "attach_pdf", "status": "FAIL", "data": {}},
    ]


class TestSmokeRow:
    def _row(self, **kwargs):
        defaults = dict(
            started_at="2026-11-07T04:10:00.000001Z", finished_at="2026-11-07T04:12:00.000001Z",
            target="https://dev.boisestate.ai/api", results=_smoke_results(),
            served=lp.ServedBuild("1.33.0", RUNTIME_BUILD, "cost_rows"),
            first_token_ms=[900.4, 1200.0, 1500.0], published_at="2026-11-07T04:12:05Z",
        )
        defaults.update(kwargs)
        return lp.smoke_ledger_row(**defaults)

    def test_counts_every_status_and_names_each_rows_status(self):
        row = self._row()
        assert row["PK"] == "LEDGER#SMOKE" and row["SK"] == "2026-11-07T04:10:00.000001Z"
        assert (row["pass"], row["fail"], row["skip"], row["observed"]) == (2, 1, 1, 1)
        assert row["rows"]["attach_pdf"] == "FAIL" and len(row["rows"]) == 5
        assert row["firstTokenMsMedian"] == 1200 and row["firstTokenTurns"] == 3
        assert row["durationS"] == 120.0
        lp.assert_content_free(row, lp.SMOKE_ROW_FIELDS)

    def test_second_turn_cache_from_the_row_when_the_anatomy_was_readable(self):
        results = _smoke_results()
        results[1]["data"] = {"cacheRead": 4100, "cacheWrite": 12}
        row = self._row(results=results)
        assert (row["secondTurnCacheRead"], row["secondTurnCacheWrite"]) == (4100, 12)

    def test_second_turn_cache_from_the_shared_sessions_last_cost_row_otherwise(self):
        first = _call(timestamp="2026-11-07T04:10:01Z", tokenUsage={"cacheReadInputTokens": 0, "cacheWriteInputTokens": 3900})
        second = _call(timestamp="2026-11-07T04:10:09Z", tokenUsage={"cacheReadInputTokens": 3900, "cacheWriteInputTokens": 40})
        row = self._row(shared_session_calls=[second, first])
        assert (row["secondTurnCacheRead"], row["secondTurnCacheWrite"]) == (3900, 40)

    def test_detail_and_warnings_never_reach_the_row(self):
        results = [{"row": "tool_turn", "status": "FAIL", "detail": "model said: the secret is 42",
                    "warnings": ["prose"], "data": {}}]
        text = json.dumps(self._row(results=results))
        assert "secret" not in text and "prose" not in text


# --------------------------------------------------------------------------- guard


class TestContentGuard:
    def test_refuses_prose(self):
        with pytest.raises(lp.PublishError, match="not id-shaped"):
            lp.assert_content_free({"PK": "LEDGER#EVAL", "target": "the answer was wrong"}, lp.EVAL_ROW_FIELDS)

    def test_refuses_the_runs_own_ids_even_inside_an_id(self):
        row = {"PK": "LEDGER#SMOKE", "target": f"runtime:{SESSION}"}
        with pytest.raises(lp.PublishError, match="session or user ids"):
            lp.assert_content_free(row, lp.SMOKE_ROW_FIELDS, forbidden=[SESSION])

    def test_refuses_an_attribute_it_does_not_know(self):
        with pytest.raises(lp.PublishError, match="unexpected attribute"):
            lp.assert_content_free({"PK": "LEDGER#EVAL", "sessionIds": []}, lp.EVAL_ROW_FIELDS)

    def test_refuses_a_prose_map_key(self):
        with pytest.raises(lp.PublishError):
            lp.assert_content_free({"rows": {"a row with spaces": "PASS"}}, lp.SMOKE_ROW_FIELDS)


# --------------------------------------------------------------------------- write


@pytest.fixture
def rollup_table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-west-2")
        table = ddb.create_table(
            TableName="test-system-cost-rollup",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield table


class TestWrite:
    def test_publishing_one_run_twice_is_one_item(self, rollup_table):
        row = _eval_row(call_rows=[_call()])
        assert lp.put_ledger_row(row, table=rollup_table)
        assert lp.put_ledger_row(row, table=rollup_table)
        items = rollup_table.scan()["Items"]
        assert len(items) == 1
        assert items[0]["families"]["tool_selection"]["p"] == Decimal("1.0")
        assert items[0]["costUsd"] == Decimal("0.0021")

    def test_a_failed_write_returns_false_and_never_raises(self):
        class Broken:
            def put_item(self, **_):
                raise RuntimeError("AccessDenied")

        assert lp.put_ledger_row({"PK": "LEDGER#EVAL", "SK": "x"}, table=Broken()) is False
        assert lp.put_ledger_row({"PK": "LEDGER#EVAL", "SK": "x"}) is False  # no table, no prefix

    def test_the_table_name_is_derived_from_the_prefix(self):
        assert lp.rollup_table_name("dev-boisestateai-v2") == "dev-boisestateai-v2-system-cost-rollup"


# --------------------------------------------------------------------------- read back


def test_the_ledger_reader_reads_a_published_row_back_whole(rollup_table):
    """The quality route filters rows through ``export_row`` and matches them by unit (PR-4)."""
    reader = pytest.importorskip("apis.shared.costs.ledger_reader")
    cases = {"tool_selection.a": {"pass": True, "passes": 1, "samples": 1, "base": False,
                                  "codes": ["tool_missing"], "names": ["calculator"]}}
    for row in (_eval_row(call_rows=[_call()], cases=cases, track="nightly"),
                TestSmokeRow()._row(results=[dict(r) for r in _smoke_results()])):
        assert lp.put_ledger_row(row, table=rollup_table)
    store = reader.LedgerStore(table=rollup_table)
    evals, smokes = store.eval_rows("2026-11-07", "2026-11-07"), store.smoke_rows("2026-11-07", "2026-11-07")
    assert len(evals) == 1 and len(smokes) == 1
    for stored in (evals[0], smokes[0]):
        exported = reader.export_row(stored)
        assert set(exported) == set(stored), "the reader's id filter dropped a published attribute"
        assert reader.Unit("1.33.0", RUNTIME_BUILD).matches(stored)
        assert reader.Unit("1.33.0").matches(stored)
        assert not reader.Unit("1.33.0", APP_API_BUILD).matches(stored)
    assert reader.export_row(evals[0])["cases"] == cases
