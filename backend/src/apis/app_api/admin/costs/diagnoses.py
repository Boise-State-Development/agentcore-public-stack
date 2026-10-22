"""Diagnosis rules for the admin session profile.

Every quota investigation on this platform so far ended in a *named,
computable* classification — "partial prefix miss", "the compaction summary is
40% of the window", "`extra_tools` agent-cache bypass cohort" — and a concrete
cost-effectiveness fix. Each took an afternoon of hand-written DynamoDB queries
to reach. These rules encode those classifications so a conversation arrives
pre-labelled: the evidence is numbers already on the session's rows, the
suggestion is the fix the investigation landed on, and ``ref`` points at the
document that argued it.

Design rules, in order of how expensive they are to get wrong:

1. **Pure.** A rule is ``ProfileFacts -> Optional[Diagnosis]``. No I/O, no
   clock, no env reads inside a rule — the one environment-dependent value
   (the compaction threshold) is resolved once by the caller and passed in.
   That is what makes each rule a one-line unit test.
2. **Content-free by construction.** ``ProfileFacts`` carries counts, tokens,
   hashes-as-counts, dollars and tool *ids*. Nothing here can see text.
3. **Evidence over adjectives.** A diagnosis that says "heavy" without the
   number is an opinion; every ``evidence`` dict carries the values the rule
   compared and the threshold it compared them to.
4. **Thresholds come from where they live.** The compaction threshold is the
   runtime's, resolved exactly as ``CompactionConfig.from_env`` resolves it;
   the write:read ratio is the shipped ``partial_miss`` classifier's. Values
   that exist only as a proposal (the summary budget) say so in a comment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agents.main_agent.config.constants import Defaults, EnvVars
from apis.shared.observability.prompt_cache import PARTIAL_MISS_WRITE_READ_RATIO
from apis.shared.tools.injected import (
    INJECTED_TOOL_IDS,
    KEY_DESCRIBED_INJECTED_TOOL_IDS,
)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: #833 PR-2's proposed cap on the persisted compaction summary. Not yet a
#: runtime constant — the spec's §4.1 scan found 32% of compacting sessions
#: already over it. Promote to `constants.py` when PR-2 lands and import it.
SUMMARY_TOKEN_BUDGET = 8_000
CHARS_PER_TOKEN = 4

#: Enabled catalog ids at or above which the tool set is flagged. Every
#: enabled tool's schema is read on every call, so this is a context-share
#: signal, not a cost-per-tool one.
LARGE_TOOLSET_MIN_IDS = 8

ATTACHMENT_HEAVY_BYTES = 5 * 1024 * 1024
ATTACHMENT_HEAVY_COUNT = 5

#: A single conversation at or above this share of the user's period spend is
#: the one whose anatomy is worth reading first.
DOMINANT_SESSION_SHARE_PCT = 50.0

#: Partial-miss dollars at or above this share of a session's cost means the
#: session is paying mostly for prefix re-writes that read as "hit".
PARTIAL_MISS_SHARE_OF_COST = 0.5

TOOL_ERROR_RATE = 0.25
TOOL_ERROR_MIN_CALLS = 4
TOOL_HEAVY_CALLS = 40
AGENT_SWITCH_CHURN_MIN = 3

REF_SPIRAL = "docs/specs/compaction-over-threshold-cache-spiral.md"
REF_FLEET = "docs/one-pagers/fleet-prefix-spend-anatomy.md"
REF_BYPASS = "docs/specs/agent-cache-extra-tools-bypass.md"
REF_ROADMAP = "docs/one-pagers/cost-effectiveness-roadmap.md"

SEVERITY_ORDER: Dict[str, int] = {"high": 0, "warn": 1, "info": 2}


def compaction_token_threshold() -> int:
    """The live compaction threshold, resolved as ``CompactionConfig.from_env`` does.

    Read here rather than imported as a constant so an environment that tunes
    ``AGENTCORE_MEMORY_COMPACTION_TOKEN_THRESHOLD`` diagnoses against the value
    its runtime actually uses. Malformed or empty falls back to the default,
    never raises — a diagnosis must not 500 a page.
    """
    raw = os.environ.get(EnvVars.COMPACTION_TOKEN_THRESHOLD, "").strip()
    if not raw:
        return Defaults.COMPACTION_TOKEN_THRESHOLD
    try:
        return int(raw)
    except ValueError:
        return Defaults.COMPACTION_TOKEN_THRESHOLD


# ---------------------------------------------------------------------------
# Inputs and outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Diagnosis:
    """One named finding about a conversation. Serializes to the API as-is."""

    code: str
    severity: str  # "high" | "warn" | "info"
    # `headline`, not `title`: `title` is a denylisted attribute name on the
    # session row and the content-policy test walks every response model.
    headline: str
    evidence: Dict[str, Any]
    suggestion: str
    ref: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "headline": self.headline,
            "evidence": dict(self.evidence),
            "suggestion": self.suggestion,
            "ref": self.ref,
        }


@dataclass
class ProfileFacts:
    """Everything a rule may look at. All content-free; all optional-tolerant.

    ``None`` means "not recorded" (a row written before the field shipped),
    which every rule treats as "cannot fire", never as zero.
    """

    cost_known: bool = False
    total_cost: float = 0.0
    call_count: int = 0

    peak_context_tokens: Optional[int] = None
    context_window: Optional[int] = None
    compaction_threshold: int = Defaults.COMPACTION_TOKEN_THRESHOLD

    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    wasted_usd: float = 0.0
    partial_miss_usd: float = 0.0
    partial_miss_count: int = 0

    summary_approx_tokens: Optional[int] = None
    checkpoint: Optional[int] = None
    truncation_anchor: Optional[int] = None

    #: Distinct fingerprint hashes among calls that did NOT switch Agent — an
    #: `@`-mention re-writes the prefix on purpose and is counted separately.
    distinct_system_prompt_hashes: int = 0
    distinct_tool_config_hashes: int = 0
    agent_switch_count: int = 0

    enabled_tools: List[str] = field(default_factory=list)

    attachment_count: int = 0
    attachment_bytes: int = 0

    share_of_user_period: Optional[float] = None

    tool_call_count: Optional[int] = None
    tool_error_count: Optional[int] = None

    @property
    def write_read_ratio(self) -> Optional[float]:
        """cacheWrite / cacheRead, or None when nothing was ever read."""
        if self.cache_read_tokens <= 0:
            return None
        return self.cache_write_tokens / self.cache_read_tokens


Rule = Callable[[ProfileFacts], Optional[Diagnosis]]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def cost_unknown(f: ProfileFacts) -> Optional[Diagnosis]:
    if f.cost_known:
        return None
    return Diagnosis(
        code="COST_UNKNOWN",
        severity="info",
        headline="Cost is unknown for this conversation",
        evidence={"callCount": f.call_count},
        suggestion=(
            "The session's cost aggregate was never written or backfilled. It is "
            "not $0 — it is unrecorded. Fleet-wide, 20% of session rows are in "
            "this state; treat totals that include this session as a floor."
        ),
        ref=REF_FLEET,
    )


def over_compaction_threshold(f: ProfileFacts) -> Optional[Diagnosis]:
    if f.peak_context_tokens is None or f.peak_context_tokens <= f.compaction_threshold:
        return None
    return Diagnosis(
        code="OVER_COMPACTION_THRESHOLD",
        severity="warn",
        headline="Context crossed the compaction threshold",
        evidence={
            "peakContextTokens": f.peak_context_tokens,
            "compactionThreshold": f.compaction_threshold,
            "contextWindow": f.context_window,
        },
        suggestion=(
            "Above the threshold every turn re-writes the prefix unless compaction "
            "actually shrinks what is sent to the model. Look for a plateau above "
            "the line in the context trajectory; 9% of sessions in this state "
            "carried 70% of a month's spend."
        ),
        ref=REF_SPIRAL,
    )


def prefix_spiral(f: ProfileFacts) -> Optional[Diagnosis]:
    if f.peak_context_tokens is None or f.peak_context_tokens <= f.compaction_threshold:
        return None
    ratio = f.write_read_ratio
    writes_without_reads = f.cache_read_tokens <= 0 and f.cache_write_tokens > 0
    if not writes_without_reads and (ratio is None or ratio <= PARTIAL_MISS_WRITE_READ_RATIO):
        return None
    return Diagnosis(
        code="PREFIX_SPIRAL",
        severity="high",
        headline="Over threshold and re-writing the prefix on every call",
        evidence={
            "writeReadRatio": round(ratio, 2) if ratio is not None else None,
            "cacheWriteTokens": f.cache_write_tokens,
            "cacheReadTokens": f.cache_read_tokens,
            "peakContextTokens": f.peak_context_tokens,
            "ratioThreshold": PARTIAL_MISS_WRITE_READ_RATIO,
        },
        suggestion=(
            "The #833 shape: the tools+system segment hits while the history "
            "segment is re-written at the cache-write premium on every call. One "
            "conversation like this spent a monthly quota in five days. Bound the "
            "compaction summary and make compaction act on the live message list."
        ),
        ref=REF_SPIRAL,
    )


def partial_miss_heavy(f: ProfileFacts) -> Optional[Diagnosis]:
    if not f.cost_known or f.total_cost <= 0:
        return None
    share = f.partial_miss_usd / f.total_cost
    if share < PARTIAL_MISS_SHARE_OF_COST:
        return None
    return Diagnosis(
        code="PARTIAL_MISS_HEAVY",
        severity="high",
        headline="Most of the spend is prefix re-writes that read as hits",
        evidence={
            "partialMissUsd": round(f.partial_miss_usd, 4),
            "totalCost": round(f.total_cost, 4),
            "shareOfCost": round(share, 3),
            "partialMissCount": f.partial_miss_count,
        },
        suggestion=(
            "Open the anatomy and diff toolConfigHash / systemPromptHash / "
            "historyHash between consecutive partial_miss rows — the hash that "
            "changed names the cache-buster."
        ),
        ref=REF_SPIRAL,
    )


def summary_over_budget(f: ProfileFacts) -> Optional[Diagnosis]:
    if f.summary_approx_tokens is None or f.summary_approx_tokens <= SUMMARY_TOKEN_BUDGET:
        return None
    return Diagnosis(
        code="SUMMARY_OVER_BUDGET",
        severity="warn",
        headline="Persisted compaction summary exceeds its budget",
        evidence={
            "summaryApproxTokens": f.summary_approx_tokens,
            "budgetTokens": SUMMARY_TOKEN_BUDGET,
            "contextWindow": f.context_window,
        },
        suggestion=(
            "A summary that is a large share of the window guarantees compaction "
            "can never get back under the threshold (#833 D2) — it is a log of the "
            "conversation, not a compression of it. Cap or re-summarize."
        ),
        ref=REF_SPIRAL,
    )


def anchor_mismatch(f: ProfileFacts) -> Optional[Diagnosis]:
    if f.checkpoint is None or f.truncation_anchor is None:
        return None
    if f.checkpoint == f.truncation_anchor:
        return None
    return Diagnosis(
        code="ANCHOR_MISMATCH",
        severity="info",
        headline="Compaction checkpoint and truncation anchor disagree",
        evidence={"checkpoint": f.checkpoint, "truncationAnchor": f.truncation_anchor},
        suggestion=(
            "One coordinate is window-relative and the other absolute (#833 D3); "
            "the restored prefix may not be byte-stable turn to turn. Compare "
            "historyHash across consecutive calls in the anatomy."
        ),
        ref=REF_SPIRAL,
    )


def system_prompt_mutated(f: ProfileFacts) -> Optional[Diagnosis]:
    if f.distinct_system_prompt_hashes <= 1:
        return None
    return Diagnosis(
        code="SYSTEM_PROMPT_MUTATED",
        severity="warn",
        headline="System prompt changed mid-conversation",
        evidence={
            "distinctSystemPromptHashes": f.distinct_system_prompt_hashes,
            "callCount": f.call_count,
            "agentSwitchesExcluded": f.agent_switch_count,
        },
        suggestion=(
            "On calls that did not switch Agent the system prompt still changed — "
            "a memory-augmented prompt or an unstable skill list re-writes the "
            "cached prefix (#833 D4, the most general defect found). Pin the prompt "
            "per session visit."
        ),
        ref=REF_SPIRAL,
    )


def toolconfig_mutated(f: ProfileFacts) -> Optional[Diagnosis]:
    if f.distinct_tool_config_hashes <= 1:
        return None
    return Diagnosis(
        code="TOOLCONFIG_MUTATED",
        severity="warn",
        headline="Tool configuration changed mid-conversation",
        evidence={
            "distinctToolConfigHashes": f.distinct_tool_config_hashes,
            "callCount": f.call_count,
            "agentSwitchesExcluded": f.agent_switch_count,
        },
        suggestion=(
            "toolConfig is the first cache segment; a change re-writes everything "
            "behind it. Either the tool set changed between turns or a tool list "
            "is not deterministically ordered at its source."
        ),
        ref=REF_FLEET,
    )


def agent_switch_churn(f: ProfileFacts) -> Optional[Diagnosis]:
    if f.agent_switch_count < AGENT_SWITCH_CHURN_MIN:
        return None
    return Diagnosis(
        code="AGENT_SWITCH_CHURN",
        severity="info",
        headline="Repeated Agent switches",
        evidence={"agentSwitchCount": f.agent_switch_count, "callCount": f.call_count},
        suggestion=(
            "An @-mention hands a turn to a different Agent and re-writes the prefix "
            "by design. This is the cost of that feature, not a regression — but it "
            "is a subset of wastedUsd, so subtract it before hunting for one."
        ),
        ref=REF_FLEET,
    )


def agent_cache_bypass(f: ProfileFacts) -> Optional[Diagnosis]:
    enabled = set(f.enabled_tools)
    bypassing = sorted((enabled & INJECTED_TOOL_IDS) - KEY_DESCRIBED_INJECTED_TOOL_IDS)
    if not bypassing:
        return None
    return Diagnosis(
        code="AGENT_CACHE_BYPASS",
        severity="info",
        headline="Enabled tools bypass the agent cache",
        evidence={"bypassingToolIds": bypassing},
        suggestion=(
            "These injected tools capture request scope the agent-cache key does "
            "not describe, so every turn builds a fresh Agent (full initialize + "
            "memory restore). 76% of sessions were in this cohort. Promote the "
            "family once its closures are key-described, or disable it if unused."
        ),
        ref=REF_BYPASS,
    )


def large_toolset(f: ProfileFacts) -> Optional[Diagnosis]:
    if len(f.enabled_tools) < LARGE_TOOLSET_MIN_IDS:
        return None
    return Diagnosis(
        code="LARGE_TOOLSET",
        severity="info",
        headline="Large enabled tool set",
        evidence={
            "enabledToolCount": len(f.enabled_tools),
            "threshold": LARGE_TOOLSET_MIN_IDS,
            "enabledToolIds": sorted(f.enabled_tools),
        },
        suggestion=(
            "Every enabled tool's schema is read by the model before the user's "
            "question; an MCP server can contribute dozens. This spends the working "
            "budget before the first word and degrades tool selection."
        ),
        ref=REF_ROADMAP,
    )


def attachment_heavy(f: ProfileFacts) -> Optional[Diagnosis]:
    if f.attachment_count < ATTACHMENT_HEAVY_COUNT and f.attachment_bytes < ATTACHMENT_HEAVY_BYTES:
        return None
    return Diagnosis(
        code="ATTACHMENT_HEAVY",
        severity="warn",
        headline="Attachment-heavy conversation",
        evidence={
            "attachmentCount": f.attachment_count,
            "attachmentBytes": f.attachment_bytes,
            "countThreshold": ATTACHMENT_HEAVY_COUNT,
            "bytesThreshold": ATTACHMENT_HEAVY_BYTES,
        },
        suggestion=(
            "Attachments are 11% of sessions and 31% of spend. Long-document "
            "iteration belongs in artifacts or an offloaded store, not inline in "
            "the prefix where it is re-read — or re-written — every turn."
        ),
        ref=REF_ROADMAP,
    )


def dominant_session(f: ProfileFacts) -> Optional[Diagnosis]:
    if f.share_of_user_period is None or f.share_of_user_period < DOMINANT_SESSION_SHARE_PCT:
        return None
    return Diagnosis(
        code="DOMINANT_SESSION",
        severity="info",
        headline="This conversation dominates the user's period spend",
        evidence={
            "shareOfUserPeriodPct": round(f.share_of_user_period, 1),
            "threshold": DOMINANT_SESSION_SHARE_PCT,
        },
        suggestion=(
            "A quota exhaustion is usually one thread, not a busy month. This is the "
            "one to read the anatomy of."
        ),
        ref=REF_SPIRAL,
    )


def tool_error_rate(f: ProfileFacts) -> Optional[Diagnosis]:
    if f.tool_call_count is None or f.tool_error_count is None:
        return None
    if f.tool_call_count < TOOL_ERROR_MIN_CALLS:
        return None
    rate = f.tool_error_count / f.tool_call_count
    if rate < TOOL_ERROR_RATE:
        return None
    return Diagnosis(
        code="TOOL_ERROR_RATE",
        severity="warn",
        headline="High tool failure rate",
        evidence={
            "toolCallCount": f.tool_call_count,
            "toolErrorCount": f.tool_error_count,
            "errorRate": round(rate, 3),
            "threshold": TOOL_ERROR_RATE,
        },
        suggestion=(
            "Each failed call still costs a model round-trip plus the error payload "
            "in context, and the model usually retries. Check the tool census for "
            "which tool is failing."
        ),
        ref=REF_ROADMAP,
    )


def tool_heavy(f: ProfileFacts) -> Optional[Diagnosis]:
    if f.tool_call_count is None or f.tool_call_count < TOOL_HEAVY_CALLS:
        return None
    return Diagnosis(
        code="TOOL_HEAVY",
        severity="info",
        headline="Tool-heavy conversation",
        evidence={"toolCallCount": f.tool_call_count, "threshold": TOOL_HEAVY_CALLS},
        suggestion=(
            "Tool results are the unbounded per-turn payload. Check the census for "
            "a tool whose results should be bounded or offloaded."
        ),
        ref=REF_ROADMAP,
    )


RULES: List[Rule] = [
    cost_unknown,
    prefix_spiral,
    partial_miss_heavy,
    over_compaction_threshold,
    summary_over_budget,
    system_prompt_mutated,
    toolconfig_mutated,
    attachment_heavy,
    tool_error_rate,
    anchor_mismatch,
    agent_switch_churn,
    agent_cache_bypass,
    large_toolset,
    dominant_session,
    tool_heavy,
]


def run_diagnoses(facts: ProfileFacts) -> List[Diagnosis]:
    """Apply every rule; return findings ordered high → warn → info, then by code."""
    findings = [d for d in (rule(facts) for rule in RULES) if d is not None]
    findings.sort(key=lambda d: (SEVERITY_ORDER.get(d.severity, 99), d.code))
    return findings


def top_severity(findings: List[Diagnosis]) -> Optional[str]:
    return findings[0].severity if findings else None
