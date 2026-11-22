"""A workflow run's checklist steps on the Runtime (workflow-designer.md §22.3).

``PromptStepRun`` runs checklist steps on the prompt-step invocation, and every dependency is
injected, so these drive it end to end with no model, no DynamoDB and no S3: the run row, the step
rows (with the claim's kind condition), the field sets, the documents, the checklist call, the
provenance text, the ``WFCHK#`` writer and the meter are fakes that record what they were asked.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from apis.inference_api.chat import workflow_checklist as wc
from apis.inference_api.chat.workflow_prompt import CallResult, ModelPlan, PromptStepRun, prefix_key
from apis.shared.extraction import checklist as cl
from apis.shared.extraction.field_sets import Field, FieldSet
from apis.shared.files.attachment_plan import FileFacts
from apis.shared.files.source_provenance import document_units
from apis.shared.files.whole_document import WholeDocument
from apis.shared.handoff.prompt_steps import WorkflowPromptRequest
from apis.shared.handoff.runs import WorkflowRun, WorkflowRunSource, WorkflowRunStep

pytestmark = pytest.mark.asyncio

MODEL = "us.anthropic.claude-haiku-5-5"
SONNET = "us.anthropic.claude-sonnet-5-5"
PDF = b"%PDF-1.7 fake proposal bytes"
VERDICT = ("pass", "fail", "cant_tell")
PAGES = ["Total request: $598,400.", "A Data Management Plan is included.", "No postdoc mentoring plan."]

SETS = {
    "budget": FieldSet("budget", "Budget", "2", (Field("total_request", "Total request", "currency", "The total requested."),)),
    "plans": FieldSet(
        "plans",
        "Plans",
        "1",
        (
            Field("dmp", "Data management plan", "enum", "Whether a DMP is included.", enum=VERDICT),
            Field("postdoc", "Postdoc mentoring plan", "enum", "Whether a postdoc plan is included.", enum=VERDICT),
        ),
    ),
    "people": FieldSet("people", "People", "1", (Field("pi_name", "PI", "string", "The PI's name."),)),
}
REPLIES = {
    "Budget": [{"name": "total_request", "value": "$598,400", "quote": "Total request: $598,400."}],
    "Plans": [
        {"name": "dmp", "value": "pass", "quote": "A Data Management Plan is included."},
        {"name": "postdoc", "value": "fail", "quote": "No postdoc mentoring plan."},
    ],
    "People": [{"name": "pi_name", "value": None, "quote": ""}],
    "Aims": [
        {"name": "aims", "value": ["Map the watershed", "Model the runoff"], "quote": "Map the watershed"},
        {"name": "partners", "value": None, "quote": ""},
    ],
}


def _checklist(n: int, sets: List[str], *, model: str = MODEL, reads: Optional[List[str]] = None, request: str = "") -> WorkflowRunStep:
    return WorkflowRunStep(
        step_id=f"s{n}",
        handoff_id=f"h{n}",
        kind="checklist",
        agent_id="",
        agent_name="Claude Haiku 5.5",
        step_name=f"audit-{n}",
        request=request,
        model_id=model,
        field_sets=sets,
        reads=reads if reads is not None else ["files"],
    )


def _prompt(n: int, *, model: str = MODEL) -> WorkflowRunStep:
    return WorkflowRunStep(
        step_id=f"s{n}",
        handoff_id=f"h{n}",
        kind="prompt",
        agent_name="Claude Haiku 5.5",
        step_name=f"lens-{n}",
        request=f"Review it through lens {n}.",
        model_id=model,
        reads=["files"],
    )


def _run(steps: List[WorkflowRunStep]) -> WorkflowRun:
    now = datetime.now(timezone.utc).isoformat()
    return WorkflowRun(
        run_id="r1",
        user_id="alice",
        session_id="src",
        status="approved",
        request="Audit this proposal",
        steps=steps,
        file_ids=["f1"],
        created_at=now,
        updated_at=now,
        approved_at=now,
        workflow=WorkflowRunSource(skill_id="skill-1", name="audit"),
    )


class Log:
    """One ordered record of the writes, so "rows before done" is checkable."""

    def __init__(self) -> None:
        self.entries: List[str] = []


class RunStore:
    def __init__(self, run: WorkflowRun) -> None:
        self.run = run

    def get(self, user_id: str, run_id: str) -> Optional[WorkflowRun]:
        return self.run


class StepStore:
    def __init__(self, kinds: Dict[str, str], log: Log) -> None:
        self.rows = {h: {"status": "open", "kind": k, "runId": "r1"} for h, k in kinds.items()}
        self.log = log
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
            self.log.entries.append(f"finish:{handoff_id}")
            return True

    def stop(self, user_id: str, handoff_id: str, reason: str) -> bool:
        with self._lock:
            row = self.rows[handoff_id]
            if row["status"] != "running":
                return False
            row.update(status="stopped", stoppedReason=reason)
            self.log.entries.append(f"stop:{handoff_id}:{reason}")
            return True


class Checklist:
    """The checklist call: answers from ``REPLIES`` by the set's title in the instruction."""

    def __init__(self, *, misses: Optional[Dict[str, int]] = None, delay: float = 0.01, wait_for_cancel: bool = False) -> None:
        self.misses = dict(misses or {})
        self.delay = delay
        self.wait_for_cancel = wait_for_cancel
        self.calls: List[List[Dict[str, Any]]] = []
        self.events: List[str] = []

    async def __call__(self, model: Any, messages: List[Dict[str, Any]], cancel: threading.Event) -> CallResult:
        self.calls.append(messages)
        index = len(self.calls)
        title = messages[0]["content"][-1]["text"].split("Checklist: ", 1)[1].split(" (version", 1)[0]
        self.events.append(f"start:{title}")
        try:
            if self.wait_for_cancel:
                while not cancel.is_set():
                    await asyncio.sleep(0.005)
                return CallResult(stop_reason="cancelled", answered=True)
            await asyncio.sleep(self.delay)
            usage = {
                "inputTokens": 30,
                "outputTokens": 120,
                "totalTokens": 10_230,
                "cacheWriteInputTokens": 10_080 if index == 1 else 0,
                "cacheReadInputTokens": 0 if index == 1 else 10_080,
            }
            if self.misses.get(title, 0) > 0:
                self.misses[title] -= 1
                return CallResult(stop_reason="end_turn", text="Here are my findings.", usage=usage, answered=True,
                                  content=[{"text": "Here are my findings."}])
            tool_use = {"toolUse": {"toolUseId": f"t{index}", "name": "record_checklist", "input": {"fields": REPLIES[title]}}}
            return CallResult(stop_reason="tool_use", usage=usage, answered=True, content=[tool_use])
        finally:
            self.events.append(f"end:{title}")


