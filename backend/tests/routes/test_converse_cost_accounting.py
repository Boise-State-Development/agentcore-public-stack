"""Bug condition exploration tests for api-converse cost accounting.

These tests verify that the /chat/api-converse endpoint records costs and
enforces quotas. On UNFIXED code they are EXPECTED TO FAIL, confirming the
bug exists (cost accounting is completely bypassed).

**Validates: Requirements 1.1, 1.2, 1.3**

Property 1: Bug Condition - api-converse Bypasses Cost Accounting
"""

import json
import re
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apis.inference_api.chat.converse_routes import router
from apis.app_api.auth.api_keys.models import ValidatedApiKey


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_API_KEY = "test-api-key-12345"

MOCK_VALIDATED_KEY = ValidatedApiKey(
    key_id="test-key",
    user_id="test-user",
    name="Test Key",
)

CANNED_CONVERSE_RESPONSE = {
    "output": {
        "message": {
            "role": "assistant",
            "content": [{"text": "Hello from Bedrock!"}],
        }
    },
    "usage": {"inputTokens": 500, "outputTokens": 200},
    "stopReason": "end_turn",
}


def _make_stream_events():
    """Return a list of Bedrock converse_stream events with usage metadata."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
        {
            "contentBlockDelta": {
                "contentBlockIndex": 0,
                "delta": {"text": "Hi!"},
            }
        },
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
        {
            "metadata": {
                "usage": {"inputTokens": 500, "outputTokens": 200},
                "metrics": {"latencyMs": 123},
            }
        },
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Minimal FastAPI app with only the converse router."""
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def mock_validate_api_key():
    """Patch _validate_api_key to return a canned ValidatedApiKey."""
    with patch(
        "apis.inference_api.chat.converse_routes._validate_api_key",
        new_callable=AsyncMock,
        return_value=MOCK_VALIDATED_KEY,
    ) as m:
        yield m


@pytest.fixture
def mock_bedrock_client():
    """Patch _get_bedrock_client to return a fake Bedrock client."""
    fake_client = MagicMock()
    fake_client.converse.return_value = CANNED_CONVERSE_RESPONSE

    # For streaming: converse_stream returns an object with a "stream" key
    fake_client.converse_stream.return_value = {
        "stream": iter(_make_stream_events())
    }

    with patch(
        "apis.inference_api.chat.converse_routes._get_bedrock_client",
        return_value=fake_client,
    ) as m:
        yield fake_client


@pytest.fixture
def mock_store_metadata():
    """Patch store_message_metadata where it is imported in converse_routes.

    The fixed converse_routes.py imports store_message_metadata directly, so
    we must patch at the converse_routes module level for the mock to intercept.
    """
    with patch(
        "apis.inference_api.chat.converse_routes.store_message_metadata",
        new_callable=AsyncMock,
    ) as m:
        yield m


@pytest.fixture
def mock_pricing():
    """Patch create_pricing_snapshot to return canned pricing (avoids DynamoDB).

    The fixed converse_routes.py calls create_pricing_snapshot inside
    _record_cost; without this mock the call hits DynamoDB and fails.
    """
    pricing = {
        "inputPricePerMtok": 1.0,
        "outputPricePerMtok": 5.0,
        "currency": "USD",
        "snapshotAt": "2025-01-15T00:00:00Z",
    }
    with patch(
        "apis.inference_api.chat.converse_routes.create_pricing_snapshot",
        new_callable=AsyncMock,
        return_value=pricing,
    ) as m:
        yield m


@pytest.fixture
def mock_quota_disabled():
    """Ensure quota enforcement is disabled (isolate cost recording tests).

    Patches at the converse_routes module level where shared_quota is used.
    """
    with patch(
        "apis.inference_api.chat.converse_routes.shared_quota.is_quota_enforcement_enabled",
        return_value=False,
    ) as m:
        yield m


@pytest.fixture
def mock_app_role_service():
    """Patch get_app_role_service to allow all model access (avoids DynamoDB)."""
    svc = MagicMock()
    svc.can_access_model = AsyncMock(return_value=True)
    with patch(
        "apis.inference_api.chat.converse_routes.get_app_role_service",
        return_value=svc,
    ):
        yield svc


# ---------------------------------------------------------------------------
# Bug Condition Test 1: Non-streaming cost recording
# Validates: Requirements 1.1 (2.1)
# ---------------------------------------------------------------------------


