"""Scheduled time-to-first-token probe (docs/specs/turn-path-ttft.md §6E).

The client metric (``routes.py``) records what real users wait, which moves with
traffic mix: models, agents, attachments, how many conversations are new. A
baseline worth alarming on needs a turn that never changes, so this runs one
fixed shape on a schedule, as one dedicated user, and records:

- ``TtftProbeColdFirstTokenMs``: a new conversation's first turn, no prewarm;
- ``TtftProbeFollowUpFirstTokenMs``: a second turn on that conversation;
- ``TtftProbeWarmedFirstTokenMs``: a new conversation's first turn sent
  ``PROBE_THINK_SECONDS`` after a session prewarm;
- ``TtftProbeWarmCallMs``: how long that warm call took (the session start);
- ``TtftProbeRuns`` (1 per complete run) or ``TtftProbeFailures``.

The clock is this task's: from the request leaving to the first
``content_block_delta`` arriving, through the Runtime's public data plane, as
an SPA's request travels after app-api. It skips the browser and the app-api
hop, which the client metric covers.

Runs as a one-off Fargate task on app-api's own task definition
(``infrastructure/lib/constructs/observability/ttft-probe-construct.ts``), with
app-api's role: minting the probe user's bearer reads the BFF sessions table and
app-client secret, which app-api already can. The probe user needs an active
headless grant (sign in, enable headless runs; it lasts up to 30 days from that
sign-in), and its turns count against that user's quota. Sessions the probe
creates are soft-deleted at the end of every run.

``python -m apis.app_api.telemetry.ttft_probe`` reads ``TTFT_PROBE_USER_ID`` and
optionally ``TTFT_PROBE_MODEL_ID`` (unset means the user's default model, which
moves when the default does; pin one for a stable baseline).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

from apis.shared.harness.auth import CognitoRefreshBearerAuth
from apis.shared.harness.runner import apply_runtime_session_header, build_invocations_url
from apis.shared.observability.emf import emit_emf_metrics

logger = logging.getLogger("ttft_probe")

TURN_LATENCY_NAMESPACE = "AgentCoreStack/TurnLatency"
PROBE_PROMPT = "Reply with exactly the single word PONG and nothing else."
PROBE_SESSION_PREFIX = "ttftprobe-"
PROBE_THINK_SECONDS = 5.0
PROBE_TURN_TIMEOUT_SECONDS = 120.0


@dataclass
class ProbeResult:
    """One run's measurements, in milliseconds. ``None`` where that step failed."""

    cold_ms: Optional[int] = None
    follow_up_ms: Optional[int] = None
    warmed_ms: Optional[int] = None
    warm_call_ms: Optional[int] = None
    errors: List[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.errors and None not in (self.cold_ms, self.follow_up_ms, self.warmed_ms, self.warm_call_ms)


async def stream_first_token(
    client: httpx.AsyncClient, url: str, headers: Dict[str, str], payload: Dict[str, object]
) -> Tuple[Optional[int], Optional[str]]:
    """POST a turn and return (ms to the first ``content_block_delta``, error).

    Reads the stream to its end, so the turn completes and persists normally
    before the session is deleted.
    """
    started = time.monotonic()
    first: Optional[int] = None
    try:
        async with client.stream("POST", url, json=payload, headers=headers, timeout=PROBE_TURN_TIMEOUT_SECONDS) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")[:200]
                return None, f"HTTP {response.status_code}: {body}"
            async for line in response.aiter_lines():
                if first is None and line.strip() == "event: content_block_delta":
                    first = int((time.monotonic() - started) * 1000)
    except httpx.HTTPError as exc:
        return first, f"{type(exc).__name__}: {exc}"
    if first is None:
        return None, "the turn produced no content_block_delta"
    return first, None


async def warm(client: httpx.AsyncClient, url: str, headers: Dict[str, str], session_id: str) -> Tuple[Optional[int], Optional[str]]:
    """Send the session-prewarm invocation and time it."""
    started = time.monotonic()
    try:
        response = await client.post(url, json={"session_id": session_id, "warm": True}, headers=headers, timeout=PROBE_TURN_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        return None, f"warm: {type(exc).__name__}: {exc}"
    if response.status_code >= 400:
        return None, f"warm: HTTP {response.status_code}"
    return int((time.monotonic() - started) * 1000), None


def _headers(bearer: str, session_id: str, user_id: str) -> Dict[str, str]:
    return apply_runtime_session_header(
        {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"}, session_id, user_id
    )


def _payload(session_id: str, model_id: Optional[str]) -> Dict[str, object]:
    payload: Dict[str, object] = {"session_id": session_id, "message": PROBE_PROMPT, "enabled_tools": []}
    if model_id:
        payload["model_id"] = model_id
    return payload


async def run_probe(
    user_id: str,
    model_id: Optional[str],
    *,
    invocations_url: str,
    bearer: str,
    client: httpx.AsyncClient,
    think_seconds: float = PROBE_THINK_SECONDS,
) -> Tuple[ProbeResult, List[str]]:
    """The three measured turns. Returns the result and the session ids created."""
    result = ProbeResult()
    cold = f"{PROBE_SESSION_PREFIX}{uuid.uuid4().hex[:16]}"
    warmed = f"{PROBE_SESSION_PREFIX}{uuid.uuid4().hex[:16]}"
    created = [cold, warmed]

    result.cold_ms, err = await stream_first_token(client, invocations_url, _headers(bearer, cold, user_id), _payload(cold, model_id))
    if err:
        result.errors.append(f"cold: {err}")
    else:
        result.follow_up_ms, err = await stream_first_token(
            client, invocations_url, _headers(bearer, cold, user_id), _payload(cold, model_id)
        )
        if err:
            result.errors.append(f"follow-up: {err}")

    result.warm_call_ms, err = await warm(client, invocations_url, _headers(bearer, warmed, user_id), warmed)
    if err:
        result.errors.append(err)
    else:
        await asyncio.sleep(think_seconds)
        result.warmed_ms, err = await stream_first_token(
            client, invocations_url, _headers(bearer, warmed, user_id), _payload(warmed, model_id)
        )
        if err:
            result.errors.append(f"warmed: {err}")
    return result, created


def emit(result: ProbeResult, model_id: Optional[str]) -> None:
    """One EMF record: the measurements that succeeded, and a run or a failure."""
    metrics: Dict[str, int] = {}
    for name, value in (
        ("TtftProbeColdFirstTokenMs", result.cold_ms),
        ("TtftProbeFollowUpFirstTokenMs", result.follow_up_ms),
        ("TtftProbeWarmedFirstTokenMs", result.warmed_ms),
        ("TtftProbeWarmCallMs", result.warm_call_ms),
    ):
        if value is not None:
            metrics[name] = value
    units = {name: "Milliseconds" for name in metrics}
    count_name = "TtftProbeRuns" if result.complete else "TtftProbeFailures"
    metrics[count_name] = 1
    units[count_name] = "Count"
    emit_emf_metrics(
        namespace=TURN_LATENCY_NAMESPACE,
        metrics=metrics,
        properties={"source": "probe", "modelId": model_id or "user-default", "errors": result.errors or None},
        units=units,
    )


async def _delete_sessions(user_id: str, session_ids: List[str]) -> None:
    from apis.app_api.sessions.services.session_service import SessionService

    service = SessionService()
    for session_id in session_ids:
        try:
            await service.delete_session(user_id=user_id, session_id=session_id)
        except Exception as exc:  # noqa: BLE001 - cleanup is best-effort; the next run starts fresh
            logger.warning("ttft probe: could not delete %s: %s", session_id, type(exc).__name__)


async def main_async() -> int:
    user_id = os.environ.get("TTFT_PROBE_USER_ID", "").strip()
    if not user_id:
        logger.error("ttft probe: TTFT_PROBE_USER_ID is not set")
        return 2
    model_id = os.environ.get("TTFT_PROBE_MODEL_ID", "").strip() or None
    base = os.environ.get("INFERENCE_API_URL", "")
    if not base:
        logger.error("ttft probe: INFERENCE_API_URL is not set")
        return 2

    created: List[str] = []
    try:
        bearer = await CognitoRefreshBearerAuth().mint_bearer_for_user(user_id)
    except Exception as exc:  # noqa: BLE001 - reported as a failed run, which the alarm sees
        result = ProbeResult(errors=[f"auth: {type(exc).__name__}: {exc}"])
    else:
        async with httpx.AsyncClient(timeout=httpx.Timeout(PROBE_TURN_TIMEOUT_SECONDS, connect=30.0)) as client:
            result, created = await run_probe(
                user_id, model_id, invocations_url=build_invocations_url(base), bearer=bearer, client=client
            )
    emit(result, model_id)
    logger.info("ttft probe %s", json.dumps({**result.__dict__, "complete": result.complete}))
    if created:
        await _delete_sessions(user_id, created)
    return 0 if result.complete else 1


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
