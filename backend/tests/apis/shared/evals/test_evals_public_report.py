"""The public benchmark document (eval-harness spec §19).

What matters, in order:

1. Nothing private leaves: session ids, user ids, the deployment prefix, answers
   and failure prose never reach the document, and the guard refuses a
   document that carries them anyway.
2. Failures are published, by case id, never hidden or rounded away.
3. Cost covers every sample, passing or failing, from the metered cost rows.
"""

from __future__ import annotations

import pytest

from apis.shared.evals.public_report import (
    PublishError,
    assert_publishable,
    build_document,
    headline_markdown,
    run_document,
)

META = {
    "model_id": "us.anthropic.claude-haiku-5-5", "k": 3, "startedAt": "2026-11-13T02:00:00Z",
    "appVersion": "1.35.0", "gitCommit": "abc1234", "imageTags": {"inference-api": "img0001", "app-api": "img0002"},
    "user_id": "user-secret-0001", "prefix": "dev-secret-prefix", "target": "runtime:dev-secret-prefix",
}


def _rows_and_calls():
    rows, calls = [], {}
    for case_id, outcomes in (("tool_selection.a", (True, True, False)), ("tool_selection.b", (False, False, True))):
        for sample, passed in enumerate(outcomes):
            session_id = f"eval-session-{case_id[-1]}{sample}"
            calls[session_id] = {"modelCalls": 2.0, "outputTokens": 100.0, "inputTokens": 1000.0, "costUsd": 0.01}
            rows.append({
                "case_id": case_id, "family": "tool_selection", "sample": sample, "session_id": session_id,
                "passed": passed, "failures": [] if passed else ["did not call calculator"],
                "final_message": "the answer is 42",
                "timing": [{"firstTokenMs": 800, "turnDurationMs": 3000}],
                "turns": [{"tool_trace": [{"name": "calculator", "input": {"expression": "6*7"}}]}],
            })
    return rows, calls


def _document():
    rows, calls = _rows_and_calls()
    return build_document([run_document(META, rows, calls)], generated_at="2026-11-13"), rows


def test_a_family_publishes_quality_speed_and_cost():
    document, _ = _document()
    (run,) = document["runs"]
    (family,) = run["families"]
    assert (family["cases"], family["passing"], family["failing"]) == (2, 1, ["tool_selection.b"])
    assert family["costPerSampleUsd"] == 0.01 and family["modelCalls"] == 2.0
    assert (family["turnMs"], family["firstTokenMs"]) == (3000.0, 800.0)
    assert run["totals"]["costUsd"] == pytest.approx(0.06) and run["totals"]["samples"] == 6
    assert run["runtimeImage"] == "img0001" and run["environment"].startswith("development deployment")


def test_nothing_private_reaches_the_document():
    document, rows = _document()
    text = repr(document)
    for secret in ("user-secret-0001", "dev-secret-prefix", "eval-session-", "the answer is 42",
                   "did not call calculator", "6*7"):
        assert secret not in text
    assert_publishable(document, known_case_ids={"tool_selection.a", "tool_selection.b"},
                       forbidden=["user-secret-0001", "dev-secret-prefix", *(r["session_id"] for r in rows)])


def test_the_guard_refuses_a_private_id_an_unknown_case_or_an_extra_field():
    document, _ = _document()
    known = {"tool_selection.a", "tool_selection.b"}
    leaked = {**document, "runs": [{**document["runs"][0], "gitCommit": "dev-secret-prefix"}]}
    with pytest.raises(PublishError, match="prefix"):
        assert_publishable(leaked, known_case_ids=known, forbidden=["dev-secret-prefix"])
    with pytest.raises(PublishError, match="golden set"):
        assert_publishable(document, known_case_ids={"tool_selection.a"})
    with pytest.raises(PublishError, match="unexpected run field"):
        assert_publishable({**document, "runs": [{**document["runs"][0], "answers": []}]}, known_case_ids=known)
    with pytest.raises(PublishError, match="id-shaped"):
        prose = {**document, "runs": [{**document["runs"][0], "appVersion": "line one\nline two"}]}
        assert_publishable(prose, known_case_ids=known)


def test_the_headline_names_every_model_with_its_failures_counted():
    document, _ = _document()
    table = headline_markdown(document)
    assert "`us.anthropic.claude-haiku-5-5` | 1 of 2 (k=3) | $0.0100 | 3.0 s | 0.8 s | 2026-11-13, v1.35.0" in table


def test_runs_are_ordered_cheapest_per_task_first():
    rows, calls = _rows_and_calls()
    cheap = run_document(META, rows, calls)
    dear_calls = {s: {**c, "costUsd": 0.5} for s, c in calls.items()}
    dear = run_document({**META, "model_id": "us.anthropic.claude-opus-5-5"}, rows, dear_calls)
    assert [r["model"] for r in build_document([dear, cheap], "2026-11-13")["runs"]] == [
        "us.anthropic.claude-haiku-5-5", "us.anthropic.claude-opus-5-5",
    ]
