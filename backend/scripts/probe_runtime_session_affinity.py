"""Does pinning the runtime session id give the agent cache microVM affinity?

The #834 G1 arm experiment found the in-process agent cache never hits in
cloud — not even for sessions that were always eligible. The suspected cause
is that nothing forwards ``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id``, so
AWS assigns a fresh runtime session per invocation and consecutive turns can
land on different microVMs, where a process-local cache is cold by definition.

This probe settles it with a two-arm A/B that differs by exactly one header:

    unpinned   no session-id header   (reproduces today's behavior)
    pinned     same session-id header on every turn

If the pinned arm shows hits and the unpinned arm doesn't, affinity is
achievable and session-id forwarding is a prerequisite for #834 delivering
anything. If neither hits, the agent cache is dead in this architecture and
narrowing the bypass predicate cannot help regardless.

Bypasses ``run_agent_headless`` deliberately: that helper sends only
Content-Type and Authorization, and adding a header to shared code before we
know whether it helps would be building the fix to test the hypothesis.

Usage:

    cd backend
    AWS_PROFILE=dev-ai uv run python scripts/probe_runtime_session_affinity.py \
        --user-id <cognito-sub> --turns 4
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
import uuid
from typing import Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("probe")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# AgentCore requires a runtime session id of at least 33 characters.
RUNTIME_SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


async def run_turn(
    *,
    url: str,
    bearer: str,
    session_id: str,
    prompt: str,
    enabled_tools: List[str],
    runtime_session_id: Optional[str],
    timeout: float = 180.0,
) -> str:
    """POST one turn, drain the SSE stream, return 'ok' or an error string."""
    import httpx

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {bearer}",
    }
    if runtime_session_id is not None:
        headers[RUNTIME_SESSION_HEADER] = runtime_session_id

    payload = {
        "session_id": session_id,
        "message": prompt,
        "enabled_tools": enabled_tools,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                return f"HTTP {resp.status_code}: {body.decode('utf-8', 'replace')[:300]}"
            async for _ in resp.aiter_lines():
                pass
    return "ok"


def read_outcomes(log_group: str, region: str, session_id: str, since_epoch: int) -> List[str]:
    """agent_cache hit/miss for one session, deduped (the runtime double-logs)."""
    import boto3

    logs = boto3.client("logs", region_name=region)
    try:
        resp = logs.filter_log_events(
            logGroupName=log_group,
            startTime=since_epoch * 1000,
            filterPattern=f'"agent_cache outcome=" "{session_id}"',
            limit=200,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("log lookup failed: %s", exc)
        return []

    outcomes: List[str] = []
    seen_ts: set = set()
    for event in sorted(resp.get("events", []), key=lambda e: e.get("timestamp", 0)):
        ts = event.get("timestamp")
        if ts in seen_ts:
            continue
        for token in event.get("message", "").split():
            if token.startswith("outcome="):
                seen_ts.add(ts)
                outcomes.append(token.split("=", 1)[1])
                break
    return outcomes


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--prefix", default="dev-boisestateai-v2")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--turns", type=int, default=4)
    parser.add_argument("--gap-seconds", type=float, default=15.0)
    parser.add_argument("--priming-chars", type=int, default=20000)
    args = parser.parse_args()

    from spike_headless_run import resolve_environment
    from experiment_agent_cache_arms import build_priming_prompt, runtime_log_group, TURN_PROMPTS

    resolved = resolve_environment(args.prefix, args.region)
    log_group = runtime_log_group(resolved["runtime_arn"])
    logger.info("runtime: %s", resolved["runtime_arn"])

    from apis.shared.harness.auth import CognitoRefreshBearerAuth
    from apis.shared.harness.runner import build_invocations_url

    url = build_invocations_url(os.environ["INFERENCE_API_URL"])
    bearer = await CognitoRefreshBearerAuth().mint_bearer_for_user(args.user_id)
    logger.info("bearer minted")

    started_epoch = int(time.time()) - 60
    # create_artifact: the family arm 1 promoted, so `cacheable=True` and the
    # only thing left that can prevent a hit is the process being cold.
    tools = ["create_artifact"]

    results: Dict[str, Dict] = {}
    for arm in ("unpinned", "pinned"):
        session_id = f"aff-{arm}-{uuid.uuid4().hex[:12]}"
        runtime_session_id = (
            f"pinned-{uuid.uuid4().hex}" if arm == "pinned" else None  # 39 chars
        )
        logger.info(
            "── arm %s · session %s · runtime-session-id=%s",
            arm, session_id, runtime_session_id or "(none — AWS assigns per call)",
        )
        statuses = []
        for i in range(args.turns):
            prompt = (
                build_priming_prompt(args.priming_chars) if i == 0
                else TURN_PROMPTS[i % len(TURN_PROMPTS)]
            )
            started = time.monotonic()
            status = await run_turn(
                url=url, bearer=bearer, session_id=session_id, prompt=prompt,
                enabled_tools=tools, runtime_session_id=runtime_session_id,
            )
            statuses.append(status)
            logger.info("   turn %d/%d %s (%.1fs)", i + 1, args.turns, status[:80],
                        time.monotonic() - started)
            if i < args.turns - 1:
                await asyncio.sleep(args.gap_seconds)
        results[arm] = {"session_id": session_id, "statuses": statuses}

    logger.info("waiting for log ingestion…")
    await asyncio.sleep(35)

    print("\n" + "=" * 74)
    print("RUNTIME SESSION-ID AFFINITY PROBE")
    print("=" * 74)
    for arm, data in results.items():
        outcomes = read_outcomes(log_group, args.region, data["session_id"], started_epoch)
        hits = sum(1 for o in outcomes if o == "hit")
        ok = sum(1 for s in data["statuses"] if s == "ok")
        print(f"\n▸ {arm:9} session={data['session_id']}")
        print(f"  turns ok:    {ok}/{len(data['statuses'])}")
        print(f"  agent_cache: {'/'.join(outcomes) or '(no lines found)'}")
        print(f"  hits:        {hits} of {len(outcomes)}")
        for s in data["statuses"]:
            if s != "ok":
                print(f"  error: {s[:200]}")

    print("\n" + "-" * 74)
    print("If pinned shows hits and unpinned does not, affinity is achievable and")
    print("session-id forwarding is a prerequisite for #834. If neither hits, the")
    print("in-process agent cache cannot work in this architecture.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
