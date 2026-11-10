"""Run one sample of one case and produce its result row (eval-harness spec §6.1).

The turn itself is injected (``TurnRunner``), so this module is the same code
whether the turn goes to the deployed Runtime, a local inference-api, or a
test double. In production the scripts bind ``run_agent_headless`` with the
eval user's auth and the target's URL.

A row is JSON-clean and self-contained: it holds every turn's ``RunResult``
dict and the mapped ``Session``, so ``judge`` (PR-2) and ``score --explain``
need nothing but the run directory.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Tuple

from apis.shared.evals.cases import DELIVERY_FAMILY, EvalCase
from apis.shared.evals.score import score_sample
from apis.shared.evals.session_mapper import build_session
from apis.shared.harness.models import RunResult

#: Deliberately not ``HEADLESS_SESSION_PREFIX``. That prefix marks a turn as
#: unattended, which changes behaviour (a project-memory save becomes a proposal,
#: a paused turn keeps a different TTL). The harness measures the attended chat
#: path a user gets, so its sessions must not carry it.
EVAL_SESSION_PREFIX = "eval-"
EVAL_TRIGGER = "eval"


class TurnRunner(Protocol):
    def __call__(
        self,
        *,
        prompt: str,
        session_id: str,
        title: str,
        model_id: Optional[str],
        enabled_tools: Optional[List[str]],
        enabled_skills: Optional[List[str]],
        file_upload_ids: Optional[List[str]],
        rag_assistant_id: Optional[str] = None,
    ) -> Awaitable[RunResult]: ...


#: ``(session_id, filename) -> upload_id``
FixtureRegistrar = Callable[[str, str], Awaitable[str]]


@dataclass(frozen=True)
class StagedSample:
    """A sample's session, created before the run, with its large documents attached and prepared.

    Large documents take minutes to become searchable, so a run stages every
    such sample first and waits for all of them at once, instead of each
    worker waiting in turn. ``documents`` is each one's state when the wait
    ended (``fixtures.wait_for_documents``), kept on the result row so a
    failure can be told apart from a document that was not ready.
    """

    session_id: str
    upload_ids: Tuple[str, ...]
    documents: Tuple[Dict[str, Any], ...] = ()


class OAuthRequiredError(RuntimeError):
    """A case reached a connector the eval user has not consented to.

    The eval user's grants are the fixed thing a baseline describes (spec D7),
    so this is a configuration error that stops the run, not a failed sample.
    """


#: The most client-tool pauses one turn may answer before the harness gives up:
#: a model that keeps calling the same tool is a failure, not an infinite loop.
MAX_CLIENT_TOOL_ROUNDS = 4


def client_tool_specs(case: EvalCase) -> Optional[List[Dict[str, Any]]]:
    """The case's client tools as ``InvocationRequest.client_tools`` takes them (camelCase schema)."""
    if not case.run.client_tools:
        return None
    return [
        {"name": t["name"], "description": t["description"],
         "inputSchema": t.get("input_schema") or {"type": "object", "properties": {}}}
        for t in case.run.client_tools
    ]


