"""Dev-ai driver for the headless run-entrypoint spike (F1).

Runs `apis.shared.harness.run_agent_headless` from a laptop against the
deployed dev-ai AgentCore Runtime — through the runtime gateway, exactly the
path a scheduler worker would take. See
docs/specs/harness-entrypoint-spike-findings.md.

Usage (requires an authenticated AWS profile for the dev-ai account):

    cd backend
    AWS_PROFILE=dev-ai uv run python scripts/spike_headless_run.py \
        --user-id <cognito-sub> \
        --prompt "Find 3-credit undergraduate communication classes" \
        --tools class_search

    # Negative probes for the record (gateway auth evidence):
    AWS_PROFILE=dev-ai uv run python scripts/spike_headless_run.py \
        --user-id <cognito-sub> --probe-workload-token --probe-sigv4

The script resolves all names from SSM / naming conventions for --prefix
(default dev-boisestateai-v2), exports the env vars the shared harness
expects, runs the turn, then reads back the session-metadata row and the
RUN# audit record as delivery proof.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import urllib.parse
import uuid

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("spike")


def resolve_environment(prefix: str, region: str) -> dict:
    """Resolve dev-ai names from SSM + conventions and export harness env."""
    import boto3

    ssm = boto3.client("ssm", region_name=region)
    sts = boto3.client("sts", region_name=region)
    account = sts.get_caller_identity()["Account"]

    runtime_id = ssm.get_parameter(Name=f"/{prefix}/inference-api/runtime-id")[
        "Parameter"
    ]["Value"]
    runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}"
    client_id = ssm.get_parameter(Name=f"/{prefix}/auth/cognito/bff-app-client-id")[
        "Parameter"
    ]["Value"]

    env = {
        "AWS_REGION": region,
        "INFERENCE_API_URL": (
            f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{runtime_arn}"
        ),
        "BFF_SESSIONS_TABLE_NAME": f"{prefix}-bff-sessions",
        "COGNITO_BFF_APP_CLIENT_ID": client_id,
        "COGNITO_BFF_APP_CLIENT_SECRET_ARN": f"{prefix}-cognito-bff-app-client-secret",
        "DYNAMODB_SESSIONS_METADATA_TABLE_NAME": f"{prefix}-sessions-metadata",
    }
    os.environ.update(env)
    return {"runtime_arn": runtime_arn, "account": account, **env}


def probe_workload_token(prefix: str, region: str, runtime_arn: str, user_id: str) -> None:
    """Unknown-1 'try first' path — recorded evidence: the gateway rejects it."""
    import boto3
    import httpx

    client = boto3.client("bedrock-agentcore", region_name=region)
    token = client.get_workload_access_token_for_user_id(
        workloadName=f"{prefix}-platform-workload", userId=user_id
    )["workloadAccessToken"]
    is_jwt = token.count(".") == 2
    logger.info("workload token minted (len=%d, jwt=%s)", len(token), is_jwt)

    encoded = urllib.parse.quote(runtime_arn, safe="")
    url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
        f"{encoded}/invocations?qualifier=DEFAULT"
    )
    r = httpx.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={"session_id": f"probe-{uuid.uuid4().hex[:8]}", "message": "ping"},
        timeout=30,
    )
    logger.info("PROBE workload-token bearer -> HTTP %d %s", r.status_code, r.text[:200])


def probe_sigv4(region: str, runtime_arn: str) -> None:
    """IAM data-plane call — recorded evidence: authorizer-method mismatch."""
    import boto3

    client = boto3.client("bedrock-agentcore", region_name=region)
    try:
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier="DEFAULT",
            runtimeSessionId=f"probe-sigv4-{uuid.uuid4().hex}",
            contentType="application/json",
            accept="text/event-stream",
            payload=json.dumps(
                {"session_id": f"probe-{uuid.uuid4().hex[:8]}", "message": "ping"}
            ).encode(),
        )
        logger.info("PROBE sigv4 -> statusCode=%s", resp.get("statusCode"))
    except Exception as exc:
        logger.info("PROBE sigv4 -> %s: %s", type(exc).__name__, exc)


def verify_delivery(prefix: str, region: str, user_id: str, session_id: str, run_id: str) -> None:
    """Read back the session row + audit record as F2/F6a proof."""
    import boto3
    from boto3.dynamodb.conditions import Key

    table = boto3.resource("dynamodb", region_name=region).Table(
        f"{prefix}-sessions-metadata"
    )
    rows = table.query(
        IndexName="SessionLookupIndex",
        KeyConditionExpression=Key("GSI_PK").eq(f"SESSION#{session_id}"),
    )["Items"]
    meta = [r for r in rows if str(r.get("SK", "")).startswith("S#")]
    messages = [r for r in rows if str(r.get("GSI_SK", "")).startswith("C#")]
    logger.info(
        "DELIVERY session row: %s",
        json.dumps(
            {
                k: str(v)
                for k, v in (meta[0] if meta else {}).items()
                if k in ("title", "status", "messageCount", "lastModel", "SK")
            }
        ),
    )
    logger.info("DELIVERY persisted message items: %d", len(messages))

    audit = table.get_item(
        Key={"PK": f"USER#{user_id}", "SK": f"RUN#{run_id}"}
    ).get("Item")
    logger.info(
        "AUDIT record: %s",
        json.dumps({k: str(v) for k, v in (audit or {}).items()}, sort_keys=True)[:600],
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="dev-boisestateai-v2")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--user-id", required=True, help="Cognito sub of the run owner")
    parser.add_argument("--prompt", default="Reply with the single word: pong")
    parser.add_argument(
        "--tools",
        default=None,
        help="Comma-separated enabled_tools (omit for the user's defaults)",
    )
    parser.add_argument("--title", default=None, help="Explicit session title")
    parser.add_argument("--probe-workload-token", action="store_true")
    parser.add_argument("--probe-sigv4", action="store_true")
    parser.add_argument("--skip-run", action="store_true")
    args = parser.parse_args()

    resolved = resolve_environment(args.prefix, args.region)
    logger.info("runtime: %s", resolved["runtime_arn"])

    if args.probe_workload_token:
        probe_workload_token(
            args.prefix, args.region, resolved["runtime_arn"], args.user_id
        )
    if args.probe_sigv4:
        probe_sigv4(args.region, resolved["runtime_arn"])
    if args.skip_run:
        return 0

    # Import after env export — the harness reads configuration from env.
    from apis.shared.harness import CognitoRefreshBearerAuth, run_agent_headless

    async def on_event(name: str, data: dict) -> None:
        if name in ("tool_use", "tool_result", "session_title", "stream_error"):
            logger.info("SSE %s: %s", name, json.dumps(data, default=str)[:220])

    result = await run_agent_headless(
        user_id=args.user_id,
        prompt=args.prompt,
        auth=CognitoRefreshBearerAuth(),
        enabled_tools=args.tools.split(",") if args.tools else None,
        agent_type="chat",
        trigger="spike",
        title=args.title,
        on_event=on_event,
    )

    print("\n================ RunResult ================")
    print(json.dumps(result.to_dict(), indent=2, default=str)[:4000])
    print("===========================================\n")

    verify_delivery(
        args.prefix, args.region, args.user_id, result.session_id, result.run_id
    )
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
