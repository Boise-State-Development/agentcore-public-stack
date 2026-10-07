"""Eval sampling — response-feedback spec §11 PR-4.

A down-thumb is a *sampler*: it marks the small subset of turns worth
spending judge tokens on. This module turns the queue of recent down-thumbs
into AgentCore Evaluations calls, routed by the thumb's reason code (spec
§6), and writes a **content-free** verdict back onto the thumb row.

Three rules, from the spec and the evaluations spike:

* **Offline batch only.** ``run_sampling_batch`` is driven by an admin
  request (or a future schedule), never by a turn. An inline per-turn judge
  is exactly what the cost tenet exists to stop.
* **Numbers leave, prose does not.** The Evaluate API returns an
  ``explanation`` that quotes the conversation. ``summarize`` drops it; the
  storage write refuses anything that still carries one; the content-policy
  denylist names it.
* **The judge is a seam.** :class:`Judge` is a one-method protocol so tests
  and forks without AgentCore Evaluations inject their own;
  :class:`AgentCoreJudge` is the SDK adapter, imported lazily.

Routing (spec §6 → the 16 built-in evaluators the spike verified):

    wrong         → Builtin.Correctness, Builtin.Faithfulness
    instructions  → Builtin.InstructionFollowing
    length        → Builtin.Conciseness            (the style signal)
    tool_failed   → no judge — ops, corroborated against the call's tool census
    outdated      → no judge yet — KB-freshness join is a follow-up
    other / none  → Builtin.Helpfulness            (the generic judge)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional, Protocol, Sequence

logger = logging.getLogger(__name__)

RUNTIME_LOG_GROUP_ENV = "AGENTCORE_RUNTIME_LOG_GROUP"

#: Reason code → built-in evaluator ids. An empty tuple means "no judge for
#: this bucket" (it is an ops signal, or not yet joinable), not "skip".
EVALUATORS_BY_REASON: Dict[Optional[str], tuple] = {
    "wrong": ("Builtin.Correctness", "Builtin.Faithfulness"),
    "instructions": ("Builtin.InstructionFollowing",),
    "length": ("Builtin.Conciseness",),
    "tool_failed": (),
    "outdated": (),
    "other": ("Builtin.Helpfulness",),
    None: ("Builtin.Helpfulness",),
}


def evaluators_for(reason: Optional[str]) -> tuple:
    """The evaluator ids a reason bucket routes to (unknown codes → generic)."""
    return EVALUATORS_BY_REASON.get(reason, EVALUATORS_BY_REASON[None])


class Judge(Protocol):
    """Anything that can score one conversation with a set of evaluators."""

    def judge(
        self, session_id: str, user_id: str, evaluator_ids: Sequence[str]
    ) -> List[Dict[str, Any]]:
        """Raw ``evaluationResults`` items for the user's session, or ``[]``."""


class AgentCoreJudge:
    """``bedrock_agentcore.evaluation.EvaluationClient`` over the runtime log
    group. Lazy imports keep ``apis.shared`` importable in images without the
    SDK. The session id sent is the *runtime* session id
    (``runtime_session_id_for(session_id, user_id)``), which is how the chat
    proxy pins spans."""

    def __init__(self, log_group_name: Optional[str] = None, region_name: Optional[str] = None,
                 look_back: timedelta = timedelta(days=7)):
        self.log_group_name = log_group_name or os.environ.get(RUNTIME_LOG_GROUP_ENV, "").strip()
        self.region_name = region_name or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        self.look_back = look_back
        self._client = None

    def judge(
        self, session_id: str, user_id: str, evaluator_ids: Sequence[str]
    ) -> List[Dict[str, Any]]:
        if not self.log_group_name:
            raise RuntimeError(f"{RUNTIME_LOG_GROUP_ENV} is not configured")
        if self._client is None:
            from bedrock_agentcore.evaluation import EvaluationClient  # lazy: heavy, optional

            self._client = EvaluationClient(region_name=self.region_name)
        from apis.shared.harness.runner import (
            legacy_runtime_session_id_for,
            runtime_session_id_for,
        )

        results = self._run(runtime_session_id_for(session_id, user_id), evaluator_ids)
        if results:
            return results
        # A turn from before per-user pinning shipped carries its spans under
        # the session-only id. Remove with `legacy_runtime_session_id_for`.
        return self._run(legacy_runtime_session_id_for(session_id), evaluator_ids)

    def _run(self, runtime_session_id: str, evaluator_ids: Sequence[str]) -> List[Dict[str, Any]]:
        return self._client.run(
            evaluator_ids=list(evaluator_ids),
            session_id=runtime_session_id,
            log_group_name=self.log_group_name,
            look_back_time=self.look_back,
        )