def client_tool_responses(case: EvalCase, calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The case's canned answer to each paused call, keyed by its interrupt id. Never null."""
    by_name = {t["name"]: t.get("respond") or {} for t in (case.run.client_tools or ())}
    responses = []
    for call in calls:
        respond = by_name.get(str(call.get("toolName") or "")) or {"output": "unknown tool", "is_error": True}
        response = (
            {"declined": True} if respond.get("declined")
            else {"output": str(respond.get("output", "")), "isError": bool(respond.get("is_error", False))}
        )
        responses.append({"interruptId": call.get("interruptId"), "response": response})
    return responses


def _merge_rounds(rounds: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One logical turn from its invocations: every round's tool calls, the last round's answer."""
    merged = dict(rounds[-1])
    merged["tool_trace"] = [entry for r in rounds for entry in (r.get("tool_trace") or [])]
    merged["client_tool_calls"] = [call for r in rounds for call in (r.get("client_tool_calls") or [])]
    if len(rounds) > 1:
        merged["rounds"] = len(rounds)
        merged["usage"] = {"usage": sum_usage(rounds)}
        # The user waited for the first round's first token; the turn lasted all of them.
        for key in ("first_token_ms", "client_first_token_ms"):
            merged[key] = rounds[0].get(key)
        for key in ("turn_duration_ms", "client_duration_ms"):
            values = [r.get(key) for r in rounds]
            merged[key] = sum(values) if all(isinstance(v, int) for v in values) else None
    return merged


#: Per-turn timing a result row carries (``RunResult``'s names, camelCased).
TIMING_FIELDS = (
    ("first_token_ms", "firstTokenMs"),
    ("turn_duration_ms", "turnDurationMs"),
    ("client_first_token_ms", "clientFirstTokenMs"),
    ("client_duration_ms", "clientDurationMs"),
)


def turn_timing(turns: List[Dict[str, Any]]) -> List[Dict[str, Optional[int]]]:
    """Each turn's first token and duration, as the Runtime and as the harness measured them.

    ``None`` where a turn's final ``metadata`` frame never came (an error or a
    cut stream), so a readout can tell a missing number from a fast one.
    """
    return [{label: turn.get(key) for key, label in TIMING_FIELDS} for turn in turns]


def new_session_id() -> str:
    return f"{EVAL_SESSION_PREFIX}{uuid.uuid4().hex[:16]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sum_usage(turns: List[Dict[str, Any]]) -> Dict[str, int]:
    """Token totals across a sample's turns, from each turn's ``metadata_summary`` usage."""
    totals: Dict[str, int] = {}
    for turn in turns:
        usage = (turn.get("usage") or {}).get("usage") or {}
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + int(value)
    return totals


async def run_sample(
    case: EvalCase,
    *,
    sample: int,
    model_id: str,
    run_turn: TurnRunner,
    register_fixture: Optional[FixtureRegistrar] = None,
    fixtures_dir: Optional[Path] = None,
    staged: Optional[StagedSample] = None,
    assistant_id: Optional[str] = None,
    inspect: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Run every turn of ``case`` in one fresh session and score the last.

    ``assistant_id`` is the seeded fixture the case runs against (a project's
    harness Agent or an Agent), sent as ``rag_assistant_id`` on every turn.

    A turn's ``session`` number picks its session (session 1 is the sample's
    own id, each other number a fresh one) and its user (``as_user``, passed to
    ``run_turn`` only when it is not the eval user). ``inspect`` is awaited
    after the last turn and returns the project memory a memory check scores
    (``score_sample``'s ``memory_state``); it runs even when a turn failed.

    ``staged`` is a session whose large documents were uploaded and prepared
    ahead of the run (``StagedSample``): the turns run in that session and
    attach those uploads too, and the row records each document's state.

    Raises :class:`OAuthRequiredError` if any turn needs connector consent.
    Every other failure (an error, a timeout, an empty stream) is a failed
    sample with its reason recorded, because that is what a regression looks
    like from the outside.
    """
    if case.large_documents and staged is None:
        raise ValueError(f"{case.id} names large documents but none were staged for it")
    session_id = staged.session_id if staged is not None else new_session_id()
    started_at = _now_iso()

    upload_ids: List[str] = list(staged.upload_ids) if staged is not None else []
    upload_started = time.monotonic()
    for filename in case.files:
        if register_fixture is None:
            raise ValueError(f"{case.id} names fixture files but no fixture registrar was given")
        upload_ids.append(await register_fixture(session_id, filename))
    # Upload to usable, for files attached at send time: the registrar returns
    # once the upload is READY (direct row, or presign → PUT → complete).
    upload_ms = int((time.monotonic() - upload_started) * 1000) if case.files else None

    turns: List[Dict[str, Any]] = []
    status = "completed"
    error: Optional[str] = None
    extra: Dict[str, Any] = {"rag_assistant_id": assistant_id} if assistant_id else {}
    if case.run.client_surface:
        extra["client_surface"] = case.run.client_surface
    specs = client_tool_specs(case)
    if specs:
        extra["client_tools"] = specs
    sessions: Dict[int, str] = {1: session_id}
    for index, prompt in enumerate(case.turns):
        meta = case.meta_for(index)
        turn_session = sessions.setdefault(meta.session, new_session_id())
        per_user: Dict[str, Any] = {"as_user": meta.as_user} if meta.as_user != "eval" else {}
        result = await run_turn(
            **extra,
            **per_user,
            prompt=prompt,
            session_id=turn_session,
            title=f"[eval] {case.id}",
            model_id=model_id,
            enabled_tools=list(case.run.enabled_tools) if case.run.enabled_tools is not None else None,
            enabled_skills=list(case.run.enabled_skills) if case.run.enabled_skills is not None else None,
            # Files arrive with the first turn, as a user attaching them would.
            file_upload_ids=(upload_ids or None) if index == 0 else None,
        )
        rounds = [result.to_dict()]
        # A client tool pauses the turn; the harness is the client, so it
        # answers from the case and resumes in the same session, as the
        # terminal client does after the user approves the call.
        while result.status == "client_tool_call" and len(rounds) <= MAX_CLIENT_TOOL_ROUNDS:
            result = await run_turn(
                **extra,
                **per_user,
                prompt="",
                session_id=turn_session,
                title=f"[eval] {case.id}",
                model_id=model_id,
                enabled_tools=list(case.run.enabled_tools) if case.run.enabled_tools is not None else None,
                enabled_skills=list(case.run.enabled_skills) if case.run.enabled_skills is not None else None,
                file_upload_ids=None,
                interrupt_responses=client_tool_responses(case, result.client_tool_calls),
            )
            rounds.append(result.to_dict())
        turns.append(_merge_rounds(rounds))
        if result.status == "client_tool_call":
            status = "client_tool_loop"
            error = f"still calling client tools after {MAX_CLIENT_TOOL_ROUNDS} answers"
            break
        if result.status == "oauth_required":
            providers = ", ".join(o.provider_id for o in result.oauth_required) or "unknown"
            raise OAuthRequiredError(f"{case.id}: the eval user needs consent for {providers}")
        if result.status != "completed":
            status = result.status
            error = result.error
            break

    memory_state = await inspect() if inspect is not None else None

    last = turns[-1]
    tool_calls = [
        {"name": entry.get("name"), "input": entry.get("input"), "is_error": entry.get("is_error", False)}
        for entry in (last.get("tool_trace") or [])
    ]
    scored = score_sample(
        case.expect,
        final_message=str(last.get("final_message") or ""),
        tool_calls=tool_calls,
        status=status,
        memory_state=memory_state,
    )

    return {
        "case_id": case.id,
        "family": case.family,
        "sample": sample,
        "model_id": model_id,
        "session_id": session_id,
        **({"sessions": [
            {"session_id": sid, "as_user": next(
                (case.meta_for(i).as_user for i in range(len(case.turns)) if case.meta_for(i).session == n), "eval")}
            for n, sid in sorted(sessions.items())
        ]} if len(sessions) > 1 or any(case.meta_for(i).as_user != "eval" for i in range(len(case.turns))) else {}),
        **({"memory": memory_state} if memory_state is not None else {}),
        "upload_ids": upload_ids,
        **({"documents": list(staged.documents)} if staged is not None else {}),
        "status": status,
        "error": error,
        "passed": scored.passed,
        "failures": list(scored.failures),
        "final_message": str(last.get("final_message") or ""),
        "tool_calls": tool_calls,
        "usage": sum_usage(turns),
        "timing": turn_timing(turns),
        **({"upload_ms": upload_ms} if upload_ms is not None else {}),
        **({"arm": case.run.arm, "ceiling": case.run.ceiling} if case.family == DELIVERY_FAMILY else {}),
        "started_at": started_at,
        "finished_at": _now_iso(),
        "turns": turns,
        "session": build_session(
            session_id=session_id,
            turns=list(zip(case.turns, turns)),
            available_tools=[{"name": name} for name in (case.run.enabled_tools or ())],
        ),
    }
