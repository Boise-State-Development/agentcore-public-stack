"""A workflow run's prompt steps on the Runtime (workflow-designer.md §21.3).

Every dependency of ``PromptStepRun`` is injected, so these drive the invocation end to end with
no model, no DynamoDB and no S3: the run row, the step rows (with the claim's condition), the
documents, the model call and the meter are fakes that record what they were asked.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

import pytest

from apis.inference_api.chat import workflow_prompt as wp
from apis.inference_api.chat.workflow_prompt import (
    INSTRUCTION_HEADING,
    CallResult,
    ModelPlan,
    PromptStepRun,
    document_blocks,
    step_content,
    stream_workflow_prompt,
)
from apis.shared.files.attachment_plan import FileFacts
from apis.shared.files.whole_document import WholeDocument, WholeDocumentError
from apis.shared.handoff.prompt_steps import WorkflowPromptRequest
from apis.shared.handoff.run_reads import StepReadsNotSettled
from apis.shared.handoff.send_back import DeliveryRefused
from apis.shared.handoff.runs import WorkflowRun, WorkflowRunStep

MODEL = "us.anthropic.claude-haiku-5-5"
PDF = b"%PDF-1.7 fake proposal bytes"


def _step(n: int, *, kind: str = "prompt", reads: Optional[List[str]] = None, model: str = MODEL) -> WorkflowRunStep:
    prompt = kind == "prompt"
    return WorkflowRunStep(
        step_id=f"s{n}",
        handoff_id=f"h{n}",
        agent_id="" if prompt else "agent-1",
        agent_name="Claude Haiku 5.5" if prompt else "Reviewer",
        step_name=f"lens-{n}",
        request=f"Review the proposal through lens {n}.",
        kind=kind,
        model_id=model if prompt else None,
        reads=reads,
    )


def _run(steps: List[WorkflowRunStep], **overrides: Any) -> WorkflowRun:
    now = datetime.now(timezone.utc).isoformat()
    fields: Dict[str, Any] = dict(
        run_id="r1",
        user_id="alice",
        session_id="src",
        status="approved",
        request="Review this proposal",
        steps=steps,
        file_ids=["f1"],
        created_at=now,
        updated_at=now,
        approved_at=now,
    )
    fields.update(overrides)
    return WorkflowRun(**fields)


class RunStore:
    def __init__(self, run: Optional[WorkflowRun]) -> None:
        self.run = run

    def get(self, user_id: str, run_id: str) -> Optional[WorkflowRun]:
        return self.run if self.run is not None and self.run.user_id == user_id else None


class StepStore:
    """The step rows, with the claim's condition applied atomically under a lock."""

    def __init__(self, statuses: Dict[str, str], *, kinds: Optional[Dict[str, str]] = None) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {
            h: {"status": s, "kind": (kinds or {}).get(h, "prompt"), "runId": "r1"} for h, s in statuses.items()
        }
        self._lock = threading.Lock()

    def claim(self, user_id: str, handoff_id: str, *, run_id: str, kind: str = "prompt") -> bool:
        with self._lock:
            row = self.rows.get(handoff_id)
            if not row or row["status"] != "open" or row["kind"] != kind or row["runId"] != run_id:
                return False
            row["status"] = "running"
            return True

    def finish(self, user_id: str, handoff_id: str, *, output_text: str, output_tokens: int) -> bool:
        with self._lock:
            row = self.rows[handoff_id]
            if row["status"] != "running":
                return False
            row.update(status="done", outputText=output_text, outputTokens=output_tokens)
            return True

    def stop(self, user_id: str, handoff_id: str, reason: str) -> bool:
        with self._lock:
            row = self.rows[handoff_id]
            if row["status"] != "running":
                return False
            row.update(status="stopped", stoppedReason=reason)
            return True