class TestNonStreamingCostRecording:
    """Non-streaming api-converse should record cost via store_message_metadata.

    **Validates: Requirements 1.1**

    On UNFIXED code this test FAILS because store_message_metadata is never
    called — the endpoint returns the Bedrock response without recording cost.
    """

    def test_store_message_metadata_called_with_cost(
        self,
        client,
        mock_validate_api_key,
        mock_bedrock_client,
        mock_store_metadata,
        mock_pricing,
        mock_quota_disabled,
        mock_app_role_service,
    ):
        """Non-streaming request must call store_message_metadata with cost > 0."""
        resp = client.post(
            "/chat/api-converse",
            json={
                "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": False,
            },
            headers={"X-API-Key": VALID_API_KEY},
        )

        # The endpoint should still return 200
        assert resp.status_code == 200

        # BUG CONDITION: store_message_metadata should have been called
        mock_store_metadata.assert_called_once()

        # Verify the metadata contains cost > 0 and correct token usage
        call_kwargs = mock_store_metadata.call_args
        metadata = call_kwargs.kwargs.get(
            "message_metadata"
        ) or call_kwargs.args[3]

        assert metadata.cost is not None and metadata.cost > 0, (
            f"Expected cost > 0, got {metadata.cost}"
        )
        assert metadata.token_usage is not None
        assert metadata.token_usage.input_tokens == 500
        assert metadata.token_usage.output_tokens == 200


# ---------------------------------------------------------------------------
# Bug Condition Test 2: Streaming cost recording
# Validates: Requirements 1.2 (2.2)
# ---------------------------------------------------------------------------


class TestStreamingCostRecording:
    """Streaming api-converse should record cost after stream completes.

    **Validates: Requirements 1.2**

    On UNFIXED code this test FAILS because store_message_metadata is never
    called — the stream yields events but no cost data is persisted.
    """

    def test_store_message_metadata_called_after_stream(
        self,
        client,
        mock_validate_api_key,
        mock_bedrock_client,
        mock_store_metadata,
        mock_pricing,
        mock_quota_disabled,
        mock_app_role_service,
    ):
        """Streaming request must call store_message_metadata after stream completes."""
        resp = client.post(
            "/chat/api-converse",
            json={
                "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
            headers={"X-API-Key": VALID_API_KEY},
        )

        assert resp.status_code == 200

        # Consume the full SSE stream
        body = resp.text
        assert "event: done" in body

        # BUG CONDITION: store_message_metadata should have been called
        mock_store_metadata.assert_called_once()

        call_kwargs = mock_store_metadata.call_args
        metadata = call_kwargs.kwargs.get(
            "message_metadata"
        ) or call_kwargs.args[3]

        assert metadata.cost is not None and metadata.cost > 0, (
            f"Expected cost > 0, got {metadata.cost}"
        )
        assert metadata.token_usage is not None
        assert metadata.token_usage.input_tokens == 500
        assert metadata.token_usage.output_tokens == 200


# ---------------------------------------------------------------------------
# Bug Condition Test 3: Quota enforcement
# Validates: Requirements 1.3 (2.3, 2.5)
# ---------------------------------------------------------------------------


class TestQuotaEnforcement:
    """api-converse should reject requests when quota is exceeded.

    **Validates: Requirements 1.3**

    On UNFIXED code this test FAILS because no quota check exists — the
    endpoint proceeds to call Bedrock regardless of quota state.
    """

    def test_returns_429_when_quota_exceeded(
        self,
        client,
        mock_validate_api_key,
        mock_bedrock_client,
    ):
        """Request should be rejected with 429 when quota is exceeded."""
        from agents.main_agent.quota.models import QuotaCheckResult

        mock_checker = AsyncMock()
        mock_checker.check_quota.return_value = QuotaCheckResult(
            allowed=False,
            message="Monthly quota exceeded. Current usage: $10.05 / $10.00 limit.",
            current_usage=Decimal("10.05"),
            quota_limit=Decimal("10.00"),
            percentage_used=Decimal("100.5"),
            remaining=Decimal("0"),
            warning_level="none",
        )

        with patch(
            "apis.shared.quota.is_quota_enforcement_enabled",
            return_value=True,
        ), patch(
            "apis.shared.quota.get_quota_checker",
            return_value=mock_checker,
        ):
            resp = client.post(
                "/chat/api-converse",
                json={
                    "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": False,
                },
                headers={"X-API-Key": VALID_API_KEY},
            )

        # BUG CONDITION: should return 429 when quota exceeded
        assert resp.status_code == 429, (
            f"Expected 429 (quota exceeded), got {resp.status_code}"
        )


# ===========================================================================
# Preservation Property Tests (Task 2)
#
# These tests verify that existing api-converse behavior is preserved.
# They MUST PASS on UNFIXED code to establish the baseline.
#
# **Validates: Requirements 3.2, 3.3, 3.4, 3.5, 3.6**
#
# Property 2: Preservation - api-converse Response Format and Auth Unchanged
# ===========================================================================

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

message_content_st = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=1,
    max_size=200,
)

