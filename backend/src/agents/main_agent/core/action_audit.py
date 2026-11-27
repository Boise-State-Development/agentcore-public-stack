"""The action audit: what an unattended run did, recorded before it did it.

docs/specs/proactive-trust.md §7. Every tool call an unattended run makes (a scheduled
run, a Run now, an inbox resume, or a resume of any of them answered in its
conversation) falls into one of three classes (:func:`classify_call`):

* ``read_only``: known to only read (``runs_in_parallel``, the definition triage's
  probe uses). Not audited.
* ``write``: positive evidence it writes: MCP ``destructiveHint``, the catalog's
  ``needs_approval``, the skill-authoring writes, or the catalog's ``writes`` marker
  on an MCP tool entry. Audited, keeps a watch run from being quiet (§7.3), and on a
  watch run asks first unless the schedule lists it in ``actWithoutAsking`` (§7.5).
* ``unclassified``: everything else. Audited, marked ``unclassified``, and changes no
  outcome, since most un-annotated tools are reads.

``report``, ``ask_user_question`` and ``request_user_login`` act on nothing outside the
run and are never audited.

:class:`HeadlessActionAudit` is an ``ExecuteToolStage`` middleware, not a
``BeforeToolCallEvent`` hook: Strands runs every ``Before`` callback even after one
raises an interrupt, so a hook would write a row on the pass that pauses and again on
the resume. A middleware runs only when the call really executes, after every gate.
It sits inside ``ToolConcurrencyPlugin`` (the outermost layer) and outside MCP
elicitation's pause-and-replay, so a form's replay of one call meets the same row.

It is on every agent and **inert** unless the invocation's state carries the run
(:data:`RUN_STATE_KEY`, set per request by the chat route from the harness's
``run_id`` / ``run_trigger`` or a paused run's snapshot). It never stores a run on the
agent, so a cached agent can't carry one run's id into another, and it is not an
agent-cache key element. An interactive turn pays one dictionary lookup per call.

For an audited call it writes the row off the event loop and **fails closed**: if
the write fails, the tool is not called and the model gets an error it can report.
The outcome is written after the call, best-effort.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, Optional, Set, Tuple

from strands.interrupt import InterruptException
from strands.middleware import ExecuteToolContext, ExecuteToolStage
from strands.plugins import Plugin
from strands.types.events import ToolResultEvent

from agents.main_agent.core.tool_concurrency import runs_in_parallel

logger = logging.getLogger(__name__)

#: The ``invocation_state`` key the chat route puts the run under. Per request, never on the agent.
RUN_STATE_KEY = "headless_action_run"

#: Injected tools that act on nothing outside the run (§7.1).
EXCLUDED_TOOLS: FrozenSet[str] = frozenset({"report", "ask_user_question", "request_user_login"})

#: What the model reads when a call couldn't be recorded (§7.2).
REFUSED_TEXT = "This action wasn't run because it couldn't be recorded."

#: The triggers whose runs are unattended (the harness sends one as ``run_trigger``).
UNATTENDED_TRIGGERS: FrozenSet[str] = frozenset({"schedule", "run_now", "inbox_respond"})

WRITE = "write"
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class CallClass:
    """An audited call's class and the evidence for it."""

    call_class: str  # "write" | "unclassified"
    why: str  # destructive_hint | needs_approval | catalog_writes | skill_write | unannotated


@dataclass
class ToolFacts:
    """What the agent build knows about its tools, read at call time with no I/O.

    Each lookup takes the Strands ``selected_tool``. ``approval_names`` is the set the
    approval gate pauses on (``needs_approval`` and the skill-authoring writes);
    ``write_names`` the catalog's ``writes`` marker; ``tool_id`` the catalog id behind a
    tool. Every lookup defaults to nothing, so a bare agent classifies by name and MCP
    annotations alone.
    """

    approval_names: Callable[[Any], Set[str]] = lambda tool: set()
    write_names: Callable[[Any], Set[str]] = lambda tool: set()
    tool_id: Callable[[Any], Optional[str]] = lambda tool: None
    skill_write_names: FrozenSet[str] = frozenset()


def _mcp_annotations(tool: Any) -> Any:
    mcp_tool = getattr(tool, "mcp_tool", None)
    return getattr(mcp_tool, "annotations", None) if mcp_tool is not None else None