class Model:
    """Records each call's messages and the order calls started and ended in."""

    def __init__(self, *, delay: float = 0.01, wait_for_cancel: bool = False, usage: Optional[Dict[str, int]] = None) -> None:
        self.delay = delay
        self.wait_for_cancel = wait_for_cancel
        self.usage = usage
        self.calls: List[List[Dict[str, Any]]] = []
        self.events: List[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, model: Any, messages: List[Dict[str, Any]], cancel: threading.Event) -> CallResult:
        self.calls.append(messages)
        index = len(self.calls)
        self.events.append(f"start{index}")
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.wait_for_cancel:
                while not cancel.is_set():
                    await asyncio.sleep(0.005)
                return CallResult(stop_reason="cancelled", text="partial", answered=True)
            await asyncio.sleep(self.delay)
            first = index == 1
            usage = self.usage or {
                "inputTokens": 40,
                "outputTokens": 300,
                "totalTokens": 10_420,
                "cacheWriteInputTokens": 10_080 if first else 0,
                "cacheReadInputTokens": 0 if first else 10_080,
            }
            return CallResult(stop_reason="end_turn", text=f"Answer {index}", usage=usage, answered=True)
        finally:
            self.in_flight -= 1
            self.events.append(f"end{index}")


def _pdf_doc(pages: int = 4) -> WholeDocument:
    return WholeDocument(
        upload_id="f1",
        filename="proposal.pdf",
        fmt="pdf",
        pdf_bytes=PDF,
        pages=pages,
        facts=FileFacts(filename="proposal.pdf", mime_type="application/pdf", size_bytes=len(PDF), page_count=pages, text_tokens=2_000),
    )


def _plan(**overrides: Any) -> ModelPlan:
    fields: Dict[str, Any] = dict(model_id=MODEL, document_budget=50_000, pdf_page_limit=100, cache_point=True)
    fields.update(overrides)
    return ModelPlan(**fields)


class Meter:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Optional[str]:
        self.calls.append(kwargs)
        return "hit"


def _work(
    run: WorkflowRun,
    steps: StepStore,
    model: Model,
    *,
    step_ids: Optional[List[str]] = None,
    plan: Optional[ModelPlan] = None,
    docs: Optional[List[WholeDocument]] = None,
    meter: Optional[Meter] = None,
    quota: bool = True,
    enabled: bool = True,
    render_reads=None,
    **overrides: Any,
) -> PromptStepRun:
    ids = step_ids or [s.step_id for s in run.kept_steps()]
    documents = docs if docs is not None else [_pdf_doc()]

    async def read_document(user_id: str, session_id: str, file_id: str, *, reader: str) -> WholeDocument:
        return documents[run.file_ids.index(file_id)]

    async def model_plan(user, model_id: str) -> ModelPlan:
        return plan or _plan()

    async def check_quota(user, session_id: str) -> bool:
        return quota

    async def reads(user_id, session_id, run_, step):
        steps_read = [r for r in step.reads or [] if r not in ("request", "files")]
        return "<handoff_context>earlier</handoff_context>" if steps_read else ""


    user = type("U", (), {"user_id": "alice"})()
    return PromptStepRun(
        WorkflowPromptRequest(run_id=run.run_id, step_ids=ids),
        user,  # type: ignore[arg-type]
        "src",
        runs=RunStore(run),  # type: ignore[arg-type]
        steps=steps,  # type: ignore[arg-type]
        call_model=model,
        build_model=lambda model_id: object(),
        render_reads=render_reads or reads,
        read_document=read_document,
        model_plan=model_plan,
        quota=check_quota,
        meter=meter or Meter(),
        enabled=lambda: enabled,
        **overrides,
    )


async def _drain(work: PromptStepRun) -> List[Dict[str, Any]]:
    await work.run()
    frames = []
    while not work.frames.empty():
        frame = work.frames.get_nowait()
        if frame is None:
            break
        head, data = frame.strip().split("\ndata: ", 1)
        frames.append({"event": head.removeprefix("event: "), **json.loads(data)})
    return frames


def _statuses(frames: List[Dict[str, Any]]) -> Dict[str, str]:
    return {f["stepId"]: f["status"] for f in frames if f["event"] == "workflow_prompt_step"}


# --- The request layout --------------------------------------------------------------------------


def test_the_cache_point_follows_the_documents_and_precedes_the_steps_own_content():
    prefix = document_blocks([_pdf_doc()])
    content = step_content(prefix, "<handoff_context>earlier</handoff_context>", "Score it.", cache_point=True)

    assert [next(iter(block)) for block in content] == ["document", "cachePoint", "text", "text"]
    assert content[1] == {"cachePoint": {"type": "default"}}
    assert content[2]["text"].startswith("<handoff_context>")
    assert content[3]["text"] == f"{INSTRUCTION_HEADING}\n\nScore it."