temperature_st = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)
max_tokens_st = st.integers(min_value=1, max_value=8192)
top_p_st = st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0, allow_nan=False))


def _make_converse_response(text: str = "Hello from Bedrock!"):
    """Build a canned Bedrock converse() response with the given text."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": text}],
            }
        },
        "usage": {"inputTokens": 500, "outputTokens": 200},
        "stopReason": "end_turn",
    }


def _make_reasoning_converse_response():
    """Build a canned Bedrock converse() response with reasoning content."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"reasoningContent": {"reasoningText": {"text": "Let me think..."}}},
                    {"text": "The answer is 42."},
                ],
            }
        },
        "usage": {"inputTokens": 800, "outputTokens": 400},
        "stopReason": "end_turn",
    }


def _parse_sse_events(body: str) -> list[tuple[str, dict]]:
    """Parse SSE text into a list of (event_type, data_dict) tuples."""
    events = []
    for match in re.finditer(r"event: (\S+)\ndata: (.+?)(?:\n\n|\Z)", body, re.DOTALL):
        event_type = match.group(1)
        data = json.loads(match.group(2))
        events.append((event_type, data))
    return events


# ---------------------------------------------------------------------------
# Preservation Test 1: Non-streaming response format
# Validates: Requirements 3.4
# ---------------------------------------------------------------------------


