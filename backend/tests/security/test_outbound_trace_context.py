"""Outbound requests carry trace ids, never the inbound ``session.id`` baggage.

Every invocation reaches the Runtime with W3C baggage ``session.id=<runtime
session id>``, a stable hash naming one user's conversation, and ADOT's
propagators (``baggage,xray,tracecontext``) inject that baggage into every
outbound carrier. Before ``install_outbound_trace_policy`` it went to every MCP
server twice per request, in the HTTP headers (the httpx2 instrumentor) and in
``params._meta`` (the mcp 2.x SDK), and in the headers of every site
``fetch_url_content`` opened. These tests run the propagators ADOT configures and
the production MCP path (Strands' ``MCPClient`` over our streamable-HTTP
transport, httpx2 instrumented the way ``opentelemetry-instrument`` does it)
and read the bytes the server received. Each has a control without the policy
that expects the baggage to appear, so a test that stops exercising the inject
path cannot pass by accident.
"""

from __future__ import annotations

import ast
import os
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import mcp.shared._otel as mcp_otel
import pytest
from amazon.opentelemetry.distro.aws_opentelemetry_distro import AwsOpenTelemetryDistro
from opentelemetry import baggage, context, propagate
from opentelemetry.context import Context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import set_span_in_context

from apis.shared.observability.trace_propagation import (
    OutboundWithoutBaggage,
    install_outbound_trace_policy,
)

_SESSION_ID = "sid-SENTINEL-7f3a"
_INFERENCE_MAIN = Path(__file__).resolve().parents[2] / "src" / "apis" / "inference_api" / "main.py"


@pytest.fixture
def adot_propagators() -> Iterator[None]:
    """The global propagator exactly as ADOT configures it under AgentCore.

    ADOT only ``setdefault``s ``OTEL_PROPAGATORS`` and then reloads
    ``opentelemetry.propagate``, so running its configure step with the variable
    unset reproduces the container's propagator. If ADOT changes its default,
    these tests follow it. The environment and the previous global propagator
    are restored afterwards.
    """
    saved = propagate.get_global_textmap()
    with patch.dict(os.environ):
        os.environ.pop("OTEL_PROPAGATORS", None)
        AwsOpenTelemetryDistro()._configure(apply_patches=False)
        assert {"baggage", "traceparent"} <= set(propagate.get_global_textmap().fields), (
            "ADOT no longer configures baggage and tracecontext propagation; re-check what "
            "reaches outbound requests before trusting the tests below"
        )
        try:
            yield
        finally:
            propagate.set_global_textmap(saved)


@pytest.fixture
def provider() -> TracerProvider:
    return TracerProvider()


def _in_session_context(provider: TracerProvider):
    """A span plus the inbound baggage every dev span carries, as the Runtime sees a turn."""
    span = provider.get_tracer("test").start_span("turn")
    ctx = baggage.set_baggage("session.id", _SESSION_ID, Context())
    return span, set_span_in_context(span, ctx)


def _carrier(provider: TracerProvider) -> dict[str, str]:
    span, ctx = _in_session_context(provider)
    carrier: dict[str, str] = {}
    propagate.inject(carrier, context=ctx)
    span.end()
    return carrier


def test_control_adot_injects_the_session_baggage(adot_propagators: None, provider: TracerProvider) -> None:
    carrier = _carrier(provider)
    assert carrier.get("baggage") == f"session.id={_SESSION_ID}"


def test_policy_injects_trace_ids_without_baggage(adot_propagators: None, provider: TracerProvider) -> None:
    install_outbound_trace_policy()
    carrier = _carrier(provider)

    assert "baggage" not in {key.lower() for key in carrier}
    assert _SESSION_ID not in "".join(carrier.values())
    assert carrier.get("traceparent", "").startswith("00-")
    assert carrier.get("X-Amzn-Trace-Id", "").startswith("Root=1-")
    assert "baggage" not in propagate.get_global_textmap().fields


