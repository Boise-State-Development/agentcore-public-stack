"""Agent-build A/B: does sharing Memory clients / building off the loop help a first turn?

Drives real first turns through the deployed AgentCore Runtime and compares the
three arms of ``agent_build_experiment_arm`` (``apis/shared/feature_flags.py``):

    control                  today's build
    shared_clients           AgentCore Memory session managers share boto3 clients
    shared_clients_off_loop  that, plus the synchronous build runs in a worker thread

**Precondition:** the Runtime must have ``AGENT_BUILD_EXPERIMENT=ab``. Arms are
assigned server-side by hashing the session id, so this script generates session
ids until each lands in the arm it wants, then runs the arms interleaved (one of
each per round) so network drift over the run hits every arm alike. Every
conversation runs in its own Runtime process, which is exactly the first-turn,
cold-process build under test (``processBuilds`` = 1 on ``turn_prelude``).

Two sources per turn, joined on session id:

- **Client side** (this script, via ``on_event``): when ``preparing``,
  ``prepared``, ``session_title``, the first ``content_block_delta`` and
  ``done`` arrived, measured from the request.
- **Server side** (``turn_prelude`` in the Runtime log group): the build's
  sub-stages, ``buildArm`` and ``processBuilds``.

What it establishes: per-arm medians for the build and its sub-stages, time to
first token, and whether the title beats ``prepared``. What it cannot: fleet
magnitude, or behaviour under concurrent load. Report which claim you make.

Each turn is a real conversation owned by ``--user-id``: it draws on that user's
quota and appears in their sidebar. Use ``--cleanup`` to soft-delete the
experiment's sessions afterwards.

Usage (an authenticated dev-ai profile and an active headless grant for
--user-id; see apis/shared/harness/grants.py):

    cd backend
    AWS_PROFILE=dev-ai uv run python scripts/experiment_agent_build_arms.py \\
        --user-id <sub> --per-arm 15
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import statistics
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("experiment")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

ARMS = ("control", "shared_clients", "shared_clients_off_loop")

# Frames whose first arrival is recorded, keyed by how the table names them.
_TIMED = ("preparing", "prepared", "session_title", "first_token", "done")


@dataclass
class Turn:
    arm: str
    session_id: str
    ok: bool = False
    error: Optional[str] = None
    client_ms: Dict[str, int] = field(default_factory=dict)
    prelude: Dict[str, Any] = field(default_factory=dict)


def session_for(arm: str) -> str:
    """A fresh session id that the server's ``ab`` hash puts in ``arm``."""
    from apis.shared.feature_flags import agent_build_experiment_arm

    previous = os.environ.get("AGENT_BUILD_EXPERIMENT")
    os.environ["AGENT_BUILD_EXPERIMENT"] = "ab"
    try:
        while True:
            candidate = str(uuid.uuid4())
            if agent_build_experiment_arm(candidate) == arm:
                return candidate
    finally:
        if previous is None:
            os.environ.pop("AGENT_BUILD_EXPERIMENT", None)
        else:
            os.environ["AGENT_BUILD_EXPERIMENT"] = previous


async def run_turn(*, arm: str, user_id: str, prompt: str, auth: Any, model_id: Optional[str]) -> Turn:
    from apis.shared.harness import run_agent_headless

    turn = Turn(arm=arm, session_id=session_for(arm))
    started = time.monotonic()

    def stamp(key: str) -> None:
        turn.client_ms.setdefault(key, int((time.monotonic() - started) * 1000))

    async def on_event(name: str, data: Dict[str, Any]) -> None:
        if name == "agent_status" and data.get("phase") in ("preparing", "prepared"):
            stamp(data["phase"])
        elif name == "session_title":
            stamp("session_title")
        elif name == "content_block_delta":
            stamp("first_token")
        elif name == "done":
            stamp("done")

    try:
        run = await run_agent_headless(
            user_id=user_id,
            prompt=prompt,
            auth=auth,
            session_id=turn.session_id,
            model_id=model_id,
            trigger="experiment",
            on_event=on_event,
        )
        turn.ok = run.status == "completed"
        turn.error = None if turn.ok else (run.error or run.status)
    except Exception as exc:  # noqa: BLE001 - one bad turn must not end the run
        turn.error = str(exc)[:200]
    logger.info(
        "   %-24s %s %s", arm, turn.session_id[:8],
        "ok" if turn.ok else f"FAILED: {turn.error}",
    )
    return turn


def attach_preludes(turns: List[Turn], log_group: str, region: str, since: float) -> None:
    """Join each turn to its ``turn_prelude`` line by session id."""
    import boto3

    logs = boto3.client("logs", region_name=region)
    by_session = {t.session_id: t for t in turns}
    query = logs.start_query(
        logGroupName=log_group,
        startTime=int(since) - 60,
        endTime=int(time.time()) + 60,
        queryString="fields body | filter body like /turn_prelude/ | limit 10000",
    )["queryId"]
    while True:
        result = logs.get_query_results(queryId=query)
        if result["status"] in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        time.sleep(2)
    for row in result.get("results", []):
        body = next((f["value"] for f in row if f["field"] == "body"), "")
        match = re.search(r"turn_prelude (\{.*\})", body)
        if not match:
            continue
        prelude = json.loads(match.group(1))
        turn = by_session.get(prelude.get("sessionId"))
        if turn is not None:
            turn.prelude = prelude


def _median(values: List[float]) -> str:
    return f"{statistics.median(values):.0f}" if values else "-"


