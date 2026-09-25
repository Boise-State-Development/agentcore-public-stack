"""The inference-api image must not record conversation content in OpenTelemetry.

Under AgentCore, ADOT (``aws-opentelemetry-distro``) exports two streams of
GenAI telemetry into the runtime log group's ``otel-rt-logs`` stream, and both
carried every prompt and reply by default:

- ``strands.telemetry.tracer``: Strands puts messages on its spans as events;
  ADOT's ``LLOHandler`` lifts them off the span and re-emits them as OTLP log
  records. Strands redacts them only when ``OTEL_SEMCONV_STABILITY_OPT_IN``
  carries a ``gen_ai_unredacted_attributes=`` token.
- ``opentelemetry.instrumentation.botocore.bedrock-runtime``: the Converse
  instrumentation writes ``gen_ai.*`` events whose body holds the text only
  when ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`` is ``true``. ADOT
  sets it to ``true`` whenever ``AGENT_OBSERVABILITY_ENABLED`` is on, with
  ``setdefault``, so the image has to set it first.

``Dockerfile.inference-api`` sets both. These tests read the values from the
Dockerfile, not from a copy, and run them through the installed Strands,
ADOT and botocore-instrumentation code. A dependency upgrade that renames a
token or changes a default then fails here, instead of quietly putting user
conversations back into CloudWatch. Each redaction test has a control that
runs with capture ON and expects the sentinel to appear, so a test that stops
exercising the content path cannot pass by accident.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

import pytest
from amazon.opentelemetry.distro.llo_handler import LLOHandler
from opentelemetry.instrumentation.botocore.extensions.bedrock_utils import (
    _Choice,
    genai_capture_message_content,
    message_to_event,
)
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from strands.agent.agent_result import AgentResult
from strands.telemetry.metrics import EventLoopMetrics
from strands.telemetry.tracer import Tracer

_DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile.inference-api"

_CAPTURE_VAR = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
_SEMCONV_VAR = "OTEL_SEMCONV_STABILITY_OPT_IN"

# Stands in for anything a user typed or a model wrote. Distinct per role so a
# failure names which part of the conversation leaked.
_PROMPT = "SENTINEL-PROMPT-7f3a"
_SYSTEM = "SENTINEL-SYSTEM-7f3a"
_REPLY = "SENTINEL-REPLY-7f3a"
_TOOL_INPUT = "SENTINEL-TOOL-INPUT-7f3a"
_TOOL_RESULT = "SENTINEL-TOOL-RESULT-7f3a"
_ALL_SENTINELS = (_PROMPT, _SYSTEM, _REPLY, _TOOL_INPUT, _TOOL_RESULT)


def _image_env() -> dict[str, str]:
    """``ENV KEY=value`` instructions from the inference-api Dockerfile."""
    env: dict[str, str] = {}
    for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*ENV\s+([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if match:
            env[match.group(1)] = match.group(2).strip().strip('"')
    return env


@pytest.fixture
def image_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Apply the image's content switches, exactly as the container sees them."""
    env = _image_env()
    for name in (_CAPTURE_VAR, _SEMCONV_VAR):
        monkeypatch.setenv(name, env[name])
    return env


@pytest.fixture
def capture_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-fix state: ADOT's capture default, and no Strands redaction token."""
    monkeypatch.setenv(_CAPTURE_VAR, "true")
    monkeypatch.delenv(_SEMCONV_VAR, raising=False)