def _safe(lookup: Callable[[Any], Any], tool: Any, default: Any) -> Any:
    try:
        value = lookup(tool)
    except Exception:  # noqa: BLE001 - a lookup must never break classification
        logger.debug("Tool fact lookup failed", exc_info=True)
        return default
    return value if value is not None else default


def classify_call(tool: Any, tool_name: str, facts: Optional[ToolFacts] = None) -> Optional[CallClass]:
    """The call's class, or ``None`` for one that is not audited (read-only or excluded).

    Positive evidence only makes a ``write``; anything else not known to be read-only is
    ``unclassified``. No I/O.
    """
    if not tool_name or tool_name in EXCLUDED_TOOLS:
        return None
    if runs_in_parallel(tool, tool_name):
        return None
    facts = facts or ToolFacts()
    if tool_name in facts.skill_write_names:
        return CallClass(WRITE, "skill_write")
    if tool_name in _safe(facts.approval_names, tool, set()):
        return CallClass(WRITE, "needs_approval")
    annotations = _mcp_annotations(tool)
    if annotations is not None and getattr(annotations, "destructiveHint", None) is True:
        return CallClass(WRITE, "destructive_hint")
    if tool_name in _safe(facts.write_names, tool, set()):
        return CallClass(WRITE, "catalog_writes")
    return CallClass(UNCLASSIFIED, "unannotated")


def server_name_of(tool: Any) -> Optional[str]:
    """The MCP server's display name behind a tool, or ``None`` for a built-in."""
    client = getattr(tool, "mcp_client", None)
    name = getattr(client, "context_label", None) if client is not None else None
    return str(name) if name else None


def approved_tool_use_ids(interrupt_responses: Optional[Iterable[Any]]) -> FrozenSet[str]:
    """The calls whose approval gate this invocation answers ``approved``.

    A ``BeforeToolCallEvent`` interrupt id is ``v1:before_tool_call:{toolUseId}:{uuid}``
    (Strands' scheme), so the call is in the id. A declined call never executes and is
    never asked about. Accepts request entries (``interruptId`` / ``response``) as
    models or dicts.
    """
    approved: Set[str] = set()
    for entry in interrupt_responses or []:
        interrupt_id = getattr(entry, "interruptId", None)
        response = getattr(entry, "response", None)
        if isinstance(entry, dict):
            interrupt_id = entry.get("interruptId", interrupt_id)
            response = entry.get("response", response)
        if response != "approved" or not isinstance(interrupt_id, str):
            continue
        parts = interrupt_id.split(":")
        if len(parts) >= 4 and parts[0] == "v1" and parts[1] == "before_tool_call" and parts[2]:
            approved.add(parts[2])
    return frozenset(approved)


@dataclass
class UnattendedRun:
    """The run an invocation belongs to, carried in its ``invocation_state`` (never on an agent).

    ``run_id`` and ``trigger`` are the run's own: on a resume, the paused run's, from its
    snapshot, so an approval answered in the conversation lands on the same rows.
    ``approved_via`` is how this invocation answers its approvals: ``inbox`` for the
    worker's resume, ``conversation`` for one sent from the conversation, ``None`` on a
    run's first leg. ``audited`` collects each audited call's class for the stream's
    ``tool_result`` frames and the harness's write count. An object, not a dict, so a
    copy of ``invocation_state`` still shares it.
    """

    run_id: str
    user_id: str
    session_id: str
    trigger: str
    schedule_id: Optional[str] = None
    approved: FrozenSet[str] = frozenset()
    approved_via: Optional[str] = None
    approving_run_id: Optional[str] = None
    #: A watch run's ``actWithoutAsking``, carried so a pause snapshots it (§7.5).
    act_without_asking: Tuple[str, ...] = ()
    audited: Dict[str, str] = field(default_factory=dict)

    def approval_for(self, tool_use_id: str) -> Tuple[str, Optional[str], Optional[str]]:
        """``(approval, approvedVia, approvingRunId)`` for one call."""
        if tool_use_id in self.approved:
            return "approved", self.approved_via, self.approving_run_id
        return "not_required", None, None


def run_from_state(invocation_state: Any) -> Optional[UnattendedRun]:
    if not isinstance(invocation_state, dict):
        return None
    run = invocation_state.get(RUN_STATE_KEY)
    return run if isinstance(run, UnattendedRun) else None


