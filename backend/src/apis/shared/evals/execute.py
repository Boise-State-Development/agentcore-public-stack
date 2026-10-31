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

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

from apis.shared.evals.cases import EvalCase
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
    ) -> Awaitable[RunResult]: ...


#: ``(session_id, filename) -> upload_id``
FixtureRegistrar = Callable[[str, str], Awaitable[str]]


class OAuthRequiredError(RuntimeError):
    """A case reached a connector the eval user has not consented to.

    The eval user's grants are the fixed thing a baseline describes (spec D7),
    so this is a configuration error that stops the run, not a failed sample.
    """


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
) -> Dict[str, Any]:
    """Run every turn of ``case`` in one fresh session and score the last.

    Raises :class:`OAuthRequiredError` if any turn needs connector consent.
    Every other failure (an error, a timeout, an empty stream) is a failed
    sample with its reason recorded, because that is what a regression looks
    like from the outside.
    """
    session_id = new_session_id()
    started_at = _now_iso()

    upload_ids: List[str] = []
    for filename in case.files:
        if register_fixture is None:
            raise ValueError(f"{case.id} names fixture files but no fixture registrar was given")
        upload_ids.append(await register_fixture(session_id, filename))

    turns: List[Dict[str, Any]] = []
    status = "completed"
    error: Optional[str] = None
    for index, prompt in enumerate(case.turns):
        result = await run_turn(
            prompt=prompt,
            session_id=session_id,
            title=f"[eval] {case.id}",
            model_id=model_id,
            enabled_tools=list(case.run.enabled_tools) if case.run.enabled_tools is not None else None,
            enabled_skills=list(case.run.enabled_skills) if case.run.enabled_skills is not None else None,
            # Files arrive with the first turn, as a user attaching them would.
            file_upload_ids=(upload_ids or None) if index == 0 else None,
        )
        turns.append(result.to_dict())
        if result.status == "oauth_required":
            providers = ", ".join(o.provider_id for o in result.oauth_required) or "unknown"
            raise OAuthRequiredError(f"{case.id}: the eval user needs consent for {providers}")
        if result.status != "completed":
            status = result.status
            error = result.error
            break

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
    )

    return {
        "case_id": case.id,
        "family": case.family,
        "sample": sample,
        "model_id": model_id,
        "session_id": session_id,
        "upload_ids": upload_ids,
        "status": status,
        "error": error,
        "passed": scored.passed,
        "failures": list(scored.failures),
        "final_message": str(last.get("final_message") or ""),
        "tool_calls": tool_calls,
        "usage": sum_usage(turns),
        "started_at": started_at,
        "finished_at": _now_iso(),
        "turns": turns,
        "session": build_session(
            session_id=session_id,
            turns=list(zip(case.turns, turns)),
            available_tools=[{"name": name} for name in (case.run.enabled_tools or ())],
        ),
    }