def test_policy_still_extracts_inbound_baggage(adot_propagators: None) -> None:
    """ADOT's BaggageSpanProcessor copies ``session.id`` from the extracted
    context onto our spans; the AgentCore console groups sessions by it."""
    install_outbound_trace_policy()
    ctx = propagate.extract(
        {
            "baggage": f"session.id={_SESSION_ID}",
            "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        }
    )
    assert baggage.get_baggage("session.id", ctx) == _SESSION_ID


def test_install_is_idempotent(adot_propagators: None) -> None:
    install_outbound_trace_policy()
    first = propagate.get_global_textmap()
    install_outbound_trace_policy()
    assert propagate.get_global_textmap() is first
    assert isinstance(first, OutboundWithoutBaggage)
    assert not isinstance(first.inner, OutboundWithoutBaggage)


@pytest.fixture
def instrumented_http(provider: TracerProvider) -> Iterator[None]:
    """The httpx and httpx2 instrumentors, loaded as ``opentelemetry-instrument`` does."""
    loaded = []
    for entry_point in entry_points(group="opentelemetry_instrumentor"):
        if entry_point.name in {"httpx", "httpx2"}:
            instrumentor = entry_point.load()()
            instrumentor.instrument(tracer_provider=provider)
            loaded.append(instrumentor)
    assert {type(i).__name__ for i in loaded} >= {"HTTPX2ClientInstrumentor"}, (
        "the httpx2 instrumentor is no longer registered; the MCP transport's HTTP headers "
        "may have stopped carrying trace context, so re-check what these tests prove"
    )
    try:
        yield
    finally:
        for instrumentor in loaded:
            instrumentor.uninstrument()


def _mcp_wire(provider: TracerProvider) -> list[tuple[dict[str, Any], dict[str, str]]]:
    """One ``tools/list`` and one ``tools/call`` inside a turn's context, over the
    production client path. Returns each request's ``_meta`` and HTTP headers."""
    from strands.tools.mcp import MCPClient

    from agents.main_agent.integrations.mcp_transport import streamable_http_transport
    from tests.fixtures.flaky_mcp_server import FlakyMCPServer

    def setup(server: Any) -> None:
        @server.tool()
        def lookup(query: str) -> str:
            return f"ok {query}"

    span, ctx = _in_session_context(provider)
    with patch.object(mcp_otel, "_tracer", provider.get_tracer("mcp-python-sdk")), FlakyMCPServer(
        fail_calls=False, setup=setup
    ) as server:
        token = context.attach(ctx)
        try:
            client = MCPClient(lambda: streamable_http_transport(server.url))
            client.start()
            try:
                client.list_tools_sync()
                client.call_tool_sync("t-1", "lookup", {"query": "q"})
            finally:
                client.stop(None, None, None)
        finally:
            context.detach(token)
            span.end()
        wire = []
        for body, headers in zip(server.requests, server.request_headers):
            for message in body if isinstance(body, list) else [body]:
                if isinstance(message, dict) and message.get("method"):
                    wire.append(((message.get("params") or {}).get("_meta") or {}, headers))
    assert len(wire) >= 3, f"expected discover/initialize, tools/list and tools/call; saw {len(wire)}"
    return wire


def test_control_mcp_requests_carry_the_session_baggage(
    adot_propagators: None, instrumented_http: None, provider: TracerProvider
) -> None:
    wire = _mcp_wire(provider)
    assert all(meta.get("baggage") == f"session.id={_SESSION_ID}" for meta, _ in wire)
    assert all(headers.get("baggage") == f"session.id={_SESSION_ID}" for _, headers in wire)


def test_mcp_requests_carry_trace_ids_not_baggage(
    adot_propagators: None, instrumented_http: None, provider: TracerProvider
) -> None:
    install_outbound_trace_policy()
    wire = _mcp_wire(provider)

    for meta, headers in wire:
        assert "baggage" not in meta, f"MCP _meta still carries baggage: {meta}"
        assert "baggage" not in headers, "MCP HTTP headers still carry baggage"
        assert _SESSION_ID not in repr(meta) + repr(headers)
        # Trace ids keep flowing, so a server can join its logs to the turn.
        assert meta.get("traceparent", "").startswith("00-")
        assert headers.get("traceparent", "").startswith("00-")


def test_inference_api_installs_the_policy_at_import() -> None:
    """The call must be module-level in ``main.py``: that runs after
    ``opentelemetry-instrument`` has set the global propagator and before any
    request goes out."""
    tree = ast.parse(_INFERENCE_MAIN.read_text(encoding="utf-8"))
    calls = [
        node.value.func.id
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]
    assert "install_outbound_trace_policy" in calls
