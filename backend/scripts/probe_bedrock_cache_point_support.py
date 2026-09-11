"""Which Bedrock model families actually accept an explicit cachePoint?

`ModelConfig.bedrock_cache_points_supported()` answers "Anthropic only", because
it mirrors what *Strands* recognizes (`BedrockModel._cache_strategy`). That is
not the same question as what *Bedrock* accepts, and the two have already
diverged — Nova Micro honors a system cachePoint that our predicate refuses.
More families will gain prompt caching, and nothing in CI can notice: the
predicate is a string test, so it keeps returning the same answer as the
platform moves underneath it.

This is the measurement that closes that gap. It reports two independent
layers, and the interesting rows are where they disagree:

  1. OFFLINE — what the pinned strands-agents emits for a model id. No AWS
     calls. Answers "does upstream place a tools point / auto-inject a system
     point", and "does upstream strip a hand-placed system point" (it does not,
     which is the whole reason our gate exists).
  2. LIVE — whether Bedrock accepts a request carrying an explicit system
     cachePoint, and whether it actually caches. A model that fails answers
     `AccessDeniedException` ("You invoked an unsupported model or your request
     did not allow prompt caching"), NOT the ValidationException our comments
     claimed until 2026-09-11.

Read-only apart from the model invocations themselves. Nothing is written to
DynamoDB, the catalog, or the agent loop; each probe is its own boto3
`converse` call with `maxTokens: 5`.

⚠️ Real spend, but small: two calls per model at ~7.9k input tokens (the prefix
has to clear the largest cache minimum — see _SYSTEM_TEXT). The script prints an
estimate and totals actual tokens at the end. A model the account cannot invoke
at all is reported as SKIPPED, distinct from a model that refuses the cache
point.

Usage:

    cd backend
    AWS_PROFILE=dev-ai uv run python scripts/probe_bedrock_cache_point_support.py

    # offline layer only — no AWS calls, no spend
    uv run python scripts/probe_bedrock_cache_point_support.py --offline-only

    # check a newly announced family
    AWS_PROFILE=dev-ai uv run python scripts/probe_bedrock_cache_point_support.py \
        --model-id us.amazon.nova-premier-v1:0 --model-id us.writer.palmyra-x5-v1:0

Baseline, us-west-2, 2026-09-11, strands-agents 1.55.0:

    us.anthropic.claude-haiku-4-5   strands=anthropic  ACCEPTS+CACHES
    us.amazon.nova-micro-v1:0       strands=None       ACCEPTS+CACHES  <- divergence
    us.meta.llama3-3-70b            strands=None       REFUSED (AccessDenied)
    mistral.mistral-large-2407      strands=None       REFUSED (AccessDenied)
    us.deepseek.r1-v1:0             strands=None       REFUSED (AccessDenied)
"""

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import boto3
import botocore.exceptions

# Default sweep: one model known to cache (the control), one known divergence,
# and three known refusals. Add families here as they gain caching.
DEFAULT_MODEL_IDS = [
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.amazon.nova-micro-v1:0",
    "us.meta.llama3-3-70b-instruct-v1:0",
    "mistral.mistral-large-2407-v1:0",
    "us.deepseek.r1-v1:0",
]

# Sized well over every model's cache minimum, because BELOW that minimum
# Bedrock silently ignores the cache point — no error, no cache buckets, a
# result indistinguishable from "unsupported". Measured on Haiku 4.5 in
# us-west-2 (2026-09-11): 2,351 and 3,911 tokens both wrote NOTHING, 5,202
# wrote in full, bracketing the real floor at 4,096 — twice the 2,048 the
# first-party Anthropic docs give for Haiku. Do not shrink this to save
# pennies; an undersized prefix turns every row into a false negative.
_FILLER = "You are a helpful assistant operating inside a controlled test harness. "
_SYSTEM_TEXT = _FILLER * 600
_APPROX_PROMPT_TOKENS = 7_900
_MESSAGES = [{"role": "user", "content": [{"text": "Say OK."}]}]


@dataclass
class ProbeResult:
    """One model's answer from both layers."""

    model_id: str
    strands_strategy: Optional[str] = None
    strands_emits_tools_point: bool = False
    strands_autoinjects_system_point: bool = False
    strands_strips_placed_point: bool = False
    live_status: str = "not run"
    live_detail: str = ""
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    input_tokens_uncached: int = 0
    input_tokens_cached: int = 0
    tokens_billed: int = 0
    notes: List[str] = field(default_factory=list)


def probe_offline(model_id: str, result: ProbeResult) -> None:
    """Ask the pinned SDK what it would put on the wire. No AWS calls."""
    from strands.models import BedrockModel, CacheConfig

    tool_specs = [{"name": "t", "description": "d", "inputSchema": {"json": {"type": "object"}}}]
    model = BedrockModel(
        model_id=model_id,
        cache_config=CacheConfig(strategy="auto", system_prompt_ttl=True, tools_ttl=True),
        region_name="us-west-2",
    )
    result.strands_strategy = model._cache_strategy

    plain = model.format_request(_MESSAGES, tool_specs, system_prompt_content=[{"text": "SYSTEM"}])
    result.strands_emits_tools_point = any("cachePoint" in t for t in plain["toolConfig"]["tools"])
    result.strands_autoinjects_system_point = any("cachePoint" in b for b in plain["system"])

    # The load-bearing question: does upstream filter a point WE placed?
    placed = model.format_request(
        _MESSAGES,
        tool_specs,
        system_prompt_content=[{"text": "SYSTEM"}, {"cachePoint": {"type": "default"}}],
    )
    survivors = [b for b in placed["system"] if "cachePoint" in b]
    result.strands_strips_placed_point = not survivors
    if len(survivors) > 1:
        result.notes.append(f"DOUBLED system cachePoint ({len(survivors)}) — invariant broken")


