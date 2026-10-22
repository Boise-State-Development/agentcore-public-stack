"""Admin models for the feedback eval-sampling surface (spec §11 PR-4).
Content-free: ids, codes, scores, counts. No conversation text, and never
the judge's explanation (the content-policy walk covers this module)."""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class EvaluatorScore(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    value: float
    rating: Optional[str] = None
    n: int = 1
    tokens: int = 0


class FeedbackVerdict(BaseModel):
    """The judged result stored on a thumb row."""
    model_config = ConfigDict(populate_by_name=True)

    reason: str = "none"
    evaluators: List[str] = Field(default_factory=list)
    scores: Dict[str, EvaluatorScore] = Field(default_factory=dict)
    tool_failure_corroborated: Optional[bool] = Field(None, alias="toolFailureCorroborated")


class DownThumbQueueItem(BaseModel):
    """One recent down-thumb as the sampler's queue sees it."""
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(..., alias="sessionId")
    message_id: int = Field(..., alias="messageId")
    reason: Optional[str] = None
    updated_at: str = Field("", alias="updatedAt")
    retry_message_id: Optional[int] = Field(None, alias="retryMessageId")
    evaluated_at: Optional[str] = Field(None, alias="evaluatedAt")
    evaluation: Optional[FeedbackVerdict] = None


class DownThumbQueueResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    items: List[DownThumbQueueItem]
    pending: int = Field(0, description="Items in this page not yet judged")
    sampling_enabled: bool = Field(False, alias="samplingEnabled")


class FleetArm(BaseModel):
    """One arm of one dimension. ``downRate`` is deliberately ``None`` below
    the coverage floor: there is then no number for anyone to quote."""
    model_config = ConfigDict(populate_by_name=True)

    key: str
    up: int = 0
    down: int = 0
    n: int = 0
    down_rate: Optional[float] = Field(None, alias="downRate")
    below_floor: bool = Field(False, alias="belowFloor")


class FleetWindow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    start: str
    end: str
    days: int


class FleetCoverage(BaseModel):
    """How much of the window the arms actually rest on. Spec §9: every
    response carries its n and its coverage, so a comparison can be judged
    rather than taken."""
    model_config = ConfigDict(populate_by_name=True)

    thumbs: int = 0
    joined: int = 0
    unjoined: int = 0
    join_rate: Optional[float] = Field(None, alias="joinRate")
    sessions_with_feedback: int = Field(0, alias="sessionsWithFeedback")
    sessions_joined: int = Field(0, alias="sessionsJoined")
    sessions_omitted: int = Field(0, alias="sessionsOmitted")
    truncated: bool = False


class FleetTotals(BaseModel):
    """Counts only. There is deliberately no fleet-wide rate field here —
    see `fleet.py` and spec §9."""
    model_config = ConfigDict(populate_by_name=True)

    up: int = 0
    down: int = 0
    thumbs: int = 0


class FleetFeedbackResponse(BaseModel):
    """Down-thumb rate by config arm across the fleet (spec §7)."""
    model_config = ConfigDict(populate_by_name=True)

    window: FleetWindow
    totals: FleetTotals
    coverage: FleetCoverage
    arms: Dict[str, List[FleetArm]]
    reasons: Dict[str, int] = Field(default_factory=dict)
    minimum_n: int = Field(20, alias="minimumN")


class SamplingRunResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    accepted: bool
    limit: int
    note: str