class Meter:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Optional[str]:
        self.calls.append(kwargs)
        return "hit"


def _doc() -> WholeDocument:
    return WholeDocument(
        upload_id="f1",
        filename="proposal.pdf",
        fmt="pdf",
        pdf_bytes=PDF,
        pages=3,
        facts=FileFacts(filename="proposal.pdf", mime_type="application/pdf", size_bytes=len(PDF), page_count=3, text_tokens=2_000),
    )


def _work(
    run: WorkflowRun,
    steps: StepStore,
    checklist: Checklist,
    log: Log,
    *,
    sets: Optional[Dict[str, FieldSet]] = None,
    meter: Optional[Meter] = None,
    prompt_model: Any = None,
    checklist_enabled: bool = True,
    plan: Optional[ModelPlan] = None,
    rows: Optional[List[Dict[str, Any]]] = None,
    poll_seconds: float = 2.0,
) -> PromptStepRun:
    field_sets = SETS if sets is None else sets

    async def read_document(user_id: str, session_id: str, file_id: str, *, reader: str) -> WholeDocument:
        return _doc()

    async def model_plan(user, model_id: str) -> ModelPlan:
        return plan or ModelPlan(model_id=model_id, document_budget=50_000, pdf_page_limit=100, cache_point=True)

    async def load(user, run_, names):
        return {name: field_sets.get(name) for name in names}

    async def units(user_id, session_id, upload_id):
        return document_units("pdf", PAGES)

    def put(**kwargs: Any) -> bool:
        log.entries.append(f"row:{kwargs['step_id']}:{kwargs['field_set']}:{kwargs['status']}")
        if rows is not None:
            rows.append(kwargs)
        return True

    async def reads(user_id, session_id, run_, step):
        return ""

    async def no_prompt_call(model, messages, cancel):  # pragma: no cover - a prompt step's call
        return CallResult(stop_reason="end_turn", text="Prompt answer", usage={"inputTokens": 1, "outputTokens": 1}, answered=True)

    user = type("U", (), {"user_id": "alice"})()
    return PromptStepRun(
        WorkflowPromptRequest(run_id="r1", step_ids=[s.step_id for s in run.kept_steps()]),
        user,  # type: ignore[arg-type]
        "src",
        runs=RunStore(run),  # type: ignore[arg-type]
        steps=steps,  # type: ignore[arg-type]
        call_model=prompt_model or no_prompt_call,
        build_model=lambda model_id: object(),
        render_reads=reads,
        read_document=read_document,
        model_plan=model_plan,
        quota=lambda user, session_id: _true(),
        meter=meter or Meter(),
        enabled=lambda: True,
        poll_seconds=poll_seconds,
        checklist_enabled=lambda: checklist_enabled,
        call_checklist=checklist,
        build_checklist_model=lambda model_id: object(),
        load_field_sets=load,
        provenance_units=units,
        put_checklist_set=put,
    )