def test_no_cache_point_without_documents_or_without_model_support():
    prefix = document_blocks([_pdf_doc()])

    assert "cachePoint" not in [next(iter(b)) for b in step_content(prefix, "", "Score it.", cache_point=False)]
    assert step_content([], "", "Score it.", cache_point=True) == [{"text": f"{INSTRUCTION_HEADING}\n\nScore it."}]


def test_documents_go_in_file_order_under_fixed_names_and_text_as_text():
    text_doc = WholeDocument(upload_id="f2", filename="notes.docx", fmt="docx", text='<document name="notes.docx" format="docx">\nhi\n</document>')

    blocks = document_blocks([_pdf_doc(), text_doc])

    assert blocks[0]["document"]["name"] == "1 proposal"
    assert blocks[0]["document"]["source"]["bytes"] == PDF
    assert blocks[1] == {"text": text_doc.text}
    assert document_blocks([_pdf_doc(), text_doc]) == blocks


@pytest.mark.asyncio
async def test_every_step_sends_a_byte_identical_prefix():
    model = Model()
    await _drain(_work(_run([_step(1), _step(2), _step(3)]), StepStore({"h1": "open", "h2": "open", "h3": "open"}), model))

    prefixes = [messages[0]["content"][:2] for messages in model.calls]
    assert len(prefixes) == 3
    assert prefixes[0] == prefixes[1] == prefixes[2]
    instructions = [messages[0]["content"][-1]["text"] for messages in model.calls]
    assert len(set(instructions)) == 3


# --- Warm, then fan out --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_step_finishes_alone_before_the_rest_start_together():
    model = Model(delay=0.02)
    run = _run([_step(n) for n in range(1, 6)])
    steps = StepStore({f"h{n}": "open" for n in range(1, 6)})

    frames = await _drain(_work(run, steps, model))

    assert model.events[:2] == ["start1", "end1"]
    assert set(model.events[2:6]) == {"start2", "start3", "start4", "start5"}
    assert model.max_in_flight == 4
    assert _statuses(frames) == {f"s{n}": "done" for n in range(1, 6)}
    assert all(steps.rows[f"h{n}"]["status"] == "done" for n in range(1, 6))
    assert frames[-1] == {"event": "done", "type": "done", "runId": "r1"}


@pytest.mark.asyncio
async def test_the_fan_out_keeps_to_the_runs_concurrency():
    model = Model(delay=0.02)
    run = _run([_step(n) for n in range(1, 6)])
    run.limits.concurrency = 2

    await _drain(_work(run, StepStore({f"h{n}": "open" for n in range(1, 6)}), model))

    assert model.max_in_flight == 2


# --- Claims --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_invocations_for_one_step_run_it_once():
    model = Model(delay=0.02)
    run = _run([_step(1)])
    steps = StepStore({"h1": "open"})

    first, second = await asyncio.gather(_drain(_work(run, steps, model)), _drain(_work(run, steps, model)))

    assert len(model.calls) == 1
    outcomes = sorted([_statuses(first)["s1"], _statuses(second)["s1"]])
    assert outcomes == ["done", "skipped"]
    skipped = next(f for f in first + second if f.get("status") == "skipped")
    assert skipped["reason"] == "not_open"


@pytest.mark.asyncio
async def test_a_settled_step_is_not_run_again():
    model = Model()
    frames = await _drain(_work(_run([_step(1), _step(2)]), StepStore({"h1": "done", "h2": "open"}), model))

    assert _statuses(frames) == {"s1": "skipped", "s2": "done"}
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_agent_steps_unknown_steps_and_unsettled_reads_are_skipped_unclaimed():
    run = _run([_step(1), _step(2, kind="agent"), _step(3, reads=["lens-1"])])
    steps = StepStore({"h1": "open", "h2": "open", "h3": "open"}, kinds={"h2": "mention"})

    async def pending(user_id, session_id, run_, step):
        if step.reads:
            raise StepReadsNotSettled(["lens-1"])
        return ""

    frames = await _drain(_work(run, steps, Model(), step_ids=["s1", "s2", "s3", "nope"], render_reads=pending))

    reasons = {f["stepId"]: f.get("reason") for f in frames if f.get("status") == "skipped"}
    assert reasons == {"s2": "not_prompt", "s3": "reads_pending", "nope": "not_in_run"}
    assert steps.rows["h2"]["status"] == "open" and steps.rows["h3"]["status"] == "open"


