"""The judge seam (eval-harness spec D2, D11).

A judge scores one scrubbed, ``Session``-shaped sample with named evaluators
and returns numbers. It is advisory: no family gates on a judge (spec D5).

This module defines only the contract. The strands-agents-evals adapter lands
in phase 1 PR-2 together with the pinned dependency; it will import the
package lazily, inside its methods, so ``apis.shared`` stays importable in
every service image, and a breaking upstream release is one file to fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence


@dataclass(frozen=True)
class JudgeVerdict:
    """One evaluator's score for one sample. ``reason`` stays in the run directory, never in a baseline."""

    evaluator: str
    score: float
    passed: bool
    label: Optional[str] = None
    reason: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class Judge(Protocol):
    def judge(
        self,
        session: Mapping[str, Any],
        *,
        evaluators: Sequence[str],
        rubric: Optional[str] = None,
    ) -> List[JudgeVerdict]:
        """Score one scrubbed session with each named evaluator."""