def _strings(value: Any) -> Iterator[str]:
    """Every string reachable inside a span/log attribute value or body."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
    elif value is not None:
        yield json.dumps(value, default=str)


def _leaked(texts: list[str]) -> list[str]:
    return [s for s in _ALL_SENTINELS if any(s in text for text in texts)]


def _run_strands_turn() -> tuple[list[str], list[str]]:
    """Drive one agent turn (model call, tool call, reply) through the Strands
    tracer, then through ADOT's LLOHandler as its span exporter does under
    AgentCore. Returns the text on the exported spans and on the log records
    bound for ``otel-rt-logs``.
    """
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))

    tracer = Tracer()  # reads OTEL_SEMCONV_STABILITY_OPT_IN, as the runtime's does
    tracer.tracer = tracer_provider.get_tracer("strands.telemetry.tracer")

    user_message = {"role": "user", "content": [{"text": _PROMPT}]}
    tool_use = {"toolUseId": "tool-1", "name": "lookup", "input": {"query": _TOOL_INPUT}}
    assistant_tool_call = {"role": "assistant", "content": [{"toolUse": tool_use}]}
    tool_result_message = {
        "role": "user",
        "content": [
            {"toolResult": {"toolUseId": "tool-1", "status": "success", "content": [{"text": _TOOL_RESULT}]}}
        ],
    }
    reply = {"role": "assistant", "content": [{"text": _REPLY}]}
    usage = {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}

    agent_span = tracer.start_agent_span(messages=[user_message], agent_name="agent", model_id="model")
    cycle_span = tracer.start_event_loop_cycle_span(
        invocation_state={}, messages=[user_message], parent_span=agent_span
    )
    model_span = tracer.start_model_invoke_span(
        messages=[user_message], parent_span=cycle_span, model_id="model", system_prompt=_SYSTEM
    )
    tracer.end_model_invoke_span(model_span, assistant_tool_call, usage, {"latencyMs": 1}, "tool_use")
    tool_span = tracer.start_tool_call_span(tool_use, parent_span=cycle_span)
    tracer.end_tool_call_span(tool_span, tool_result_message["content"][0]["toolResult"])
    tracer.end_event_loop_cycle_span(cycle_span, assistant_tool_call, tool_result_message)
    tracer.end_agent_span(
        agent_span,
        AgentResult(stop_reason="end_turn", message=reply, metrics=EventLoopMetrics(), state={}),
    )

    spans = span_exporter.get_finished_spans()
    assert spans, "the Strands tracer exported no spans; the test is not exercising it"

    log_exporter = InMemoryLogExporter()
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    exported_spans = LLOHandler(logger_provider).process_spans(spans)

    span_texts = [
        text
        for span in exported_spans
        for text in [*_strings(dict(span.attributes or {}))]
        + [t for event in span.events for t in _strings(dict(event.attributes or {}))]
    ]
    log_texts = [
        text for record in log_exporter.get_finished_logs() for text in _strings(record.log_record.body)
    ]
    return span_texts, log_texts


def test_dockerfile_sets_both_content_switches() -> None:
    env = _image_env()
    assert env.get(_CAPTURE_VAR) == "false", (
        f"Dockerfile.inference-api must set {_CAPTURE_VAR}=false. ADOT defaults it to true "
        f"under AgentCore, which writes every Bedrock prompt and reply to otel-rt-logs."
    )
    tokens = {token.strip() for token in env.get(_SEMCONV_VAR, "").split(",")}
    assert "gen_ai_unredacted_attributes=" in tokens, (
        f"Dockerfile.inference-api must include the empty `gen_ai_unredacted_attributes=` token "
        f"in {_SEMCONV_VAR}. Without it Strands emits messages unredacted and ADOT copies them "
        f"to otel-rt-logs. Add other opt-in tokens alongside it, comma-separated."
    )


def test_strands_turn_reaches_otel_rt_logs_redacted(image_env: dict[str, str]) -> None:
    span_texts, log_texts = _run_strands_turn()

    assert log_texts, "LLOHandler emitted no log records; the otel-rt-logs path was not exercised"
    assert not _leaked(log_texts), f"conversation content reached otel-rt-logs: {_leaked(log_texts)}"
    assert not _leaked(span_texts), f"conversation content stayed on the spans: {_leaked(span_texts)}"
    assert "[REDACTED]" in "".join(log_texts)


def test_strands_turn_control_leaks_without_the_switch(capture_on: None) -> None:
    _, log_texts = _run_strands_turn()
    assert set(_leaked(log_texts)) == set(_ALL_SENTINELS), (
        "with redaction off every part of the turn should reach the log records; if it no longer "
        "does, the redaction test above has stopped proving anything"
    )


def test_documented_opt_back_in_restores_strands_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """``feedback_eval_sampling_enabled`` tells an environment that wants the
    AgentCore Evaluations judge to override the variable with this value."""
    monkeypatch.setenv(_SEMCONV_VAR, "gen_ai_unredacted_attributes=gen_ai.*")
    _, log_texts = _run_strands_turn()
    assert set(_leaked(log_texts)) == set(_ALL_SENTINELS)


def _botocore_converse_bodies() -> list[str]:
    """The event bodies the Converse instrumentation emits for one exchange,
    built the way ``bedrock.py`` builds them, via the same env-driven gate."""
    capture = genai_capture_message_content()
    request_messages = [
        {"role": "user", "content": [{"text": _PROMPT}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "lookup", "input": {"q": _TOOL_INPUT}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": _TOOL_RESULT}]}}]},
    ]
    records = [record for message in request_messages for record in message_to_event(message, capture)]
    response = {"output": {"message": {"role": "assistant", "content": [{"text": _REPLY}]}}, "stopReason": "end_turn"}
    records.append(_Choice.from_converse(response, capture).to_choice_event())
    return [text for record in records for text in _strings(record.body)]


def test_botocore_converse_events_carry_no_content(image_env: dict[str, str]) -> None:
    assert genai_capture_message_content() is False
    bodies = _botocore_converse_bodies()
    assert not _leaked(bodies), f"Bedrock Converse events still carry content: {_leaked(bodies)}"


def test_botocore_control_leaks_with_capture_on(capture_on: None) -> None:
    assert {_PROMPT, _TOOL_INPUT, _TOOL_RESULT, _REPLY} <= set(_leaked(_botocore_converse_bodies()))