def summarize(results: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Collapse raw ``evaluationResults`` into ``{evaluatorId: {value, rating,
    n, tokens}}`` — numbers and the evaluator's own rating vocabulary (its
    ``label``, stored as ``rating`` because ``label`` is a content-bearing
    path elsewhere in the table and the denylist is by name). The
    ``explanation`` (prose quoting the conversation) and the span context
    are dropped here and nowhere later. TRACE-level evaluators return one
    result per trace; ``value`` is their mean and ``n`` says how many."""
    out: Dict[str, Dict[str, Any]] = {}
    for item in results:
        evaluator = item.get("evaluatorId") or item.get("evaluatorName")
        if not evaluator or item.get("errorCode"):
            continue
        value = item.get("value")
        if not isinstance(value, (int, float)):
            continue
        slot = out.setdefault(evaluator, {"value": 0.0, "n": 0, "tokens": 0})
        n = slot["n"]
        slot["value"] = round((slot["value"] * n + float(value)) / (n + 1), 4)
        slot["n"] = n + 1
        slot["tokens"] += int((item.get("tokenUsage") or {}).get("totalTokens") or 0)
        rating = item.get("label")
        if isinstance(rating, str) and rating:
            slot["rating"] = rating
    return out


def corroborate_tool_failure(cost_row: Optional[Dict[str, Any]]) -> Optional[bool]:
    """For a ``tool_failed`` thumb: did the call's tool census (``toolCalls``
    on the ``C#`` row) actually record an error? ``None`` when the row or the
    census is missing — "could not check", not "no"."""
    if not cost_row:
        return None
    census = cost_row.get("toolCalls")
    if not isinstance(census, dict):
        return None
    for entry in census.values():
        errors = entry.get("errors") if isinstance(entry, dict) else None
        try:
            if int(errors or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


@dataclass
class SamplingReport:
    """What one batch did — counts only, for the log line and tests."""
    candidates: int = 0
    judged: int = 0
    corroborated: int = 0
    skipped_no_judge: int = 0
    skipped_already: int = 0
    failed: int = 0
    tokens: int = 0
    errors: List[str] = field(default_factory=list)


def build_verdict(
    reason: Optional[str],
    results: Sequence[Dict[str, Any]],
    cost_row: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """The content-free ``evaluation`` map for one thumb."""
    verdict: Dict[str, Any] = {"reason": reason or "none", "evaluators": list(evaluators_for(reason))}
    if results:
        verdict["scores"] = summarize(results)
    if reason == "tool_failed":
        corroborated = corroborate_tool_failure(cost_row)
        if corroborated is not None:
            verdict["toolFailureCorroborated"] = corroborated
    return verdict


async def run_sampling_batch(
    table,
    judge: Judge,
    *,
    limit: int = 10,
    cost_row_lookup=None,
) -> SamplingReport:
    """Judge up to ``limit`` recent, not-yet-judged down-thumbs.

    ``cost_row_lookup(session_id, message_id) -> Optional[dict]`` supplies the
    call's ``C#`` row for tool-failure corroboration; optional. Every thumb is
    independent: one judge failure is recorded and the batch continues.
    """
    from apis.shared.sessions.feedback import is_explicit, list_recent_down_thumbs, store_evaluation

    report = SamplingReport()
    rows = [r for r in list_recent_down_thumbs(table, limit=limit * 3) if is_explicit(r)]
    for row in rows:
        if report.judged + report.skipped_no_judge + report.failed >= limit:
            break
        if row.get("evaluatedAt"):
            report.skipped_already += 1
            continue
        report.candidates += 1
        session_id = str(row.get("sessionId") or "")
        user_id = str(row.get("userId") or "")
        try:
            message_id = int(row.get("messageId"))
        except (TypeError, ValueError):
            report.failed += 1
            continue
        reason = row.get("reason") if isinstance(row.get("reason"), str) else None
        evaluator_ids = evaluators_for(reason)
        cost_row = cost_row_lookup(session_id, message_id) if cost_row_lookup else None
        results: List[Dict[str, Any]] = []
        try:
            if evaluator_ids:
                results = judge.judge(session_id, user_id, evaluator_ids)
            verdict = build_verdict(reason, results, cost_row)
            store_evaluation(table, user_id, session_id, message_id, verdict)
        except Exception as e:  # noqa: BLE001 - one bad thumb must not stop the batch
            report.failed += 1
            report.errors.append(type(e).__name__)
            logger.warning("eval sampling failed for a thumb: %s", type(e).__name__)
            continue
        if evaluator_ids:
            report.judged += 1
            report.tokens += sum(s.get("tokens", 0) for s in verdict.get("scores", {}).values())
        else:
            report.skipped_no_judge += 1
        if verdict.get("toolFailureCorroborated"):
            report.corroborated += 1
    logger.info(
        "eval sampling batch: candidates=%d judged=%d no_judge=%d already=%d failed=%d tokens=%d",
        report.candidates, report.judged, report.skipped_no_judge, report.skipped_already,
        report.failed, report.tokens,
    )
    return report