@pytest.mark.asyncio
async def test_reads_a_delivery_rule_refuses_leave_the_step_open_and_unclaimed():
    steps = StepStore({"h1": "open"})

    async def refused(user_id, session_id, run_, step):
        raise DeliveryRefused("project_task")

    frames = await _drain(_work(_run([_step(1, reads=["lens-0"])]), steps, Model(), render_reads=refused))

    assert next(f for f in frames if f["event"] == "workflow_prompt_step")["reason"] == "reads_refused"
    assert steps.rows["h1"]["status"] == "open"


@pytest.mark.asyncio
async def test_the_default_renderer_is_the_shared_one_prepare_uses():
    run = _run([_step(1, reads=["lens-0"])])
    with patch.object(wp, "render_step_reads", AsyncMock(return_value=("<block/>", []))) as shared:
        block = await wp.render_reads("alice", "src", run, run.steps[0])

    assert block == "<block/>"
    shared.assert_awaited_once_with("alice", "src", run, run.steps[0])


@pytest.mark.asyncio
async def test_a_step_that_reads_others_carries_the_rendered_results_after_the_cache_point():
    model = Model()
    await _drain(_work(_run([_step(1, reads=["files", "lens-0"])]), StepStore({"h1": "open"}), model))

    content = model.calls[0][0]["content"]
    assert [next(iter(b)) for b in content] == ["document", "cachePoint", "text", "text"]
    assert content[2] == {"text": "<handoff_context>earlier</handoff_context>"}


# --- Refusals ------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_overrides, enabled, reason",
    [({}, False, "flag_off"), ({"status": "open"}, True, "not_approved"), ({"session_id": "other"}, True, "not_found")],
)
async def test_a_whole_request_refusal_claims_nothing(run_overrides, enabled, reason):
    steps = StepStore({"h1": "open"})
    model = Model()

    frames = await _drain(_work(_run([_step(1)], **run_overrides), steps, model, enabled=enabled))

    assert [f["event"] for f in frames] == ["workflow_prompt_refused", "done"]
    assert frames[0]["reason"] == reason
    assert steps.rows["h1"]["status"] == "open"
    assert model.calls == []


@pytest.mark.asyncio
async def test_a_refused_model_stops_its_steps_without_a_call():
    steps = StepStore({"h1": "open", "h2": "open"})
    model = Model()

    frames = await _drain(_work(_run([_step(1), _step(2)]), steps, model, plan=_plan(refusal="model_refused")))

    assert model.calls == []
    assert {f["stepId"]: f.get("stoppedReason") for f in frames if f.get("status") == "stopped"} == {
        "s1": "model_refused",
        "s2": "model_refused",
    }
    assert steps.rows["h1"]["stoppedReason"] == "model_refused"


@pytest.mark.asyncio
async def test_quota_stops_every_claimed_step():
    steps = StepStore({"h1": "open"})
    frames = await _drain(_work(_run([_step(1)]), steps, Model(), quota=False))

    assert steps.rows["h1"] == {**steps.rows["h1"], "status": "stopped", "stoppedReason": "quota"}
    assert _statuses(frames) == {"s1": "stopped"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan, docs",
    [
        (_plan(document_budget=1_000), None),
        (_plan(pdf_page_limit=3), [_pdf_doc(pages=4)]),
    ],
    ids=["over-the-document-budget", "over-the-pdf-page-limit"],
)
async def test_too_large_stops_the_step_and_never_truncates(plan, docs):
    steps = StepStore({"h1": "open"})
    model = Model()

    frames = await _drain(_work(_run([_step(1)]), steps, model, plan=plan, docs=docs))

    assert model.calls == []
    assert steps.rows["h1"]["stoppedReason"] == "too_large"
    assert next(f for f in frames if f["event"] == "workflow_prompt_step")["stoppedReason"] == "too_large"


@pytest.mark.asyncio
async def test_a_document_too_large_to_read_whole_stops_the_steps_too_large():
    steps = StepStore({"h1": "open"})

    async def read_document(*args, **kwargs):
        raise WholeDocumentError("too_large", "too big")

    work = _work(_run([_step(1)]), steps, Model())
    work._read_document = read_document
    await _drain(work)

    assert steps.rows["h1"]["stoppedReason"] == "too_large"


