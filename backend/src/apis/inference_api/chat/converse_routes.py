"""API-key authenticated converse endpoint.

Provides a direct Bedrock Converse API wrapper authenticated via API keys
(X-API-Key header). Supports:
- Single-shot and multi-turn conversations
- Streaming (SSE) and non-streaming responses
- Reasoning models (extended thinking / reasoning content blocks)
- Multiple Bedrock model IDs

RBAC model access is enforced via ``AppRoleService.can_access_model()``
before any Bedrock invocation occurs. Requests for models the caller's
role does not permit are rejected with HTTP 403.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import AsyncGenerator

import boto3
from botocore.exceptions import ClientError as BotoClientError
from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import StreamingResponse

from apis.shared.auth.models import User
from apis.shared import quota as shared_quota
from apis.shared.rbac.service import get_app_role_service
from apis.app_api.costs.calculator import CostCalculator
from apis.app_api.costs.pricing_config import create_pricing_snapshot
from apis.shared.sessions.metadata import store_message_metadata
from apis.shared.sessions.models import (
    MessageMetadata,
    TokenUsage,
    ModelInfo,
    Attribution,
)

from .models import ConverseRequest, ConverseResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["api-converse"])


# ---------------------------------------------------------------------------
# API key validation dependency
# ---------------------------------------------------------------------------

async def _validate_api_key(api_key: str):
    """Validate the raw API key and return the ValidatedApiKey, or raise 401."""
    from apis.app_api.auth.api_keys.service import get_api_key_service

    service = get_api_key_service()
    validated = await service.validate_key(api_key)
    if validated is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key",
        )
    return validated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_user_from_api_key(validated_key) -> User:
    """Construct a minimal User from a ValidatedApiKey for quota checking."""
    return User(
        email=f"{validated_key.user_id}@api-key",
        user_id=validated_key.user_id,
        name=validated_key.name,
        roles=["user"],
    )

async def _record_cost(user_id: str, model_id: str, usage: dict, key_id: str) -> None:
    """Calculate and store cost metadata for an api-converse request.

    Fail-open: any error is logged but never re-raised so the caller's
    response is not blocked.
    """
    try:
        pricing = await create_pricing_snapshot(model_id)
        if pricing is None:
            logger.warning(
                "No pricing snapshot for model; skipping cost recording"
            )
            return

        total_cost, breakdown = CostCalculator.calculate_message_cost(usage, pricing)

        token_usage = TokenUsage(
            inputTokens=usage.get("inputTokens", 0),
            outputTokens=usage.get("outputTokens", 0),
            totalTokens=usage.get("inputTokens", 0) + usage.get("outputTokens", 0),
            cacheReadInputTokens=usage.get("cacheReadInputTokens"),
            cacheWriteInputTokens=usage.get("cacheWriteInputTokens"),
        )

        model_info = ModelInfo(
            modelId=model_id,
            modelName=model_id,
            provider="bedrock",
        )

        attribution = Attribution(
            userId=user_id,
            sessionId=f"api-converse-{key_id}",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        metadata = MessageMetadata(
            token_usage=token_usage,
            model_info=model_info,
            attribution=attribution,
            cost=total_cost,
        )

        await store_message_metadata(
            session_id=f"api-converse-{key_id}",
            user_id=user_id,
            message_id=0,
            message_metadata=metadata,
        )
    except Exception as exc:
        logger.error(
            "Failed to record cost",
            exc_info=True,
        )


def _get_bedrock_client():
    """Return a boto3 bedrock-runtime client."""
    region = os.environ.get("AWS_REGION", "us-east-1")
    return boto3.client("bedrock-runtime", region_name=region)


def _build_converse_params(request: ConverseRequest) -> dict:
    """Build the kwargs dict for bedrock.converse / converse_stream."""
    # Convert messages to Bedrock Converse format
    messages = [
        {"role": m.role, "content": [{"text": m.content}]}
        for m in request.messages
    ]

    inference_config: dict = {}
    if request.temperature is not None:
        inference_config["temperature"] = request.temperature
    if request.max_tokens is not None:
        inference_config["maxTokens"] = request.max_tokens
    if request.top_p is not None:
        inference_config["topP"] = request.top_p

    params: dict = {
        "modelId": request.model_id,
        "messages": messages,
    }

    if request.system_prompt:
        params["system"] = [{"text": request.system_prompt}]

    if inference_config:
        params["inferenceConfig"] = inference_config

    return params


def _extract_reasoning_and_text(content_blocks: list) -> tuple[str, str | None]:
    """Extract text and optional reasoning from Bedrock response content blocks.

    Reasoning models (e.g. Claude with extended thinking) return a
    ``reasoningContent`` block alongside the normal ``text`` block.

    Returns:
        (text, reasoning) – reasoning is None for non-reasoning models.
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []

    for block in content_blocks:
        if "text" in block:
            text_parts.append(block["text"])
        elif "reasoningContent" in block:
            # Extended thinking / reasoning block
            rc = block["reasoningContent"]
            if "reasoningText" in rc:
                reasoning_parts.append(rc["reasoningText"].get("text", ""))

    text = "".join(text_parts)
    reasoning = "".join(reasoning_parts) if reasoning_parts else None
    return text, reasoning


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------