class TestNonStreamingResponseFormatPreservation:
    """Non-streaming api-converse response must have exact ConverseResponse shape.

    **Validates: Requirements 3.4**

    Verifies that the response JSON always contains: content, model_id, usage,
    stop_reason, reasoning, and role fields with correct types.
    """

    def test_response_has_correct_shape(
        self,
        client,
        mock_validate_api_key,
        mock_bedrock_client,
        mock_app_role_service,
    ):
        """Non-streaming response must contain all ConverseResponse fields."""
        resp = client.post(
            "/chat/api-converse",
            json={
                "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": False,
            },
            headers={"X-API-Key": VALID_API_KEY},
        )

        assert resp.status_code == 200
        data = resp.json()

        # All ConverseResponse fields must be present
        assert "content" in data
        assert "model_id" in data
        assert "usage" in data
        assert "stop_reason" in data
        assert "reasoning" in data
        assert "role" in data

        # Verify exact values from canned response
        assert data["content"] == "Hello from Bedrock!"
        assert data["model_id"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert data["usage"] == {"inputTokens": 500, "outputTokens": 200}
        assert data["stop_reason"] == "end_turn"
        assert data["reasoning"] is None
        assert data["role"] == "assistant"

    def test_reasoning_model_response_shape(
        self,
        client,
        mock_validate_api_key,
        mock_app_role_service,
    ):
        """Reasoning model response must include reasoning field."""
        fake_client = MagicMock()
        fake_client.converse.return_value = _make_reasoning_converse_response()

        with patch(
            "apis.inference_api.chat.converse_routes._get_bedrock_client",
            return_value=fake_client,
        ):
            resp = client.post(
                "/chat/api-converse",
                json={
                    "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                    "messages": [{"role": "user", "content": "Think step by step"}],
                    "stream": False,
                },
                headers={"X-API-Key": VALID_API_KEY},
            )

        assert resp.status_code == 200
        data = resp.json()

        assert data["content"] == "The answer is 42."
        assert data["reasoning"] == "Let me think..."
        assert data["usage"] == {"inputTokens": 800, "outputTokens": 400}
        assert data["stop_reason"] == "end_turn"

    @given(
        content=message_content_st,
        temperature=temperature_st,
        max_tokens=max_tokens_st,
        top_p=top_p_st,
    )
    @settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_response_format_consistent_across_inputs(
        self,
        client,
        mock_validate_api_key,
        mock_app_role_service,
        content,
        temperature,
        max_tokens,
        top_p,
    ):
        """Response format is always consistent regardless of input parameters.

        **Validates: Requirements 3.4**
        """
        reply_text = f"Reply to: {content[:50]}"
        fake_client = MagicMock()
        fake_client.converse.return_value = _make_converse_response(reply_text)

        with patch(
            "apis.inference_api.chat.converse_routes._get_bedrock_client",
            return_value=fake_client,
        ):
            payload = {
                "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "messages": [{"role": "user", "content": content}],
                "stream": False,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if top_p is not None:
                payload["top_p"] = top_p

            resp = client.post(
                "/chat/api-converse",
                json=payload,
                headers={"X-API-Key": VALID_API_KEY},
            )

        assert resp.status_code == 200
        data = resp.json()

        # Shape is always the same
        required_keys = {"content", "model_id", "usage", "stop_reason", "reasoning", "role"}
        assert required_keys.issubset(data.keys()), (
            f"Missing keys: {required_keys - data.keys()}"
        )

        # Types are always correct
        assert isinstance(data["content"], str)
        assert isinstance(data["model_id"], str)
        assert data["role"] == "assistant"
        assert data["usage"] is None or isinstance(data["usage"], dict)
        assert data["stop_reason"] is None or isinstance(data["stop_reason"], str)
        assert data["reasoning"] is None or isinstance(data["reasoning"], str)


# ---------------------------------------------------------------------------
# Preservation Test 2: Streaming SSE format
# Validates: Requirements 3.5
# ---------------------------------------------------------------------------


class TestStreamingSSEFormatPreservation:
    """Streaming api-converse must emit SSE events in the correct order and format.

    **Validates: Requirements 3.5**

    Verifies that SSE events follow the expected lifecycle:
    message_start → content_block_start → content_block_delta →
    content_block_stop → message_stop → metadata → done
    """

    def test_sse_event_order_and_format(
        self,
        client,
        mock_validate_api_key,
        mock_bedrock_client,
        mock_app_role_service,
    ):
        """Streaming response must emit SSE events in correct order with correct data."""
        resp = client.post(
            "/chat/api-converse",
            json={
                "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
            headers={"X-API-Key": VALID_API_KEY},
        )

        assert resp.status_code == 200
        events = _parse_sse_events(resp.text)
        event_types = [e[0] for e in events]

        # Verify expected event order
        assert event_types == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "message_stop",
            "metadata",
            "done",
        ]

        # Verify event data payloads
        assert events[0][1] == {"role": "assistant"}  # message_start
        assert events[1][1]["contentBlockIndex"] == 0  # content_block_start
        assert events[1][1]["type"] == "text"
        assert events[2][1]["text"] == "Hi!"  # content_block_delta
        assert events[2][1]["type"] == "text"
        assert events[3][1] == {"contentBlockIndex": 0}  # content_block_stop
        assert events[4][1] == {"stopReason": "end_turn"}  # message_stop
        assert events[5][1]["usage"] == {"inputTokens": 500, "outputTokens": 200}  # metadata
        assert events[6][1] == {}  # done

    @given(content=message_content_st)
    @settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_sse_always_ends_with_done(
        self,
        client,
        mock_validate_api_key,
        mock_app_role_service,
        content,
    ):
        """Streaming response always ends with a 'done' event regardless of input.

        **Validates: Requirements 3.5**
        """
        stream_events = [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockStart": {"contentBlockIndex": 0, "start": {}}},
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"text": content[:50]},
                }
            },
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "end_turn"}},
            {
                "metadata": {
                    "usage": {"inputTokens": 100, "outputTokens": 50},
                    "metrics": {"latencyMs": 42},
                }
            },
        ]

        fake_client = MagicMock()
        fake_client.converse_stream.return_value = {"stream": iter(stream_events)}

        with patch(
            "apis.inference_api.chat.converse_routes._get_bedrock_client",
            return_value=fake_client,
        ):
            resp = client.post(
                "/chat/api-converse",
                json={
                    "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                    "messages": [{"role": "user", "content": content}],
                    "stream": True,
                },
                headers={"X-API-Key": VALID_API_KEY},
            )

        assert resp.status_code == 200
        events = _parse_sse_events(resp.text)
        event_types = [e[0] for e in events]

        # Must always start with message_start and end with done
        assert event_types[0] == "message_start"
        assert event_types[-1] == "done"

        # Must always contain metadata before done
        assert "metadata" in event_types
        metadata_idx = event_types.index("metadata")
        done_idx = event_types.index("done")
        assert metadata_idx < done_idx


