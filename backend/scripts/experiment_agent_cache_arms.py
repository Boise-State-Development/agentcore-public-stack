"""G1 read: does narrowing the agent-cache bypass actually change anything?

Drives real multi-turn sessions through the deployed AgentCore Runtime and
reports, per arm, whether turns reused a cached Agent and what that did to the
prompt-cache token split. This is the controlled experiment
`docs/specs/agent-cache-extra-tools-bypass.md` §6 asks for — the §3 numbers
cannot separate the bypass from the workload, and dev never accumulates enough
observational traffic to try.

**The arms need no redeploy.** Rather than flipping
`AGENT_CACHE_INJECTED_TOOLS_ENABLED` (a platform deploy each way), the arms use
injected-tool families with *identical capture profiles* that differ only in
whether arm 1 promoted them:

    control    enabled_tools=[create_word_document]  bypassed (not promoted)
    treatment  enabled_tools=[create_artifact]       cached   (promoted)
    ceiling    enabled_tools=[]                      no injected tools at all

Same builder shape, same closures, one variable.

**What this can and cannot establish.** It proves *mechanism and direction*:
whether `initialize()` stops running per turn, and whether the prefix gets read
instead of re-written. It does **not** establish fleet-wide magnitude — that is
the roadmap's metric 1 and needs prod traffic. Say which claim you are making.

⚠️ **Read the affinity column first.** The in-process agent cache can only hit
if consecutive turns land on the same microVM, and nothing forwards
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` today, so AWS assigns a fresh
runtime session per invocation. If the treatment arm shows miss/miss/miss, the
finding is *"no microVM affinity"* — NOT "the prompt-cache theory is wrong".
Distinguishing those two is the whole reason this script prints hit/miss per
turn instead of only dollars.

Usage (needs an authenticated dev-ai profile and an active headless grant for
--user-id; see apis/shared/harness/grants.py for what a grant is):

    cd backend
    AWS_PROFILE=dev-ai uv run python scripts/experiment_agent_cache_arms.py \
        --user-id b8d1a320-8021-7065-3a43-6157cdff3e53 --turns 4

    # one arm only, e.g. while iterating
    AWS_PROFILE=dev-ai uv run python scripts/experiment_agent_cache_arms.py \
        --user-id <sub> --arms treatment

Turns are attributed to --user-id and draw on that user's quota.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("experiment")

# Reuse the spike driver's env resolution — one definition of how dev-ai names
# map to harness env vars, already proven against this runtime.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Turn 1 carries a large document. Without it the whole experiment is void: a
# short prompt sits below Bedrock's minimum cacheable prefix, so every call
# comes back `uncached` with read=0/write=0 and there is no cache behavior to
# compare (observed on the first smoke run). Priming once and asking short
# questions afterwards also mirrors the incident's shape — a big history with
# small turns — which is the workload the arms are supposed to represent.
PRIMING_PREAMBLE = (
    "Here is a reference document. Read it, then answer my questions about "
    "unrelated topics concisely. Do not summarize the document unless asked.\n\n"
)


def build_priming_prompt(target_chars: int) -> str:
    """A deterministic filler document of roughly ``target_chars`` characters.

    Deterministic so both arms prime with byte-identical text — a difference
    here would show up as a prefix difference and confound the comparison.
    """
    paragraph = (
        "Section {n}. The platform records each model call with its token "
        "usage, pricing snapshot, and derived cache classification. Operators "
        "review these records to distinguish an appended-tail write from a "
        "re-written prefix, since only the latter represents avoidable spend. "
        "Retention policies, quota windows, and compaction checkpoints are "
        "documented separately and are not restated in this section.\n\n"
    )
    out = [PRIMING_PREAMBLE]
    n = 1
    while sum(len(p) for p in out) < target_chars:
        out.append(paragraph.format(n=n))
        n += 1
    out.append("\nIn one sentence, what is the capital of France?")
    return "".join(out)


# Follow-up prompts are deliberately boring and tool-free: the experiment
# measures agent construction and prefix caching, not tool behavior. A turn that
# actually invoked create_artifact would add tool_use/tool_result blocks and
# change the history shape between arms, which is the one thing that must stay
# comparable.
TURN_PROMPTS = [
    "In one sentence, what is the capital of France?",
    "In one sentence, what is the capital of Japan?",
    "In one sentence, what is the capital of Brazil?",
    "In one sentence, what is the capital of Kenya?",
    "In one sentence, what is the capital of Norway?",
    "In one sentence, what is the capital of Peru?",
]

ARMS: Dict[str, Optional[List[str]]] = {
    # Injected tools that arm 1 did NOT promote → still bypasses the cache.
    "control": ["create_word_document"],
    # Injected tools that arm 1 promoted → eligible for the cache.
    "treatment": ["create_artifact"],
    # No injected tools at all → always was cacheable. The ceiling: whatever
    # this arm achieves is the best the treatment arm could possibly reach.
    "ceiling": [],
}


@dataclass
class TurnObservation:
    index: int
    ok: bool
    latency_s: float
    error: Optional[str] = None
    cache_read: int = 0
    cache_write: int = 0
    cache_status: Optional[str] = None
    input_tokens: int = 0


@dataclass
class ArmResult:
    name: str
    session_id: str
    enabled_tools: Optional[List[str]]
    turns: List[TurnObservation] = field(default_factory=list)
    agent_cache_outcomes: List[str] = field(default_factory=list)


async def run_arm(
    *,
    name: str,
    user_id: str,
    enabled_tools: Optional[List[str]],
    turns: int,
    model_id: Optional[str],
    gap_seconds: float,
    priming_chars: int,
) -> ArmResult:
    """Run one arm's session: N sequential turns, same session_id."""
    from apis.shared.harness import run_agent_headless
    from apis.shared.harness.auth import CognitoRefreshBearerAuth

    session_id = f"exp-{name}-{uuid.uuid4().hex[:12]}"
    result = ArmResult(name=name, session_id=session_id, enabled_tools=enabled_tools)
    auth = CognitoRefreshBearerAuth()

    logger.info("── arm %s · session %s · tools=%s", name, session_id, enabled_tools)
    for i in range(turns):
        # Turn 1 primes the history over the minimum cacheable prefix; every
        # turn after it is short, so what gets cached is the accumulated history.
        prompt = (
            build_priming_prompt(priming_chars) if i == 0
            else TURN_PROMPTS[i % len(TURN_PROMPTS)]
        )
        started = time.monotonic()
        try:
            run = await run_agent_headless(
                user_id=user_id,
                prompt=prompt,
                auth=auth,
                session_id=session_id,
                # None would mean "all RBAC-allowed tools" — an explicit list is
                # what makes the arms differ by exactly one variable.
                enabled_tools=enabled_tools,
                model_id=model_id,
                trigger="experiment",
            )
            elapsed = time.monotonic() - started
            # RunStatus is completed | error | timeout | oauth_required.
            ok = run.status == "completed"
            result.turns.append(
                TurnObservation(
                    index=i + 1, ok=ok, latency_s=elapsed,
                    error=None if ok else (run.error or run.status),
                )
            )
            logger.info(
                "   turn %d/%d %s (%.1fs)", i + 1, turns,
                "ok" if ok else f"FAILED: {run.error or run.status}", elapsed,
            )
        except Exception as exc:  # noqa: BLE001 - one bad turn shouldn't kill the arm
            elapsed = time.monotonic() - started
            result.turns.append(
                TurnObservation(index=i + 1, ok=False, latency_s=elapsed, error=str(exc)[:200])
            )
            logger.warning("   turn %d/%d raised: %s", i + 1, turns, exc)

        # Stay inside the 5-minute Bedrock TTL so a re-write is unexplained —
        # that is the only window where a cache miss means anything.
        if i < turns - 1:
            await asyncio.sleep(gap_seconds)

    return result