@pytest.mark.asyncio
async def test_an_uncataloged_model_is_refused_as_the_caller():
    from apis.shared.models.retirement import EffectiveModel

    effective = EffectiveModel(requested_id=MODEL, model_id=MODEL, record=None)
    access = AsyncMock(return_value=True)
    with patch("apis.shared.models.retirement.resolve_effective_model", AsyncMock(return_value=effective)), patch(
        "apis.shared.rbac.service.get_app_role_service"
    ) as service:
        service.return_value.can_access_model = access
        plan = await wp.resolve_model_plan(object(), MODEL)  # type: ignore[arg-type]

    assert plan.refusal == "model_refused"
    access.assert_not_called()


@pytest.mark.asyncio
async def test_a_model_the_caller_cannot_use_is_refused():
    from apis.shared.models.models import ManagedModel
    from apis.shared.models.retirement import EffectiveModel

    record = ManagedModel.model_validate(
        {
            "id": "m1",
            "modelId": MODEL,
            "modelName": "Claude Haiku 5.5",
            "provider": "bedrock",
            "providerName": "Anthropic",
            "inputModalities": ["TEXT", "IMAGE"],
            "outputModalities": ["TEXT"],
            "maxInputTokens": 200_000,
            "enabled": True,
            "inputPricePerMillionTokens": 1.0,
            "outputPricePerMillionTokens": 5.0,
            "createdAt": "2026-11-21T00:00:00Z",
            "updatedAt": "2026-11-21T00:00:00Z",
        }
    )
    effective = EffectiveModel(requested_id=MODEL, model_id=MODEL, record=record)
    for allowed, refusal in ((False, "model_refused"), (True, None)):
        with patch("apis.shared.models.retirement.resolve_effective_model", AsyncMock(return_value=effective)), patch(
            "apis.shared.rbac.service.get_app_role_service"
        ) as service:
            service.return_value.can_access_model = AsyncMock(return_value=allowed)
            plan = await wp.resolve_model_plan(object(), MODEL)  # type: ignore[arg-type]
        assert plan.refusal == refusal
    assert plan.cache_point is True and plan.document_budget and plan.pdf_page_limit


@pytest.mark.asyncio
async def test_a_text_only_model_runs_a_step_that_reads_only_steps_and_refuses_one_that_reads_files():
    steps = StepStore({"h1": "open", "h2": "open"})
    model = Model()
    run = _run([_step(1, reads=["files"]), _step(2, reads=["request", "lens-1"])])

    frames = await _drain(_work(run, steps, model, plan=_plan(reads_documents=False, cache_point=False)))

    assert steps.rows["h1"]["stoppedReason"] == "model_refused"
    assert steps.rows["h2"]["status"] == "done"
    assert len(model.calls) == 1 and "document" not in model.calls[0][0]["content"][0]
    assert _statuses(frames) == {"s1": "stopped", "s2": "done"}


# --- Stop and timeout ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stop_mid_run_cancels_the_calls_in_flight_and_stops_every_claimed_step():
    run = _run([_step(1), _step(2), _step(3)])
    steps = StepStore({"h1": "open", "h2": "open", "h3": "open"})
    model = Model(wait_for_cancel=True)
    meter = Meter()
    work = _work(run, steps, model, meter=meter, poll_seconds=0.01)

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        run.status = "stopped"

    frames, _ = await asyncio.gather(_drain(work), stop_soon())

    assert {f["stepId"]: f.get("stoppedReason") for f in frames if f["event"] == "workflow_prompt_step"} == {
        "s1": "user_stopped",
        "s2": "user_stopped",
        "s3": "user_stopped",
    }
    assert len(model.calls) == 1, "the steps after the warm call never start once the run stopped"
    assert all(row["status"] == "stopped" for row in steps.rows.values())
    # The cancelled warm call had been answered, so it is metered from an estimate.
    assert len(meter.calls) == 1 and meter.calls[0]["usage_estimate"]["reason"] == "interrupted"


@pytest.mark.asyncio
async def test_a_step_past_its_timeout_stops_timeout():
    steps = StepStore({"h1": "open"})
    work = _work(_run([_step(1)]), steps, Model(wait_for_cancel=True), step_timeout=0.05)

    await _drain(work)

    assert steps.rows["h1"]["stoppedReason"] == "timeout"


