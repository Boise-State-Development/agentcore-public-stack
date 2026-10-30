"""Running one sample of a case against a stand-in turn runner (eval-harness spec §6.1)."""

from __future__ import annotations

import json

import pytest

from apis.shared.evals.cases import parse_case
from apis.shared.evals.execute import EVAL_SESSION_PREFIX, OAuthRequiredError, run_sample, sum_usage
from apis.shared.harness.models import OAuthConsentRequired, RunResult, ToolTraceEntry


def _retention_case():
    return parse_case(
        {
            "id": "retention.code",
            "family": "retention",
            "turns": [{"prompt": "My code is QX-4471."}, {"prompt": "Filler."}, {"prompt": "What was my code?"}],
            "run": {"enabled_tools": [], "enabled_skills": []},
            "expect": {"contains_any": ["QX-4471"]},
        },
        family="retention",
    )


def _doc_case():
    return parse_case(
        {
            "id": "document_qa.fee",
            "family": "document_qa",
            "turns": [{"prompt": "What is the fee?"}, {"prompt": "And again?"}],
            "run": {"enabled_tools": ["calculator"]},
            "expect": {"contains_any": ["$35"], "tools": {"must_not_call": ["calculator"]}},
            "files": ["policy.md"],
        },
        family="document_qa",
    )


class FakeRunner:
    def __init__(self, answers, *, status="completed", tools=None, fail_on=None, oauth_on=None):
        self.answers = list(answers)
        self.status = status
        self.tools = tools or []
        self.fail_on = fail_on
        self.oauth_on = oauth_on
        self.calls = []

    async def __call__(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        result = RunResult(
            run_id=f"r{index}",
            session_id=kwargs["session_id"],
            user_id="u",
            status="completed",
            final_message=self.answers[index],
            started_at="2026-10-29T10:00:00Z",
            finished_at="2026-10-29T10:00:05Z",
            usage={"usage": {"inputTokens": 100, "outputTokens": 10}},
        )
        if index == len(self.answers) - 1:
            result.tool_trace = [ToolTraceEntry(tool_use_id="t1", name=n, input={}) for n in self.tools]
        if self.fail_on == index:
            result.status = "timeout"
            result.error = "stream exceeded 300s budget"
        if self.oauth_on == index:
            result.status = "oauth_required"
            result.oauth_required = [OAuthConsentRequired(provider_id="google", authorization_url="https://x")]
        return result


@pytest.mark.asyncio
async def test_every_turn_shares_one_fresh_session_and_only_the_last_is_scored():
    runner = FakeRunner(["Noted.", "Sure.", "Your code was QX-4471."])
    row = await run_sample(_retention_case(), sample=2, model_id="m", run_turn=runner)

    session_ids = {call["session_id"] for call in runner.calls}
    assert len(session_ids) == 1 and next(iter(session_ids)).startswith(EVAL_SESSION_PREFIX)
    assert [call["prompt"] for call in runner.calls] == ["My code is QX-4471.", "Filler.", "What was my code?"]
    assert all(call["enabled_tools"] == [] and call["model_id"] == "m" for call in runner.calls)
    assert row["passed"] and row["sample"] == 2 and row["status"] == "completed"
    assert row["usage"] == {"inputTokens": 300, "outputTokens": 30}
    assert len(row["session"]["traces"]) == 3
    json.dumps(row)  # the row is written to results.jsonl as-is


@pytest.mark.asyncio
async def test_a_failed_middle_turn_fails_the_sample_without_running_the_rest():
    runner = FakeRunner(["Noted.", "Sure.", "QX-4471"], fail_on=1)
    row = await run_sample(_retention_case(), sample=0, model_id="m", run_turn=runner)
    assert len(runner.calls) == 2
    assert not row["passed"] and row["status"] == "timeout"
    assert row["failures"] == ["status: timeout"] and "300s" in row["error"]


@pytest.mark.asyncio
async def test_oauth_required_stops_the_run_instead_of_scoring():
    with pytest.raises(OAuthRequiredError, match="google"):
        await run_sample(_retention_case(), sample=0, model_id="m", run_turn=FakeRunner(["a", "b", "c"], oauth_on=0))


@pytest.mark.asyncio
async def test_fixtures_are_registered_in_the_session_and_sent_with_the_first_turn_only():
    registered = []

    async def register(session_id: str, filename: str) -> str:
        registered.append((session_id, filename))
        return "upload-1"

    runner = FakeRunner(["It's $35.", "Still $35."], tools=["calculator"])
    row = await run_sample(_doc_case(), sample=0, model_id="m", run_turn=runner, register_fixture=register)

    assert registered == [(runner.calls[0]["session_id"], "policy.md")]
    assert runner.calls[0]["file_upload_ids"] == ["upload-1"]
    assert runner.calls[1]["file_upload_ids"] is None
    assert runner.calls[0]["enabled_skills"] is None
    assert row["upload_ids"] == ["upload-1"]
    assert not row["passed"] and row["failures"] == ["called calculator"]
    assert row["session"]["traces"][0]["spans"][0]["available_tools"][0]["name"] == "calculator"


@pytest.mark.asyncio
async def test_a_case_with_files_needs_a_registrar():
    with pytest.raises(ValueError, match="registrar"):
        await run_sample(_doc_case(), sample=0, model_id="m", run_turn=FakeRunner(["a", "b"]))


def test_sum_usage_ignores_non_numeric_values():
    turns = [{"usage": {"usage": {"inputTokens": 5, "flag": True, "note": "x"}}}, {"usage": {}}, {}]
    assert sum_usage(turns) == {"inputTokens": 5}