async def _true() -> bool:
    return True


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


def _step_frames(frames: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {f["stepId"]: f for f in frames if f["event"] == "workflow_prompt_step"}


# --- One step, three sets --------------------------------------------------------------------------


async def test_one_call_per_set_the_first_alone_then_the_rest():
    log, model, meter, rows = Log(), Checklist(), Meter(), []
    steps = StepStore({"h1": "checklist"}, log)
    work = _work(_run([_checklist(1, ["budget", "plans", "people"])]), steps, model, log, meter=meter, rows=rows)

    frames = _step_frames(await _drain(work))

    assert frames["s1"]["status"] == "done"
    assert frames["s1"]["kind"] == "checklist"
    assert len(model.calls) == 3
    # The warm call ended before any other call started.
    assert model.events[:2] == ["start:Budget", "end:Budget"]
    assert work.call_order == ["s1:budget", "s1:plans", "s1:people"]
    # Every call sends the same prefix: the document and its cachePoint.
    prefixes = [json.dumps(call[0]["content"][:2], default=str) for call in model.calls]
    assert len(set(prefixes)) == 1
    assert model.calls[0][0]["content"][1] == {"cachePoint": {"type": "default"}}
    # Each set's instruction is its own field list, written as document_extract writes it.
    assert cl.field_list(SETS["plans"]) in model.calls[1][0]["content"][-1]["text"]


async def test_every_sets_row_is_written_before_the_step_is_done():
    log, rows = Log(), []
    steps = StepStore({"h1": "checklist"}, log)
    await _drain(_work(_run([_checklist(1, ["budget", "plans", "people"])]), steps, Checklist(), log, rows=rows))

    assert log.entries[-1] == "finish:h1"
    assert sorted(e for e in log.entries if e.startswith("row:")) == [
        "row:s1:budget:done",
        "row:s1:people:done",
        "row:s1:plans:done",
    ]
    by_set = {row["field_set"]: row for row in rows}
    assert by_set["budget"]["set_index"] == 0 and by_set["people"]["set_index"] == 2
    assert by_set["budget"]["field_set_version"] == "2"
    assert by_set["budget"]["field_set_title"] == "Budget"
    assert by_set["budget"]["model_id"] == MODEL
    assert by_set["budget"]["session_id"] == "src" and by_set["budget"]["run_id"] == "r1"
    budget = by_set["budget"]["fields"][0]
    assert budget["value"] == "$598,400"
    assert budget["state"] == "supported"
    assert budget["quote"] == "Total request: $598,400."
    assert budget["locator"] == {"kind": "page", "value": 1, "precision": "measured"}
    plans = {f["name"]: f for f in by_set["plans"]["fields"]}
    assert plans["dmp"]["value"] == "pass" and plans["dmp"]["state"] == "unassessed"
    assert by_set["people"]["fields"][0]["value"] is None


async def test_the_answer_is_values_and_states_one_table_per_set():
    log = Log()
    steps = StepStore({"h1": "checklist"}, log)
    await _drain(_work(_run([_checklist(1, ["budget", "plans"])]), steps, Checklist(), log))

    text = steps.rows["h1"]["outputText"]
    assert text.index("### Budget") < text.index("### Plans")
    assert "| Total request | $598,400 | supported |" in text
    assert "| Postdoc mentoring plan | fail | unassessed |" in text
    assert "Total request: $598,400." not in text  # quotes are display only
    assert steps.rows["h1"]["outputTokens"] == 240


async def test_each_call_is_metered_as_a_checklist_call_with_its_set():
    log, meter = Log(), Meter()
    steps = StepStore({"h1": "checklist"}, log)
    await _drain(_work(_run([_checklist(1, ["budget", "plans"])]), steps, Checklist(), log, meter=meter))

    assert [m["call_kind"] for m in meter.calls] == ["workflow_checklist", "workflow_checklist"]
    assert [m["attributes"] for m in meter.calls] == [
        {"runId": "r1", "stepId": "s1", "fieldSet": "budget"},
        {"runId": "r1", "stepId": "s1", "fieldSet": "plans"},
    ]
    prints = [m["fingerprint"] for m in meter.calls]
    # Stateless and keyed on the prefix: both sets share one key, so the second reads as a hit.
    assert prints[0]["messagesHash"] == prints[1]["messagesHash"]
    assert prints[0]["extendsMessagesHashes"] == [prints[0]["messagesHash"]]
    assert prints[0]["toolConfigHash"] == prints[1]["toolConfigHash"]
    assert prints[0]["systemPromptHash"] == prints[1]["systemPromptHash"]


def test_a_checklist_key_is_never_a_prompt_steps_key():
    from apis.inference_api.chat.workflow_prompt import document_blocks

    prefix = document_blocks([_doc()])
    checklist_key = prefix_key(MODEL, prefix, system=cl.SYSTEM_PROMPT, tools=wc.TOOLS)

    assert checklist_key != prefix_key(MODEL, prefix)
    assert checklist_key != prefix_key(SONNET, prefix, system=cl.SYSTEM_PROMPT, tools=wc.TOOLS)
    assert checklist_key == prefix_key(MODEL, document_blocks([_doc()]), system=cl.SYSTEM_PROMPT, tools=wc.TOOLS)


# --- Sets that fail on their own -------------------------------------------------------------------


async def test_a_missing_set_is_its_own_error_and_makes_no_call():
    log = Log()
    steps = StepStore({"h1": "checklist"}, log)
    model = Checklist()
    sets = {k: v for k, v in SETS.items() if k != "plans"}
    await _drain(_work(_run([_checklist(1, ["budget", "plans"])]), steps, model, log, sets=sets))

    assert len(model.calls) == 1
    assert "row:s1:plans:error" in log.entries
    assert steps.rows["h1"]["status"] == "done"
    assert "### plans\n\nNot filled in: the field set is no longer in the workflow's skill" in steps.rows["h1"]["outputText"]


async def test_a_set_with_list_fields_is_called_and_its_lists_read_as_items():
    """A ``list`` field runs since 2026-11-22 (§22.3): the reply's array is the value, an absent
    list is null, and both the row and the step's answer show the items."""
    log, rows = Log(), []
    steps = StepStore({"h1": "checklist"}, log)
    model = Checklist()
    aims = FieldSet(
        "aims",
        "Aims",
        "2",
        (Field("aims", "Aims", "list", "The aims."), Field("partners", "Partners", "list", "Partners.", optional=True)),
    )
    await _drain(_work(_run([_checklist(1, ["budget", "aims"])]), steps, model, log, sets={**SETS, "aims": aims}, rows=rows))

    assert len(model.calls) == 2
    done = next(row for row in rows if row["field_set"] == "aims")
    assert done["status"] == "done"
    fields = {f["name"]: f for f in done["fields"]}
    assert fields["aims"]["value"] == "Map the watershed; Model the runoff"
    assert fields["partners"]["value"] is None
    assert steps.rows["h1"]["status"] == "done"
    assert "| Aims | Map the watershed; Model the runoff |" in steps.rows["h1"]["outputText"]


async def test_a_reply_with_no_tool_call_is_retried_once():
    log, meter = Log(), Meter()
    steps = StepStore({"h1": "checklist"}, log)
    model = Checklist(misses={"Plans": 1})
    work = _work(_run([_checklist(1, ["budget", "plans"])]), steps, model, log, meter=meter)
    await _drain(work)

    assert work.call_order == ["s1:budget", "s1:plans", "s1:plans:retry"]
    # The retry is the same request, so it reads the cached prefix like any other set.
    assert json.dumps(model.calls[1], default=str) == json.dumps(model.calls[2], default=str)
    assert steps.rows["h1"]["status"] == "done"
    assert "| Data management plan | pass | unassessed |" in steps.rows["h1"]["outputText"]
    assert [m["attributes"]["fieldSet"] for m in meter.calls] == ["budget", "plans", "plans"]


async def test_a_second_miss_is_that_sets_error_and_the_rest_still_finish():
    log, rows = Log(), []
    steps = StepStore({"h1": "checklist"}, log)
    model = Checklist(misses={"Plans": 2})
    await _drain(_work(_run([_checklist(1, ["budget", "plans"])]), steps, model, log, rows=rows))

    assert len(model.calls) == 3
    errored = next(row for row in rows if row["field_set"] == "plans")
    assert errored["status"] == "error" and errored["error"] == "no_tool_call" and errored["fields"] == []
    assert steps.rows["h1"]["status"] == "done"
    assert "Not filled in: the model did not record an answer, twice." in steps.rows["h1"]["outputText"]


async def test_a_step_whose_every_set_failed_stops_with_error():
    log = Log()
    steps = StepStore({"h1": "checklist"}, log)
    frames = _step_frames(
        await _drain(_work(_run([_checklist(1, ["budget", "plans"])]), steps, Checklist(misses={"Budget": 2, "Plans": 2}), log))
    )

    assert frames["s1"]["status"] == "stopped"
    assert frames["s1"]["stoppedReason"] == "error"
    assert steps.rows["h1"]["status"] == "stopped"
    # Their rows are still there, before the stop, saying why.
    assert log.entries.index("row:s1:budget:error") < log.entries.index("stop:h1:error")


async def test_a_step_whose_sets_are_all_gone_stops_without_a_call():
    log = Log()
    steps = StepStore({"h1": "checklist"}, log)
    model = Checklist()
    await _drain(_work(_run([_checklist(1, ["budget"])]), steps, model, log, sets={}))

    assert model.calls == []
    assert steps.rows["h1"]["stoppedReason"] == "error"


# --- A run with both kinds -------------------------------------------------------------------------


async def test_prompt_and_checklist_steps_on_one_model_are_two_warm_groups():
    log, meter = Log(), Meter()
    prompt_calls: List[str] = []

    async def prompt_model(model, messages, cancel):
        prompt_calls.append(messages[0]["content"][-1]["text"])
        return CallResult(
            stop_reason="end_turn",
            text="Prompt answer",
            usage={"inputTokens": 30, "outputTokens": 50, "totalTokens": 10_160, "cacheWriteInputTokens": 10_080},
            answered=True,
        )

    steps = StepStore({"h1": "prompt", "h2": "checklist"}, log)
    run = _run([_prompt(1), _checklist(2, ["budget", "plans"])])
    frames = _step_frames(await _drain(_work(run, steps, Checklist(), log, meter=meter, prompt_model=prompt_model)))

    assert frames["s1"]["status"] == "done" and frames["s1"]["kind"] == "prompt"
    assert frames["s2"]["status"] == "done" and frames["s2"]["kind"] == "checklist"
    kinds = {m["call_kind"] for m in meter.calls}
    assert kinds == {"workflow_prompt", "workflow_checklist"}
    prompt_print = next(m["fingerprint"] for m in meter.calls if m["call_kind"] == "workflow_prompt")
    checklist_print = next(m["fingerprint"] for m in meter.calls if m["call_kind"] == "workflow_checklist")
    assert prompt_print["messagesHash"] != checklist_print["messagesHash"]
    assert prompt_print["toolConfigHash"] != checklist_print["toolConfigHash"]


async def test_each_model_is_its_own_group_side_by_side():
    log = Log()
    steps = StepStore({"h1": "checklist", "h2": "checklist"}, log)
    model = Checklist(delay=0.03)
    run = _run([_checklist(1, ["budget"]), _checklist(2, ["plans"], model=SONNET)])
    await _drain(_work(run, steps, model, log))

    # Both warm calls were in flight together: neither group waited for the other.
    assert model.events[:2] in (["start:Budget", "start:Plans"], ["start:Plans", "start:Budget"])
    assert {steps.rows["h1"]["status"], steps.rows["h2"]["status"]} == {"done"}


# --- The gates -------------------------------------------------------------------------------------


async def test_a_checklist_step_is_skipped_unclaimed_while_checklist_steps_are_off():
    log = Log()
    steps = StepStore({"h1": "checklist"}, log)
    model = Checklist()
    frames = _step_frames(await _drain(_work(_run([_checklist(1, ["budget"])]), steps, model, log, checklist_enabled=False)))

    assert frames["s1"] == {"event": "workflow_prompt_step", "type": "workflow_prompt_step", "runId": "r1", "stepId": "s1",
                            "status": "skipped", "kind": "checklist", "reason": "not_prompt"}
    assert steps.rows["h1"]["status"] == "open"
    assert model.calls == []


async def test_a_checklist_step_is_never_claimed_as_a_prompt_step():
    """The claim's condition holds the row to its kind."""
    log = Log()
    steps = StepStore({"h1": "prompt"}, log)  # the row says prompt; the run says checklist
    frames = _step_frames(await _drain(_work(_run([_checklist(1, ["budget"])]), steps, Checklist(), log)))

    assert frames["s1"]["status"] == "skipped" and frames["s1"]["reason"] == "not_open"


async def test_a_model_that_cannot_take_documents_stops_the_step():
    log = Log()
    steps = StepStore({"h1": "checklist"}, log)
    plan = ModelPlan(model_id=MODEL, cache_point=True, reads_documents=False)
    await _drain(_work(_run([_checklist(1, ["budget"])]), steps, Checklist(), log, plan=plan))

    assert steps.rows["h1"]["stoppedReason"] == "model_refused"


async def test_documents_over_the_budget_stop_the_step_too_large():
    log = Log()
    steps = StepStore({"h1": "checklist"}, log)
    plan = ModelPlan(model_id=MODEL, cache_point=True, document_budget=10)
    model = Checklist()
    await _drain(_work(_run([_checklist(1, ["budget"])]), steps, model, log, plan=plan))

    assert steps.rows["h1"]["stoppedReason"] == "too_large"
    assert model.calls == []


async def test_a_stopped_run_stops_the_step_user_stopped():
    log = Log()
    steps = StepStore({"h1": "checklist"}, log)
    run = _run([_checklist(1, ["budget", "plans"])])
    model = Checklist(wait_for_cancel=True)
    work = _work(run, steps, model, log, poll_seconds=0.02)

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        run.status = "stopped"

    await asyncio.gather(_drain(work), stop_soon())

    assert steps.rows["h1"]["stoppedReason"] == "user_stopped"
    assert len(model.calls) == 1


async def test_the_step_guidance_and_the_run_request_reach_the_call():
    log = Log()
    steps = StepStore({"h1": "checklist"}, log)
    model = Checklist()
    step = _checklist(1, ["budget"], reads=["request", "files"], request="Use the justification's figures.")
    await _drain(_work(_run([step]), steps, model, log))

    texts = [block.get("text", "") for block in model.calls[0][0]["content"]]
    assert any(t.endswith("Audit this proposal") for t in texts)
    assert f"{cl.GUIDANCE_HEADING}\nUse the justification's figures." in texts[-1]