@pytest.mark.asyncio
async def test_a_closed_stream_does_not_cancel_the_work():
    run = _run([_step(1), _step(2)])
    steps = StepStore({"h1": "open", "h2": "open"})
    model = Model(delay=0.03)
    work_kwargs = dict(
        runs=RunStore(run),
        steps=steps,
        call_model=model,
        build_model=lambda model_id: object(),
        render_reads=AsyncMock(return_value=""),
        read_document=AsyncMock(return_value=_pdf_doc()),
        model_plan=AsyncMock(return_value=_plan()),
        quota=AsyncMock(return_value=True),
        meter=Meter(),
        enabled=lambda: True,
    )
    user = type("U", (), {"user_id": "alice"})()
    stream = stream_workflow_prompt(WorkflowPromptRequest(run_id="r1", step_ids=["s1", "s2"]), user, "src", **work_kwargs)

    from apis.inference_api.runtime_health import tracker

    baseline = tracker.in_flight
    first = await stream.__anext__()
    await stream.aclose()
    assert tracker.in_flight == baseline + 1, "the container reports busy while the work runs on"
    for _ in range(100):
        if all(row["status"] == "done" for row in steps.rows.values()) and not wp._BACKGROUND:
            break
        await asyncio.sleep(0.01)

    assert "workflow_prompt_step" in first
    assert all(row["status"] == "done" for row in steps.rows.values())
    assert tracker.in_flight == baseline


@pytest.mark.asyncio
async def test_a_failed_invocation_never_leaves_a_claimed_step_running():
    steps = StepStore({"h1": "open"})

    async def quota(user, session_id):
        raise RuntimeError("boom")

    work = _work(_run([_step(1)]), steps, Model())
    work._quota = quota
    frames = await _drain(work)

    assert steps.rows["h1"]["stoppedReason"] == "error"
    assert frames[-1]["event"] == "done"


# --- Metering ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_call_is_metered_with_its_run_and_step():
    meter = Meter()
    await _drain(_work(_run([_step(1), _step(2)]), StepStore({"h1": "open", "h2": "open"}), Model(), meter=meter))

    assert [c["attributes"] for c in meter.calls] == [{"runId": "r1", "stepId": "s1"}, {"runId": "r1", "stepId": "s2"}]
    assert all(c["session_id"] == "src" and c["model_id"] == MODEL for c in meter.calls)
    assert meter.calls[0]["usage"]["cacheWriteInputTokens"] == 10_080
    assert meter.calls[1]["usage"]["cacheReadInputTokens"] == 10_080


@pytest.mark.asyncio
async def test_the_cost_row_is_a_workflow_prompt_call_with_no_message_id():
    stored: Dict[str, Any] = {}

    async def store(**kwargs):
        stored.update(kwargs)
        return {"cacheStatus": "hit"}

    snapshot = {"snapshotAt": "2026-11-21T00:00:00Z", "inputPricePerMtok": 1.0, "outputPricePerMtok": 5.0, "cacheWritePricePerMtok": 1.25, "cacheReadPricePerMtok": 0.1}
    with patch("apis.shared.sessions.metadata.store_message_metadata", store), patch(
        "apis.shared.costs.pricing_config.create_pricing_snapshot", AsyncMock(return_value=snapshot)
    ):
        status = await wp.meter_call(
            user_id="alice",
            session_id="src",
            model_id=MODEL,
            usage={"inputTokens": 40, "outputTokens": 300, "totalTokens": 10_420, "cacheReadInputTokens": 10_080},
            metrics={"latencyMs": 900},
            started=time.time(),
            ended=time.time(),
            fingerprint={"toolConfigHash": "t", "systemPromptHash": "s", "historyHash": "h", "messageCount": 1},
            attributes={"runId": "r1", "stepId": "s2"},
        )

    assert status == "hit"
    assert stored["message_id"] is None
    row = stored["message_metadata"].model_dump(by_alias=True, exclude_none=True)
    assert row["callKind"] == "workflow_prompt"
    assert (row["runId"], row["stepId"]) == ("r1", "s2")
    assert row["cost"]["total"] > 0
    assert row["tokenUsage"]["cacheReadInputTokens"] == 10_080
    assert "prefixFingerprints" in row


def test_a_cancelled_calls_estimate_reads_the_warm_prefix():
    usage, estimate = wp.estimate_cancelled_usage(
        model_id=MODEL, prompt_tokens=10_300, cached_prefix=10_080, cache_point=True, partial_text="x" * 400
    )

    assert usage["cacheReadInputTokens"] == 10_080
    assert usage["outputTokens"] == 100
    assert estimate["reason"] == "interrupted"