def audited_class(invocation_state: Any, tool_use_id: str) -> Optional[str]:
    """The class an audited call was recorded as, for its ``tool_result`` frame, or ``None``."""
    run = run_from_state(invocation_state)
    return run.audited.get(tool_use_id) if run is not None and tool_use_id else None


def _refused(context: ExecuteToolContext) -> ToolResultEvent:
    result = {
        "toolUseId": str(context.tool_use.get("toolUseId", "")),
        "status": "error",
        "content": [{"text": REFUSED_TEXT}],
    }
    return ToolResultEvent(result, exception=Exception("action audit write failed"))


def _recorder() -> Any:
    from apis.shared.harness.governance import RunAuditRecorder

    return RunAuditRecorder()


class HeadlessActionAudit(Plugin):
    """Registers :meth:`middleware` on an agent. ``facts`` is read per call, never cached per run."""

    name = "headless-action-audit"

    def __init__(self, facts: Optional[Callable[[], ToolFacts]] = None, recorder: Optional[Callable[[], Any]] = None):
        super().__init__()
        self._facts = facts
        self._recorder = recorder or _recorder

    def init_agent(self, agent: Any) -> None:
        agent.add_middleware(ExecuteToolStage, self.middleware)

    async def middleware(
        self,
        context: ExecuteToolContext,
        next_fn: Callable[[ExecuteToolContext], AsyncIterator[Any]],
    ) -> AsyncIterator[Any]:
        run = run_from_state(context.invocation_state)
        tool_name = str(context.tool_use.get("name", ""))
        verdict = classify_call(context.tool, tool_name, self._facts() if self._facts else None) if run else None
        if run is None or verdict is None:
            async for event in next_fn(context):
                yield event
            return

        tool_use_id = str(context.tool_use.get("toolUseId", ""))
        approval, via, approving = run.approval_for(tool_use_id)
        recorder = self._recorder()
        tool = context.tool
        started = time.perf_counter()
        try:
            written = await asyncio.to_thread(
                recorder.record_action,
                user_id=run.user_id,
                run_id=run.run_id,
                session_id=run.session_id,
                tool_use_id=tool_use_id,
                trigger=run.trigger,
                tool_name=tool_name,
                call_class=verdict.call_class,
                why=verdict.why,
                approval=approval,
                tool_input=context.tool_use.get("input"),
                schedule_id=run.schedule_id,
                tool_id=(self._facts().tool_id(tool) if self._facts else None) or None,
                server_name=server_name_of(tool),
                approved_via=via,
                approving_run_id=approving,
            )
        except Exception:
            # No record, no action (F6a's rule for runs, applied to calls).
            logger.warning("Action audit: could not record %s on run %s; not running it", tool_name, run.run_id, exc_info=True)
            yield _refused(context)
            return
        run.audited[tool_use_id] = verdict.call_class
        # Ids, classes and a duration only. The write's time is what a resume answered in
        # its conversation waits before its first token (§12 Q14), so it is logged.
        logger.info(
            "Action audit: %s %s (%s, %s) on run %s in %d ms",
            written, tool_name, verdict.call_class, approval, run.run_id,
            int((time.perf_counter() - started) * 1000),
        )

        outcome: Optional[str] = None
        try:
            async for event in next_fn(context):
                if isinstance(event, ToolResultEvent):
                    status = (event.tool_result or {}).get("status")
                    outcome = "error" if status == "error" else "ok"
                yield event
        except InterruptException:
            # A form or a sign-in paused the call mid-flight; its replay meets this row again.
            raise
        except (asyncio.CancelledError, GeneratorExit, KeyboardInterrupt, SystemExit):
            # A Stop or an abandoned stream: nothing may be awaited now. The row stays
            # `started`, "may have run".
            raise
        except Exception:
            outcome = "error"
            await self._finish(recorder, run, tool_use_id, outcome)
            raise
        if outcome is not None:
            await self._finish(recorder, run, tool_use_id, outcome)

    @staticmethod
    async def _finish(recorder: Any, run: UnattendedRun, tool_use_id: str, outcome: str) -> None:
        try:
            await asyncio.to_thread(
                recorder.finish_action, user_id=run.user_id, run_id=run.run_id, tool_use_id=tool_use_id, outcome=outcome
            )
        except Exception:
            # Leaves `outcome: started`, which reads as "may have run", never "didn't".
            logger.warning("Action audit: could not record the outcome on run %s", run.run_id, exc_info=True)
