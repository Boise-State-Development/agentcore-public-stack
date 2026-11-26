"""The inbox answer's checks (docs/specs/answer-from-inbox.md §3.4–3.5), without IO."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from apis.shared.handoff.models import RelayedPrompt
from apis.shared.notifications.models import Notification
from apis.shared.scheduled_prompts.inbox_answer import (
    STALE_CLAIM,
    InvalidResponses,
    check_responses,
    read_inbox_pause,
    stale_claim_before,
    summarize_decisions,
)

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
FORM = {
    "interruptId": "f",
    "kind": "mcp_elicitation",
    "elicitation": json.dumps({"serverName": "GitHub", "message": "Pick", "fields": []}),
    "createdAt": "2026-11-26T00:00:00Z",
}


def _row(*kinds, answerable: bool = True, expires: str = "2026-12-03T12:00:00Z") -> Notification:
    return Notification(
        notification_id="0000000000001-aaaaaaaaaaaa",
        recipient_email="a@example.edu",
        kind="agent_needs_input",
        created_at="2026-11-26T00:00:00Z",
        payload={
            "sessionId": "headless-1",
            "expiresAt": expires,
            "answerable": answerable,
            "prompts": [{"interruptId": i, "kind": k} for i, k in kinds],
        },
    )


def _prompt(interrupt_id: str, kind: str) -> RelayedPrompt:
    return RelayedPrompt(interrupt_id=interrupt_id, kind=kind)


class TestPause:
    def test_a_form_reads_live_with_its_form(self):
        row = _row(("f", "mcp_elicitation"))
        meta = {"pendingInterrupts": [FORM], "pausedTurn": {"expiresAt": (NOW + timedelta(days=1)).isoformat()}}
        pause = read_inbox_pause(row, meta, now=NOW)
        assert pause.state == "live"
        assert json.loads(pause.prompts[0].elicitation)["serverName"] == "GitHub"

    def test_one_prompt_gone_is_the_pause_gone(self):
        row = _row(("f", "mcp_elicitation"), ("g", "tool_approval"))
        meta = {"pendingInterrupts": [FORM], "pausedTurn": {"expiresAt": (NOW + timedelta(days=1)).isoformat()}}
        assert read_inbox_pause(row, meta, now=NOW).state == "gone"

    def test_the_rows_own_expiry_wins(self):
        assert read_inbox_pause(_row(("f", "mcp_elicitation"), expires="2026-11-26T11:59:59Z"), None, now=NOW).state == "expired"


class TestResponses:
    @pytest.mark.parametrize(
        ("response", "decision"),
        [
            ({"action": "accept", "content": {"repo": "x"}}, "answered"),
            ({"action": "accept"}, "answered"),
            ({"action": "decline"}, "skipped"),
            ({"action": "cancel"}, "skipped"),
        ],
    )
    def test_form_answers(self, response, decision):
        answers, summary, decisions = check_responses(
            _row(("f", "mcp_elicitation")), [_prompt("f", "mcp_elicitation")], [{"interruptId": "f", "response": response}]
        )
        assert answers == [{"interruptId": "f", "response": response}]
        assert (summary, decisions) == (decision, [decision])

    @pytest.mark.parametrize("response", [{"action": "submit"}, {"action": "accept", "content": []}, "accept"])
    def test_form_shapes_the_card_never_sends(self, response):
        with pytest.raises(InvalidResponses):
            check_responses(
                _row(("f", "mcp_elicitation")), [_prompt("f", "mcp_elicitation")],
                [{"interruptId": "f", "response": response}],
            )

    @pytest.mark.parametrize("response", [{"skipped": True}, {"answers": {"Calendar": {"selected": ["Work"]}}}])
    def test_question_answers(self, response):
        _, summary, _ = check_responses(
            _row(("q", "user_question")), [_prompt("q", "user_question")], [{"interruptId": "q", "response": response}]
        )
        assert summary == ("skipped" if "skipped" in response else "answered")

    def test_a_question_needs_answers_or_skipped(self):
        with pytest.raises(InvalidResponses):
            check_responses(
                _row(("q", "user_question")), [_prompt("q", "user_question")],
                [{"interruptId": "q", "response": "Work"}],
            )


def test_one_decision_per_row():
    assert summarize_decisions(["approved", "approved"]) == "approved"
    assert summarize_decisions(["approved", "declined"]) == "declined"
    assert summarize_decisions(["approved", "answered"]) == "answered"
    assert summarize_decisions(["skipped", "declined"]) == "skipped"


def test_a_claim_goes_stale_after_the_workers_timeout():
    assert STALE_CLAIM > timedelta(minutes=15)
    assert stale_claim_before(NOW) == "2026-11-26T11:44:00Z"