async def _stream_converse(request: ConverseRequest, user_id: str, key_id: str) -> AsyncGenerator[str, None]:
    """Call Bedrock converse_stream and yield SSE events."""
    client = _get_bedrock_client()
    params = _build_converse_params(request)

    try:
        response = client.converse_stream(**params)
    except BotoClientError as exc:
        error_code = exc.response["Error"]["Code"]
        logger.error(f"Bedrock converse_stream ClientError ({error_code})", exc_info=True)
        yield _sse("error", {"error": "Model invocation failed due to a service error."})
        yield _sse("done", {})
        return
    except Exception:
        logger.error("Bedrock converse_stream error", exc_info=True)
        yield _sse("error", {"error": "Model invocation failed due to an internal error."})
        yield _sse("done", {})
        return

    stream = response.get("stream")
    if not stream:
        yield _sse("error", {"error": "No stream returned from Bedrock"})
        yield _sse("done", {})
        return

    # Track state for SSE lifecycle events
    in_reasoning = False
    accumulated_usage: dict = {}

    for event in stream:
        # --- message start ---
        if "messageStart" in event:
            role = event["messageStart"].get("role", "assistant")
            yield _sse("message_start", {"role": role})

        # --- content block start ---
        elif "contentBlockStart" in event:
            cbs = event["contentBlockStart"]
            idx = cbs.get("contentBlockIndex", 0)
            start_data = cbs.get("start", {})

            if "toolUse" in start_data:
                yield _sse("content_block_start", {
                    "contentBlockIndex": idx,
                    "type": "tool_use",
                    "toolUse": start_data["toolUse"],
                })
            else:
                yield _sse("content_block_start", {
                    "contentBlockIndex": idx,
                    "type": "text",
                })

        # --- content block delta ---
        elif "contentBlockDelta" in event:
            cbd = event["contentBlockDelta"]
            idx = cbd.get("contentBlockIndex", 0)
            delta = cbd.get("delta", {})

            if "text" in delta:
                yield _sse("content_block_delta", {
                    "contentBlockIndex": idx,
                    "type": "text",
                    "text": delta["text"],
                })
            elif "reasoningContent" in delta:
                rc = delta["reasoningContent"]
                if "text" in rc:
                    if not in_reasoning:
                        in_reasoning = True
                        yield _sse("reasoning_start", {"contentBlockIndex": idx})
                    yield _sse("reasoning_delta", {
                        "contentBlockIndex": idx,
                        "text": rc["text"],
                    })

        # --- content block stop ---
        elif "contentBlockStop" in event:
            idx = event["contentBlockStop"].get("contentBlockIndex", 0)
            if in_reasoning:
                yield _sse("reasoning_stop", {"contentBlockIndex": idx})
                in_reasoning = False
            yield _sse("content_block_stop", {"contentBlockIndex": idx})

        # --- message stop ---
        elif "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason", "end_turn")
            yield _sse("message_stop", {"stopReason": stop_reason})

        # --- metadata ---
        elif "metadata" in event:
            meta = event["metadata"]
            usage = meta.get("usage", {})
            accumulated_usage = usage
            metrics = meta.get("metrics", {})
            yield _sse("metadata", {"usage": usage, "metrics": metrics})

    yield _sse("done", {})

    # Record cost after stream completes
    if accumulated_usage:
        await _record_cost(
            user_id=user_id,
            model_id=request.model_id,
            usage=accumulated_usage,
            key_id=key_id,
        )