# ---------------------------------------------------------------------------
# Preservation Test 3: Authentication behavior
# Validates: Requirements 3.2, 3.3
# ---------------------------------------------------------------------------


class TestAuthPreservation:
    """API key authentication behavior must be preserved.

    **Validates: Requirements 3.2, 3.3**

    Valid keys proceed to Bedrock call; invalid keys return 401.
    """

    def test_invalid_api_key_returns_401(self, client):
        """Invalid API key must return 401 with correct error detail."""
        with patch(
            "apis.inference_api.chat.converse_routes._validate_api_key",
            new_callable=AsyncMock,
            side_effect=HTTPException(
                status_code=401, detail="Invalid or expired API key"
            ),
        ):
            resp = client.post(
                "/chat/api-converse",
                json={
                    "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": False,
                },
                headers={"X-API-Key": "bad-key"},
            )

        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid or expired API key"

    def test_valid_api_key_proceeds_to_bedrock(
        self,
        client,
        mock_validate_api_key,
        mock_bedrock_client,
        mock_app_role_service,
    ):
        """Valid API key must proceed to Bedrock call and return 200."""
        resp = client.post(
            "/chat/api-converse",
            json={
                "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": False,
            },
            headers={"X-API-Key": VALID_API_KEY},
        )

        assert resp.status_code == 200
        mock_bedrock_client.converse.assert_called_once()


# ---------------------------------------------------------------------------
# Preservation Test 4: Error handling
# Validates: Requirements 3.4, 3.6
# ---------------------------------------------------------------------------


class TestErrorPreservation:
    """Error handling behavior must be preserved.

    **Validates: Requirements 3.4, 3.6**

    Empty messages → 400, Bedrock error → 502.
    """

    def test_empty_messages_returns_400(
        self,
        client,
        mock_validate_api_key,
    ):
        """Empty messages array must return 400."""
        resp = client.post(
            "/chat/api-converse",
            json={
                "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "messages": [],
                "stream": False,
            },
            headers={"X-API-Key": VALID_API_KEY},
        )

        assert resp.status_code == 400
        assert "empty" in resp.json()["detail"].lower()

    def test_bedrock_error_returns_502(
        self,
        client,
        mock_validate_api_key,
        mock_app_role_service,
    ):
        """Bedrock client error must return 502."""
        fake_client = MagicMock()
        fake_client.converse.side_effect = Exception("Bedrock is down")

        with patch(
            "apis.inference_api.chat.converse_routes._get_bedrock_client",
            return_value=fake_client,
        ):
            resp = client.post(
                "/chat/api-converse",
                json={
                    "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": False,
                },
                headers={"X-API-Key": VALID_API_KEY},
            )

        assert resp.status_code == 502
        assert "Model invocation failed" in resp.json()["detail"]

    def test_streaming_bedrock_error_yields_error_event(
        self,
        client,
        mock_validate_api_key,
        mock_app_role_service,
    ):
        """Bedrock streaming error must yield error + done SSE events."""
        fake_client = MagicMock()
        # Must provide a real exception class for `except client.exceptions.ClientError`
        fake_client.exceptions.ClientError = type("ClientError", (Exception,), {})
        fake_client.converse_stream.side_effect = fake_client.exceptions.ClientError("Stream failed")

        with patch(
            "apis.inference_api.chat.converse_routes._get_bedrock_client",
            return_value=fake_client,
        ):
            resp = client.post(
                "/chat/api-converse",
                json={
                    "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
                headers={"X-API-Key": VALID_API_KEY},
            )

        assert resp.status_code == 200  # SSE always returns 200
        events = _parse_sse_events(resp.text)
        event_types = [e[0] for e in events]

        assert "error" in event_types
        assert event_types[-1] == "done"