def attach_cost_rows(result: ArmResult, prefix: str, region: str) -> None:
    """Read this arm's `C#` rows back and attach the cache verdicts."""
    import boto3
    from boto3.dynamodb.conditions import Key

    table = boto3.resource("dynamodb", region_name=region).Table(
        f"{prefix}-sessions-metadata"
    )
    rows = table.query(
        IndexName="SessionLookupIndex",
        KeyConditionExpression=(
            Key("GSI_PK").eq(f"SESSION#{result.session_id}")
            & Key("GSI_SK").begins_with("C#")
        ),
        ScanIndexForward=True,
    )["Items"]

    for turn, row in zip(result.turns, rows):
        usage = row.get("tokenUsage") or {}
        turn.cache_read = int(usage.get("cacheReadInputTokens") or 0)
        turn.cache_write = int(usage.get("cacheWriteInputTokens") or 0)
        turn.input_tokens = int(usage.get("inputTokens") or 0)
        turn.cache_status = row.get("cacheStatus")

    if len(rows) != len(result.turns):
        logger.warning(
            "arm %s: %d cost rows for %d turns — rows are written asynchronously, "
            "so re-read if this looks short",
            result.name, len(rows), len(result.turns),
        )


def runtime_log_group(runtime_arn: str) -> str:
    """The runtime's CloudWatch log group.

    Named after the runtime *id* (underscores, hyphen-suffixed hash) plus the
    endpoint qualifier — NOT the project prefix. Getting this wrong returns an
    empty result set rather than an error, which is how the CDK dashboard's
    Logs Insights widgets have been querying a non-existent group since #697.
    """
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    return f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"