def _sse(event_type: str, data: dict) -> str:
    """Format a single SSE event."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/api-converse",
    response_model=ConverseResponse,
    responses={
        200: {"description": "Non-streaming response (or SSE stream when stream=true)"},
        401: {"description": "Invalid or expired API key"},
        400: {"description": "Bad request (invalid model, empty messages, etc.)"},
    },
    summary="Converse with a Bedrock model via API key",
)
async def api_converse(
    request: ConverseRequest,
    x_api_key: str = Header(..., alias="X-API-Key"),
):
    """Direct Bedrock Converse API wrapper authenticated via API key.

    Supports streaming (SSE) and non-streaming responses, multi-turn
    conversations, and reasoning models that return extended thinking blocks.
    """
    # 1. Validate API key
    validated_key = await _validate_api_key(x_api_key)
    logger.info("api-converse request received")

    # 1.5 Per-key rate limit (fail-open)
    from apis.shared.rate_limit import get_rate_limiter

    try:
        limiter = get_rate_limiter()
        if not await limiter.check_rate_limit(validated_key.key_id):
            logger.warning(
                f"Rate limit exceeded for key {validated_key.key_id} "
                f"(user={validated_key.user_id})"
            )
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Max 60 requests per minute.",
                headers={"Retry-After": "60"},
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Rate limit check error: {exc}", exc_info=True)

    # 2. Basic validation
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages array must not be empty")

    # 2.5 Build User and synthetic session_id for quota / cost accounting
    user = _build_user_from_api_key(validated_key)
    session_id = f"api-converse-{validated_key.key_id}"

    # 2.6 Quota check (fail-open: errors are logged but don't block the request)
    if shared_quota.is_quota_enforcement_enabled():
        try:
            quota_checker = shared_quota.get_quota_checker()
            quota_result = await quota_checker.check_quota(user=user, session_id=session_id)
            if not quota_result.allowed:
                if quota_result.quota_limit is None:
                    # No quota tier configured for this API-key user — fail-open
                    # per Requirement 3.6 (don't block on internal/config issues)
                    logger.warning(
                        f"No quota tier for user {validated_key.user_id}; "
                        f"proceeding (fail-open)"
                    )
                else:
                    logger.warning(
                        f"Quota exceeded for user {validated_key.user_id}: {quota_result.message}"
                    )
                    raise HTTPException(status_code=429, detail=quota_result.message)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                f"Error checking quota for user {validated_key.user_id}: {exc}",
                exc_info=True,
            )

    # 2.7 Model access check (RBAC)
    app_role_service = get_app_role_service()
    if not await app_role_service.can_access_model(user, request.model_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to model: {request.model_id}",
        )

    # 3. Streaming path
    if request.stream:
        return StreamingResponse(
            _stream_converse(request, user_id=validated_key.user_id, key_id=validated_key.key_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # 4. Non-streaming path
    client = _get_bedrock_client()
    params = _build_converse_params(request)

    try:
        response = client.converse(**params)
    except BotoClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code == "ThrottlingException":
            logger.warning("Bedrock throttling on converse call", exc_info=True)
            raise HTTPException(
                status_code=429,
                detail="Model is temporarily overloaded. Please retry shortly.",
                headers={"Retry-After": "5"},
            )
        logger.error(f"Bedrock ClientError ({error_code}) on converse call", exc_info=True)
        if error_code in ("ValidationException", "ModelErrorException"):
            raise HTTPException(
                status_code=400,
                detail="Invalid request — check model ID, message format, and content policy.",
            )
        if error_code == "AccessDeniedException":
            raise HTTPException(status_code=403, detail="Model access is not available.")
        raise HTTPException(status_code=502, detail="Model invocation failed due to a service error.")
    except Exception:
        logger.error("Unexpected error during Bedrock converse call", exc_info=True)
        raise HTTPException(status_code=502, detail="Model invocation failed due to an internal error.")

    # Parse response
    output = response.get("output", {})
    message = output.get("message", {})
    content_blocks = message.get("content", [])

    text, reasoning = _extract_reasoning_and_text(content_blocks)

    usage = response.get("usage")
    stop_reason = response.get("stopReason")

    # Record cost for non-streaming response
    if usage is not None:
        await _record_cost(
            user_id=validated_key.user_id,
            model_id=request.model_id,
            usage=usage,
            key_id=validated_key.key_id,
        )

    return ConverseResponse(
        content=text,
        model_id=request.model_id,
        usage=usage,
        stop_reason=stop_reason,
        reasoning=reasoning,
    )
