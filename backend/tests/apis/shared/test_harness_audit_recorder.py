"""RunAuditRecorder: the ``USER#/RUN#`` row "Run now" polls.

Pins the fields ``GET /runs/{runId}`` reads (status, title, errorDetail,
finishedAt) and that ``get`` is keyed on the caller, so another user's run
is simply missing.
"""

from __future__ import annotations

from typing import Any

from apis.shared.harness.governance import RunAuditRecorder
from apis.shared.harness.models import RunResult


class FakeTable:
    """Just enough of a boto3 Table for the recorder's three calls."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.updates: list[dict[str, Any]] = []

    def put_item(self, *, Item):
        self.items[(Item["PK"], Item["SK"])] = dict(Item)

    def update_item(self, *, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        self.updates.append(
            {
                "expr": UpdateExpression,
                "names": ExpressionAttributeNames,
                "values": ExpressionAttributeValues,
            }
        )

    def get_item(self, *, Key):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item is not None else {}


def _recorder(monkeypatch) -> tuple[RunAuditRecorder, FakeTable]:
    table = FakeTable()
    recorder = RunAuditRecorder(table_name="sessions-metadata", region="us-west-2")
    monkeypatch.setattr(recorder, "_table", lambda: table)
    return recorder, table


def _start(recorder: RunAuditRecorder, **overrides) -> None:
    kwargs = dict(
        run_id="run-0123456789ab",
        user_id="user-1",
        session_id="headless-1",
        trigger="run_now",
        prompt="ping",
    )
    kwargs.update(overrides)
    recorder.record_start(**kwargs)


def test_start_row_stores_the_title_when_given(monkeypatch):
    recorder, table = _recorder(monkeypatch)

    _start(recorder, title="My Briefing")

    item = table.items[("USER#user-1", "RUN#run-0123456789ab")]
    assert item["status"] == "started"
    assert item["title"] == "My Briefing"
    assert "ping" not in str(item)  # only the prompt hash and length


def test_start_row_has_no_title_when_none_given(monkeypatch):
    recorder, table = _recorder(monkeypatch)

    _start(recorder)

    assert "title" not in table.items[("USER#user-1", "RUN#run-0123456789ab")]


def test_end_row_writes_the_final_title(monkeypatch):
    recorder, table = _recorder(monkeypatch)

    recorder.record_end(
        result=RunResult(
            run_id="run-0123456789ab",
            session_id="headless-1",
            user_id="user-1",
            status="completed",
            title="Generated title",
        )
    )

    (update,) = table.updates
    assert "title = :ti" in update["expr"]
    assert update["values"][":ti"] == "Generated title"
    assert update["values"][":s"] == "completed"


def test_get_returns_the_callers_own_row(monkeypatch):
    recorder, _ = _recorder(monkeypatch)
    _start(recorder)

    item = recorder.get(user_id="user-1", run_id="run-0123456789ab")

    assert item is not None
    assert item["sessionId"] == "headless-1"


def test_get_does_not_return_another_users_run(monkeypatch):
    recorder, _ = _recorder(monkeypatch)
    _start(recorder, user_id="someone-else")

    assert recorder.get(user_id="user-1", run_id="run-0123456789ab") is None