# --- Several models -------------------------------------------------------------------------------

SONNET = "us.anthropic.claude-sonnet-5-5"


class TaggedModel:
    """Records each call as ``(model, step)`` start and end events, and the calls in flight."""

    def __init__(self, delay: float = 0.03) -> None:
        self.delay = delay
        self.events: List[tuple] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, model: Any, messages: List[Dict[str, Any]], cancel: threading.Event) -> CallResult:
        step = messages[0]["content"][-1]["text"].rsplit("lens ", 1)[1].rstrip(".")
        self.events.append(("start", model, step))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            return CallResult(
                stop_reason="end_turn",
                text=f"Answer {step}",
                usage={"inputTokens": 40, "outputTokens": 300, "totalTokens": 340},
                answered=True,
            )
        finally:
            self.in_flight -= 1
            self.events.append(("end", model, step))


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [2, 4])
async def test_model_groups_warm_side_by_side_under_one_shared_limit(limit):
    """Two models' groups share no cache, so their warm calls overlap; every call still takes a slot
    from one semaphore, so the run's concurrency bounds the total, not each group's."""
    steps_list = [_step(1), _step(2), _step(3), _step(4), _step(5, model=SONNET), _step(6, model=SONNET), _step(7, model=SONNET), _step(8, model=SONNET)]
    run = _run(steps_list)
    run.limits.concurrency = limit
    store = StepStore({f"h{n}": "open" for n in range(1, 9)})
    model = TaggedModel()
    plans = {MODEL: _plan(), SONNET: _plan(model_id=SONNET)}

    async def model_plan(user, model_id: str) -> ModelPlan:
        return plans[model_id]

    work = _work(run, store, model)
    work._model_plan = model_plan
    work._build_model = lambda model_id: model_id
    frames = await _drain(work)

    assert _statuses(frames) == {f"s{n}": "done" for n in range(1, 9)}
    assert model.max_in_flight <= limit
    # Both groups' warm calls (steps 1 and 5) start before either ends.
    first_end = next(i for i, e in enumerate(model.events) if e[0] == "end")
    started_before = {e[2] for e in model.events[:first_end] if e[0] == "start"}
    assert {"1", "5"} <= started_before
    # Within each group, nothing fans out until its own warm call has ended.
    for warm_step, group in (("1", {"2", "3", "4"}), ("5", {"6", "7", "8"})):
        warm_end = model.events.index(("end", MODEL if warm_step == "1" else SONNET, warm_step))
        assert not any(e[0] == "start" and e[2] in group for e in model.events[:warm_end])


# --- What a step reads of the run's inputs -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_step_with_its_own_instruction_that_reads_the_request_is_sent_the_users_request():
    model = Model()
    step = _step(1, reads=["request", "files"])
    run = _run([step], request="Focus on the budget")

    await _drain(_work(run, StepStore({"h1": "open"}), model))

    content = model.calls[0][0]["content"]
    assert [next(iter(b)) for b in content] == ["document", "cachePoint", "text", "text"]
    assert content[2] == {"text": f"{wp.RUN_REQUEST_HEADING}\n\nFocus on the budget"}
    assert content[3]["text"].endswith(step.request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reads, run_request",
    [(["files"], "Focus on the budget"), (["request", "files"], "Review the proposal through lens 1.")],
    ids=["does-not-read-the-request", "its-instruction-is-the-request"],
)
async def test_the_users_request_is_sent_once_and_only_to_a_step_that_reads_it(reads, run_request):
    model = Model()
    await _drain(_work(_run([_step(1, reads=reads)], request=run_request), StepStore({"h1": "open"}), model))

    texts = [b["text"] for b in model.calls[0][0]["content"] if "text" in b]
    assert not any(t.startswith(wp.RUN_REQUEST_HEADING) for t in texts)


@pytest.mark.asyncio
async def test_a_step_that_does_not_read_files_gets_no_documents_and_no_cache_point():
    model = Model()
    reads: List[str] = []

    async def read_document(*args, **kwargs):
        reads.append(args[2])
        return _pdf_doc()

    work = _work(_run([_step(1, reads=["request"])]), StepStore({"h1": "open"}), model)
    work._read_document = read_document
    await _drain(work)

    content = model.calls[0][0]["content"]
    assert [next(iter(b)) for b in content] == ["text", "text"]
    assert reads == [], "no step reads the files, so none is read"