def _p75(values: List[float]) -> str:
    if len(values) < 4:
        return "-"
    return f"{statistics.quantiles(values, n=4)[2]:.0f}"


def report(turns: List[Turn]) -> None:
    def stage(t: Turn, name: str) -> Optional[float]:
        return t.prelude.get("stages", {}).get(name)

    rows = [
        ("agent_build (group)", lambda t: t.prelude.get("groups", {}).get("agent_build")),
        ("  session_mgr_clients", lambda t: stage(t, "agent_build.session_mgr_clients")),
        ("  session_mgr (network)", lambda t: stage(t, "agent_build.session_mgr")),
        ("  strands_agent", lambda t: stage(t, "agent_build.strands_agent")),
        ("  finalize (restore)", lambda t: stage(t, "agent_build.finalize")),
        ("prelude total", lambda t: t.prelude.get("totalMs")),
        ("client: prepared", lambda t: t.client_ms.get("prepared")),
        ("client: first token", lambda t: t.client_ms.get("first_token")),
        ("client: session_title", lambda t: t.client_ms.get("session_title")),
        (
            "title minus prepared",
            lambda t: (t.client_ms["session_title"] - t.client_ms["prepared"])
            if "session_title" in t.client_ms and "prepared" in t.client_ms else None,
        ),
    ]

    print()
    header = f"{'metric (ms)':26s}" + "".join(f"{arm:>28s}" for arm in ARMS)
    print(header)
    print(f"{'':26s}" + "".join(f"{'median / p75':>28s}" for _ in ARMS))
    for label, fn in rows:
        cells = []
        for arm in ARMS:
            values = [v for t in turns if t.arm == arm and t.ok and (v := fn(t)) is not None]
            cells.append(f"{_median(values)} / {_p75(values)} (n={len(values)})")
        print(f"{label:26s}" + "".join(f"{c:>28s}" for c in cells))

    print()
    for arm in ARMS:
        mine = [t for t in turns if t.arm == arm]
        matched = [t for t in mine if t.prelude]
        mislabeled = [t for t in matched if t.prelude.get("buildArm") != arm]
        warm = [t for t in matched if t.prelude.get("processBuilds") not in (1, None)]
        early = [
            t for t in mine
            if "session_title" in t.client_ms and "prepared" in t.client_ms
            and t.client_ms["session_title"] < t.client_ms["prepared"]
        ]
        print(
            f"{arm:24s} turns={len(mine)} ok={sum(t.ok for t in mine)} "
            f"prelude-matched={len(matched)} arm-mismatch={len(mislabeled)} "
            f"not-first-build={len(warm)} title-before-prepared={len(early)}"
        )
    if any(t.prelude and t.prelude.get("buildArm") == "control" for t in turns if t.arm != "control"):
        print("\n⚠️  Non-control turns reported buildArm=control: is AGENT_BUILD_EXPERIMENT=ab set on the Runtime?")


def cleanup(turns: List[Turn], user_id: str) -> None:
    """Soft-delete the experiment's conversations from the user's sidebar."""
    import boto3

    table = boto3.resource("dynamodb", region_name=os.environ["AWS_REGION"]).Table(
        os.environ["DYNAMODB_SESSIONS_METADATA_TABLE_NAME"]
    )
    removed = 0
    for turn in turns:
        try:
            table.update_item(
                Key={"PK": f"USER#{user_id}", "SK": f"S#{turn.session_id}"},
                UpdateExpression="SET #s = :deleted REMOVE GSI4_PK, GSI4_SK",
                ConditionExpression="attribute_exists(PK)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":deleted": "deleted"},
            )
            removed += 1
        except Exception as exc:  # noqa: BLE001 - best effort
            logger.warning("cleanup %s: %s", turn.session_id, exc)
    logger.info("soft-deleted %d/%d experiment sessions", removed, len(turns))


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--per-arm", type=int, default=15)
    parser.add_argument("--gap-seconds", type=float, default=3.0)
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--prefix", default="dev-boisestateai-v2")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--out", default=None, help="write raw per-turn JSON here")
    parser.add_argument("--cleanup", action="store_true", help="soft-delete the sessions afterwards")
    args = parser.parse_args()

    from spike_headless_run import resolve_environment
    from apis.shared.harness.auth import CognitoRefreshBearerAuth

    env = resolve_environment(args.prefix, args.region)
    runtime_id = env["runtime_arn"].rsplit("/", 1)[1]
    log_group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    auth = CognitoRefreshBearerAuth()

    since = time.time()
    turns: List[Turn] = []
    for round_index in range(args.per_arm):
        order = list(ARMS)
        # Rotate the order each round so no arm always runs first.
        order = order[round_index % len(order):] + order[: round_index % len(order)]
        logger.info("── round %d/%d", round_index + 1, args.per_arm)
        for arm in order:
            prompt = f"In one short sentence, name a river in country number {round_index + 1} of Africa, alphabetically."
            turns.append(await run_turn(arm=arm, user_id=args.user_id, prompt=prompt, auth=auth, model_id=args.model_id))
            await asyncio.sleep(args.gap_seconds)

    logger.info("waiting 45s for turn_prelude lines to reach CloudWatch…")
    await asyncio.sleep(45)
    attach_preludes(turns, log_group, args.region, since)
    report(turns)

    if args.out:
        with open(args.out, "w") as fh:
            json.dump([asdict(t) for t in turns], fh, indent=2)
        logger.info("raw results: %s", args.out)
    if args.cleanup:
        cleanup(turns, args.user_id)


if __name__ == "__main__":
    asyncio.run(main())