def attach_agent_cache_outcomes(
    result: ArmResult, log_group: str, region: str, since_epoch: int
) -> None:
    """Pull `agent_cache outcome=` lines for this session from the runtime log.

    This is the `initialize()`-per-turn gate: a hit is exactly the turn that
    skipped `initialize()`. Best-effort — CloudWatch ingestion lags, and a
    missing line is not evidence of a miss.
    """
    import boto3

    logs = boto3.client("logs", region_name=region)
    try:
        resp = logs.filter_log_events(
            logGroupName=log_group,
            startTime=since_epoch * 1000,
            filterPattern=f'"agent_cache outcome=" "{result.session_id}"',
            limit=100,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("log lookup failed for arm %s: %s", result.name, exc)
        return

    # The runtime emits each log event twice (an OTEL JSON envelope and a
    # formatted text line), so dedupe on the event's timestamp — otherwise a
    # 4-turn arm reports "0 hit of 8" and reads like twice the traffic.
    seen_ts: set = set()
    for event in sorted(resp.get("events", []), key=lambda e: e.get("timestamp", 0)):
        ts = event.get("timestamp")
        if ts in seen_ts:
            continue
        message = event.get("message", "")
        for token in message.split():
            if token.startswith("outcome="):
                seen_ts.add(ts)
                result.agent_cache_outcomes.append(token.split("=", 1)[1])
                break


def report(results: List[ArmResult]) -> None:
    print("\n" + "=" * 78)
    print("AGENT-CACHE ARM COMPARISON  (#834 G1)")
    print("=" * 78)

    for r in results:
        ok = sum(1 for t in r.turns if t.ok)
        print(f"\n▸ {r.name}   tools={r.enabled_tools}   session={r.session_id}")
        print(f"  turns ok: {ok}/{len(r.turns)}")

        print(f"  {'turn':>4} {'cacheStatus':<16} {'read':>9} {'write':>9} {'latency':>8}")
        for t in r.turns:
            print(
                f"  {t.index:>4} {(t.cache_status or '—'):<16} "
                f"{t.cache_read:>9,} {t.cache_write:>9,} {t.latency_s:>7.1f}s"
            )

        if r.agent_cache_outcomes:
            seq = "/".join(r.agent_cache_outcomes)
            hits = sum(1 for o in r.agent_cache_outcomes if o == "hit")
            print(f"  agent_cache: {seq}   ({hits} hit of {len(r.agent_cache_outcomes)})")
        else:
            print("  agent_cache: no log lines found (ingestion lag, or none emitted)")

        writes = sum(t.cache_write for t in r.turns)
        reads = sum(t.cache_read for t in r.turns)
        if reads or writes:
            print(f"  totals: read={reads:,} write={writes:,} ratio={writes / max(reads, 1):.2f} write:read")

    print("\n" + "-" * 78)
    print("HOW TO READ THIS")
    print("-" * 78)
    treatment = next((r for r in results if r.name == "treatment"), None)
    ceiling = next((r for r in results if r.name == "ceiling"), None)
    if treatment and treatment.agent_cache_outcomes:
        hits = sum(1 for o in treatment.agent_cache_outcomes if o == "hit")
        if hits == 0:
            print("  Treatment never hit the cache. Before concluding the bypass fix does")
            print("  nothing, check the CEILING arm: if it also never hit, the cause is")
            print("  microVM affinity (no runtime session id is forwarded), not the")
            print("  predicate — and session-id forwarding is a prerequisite for #834.")
        else:
            print(f"  Treatment hit the cache {hits}x — the predicate works end to end.")
            print("  Compare its write:read against control for the prefix-cost effect.")
    if ceiling and ceiling.agent_cache_outcomes:
        c_hits = sum(1 for o in ceiling.agent_cache_outcomes if o == "hit")
        print(f"  Ceiling arm hit {c_hits}x — treatment cannot beat this; it is the bound.")
    print("  Magnitude claims need prod. This measures mechanism and direction only.")
    print()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, help="Cognito sub with an active headless grant")
    parser.add_argument("--prefix", default="dev-boisestateai-v2")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--turns", type=int, default=4)
    parser.add_argument("--gap-seconds", type=float, default=20.0,
                        help="Delay between turns; keep well inside the 300s cache TTL")
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--priming-chars", type=int, default=20000,
                        help="Size of turn 1's document; must clear the model's "
                             "minimum cacheable prefix or every call reads `uncached`")
    parser.add_argument("--arms", default="control,treatment,ceiling",
                        help="Comma-separated subset of: control, treatment, ceiling")
    parser.add_argument("--json-out", default=None, help="Write raw observations here")
    args = parser.parse_args()

    from spike_headless_run import resolve_environment

    resolved = resolve_environment(args.prefix, args.region)
    log_group = runtime_log_group(resolved["runtime_arn"])
    logger.info("runtime: %s", resolved["runtime_arn"])
    logger.info("log group: %s", log_group)

    started_epoch = int(time.time()) - 60
    selected = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in selected if a not in ARMS]
    if unknown:
        parser.error(f"unknown arm(s): {unknown}; choose from {list(ARMS)}")

    results: List[ArmResult] = []
    for name in selected:
        results.append(
            await run_arm(
                name=name,
                user_id=args.user_id,
                enabled_tools=ARMS[name],
                turns=args.turns,
                model_id=args.model_id,
                gap_seconds=args.gap_seconds,
                priming_chars=args.priming_chars,
            )
        )

    # Cost rows and logs are both written asynchronously; give them a moment.
    logger.info("waiting for cost rows / log ingestion…")
    await asyncio.sleep(30)

    for r in results:
        attach_cost_rows(r, args.prefix, args.region)
        attach_agent_cache_outcomes(r, log_group, args.region, started_epoch)

    report(results)

    if args.json_out:
        payload = [
            {
                "arm": r.name,
                "session_id": r.session_id,
                "enabled_tools": r.enabled_tools,
                "agent_cache_outcomes": r.agent_cache_outcomes,
                "turns": [t.__dict__ for t in r.turns],
            }
            for r in results
        ]
        with open(args.json_out, "w") as fh:
            json.dump(payload, fh, indent=2)
        logger.info("wrote %s", args.json_out)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