@pytest.mark.asyncio
async def test_the_warm_call_is_a_step_that_sends_the_documents():
    model = Model(delay=0.01)
    run = _run([_step(1, reads=["request"]), _step(2, reads=["files"]), _step(3, reads=["files"])])

    await _drain(_work(run, StepStore({"h1": "open", "h2": "open", "h3": "open"}), model))

    first = model.calls[0][0]["content"]
    assert next(iter(first[0])) == "document"
    assert model.events[:2] == ["start1", "end1"]


def test_the_run_request_follows_the_steps_own_reads_input():
    run = _run([_step(1)], request="Focus on the budget")

    assert wp.run_request_for(run, _step(1)) == "Focus on the budget", "a row with no reads reads both"
    assert wp.run_request_for(run, _step(1, reads=["lens-0"])) == ""
    assert wp.run_request_for(run, _step(1, reads=["request", "lens-0"])) == "Focus on the budget"
    assert wp.run_request_for(run, _step(1, reads=[])) == ""


@pytest.mark.asyncio
async def test_too_large_stops_only_the_steps_that_send_the_files():
    """A step that reads only earlier steps sends none of the documents, so it still runs."""
    steps = StepStore({"h1": "open", "h2": "open"})
    model = Model()
    run = _run([_step(1, reads=["files"]), _step(2, reads=["request", "lens-1"])])

    frames = await _drain(_work(run, steps, model, plan=_plan(document_budget=1_000)))

    assert steps.rows["h1"]["stoppedReason"] == "too_large"
    assert steps.rows["h2"]["status"] == "done"
    assert len(model.calls) == 1
    assert all("document" not in block for block in model.calls[0][0]["content"])
    assert _statuses(frames) == {"s1": "stopped", "s2": "done"}


# --- The output limit ----------------------------------------------------------------------------


class CutOffModel(Model):
    """A model that reaches the output limit mid-sentence on the step it is told to."""

    def __init__(self, cut: int) -> None:
        super().__init__()
        self.cut = cut

    async def __call__(self, model, messages, cancel):
        result = await super().__call__(model, messages, cancel)
        if len(self.calls) == self.cut:
            result.stop_reason = "max_tokens"
            result.text = "**Weaknesses**\n- The budget table and the justification disagree on the gra"
            result.usage = {**result.usage, "outputTokens": wp.MAX_OUTPUT_TOKENS}
        return result


@pytest.mark.asyncio
async def test_an_answer_cut_off_at_the_output_limit_is_kept_marked_and_recorded():
    from apis.shared.handoff.prompt_steps import CUT_OFF_MARKER

    steps = StepStore({"h1": "open", "h2": "open"})
    meter = Meter()

    frames = await _drain(_work(_run([_step(1), _step(2)]), steps, CutOffModel(cut=2), meter=meter))

    assert _statuses(frames) == {"s1": "done", "s2": "done"}
    assert steps.rows["h2"]["outputText"].endswith(f"on the gra\n\n{CUT_OFF_MARKER}")
    assert not steps.rows["h1"]["outputText"].endswith(CUT_OFF_MARKER)
    assert [c["attributes"].get("truncated") for c in meter.calls] == [None, True]


def test_the_marker_survives_the_stored_answers_cap():
    from apis.shared.handoff.delivery import SEND_BACK_MAX_CHARS, cap_delivery_text
    from apis.shared.handoff.prompt_steps import CUT_OFF_MARKER, mark_cut_off

    marked = mark_cut_off("A finding. " * 5_000)

    assert len(marked) <= SEND_BACK_MAX_CHARS
    assert marked.endswith(CUT_OFF_MARKER)
    assert cap_delivery_text(marked) == marked


def test_the_step_timeout_fits_the_cap_at_the_slowest_measured_rate():
    """16,000 tokens at Sonnet 5.5's slowest measured rate on dev (116 tok/s), with margin; and a
    run's longest shape (warm call, then two waves) inside the Runtime stream's 600 s."""
    assert wp.MAX_OUTPUT_TOKENS == 16_000
    assert wp.STEP_TIMEOUT_SECONDS >= wp.MAX_OUTPUT_TOKENS / 116
    assert 3 * wp.STEP_TIMEOUT_SECONDS <= 600
