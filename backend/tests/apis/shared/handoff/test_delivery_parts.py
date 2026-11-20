"""A workflow run's result: one ``DELIVERY#`` row with a part per step (workflow-runs spec §7, PR-W.2).

The store is real, over a moto table, so the exactly-once attach is its own condition. Session
``META`` reads, the artifact index and the file rows are stubbed, as in ``test_deliveries``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List

import boto3
import pytest
from moto import mock_aws

from apis.shared.handoff import send_back
from apis.shared.handoff.context import (
    DELIVERY_PART_STATUS_NOTES,
    DeliveryPartBlock,
    context_hash,
    delivery_part_label,
    render_delivery_block,
    render_delivery_parts,
)
from apis.shared.handoff.delivery import (
    DELIVERY_MAX_PARTS,
    SEND_BACK_MAX_CHARS,
    Delivery,
    DeliveryArtifact,
    DeliveryFile,
    DeliveryPart,
    DeliveryStore,
    DeliveryView,
)
from apis.shared.handoff.delivery_turn import DeliveryAttachRequest, DeliveryPartEdit, DeliveryTurn
from apis.shared.handoff.runs import WORKFLOW_RUN_MAX_STEPS
from apis.shared.handoff.send_back import DeliveryPartRequest, DeliveryRefused, create_delivery

REGION = "us-west-2"
TABLE = "test-sessions-metadata"
NOW = "2026-11-20T00:00:00+00:00"


@pytest.fixture()
def table(monkeypatch):
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", "GSI_PK", "GSI_SK")],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "SessionLookupIndex",
                    "KeySchema": [
                        {"AttributeName": "GSI_PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI_SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table(TABLE)


@pytest.fixture()
def store(table) -> DeliveryStore:
    return DeliveryStore(table=table)


@pytest.fixture(autouse=True)
def flag_on(monkeypatch):
    monkeypatch.setenv("HANDOFFS_ENABLED", "true")


class _Sessions:
    """``load_session_meta`` over ``(session, user) -> META row``: a source and three step targets."""

    def __init__(self) -> None:
        self.rows: Dict[tuple, Dict[str, Any]] = {
            ("src", "alice"): {"title": "Proposal review"},
            ("lone", "alice"): {"title": "Unrelated"},
        }
        for step in ("s1", "s2", "s3"):
            self.rows[(f"t-{step}", "alice")] = {
                "title": "Review of the proposal",
                "spawnedFrom": {"sessionId": "src", "handoffId": f"h-{step}"},
            }
        self.reads: List[tuple] = []

    async def load(self, session_id: str, user_id: str):
        self.reads.append((session_id, user_id))
        return SimpleNamespace(row=self.rows.get((session_id, user_id)), owned_by_other=False)


@pytest.fixture()
def sessions(monkeypatch) -> _Sessions:
    s = _Sessions()
    monkeypatch.setattr("apis.shared.sessions.metadata.load_session_meta", s.load)
    return s


@pytest.fixture()
def artifacts(monkeypatch) -> Dict[tuple, List[Dict[str, Any]]]:
    heads: Dict[tuple, List[Dict[str, Any]]] = {
        ("alice", "t-s1"): [{"artifact_id": "memo", "version": 2, "title": "Memo", "content_type": "text/markdown"}],
        ("alice", "lone"): [{"artifact_id": "elsewhere", "version": 1, "title": "Other", "content_type": "text/html"}],
    }
    monkeypatch.setattr(send_back, "_artifact_heads", lambda user, session: heads.get((user, session), []))
    return heads


@pytest.fixture()
def files(monkeypatch) -> Dict[str, Any]:
    """File rows by id; each is ``READY`` in the step conversation it names."""
    from apis.shared.files.models import FileMetadata, FileStatus

    rows: Dict[str, Any] = {}
    for step in ("s1", "s2"):
        for i in range(6):
            upload = f"f-{step}-{i}"
            rows[upload] = FileMetadata(
                upload_id=upload,
                user_id="alice",
                session_id=f"t-{step}",
                filename=f"{upload}.pdf",
                mime_type="application/pdf",
                size_bytes=1024,
                s3_key=f"user_files/alice/t-{step}/{upload}/{upload}.pdf",
                s3_bucket="bucket",
                status=FileStatus.READY,
                created_at=NOW,
                updated_at=NOW,
            )
    repo = SimpleNamespace(batch_get_files_sync=lambda user, ids: {i: rows[i] for i in ids if i in rows})
    monkeypatch.setattr("apis.shared.files.repository.get_file_upload_repository", lambda: repo)
    return rows


def _parts(**overrides) -> List[DeliveryPartRequest]:
    """Two finished steps, one stopped, one never started; ``s<index>=`` replaces a slot."""
    parts = [
        DeliveryPartRequest(
            step_id="s1", agent_name="Generalist", status="done", from_session_id="t-s1",
            text="Strong aims; weak budget.", artifact_ids=["memo"],
        ),
        DeliveryPartRequest(
            step_id="s2", agent_name="Colleague", status="done", from_session_id="t-s2", text="Section 4 is thin."
        ),
        DeliveryPartRequest(step_id="s3", agent_name="Fiscal outsider", status="stopped", from_session_id="t-s3"),
        DeliveryPartRequest(step_id="s4", agent_name="Emotional outsider", status="running"),
    ]
    for index, change in overrides.items():
        parts[int(index[1:])] = change
    return parts


def _collect(store, parts=None, **kwargs) -> Delivery:
    return asyncio.run(
        create_delivery(user_id="alice", to_session_id="src", parts=parts or _parts(), run_id="run-1", store=store, **kwargs)
    )


# --- making one ---------------------------------------------------------------------------------


def test_a_run_result_is_one_row_with_a_part_per_step_in_step_order(store, sessions, artifacts, files):
    row = _collect(store)

    assert [p.step_id for p in row.parts] == ["s1", "s2", "s3", "s4"]
    assert [p.status for p in row.parts] == ["done", "done", "stopped", "running"]
    first = row.parts[0]
    assert (first.from_session_id, first.from_title, first.text_hash) == (
        "t-s1", "Review of the proposal", context_hash("Strong aims; weak budget.")
    )
    assert first.artifacts == [DeliveryArtifact(artifact_id="memo", version=2, title="Memo", content_type="text/markdown")]
    # A part with no result carries nothing and reads nothing: its conversation was never a sender.
    assert (row.parts[2].text, row.parts[2].from_title, row.parts[2].artifacts) == ("", "", [])
    assert ("t-s3", "alice") not in sessions.reads
    # The row-level body stays empty; a reader that doesn't know parts sees the first step's names.
    assert (row.text, row.artifacts, row.files, row.run_id) == ("", [], [], "run-1")
    assert (row.from_session_id, row.from_title) == ("t-s1", "Review of the proposal")

    stored = store.get("alice", row.delivery_id)
    assert stored == row
    # The receiver is read once however many steps there are, beside every sender.
    assert sessions.reads.count(("src", "alice")) == 1


def test_a_parts_row_round_trips_through_dynamodb_numbers(store, sessions, artifacts, files):
    row = _collect(store, parts=_parts(s0=DeliveryPartRequest(
        step_id="s1", agent_name="Generalist", status="done", from_session_id="t-s1",
        text="see files", file_ids=["f-s1-0", "f-s1-1"],
    )))
    item = store._table.get_item(Key={"PK": "USER#alice", "SK": f"DELIVERY#{row.delivery_id}"})["Item"]

    assert isinstance(item["parts"][0]["files"][0]["sizeBytes"], Decimal)
    assert store.get("alice", row.delivery_id).parts[0].files[0].size_bytes == 1024


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        # The sender rule, per part: the step's conversation must be linked to the receiver …
        (DeliveryPartRequest(step_id="s2", agent_name="C", status="done", from_session_id="lone", text="x"), "not_linked"),
        # … the caller's …
        (DeliveryPartRequest(step_id="s2", agent_name="C", status="done", from_session_id="nobody", text="x"), "not_found"),
        # … and every artifact made in that part's own conversation.
        (
            DeliveryPartRequest(
                step_id="s2", agent_name="C", status="done", from_session_id="t-s2", text="x", artifact_ids=["elsewhere"]
            ),
            "artifact_not_in_sender",
        ),
        (
            DeliveryPartRequest(
                step_id="s2", agent_name="C", status="done", from_session_id="t-s2", text="x", file_ids=["f-s1-0"]
            ),
            "file_not_in_sender",
        ),
    ],
)
def test_every_rule_applies_to_each_part(store, sessions, artifacts, files, change, reason):
    with pytest.raises(DeliveryRefused) as refused:
        _collect(store, parts=_parts(s1=change))

    assert refused.value.reason == reason


@pytest.mark.parametrize("project_end", ["src", "t-s2"])
def test_a_shared_project_task_on_either_end_refuses_the_result(store, sessions, artifacts, files, project_end):
    sessions.rows[(project_end, "alice")]["projectId"] = "p1"

    with pytest.raises(DeliveryRefused) as refused:
        _collect(store)

    assert refused.value.reason == "project_task"


def test_a_run_result_carries_at_most_ten_files_and_five_a_part(store, sessions, artifacts, files):
    def done(step: str, count: int) -> DeliveryPartRequest:
        return DeliveryPartRequest(
            step_id=step, agent_name=step, status="done", from_session_id=f"t-{step}",
            file_ids=[f"f-{step}-{i}" for i in range(count)],
        )

    assert sum(len(p.files) for p in _collect(store, parts=[done("s1", 5), done("s2", 5)]).parts) == 10
    with pytest.raises(DeliveryRefused) as per_part:
        _collect(store, parts=[done("s1", 6)])
    assert per_part.value.reason == "too_many_files"
    with pytest.raises(DeliveryRefused) as per_run:
        _collect(store, parts=[done("s1", 5), done("s2", 5), done("s3", 1)])
    assert per_run.value.reason == "too_many_files"


def test_a_result_with_nothing_finished_is_refused_as_empty(store, sessions, artifacts, files):
    with pytest.raises(DeliveryRefused) as refused:
        _collect(store, parts=_parts()[2:])

    assert refused.value.reason == "empty"
    assert store.list_pending("alice", "src") == []


def test_a_part_with_no_result_may_not_carry_one(store, sessions, artifacts, files):
    with pytest.raises(ValueError):
        _collect(store, parts=_parts(s2=DeliveryPartRequest(step_id="s3", agent_name="F", status="stopped", text="x")))


def test_parts_are_capped_like_a_card_and_bounded_in_number(store, sessions, artifacts, files):
    long = DeliveryPartRequest(step_id="s1", agent_name="G", status="done", from_session_id="t-s1", text="y " * 20_000)
    assert len(_collect(store, parts=_parts(s0=long)).parts[0].text) <= SEND_BACK_MAX_CHARS
    assert DELIVERY_MAX_PARTS == WORKFLOW_RUN_MAX_STEPS

    many = [
        DeliveryPartRequest(step_id=f"x{i}", agent_name="A", status="stopped") for i in range(DELIVERY_MAX_PARTS + 1)
    ]
    with pytest.raises(DeliveryRefused) as refused:
        _collect(store, parts=many)
    assert refused.value.reason == "too_many_parts"


def test_the_worst_case_row_stays_well_under_dynamodbs_item_limit():
    """Eight parts at the cap in four-byte characters, five long artifact titles each, ten files."""
    wide = "\U0001F600" * SEND_BACK_MAX_CHARS
    long_title = "t" * 240
    file = DeliveryFile(
        upload_id="u" * 36, session_id="s" * 36, from_title=long_title, filename="n" * 255,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=50 * 1024 * 1024, s3_key="k" * 300, s3_bucket="b" * 63, ttl=1_900_000_000,
    )
    parts = [
        DeliveryPart(
            step_id="x" * 36, agent_name=long_title, from_session_id="s" * 36, from_title=long_title,
            status="done", text=wide, text_hash="h" * 64,
            artifacts=[DeliveryArtifact(artifact_id="a" * 36, version=99, title=long_title, content_type="text/html")] * 5,
            files=[file] * (2 if i < 5 else 0),
        )
        for i in range(DELIVERY_MAX_PARTS)
    ]
    row = Delivery(
        delivery_id="d" * 36, user_id="u" * 64, to_session_id="s" * 36, from_session_id="s" * 36,
        from_title=long_title, run_id="r" * 36, parts=parts, created_at=NOW, updated_at=NOW,
    )
    size = len(json.dumps(row.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False).encode("utf-8"))

    assert size < 300_000  # ~290 KB, against 400 KB (spec PR-W.2)


# --- what the model reads -----------------------------------------------------------------------


def test_each_part_is_its_own_block_in_step_order_and_a_missing_step_is_named():
    out = render_delivery_parts(
        [
            DeliveryPartBlock("Strong aims.", "Generalist", "Review of the proposal", "done"),
            DeliveryPartBlock("", "Fiscal outsider", "", "stopped"),
            DeliveryPartBlock("", "Colleague", "Review", "needs_input"),
        ]
    )

    assert out == "\n\n".join(
        [
            render_delivery_block("Strong aims.", "Generalist · Review of the proposal"),
            render_delivery_block(DELIVERY_PART_STATUS_NOTES["stopped"], "Fiscal outsider"),
            render_delivery_block(DELIVERY_PART_STATUS_NOTES["needs_input"], "Colleague · Review"),
        ]
    )
    assert delivery_part_label("Generalist", "Generalist") == "Generalist"


def test_a_finished_part_the_user_emptied_gives_no_block():
    assert render_delivery_parts([DeliveryPartBlock("  ", "Generalist", "Review", "done")]) == ""


# --- attaching on a turn ------------------------------------------------------------------------


def _turn(store, *requests: DeliveryAttachRequest) -> DeliveryTurn:
    return DeliveryTurn.for_turn(
        user_id="alice", session_id="src", deliveries=list(requests), is_resume=False, is_continuation=False, store=store
    )


@pytest.fixture()
def updates(store, monkeypatch) -> List[str]:
    """Every ``UpdateItem`` the store issues, by update expression."""
    seen: List[str] = []
    real = store._table.update_item

    def counted(**kwargs):
        seen.append(kwargs["UpdateExpression"])
        return real(**kwargs)

    monkeypatch.setattr(store._table, "update_item", counted)
    return seen


def test_a_run_result_attaches_with_one_conditional_write_and_renders_every_part(store, sessions, artifacts, files, updates):
    row = _collect(store)

    blocks = asyncio.run(_turn(store, DeliveryAttachRequest(delivery_id=row.delivery_id)).blocks())

    assert len(blocks) == 1
    assert len(updates) == 1 and updates[0].startswith("SET #status = :attached")
    assert blocks[0] == render_delivery_parts(
        [
            DeliveryPartBlock(p.text, p.agent_name, p.from_title, p.status, p.artifacts) for p in row.parts
        ]
    )
    assert blocks[0].count("<handoff_context ") == 4
    assert '<artifact id="memo" version="2"' in blocks[0]
    assert DELIVERY_PART_STATUS_NOTES["stopped"] in blocks[0] and DELIVERY_PART_STATUS_NOTES["running"] in blocks[0]
    assert store.get("alice", row.delivery_id).status == "attached"
    # Exactly once: a second send of the same card attaches nothing.
    assert asyncio.run(_turn(store, DeliveryAttachRequest(delivery_id=row.delivery_id)).blocks()) == []


def test_an_edited_part_is_recapped_used_and_counted(store, sessions, artifacts, files, monkeypatch):
    seen: List[tuple] = []
    monkeypatch.setattr(
        "apis.shared.handoff.delivery_turn.emit_handoff_metric", lambda name, kind, **kw: seen.append((name, kw))
    )
    row = _collect(store)
    request = DeliveryAttachRequest(
        delivery_id=row.delivery_id,
        parts=[
            DeliveryPartEdit(step_id="s2", text="Only section 4. " + "z " * 10_000),
            DeliveryPartEdit(step_id="s1", text="Strong aims; weak budget.\r\n"),  # unchanged once normalized
            DeliveryPartEdit(step_id="s3", text="a stopped step has no result to edit"),
        ],
    )

    async def run() -> List[str]:
        blocks = await _turn(store, request).blocks()
        await asyncio.sleep(0.05)  # the edited flag is written in the background
        return blocks

    [block] = asyncio.run(run())

    assert "Only section 4." in block and "[truncated]" in block and "Section 4 is thin." not in block
    assert "a stopped step has no result" not in block
    assert ("DeliveryEdited", {"session_id": "src", "detail": "user", "count": 1}) in seen
    assert store.get("alice", row.delivery_id).edited is True


def test_a_send_without_parts_attaches_the_stored_parts(store, sessions, artifacts, files):
    """A stale SPA or the terminal client sends only ``deliveryId``; the stored text goes."""
    row = _collect(store)
    body = {"deliveryId": row.delivery_id}

    [block] = asyncio.run(_turn(store, DeliveryAttachRequest.model_validate(body)).blocks())

    assert "Strong aims; weak budget." in block and "Section 4 is thin." in block


def test_parts_on_a_plain_card_are_ignored(store):
    plain = Delivery(
        delivery_id="d1", user_id="alice", to_session_id="src", from_session_id="tgt", from_title="Rubric",
        text="Four rows.", text_hash=context_hash("Four rows."), created_at=NOW, updated_at=NOW,
    )
    store.create(plain)

    [block] = asyncio.run(
        _turn(store, DeliveryAttachRequest(delivery_id="d1", parts=[DeliveryPartEdit(step_id="s1", text="x")])).blocks()
    )

    assert block == render_delivery_block("Four rows.", "Rubric")


def test_every_parts_files_are_written_in_one_batch_and_named_in_their_own_block(store, sessions, artifacts, files, monkeypatch):
    writes: List[List[Any]] = []
    monkeypatch.setattr("apis.shared.handoff.delivery_turn.write_reference_rows", lambda rows: writes.append(list(rows)))
    parts = _parts(
        s0=DeliveryPartRequest(step_id="s1", agent_name="G", status="done", from_session_id="t-s1", file_ids=["f-s1-0"]),
        s1=DeliveryPartRequest(
            step_id="s2", agent_name="C", status="done", from_session_id="t-s2", text="see", file_ids=["f-s2-0", "f-s2-1"]
        ),
    )
    row = _collect(store, parts=parts)

    [block] = asyncio.run(_turn(store, DeliveryAttachRequest(delivery_id=row.delivery_id)).blocks())

    assert len(writes) == 1 and len(writes[0]) == 3
    first, second = block.split("\n\n<handoff_context ")[:2]
    assert 'name="f-s1-0.pdf"' in first and "f-s2-0" not in first
    assert 'name="f-s2-0.pdf"' in second and 'name="f-s2-1.pdf"' in second


def test_a_run_results_files_hint_the_tool_gates_before_it_is_attached(store, sessions, artifacts, files):
    parts = _parts(s1=DeliveryPartRequest(
        step_id="s2", agent_name="C", status="done", from_session_id="t-s2", file_ids=["f-s2-0"]
    ))
    row = _collect(store, parts=parts)

    async def hint() -> bool:
        turn = _turn(store, DeliveryAttachRequest(delivery_id=row.delivery_id))
        turn.prefetch()
        return await turn.document_hint()

    assert asyncio.run(hint()) is True
    assert store.get("alice", row.delivery_id).status == "pending"


def test_a_run_result_without_files_flips_no_tool_gate(store, sessions, artifacts, files):
    """Prompt cache: a result with no files changes nothing a gate reads, so ``toolConfig`` holds."""
    row = _collect(store)

    async def hints() -> List[bool]:
        turn = _turn(store, DeliveryAttachRequest(delivery_id=row.delivery_id))
        turn.prefetch()
        from apis.shared.files.file_kinds import KIND_DOCUMENT, KIND_IMAGE, KIND_PRESENTATION, KIND_TABULAR

        return [await turn.kind_hint(k) for k in (KIND_DOCUMENT, KIND_IMAGE, KIND_PRESENTATION, KIND_TABULAR)]

    assert asyncio.run(hints()) == [False, False, False, False]


def test_the_replay_card_shows_the_parts_without_storage_keys(store, sessions, artifacts, files):
    row = _collect(store, parts=_parts(s1=DeliveryPartRequest(
        step_id="s2", agent_name="C", status="done", from_session_id="t-s2", file_ids=["f-s2-0"]
    )))

    view = DeliveryView.from_row(store.get("alice", row.delivery_id)).model_dump(by_alias=True, exclude_none=True)

    assert view["runId"] == "run-1"
    assert [p["status"] for p in view["parts"]] == ["done", "done", "stopped", "running"]
    assert view["parts"][1]["files"][0] == {
        "uploadId": "f-s2-0", "sessionId": "t-s2", "filename": "f-s2-0.pdf", "mimeType": "application/pdf",
        "sizeBytes": 1024,
    }
    assert "textHash" not in view["parts"][0]
    # A plain card's view is unchanged: no parts key at all.
    plain = Delivery(delivery_id="p", user_id="alice", to_session_id="src", from_session_id="tgt", text="x",
                     created_at=NOW, updated_at=NOW)
    assert "parts" not in DeliveryView.from_row(plain).model_dump(by_alias=True, exclude_none=True)


# --- the answer a part carries ------------------------------------------------------------------


def test_a_finished_steps_answer_is_the_one_written_by_when_it_finished(monkeypatch):
    from apis.shared.sessions.models import MessageResponse, MessagesListResponse

    messages = [
        MessageResponse(id="msg-t-1", role="assistant", content=[{"type": "text", "text": "first answer"}],
                        created_at="2026-11-20T10:00:00Z"),
        MessageResponse(id="msg-t-3", role="assistant", content=[{"type": "text", "text": "later work"}],
                        created_at="2026-11-20T11:00:00Z"),
    ]

    async def get_messages(session_id: str, user_id: str, limit=None, next_token=None):
        return MessagesListResponse(messages=messages)

    monkeypatch.setattr("apis.shared.sessions.messages.get_messages", get_messages)

    at_finish = asyncio.run(send_back.answer_message("t", "alice", at_or_before="2026-11-20T10:00:05+00:00"))
    latest = asyncio.run(send_back.answer_message("t", "alice"))

    assert at_finish is not None and at_finish[1] == 1
    assert latest is not None and latest[1] == 3


def test_reference_rows_are_written_in_batches_of_25():
    from apis.shared.files.linked_files import write_reference_rows

    batches: List[int] = []
    repo = SimpleNamespace(batch_put_files_sync=lambda rows: batches.append(len(rows)))

    write_reference_rows([object()] * 60, repository=repo)  # type: ignore[list-item]

    assert batches == [25, 25, 10]


def test_message_times_parse_with_or_without_an_offset():
    assert send_back._parse_time("2026-11-20T10:00:00Z") == datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
    assert send_back._parse_time("2026-11-20T10:00:00") == datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
    assert send_back._parse_time("not a time") is None


# ---------------------------------------------------------------- a run step's title (workflow-runs §19)


def test_a_part_whose_title_already_names_its_agent_is_named_once():
    from apis.shared.handoff.context import run_step_title

    title = run_step_title("Budget reader", "Proposal review")

    assert title == "Budget reader · Proposal review"
    assert delivery_part_label("Budget reader", title) == "Budget reader · Proposal review"
    assert render_delivery_parts([DeliveryPartBlock("Thin.", "Budget reader", title, "done")]) == render_delivery_block(
        "Thin.", "Budget reader · Proposal review"
    )
    # A title that only starts with the name is someone else's: the prefix is still added.
    assert delivery_part_label("Budget reader", "Budget readers · x") == "Budget reader · Budget readers · x"
    assert delivery_part_label("Budget reader", "Budget reader review") == "Budget reader · Budget reader review"
    assert delivery_part_label("", "Proposal review") == "Proposal review"


class TestRunStepTitle:
    def test_the_generated_part_is_shortened_and_the_agent_name_is_never_cut(self):
        from apis.shared.handoff.context import CONVERSATION_TITLE_MAX_CHARS, run_step_title

        title = run_step_title("Budget reader", "A" * 50)

        assert len(title) == CONVERSATION_TITLE_MAX_CHARS
        assert title.startswith("Budget reader · ")
        assert title.endswith("...")

    def test_a_title_that_fits_is_kept_whole(self):
        from apis.shared.handoff.context import run_step_title

        generated = "B" * (50 - len("Budget reader · "))
        assert run_step_title("Budget reader", generated) == f"Budget reader · {generated}"

    def test_a_name_too_long_to_leave_room_keeps_the_generated_title(self):
        from apis.shared.handoff.context import RUN_STEP_TITLE_MIN_GENERATED_CHARS, run_step_title

        fits = "N" * (50 - 3 - RUN_STEP_TITLE_MIN_GENERATED_CHARS)
        assert run_step_title(fits, "Proposal review").startswith(f"{fits} · ")
        assert run_step_title(fits + "N", "Proposal review") == "Proposal review"

    @pytest.mark.parametrize("generated", ["New Conversation", "", "   "])
    def test_the_placeholder_and_an_empty_title_are_never_prefixed(self, generated):
        from apis.shared.handoff.context import run_step_title

        assert run_step_title("Budget reader", generated) == generated

    def test_an_empty_name_or_a_title_that_already_names_the_agent_is_unchanged(self):
        from apis.shared.handoff.context import run_step_title

        assert run_step_title("  ", "Proposal review") == "Proposal review"
        assert run_step_title("Budget reader", "Budget reader · Proposal review") == "Budget reader · Proposal review"