def probe_live(client: Any, model_id: str, result: ProbeResult) -> None:
    """Send a real request with, and without, an explicit system cachePoint."""
    try:
        control = client.converse(
            modelId=model_id,
            system=[{"text": _SYSTEM_TEXT}],
            messages=_MESSAGES,
            inferenceConfig={"maxTokens": 5},
        )
    except botocore.exceptions.ClientError as exc:
        result.live_status = "SKIPPED"
        result.live_detail = f"control call failed: {exc.response['Error']['Code']}"
        return

    result.input_tokens_uncached = control["usage"]["inputTokens"]
    result.tokens_billed += control["usage"]["totalTokens"]

    try:
        cached = client.converse(
            modelId=model_id,
            system=[{"text": _SYSTEM_TEXT}, {"cachePoint": {"type": "default"}}],
            messages=_MESSAGES,
            inferenceConfig={"maxTokens": 5},
        )
    except botocore.exceptions.ClientError as exc:
        error = exc.response["Error"]
        result.live_status = "REFUSED"
        result.live_detail = f"{error['Code']}: {error['Message'][:110]}"
        return

    usage = cached["usage"]
    result.tokens_billed += usage["totalTokens"]
    result.input_tokens_cached = usage["inputTokens"]
    result.cache_write_tokens = usage.get("cacheWriteInputTokens", 0)
    result.cache_read_tokens = usage.get("cacheReadInputTokens", 0)

    if result.cache_write_tokens or result.cache_read_tokens:
        result.live_status = "ACCEPTS+CACHES"
    else:
        # Accepted the block but reported no cache buckets. Two different
        # worlds, and the API does not distinguish them: either the model
        # ignores cache points, or this prefix is under that model's minimum.
        # Rule the second out before believing the first.
        result.live_status = "ACCEPTS (no cache)"
        result.notes.append(
            f"accepted the cache point but cached nothing at {result.input_tokens_cached:,} "
            "tokens — re-run with a larger prefix before concluding it is unsupported"
        )


def print_report(results: List[ProbeResult], offline_only: bool) -> None:
    """Render both layers and call out every divergence."""
    print("\n" + "=" * 96)
    print("OFFLINE — what strands-agents puts on the wire")
    print("=" * 96)
    print(f"{'model_id':<46}{'_cache_strategy':<18}{'tools pt':<11}{'our sys pt survives':<20}")
    for r in results:
        survives = "no (stripped)" if r.strands_strips_placed_point else "YES"
        print(
            f"{r.model_id:<46}{str(r.strands_strategy):<18}"
            f"{('yes' if r.strands_emits_tools_point else 'no'):<11}{survives:<20}"
        )

    if offline_only:
        return

    print("\n" + "=" * 96)
    print("LIVE — what Bedrock does with an explicit system cachePoint")
    print("=" * 96)
    # Both buckets, because a repeat run READS the entry the previous run wrote
    # — a zero in cacheWrite next to a non-zero cacheRead is a hit, not a miss.
    print(f"{'model_id':<46}{'verdict':<20}{'input (ctl->cached)':<22}{'cacheWrite':<12}{'cacheRead':<11}")
    for r in results:
        movement = f"{r.input_tokens_uncached} -> {r.input_tokens_cached}" if r.input_tokens_cached else "-"
        print(
            f"{r.model_id:<46}{r.live_status:<20}{movement:<22}"
            f"{str(r.cache_write_tokens or '-'):<12}{str(r.cache_read_tokens or '-'):<11}"
        )
        if r.live_detail:
            print(f"{'':<46}{r.live_detail}")

    print("\n" + "=" * 96)
    print("DIVERGENCE — Bedrock caches it, our predicate refuses it")
    print("=" * 96)
    divergent = [
        r for r in results if r.live_status == "ACCEPTS+CACHES" and r.strands_strategy != "anthropic"
    ]
    if not divergent:
        print("  none — upstream's 'anthropic only' test still matches what Bedrock accepts.")
    else:
        for r in divergent:
            saved = r.input_tokens_uncached - r.input_tokens_cached
            print(
                f"  {r.model_id}: caches {r.cache_write_tokens} tokens "
                f"({saved} fewer billed as fresh input), but _cache_strategy is "
                f"{r.strands_strategy!r} so neither we nor Strands will place a point."
            )
        print(
            "\n  Widening bedrock_cache_points_supported() to cover these means widening\n"
            "  past upstream. Re-read its docstring before changing it."
        )

    for r in results:
        for note in r.notes:
            print(f"\n⚠️  {r.model_id}: {note}")

    total = sum(r.tokens_billed for r in results)
    print(f"\nTokens billed by this run: {total:,}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-id",
        action="append",
        dest="model_ids",
        help="Model id to probe; repeatable. Replaces the default sweep.",
    )
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument(
        "--offline-only",
        action="store_true",
        help="Only ask the pinned SDK what it emits. No AWS calls, no spend.",
    )
    args = parser.parse_args()

    model_ids = args.model_ids or DEFAULT_MODEL_IDS
    results = [ProbeResult(model_id=m) for m in model_ids]

    for result in results:
        probe_offline(result.model_id, result)

    if not args.offline_only:
        estimate = len(model_ids) * 2 * _APPROX_PROMPT_TOKENS
        print(f"Probing {len(model_ids)} models in {args.region} (~{estimate:,} input tokens).")
        client = boto3.client("bedrock-runtime", region_name=args.region)
        for result in results:
            probe_live(client, result.model_id, result)

    print_report(results, args.offline_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
