"""A turn a model read timeout ends must be countable, because nothing else records it.

The turn reports no usage, so it writes no ``C#`` row and the session costs $0,
while Bedrock bills the input and any thinking. Prod ran ~5 a day at the old
120s timeout with nothing watching. ``ModelReadTimeout`` is what the
``model-read-timeouts`` alarm watches; the CDK test pins the same name.
"""

from __future__ import annotations

import io
import json
import logging
from types import SimpleNamespace

from agents.main_agent.streaming.stream_coordinator import _record_model_read_timeout

_MID_STREAM = (
    "AWSHTTPSConnectionPool(host='bedrock-runtime.us-west-2.amazonaws.com', port=443): Read timed out."
)
_BEFORE_STREAM = 'Read timeout on endpoint URL: "https://bedrock-runtime.us-west-2.amazonaws.com/model/x/converse-stream"'


def _capture_emf(fn, *args, **kwargs) -> list[dict]:
    from apis.shared.observability import emf

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    emf._emf_logger.addHandler(handler)
    try:
        fn(*args, **kwargs)
    finally:
        emf._emf_logger.removeHandler(handler)
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _wrapper(model_id: str = "global.anthropic.claude-opus-5-5", read_timeout: int = 600):
    return SimpleNamespace(
        model_config=SimpleNamespace(model_id=model_id, retry_config=SimpleNamespace(read_timeout=read_timeout))
    )


def test_mid_stream_timeout_is_one_countable_record_with_the_model():
    [record] = _capture_emf(_record_model_read_timeout, f"Agent force-stopped {_MID_STREAM}", _wrapper(), "s-1")

    directive = record["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == "AgentCoreStack/TurnLatency"
    assert directive["Dimensions"] == [[]]
    assert directive["Metrics"] == [{"Name": "ModelReadTimeout", "Unit": "Count"}]
    assert record["ModelReadTimeout"] == 1
    assert record["modelId"] == "global.anthropic.claude-opus-5-5"
    assert record["readTimeoutPhase"] == "mid_stream"
    assert record["readTimeoutSeconds"] == 600
    assert record["sessionId"] == "s-1"


def test_before_stream_timeout_is_told_apart():
    [record] = _capture_emf(_record_model_read_timeout, _BEFORE_STREAM, _wrapper(), "s-1")
    assert record["readTimeoutPhase"] == "before_stream"


def test_other_errors_emit_nothing():
    assert _capture_emf(_record_model_read_timeout, "ServiceUnavailableException", _wrapper(), "s-1") == []


def test_a_wrapper_without_model_config_still_counts():
    [record] = _capture_emf(_record_model_read_timeout, _MID_STREAM, None, None)
    assert record["ModelReadTimeout"] == 1
    assert "modelId" not in record


def test_upstream_drop_record_shape():
    from apis.shared.observability.emf import emit_chat_stream_upstream_dropped

    [record] = _capture_emf(
        emit_chat_stream_upstream_dropped, reason="ReadTimeout", silent_seconds=660.04, session_id="s-1"
    )
    directive = record["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == "AgentCoreStack/TurnLatency"
    assert directive["Metrics"] == [{"Name": "ChatStreamUpstreamDropped", "Unit": "Count"}]
    assert record["dropReason"] == "ReadTimeout"
    assert record["silentSeconds"] == 660.0
