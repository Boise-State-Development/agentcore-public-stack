"""Outbound trace context carries trace ids, never baggage.

Under AgentCore, ADOT sets ``OTEL_PROPAGATORS=baggage,xray,tracecontext``, and
every invocation arrives with W3C baggage ``session.id=<runtime session id>``.
That id is ``runtime_session_id_for(session, user)``: a stable hash that names
one user's conversation. The global propagator injects whatever baggage is in
context into every outbound carrier that uses it:

- the HTTP headers of every httpx/httpx2 and urllib request (the OTel
  instrumentors), which reach third-party MCP servers (GitHub, Zoom, Oracle
  Analytics) and every site ``fetch_url_content`` opens;
- the ``params._meta`` of every MCP request. The mcp 2.x SDK injects there
  per SEP-414, and Strands' ``MCPClient`` does the same for ``tools/call``.

So a third party learned an identifier for the user's conversation on every
call. Trace ids (``traceparent``, ``X-Amzn-Trace-Id``) are random and carry
nothing, so they keep flowing, and a server that reads them can join its logs
to the turn's trace. ``tests/security/test_outbound_trace_context.py`` pins
this on the real MCP wire.

Extraction is unchanged. The inbound ``session.id`` baggage stays in context,
and ADOT's ``BaggageSpanProcessor`` still copies it onto our own spans, which
is how the AgentCore Observability console groups a session.

botocore is unaffected either way: its instrumentation uses its own X-Ray
propagator, so AWS API calls never carried baggage.
"""

from __future__ import annotations

from typing import Optional, Set

from opentelemetry import baggage, propagate
from opentelemetry.context import Context, get_current
from opentelemetry.propagators.textmap import (
    CarrierT,
    Getter,
    Setter,
    TextMapPropagator,
    default_getter,
    default_setter,
)

_BAGGAGE_FIELD = "baggage"


class OutboundWithoutBaggage(TextMapPropagator):
    """Wraps the configured propagator: extracts as it does, injects without baggage.

    The inner propagator is whatever the deployment configured, so the trace
    formats it chose (``tracecontext``, ``xray``, ``b3``…) are unchanged. Only
    the context handed to ``inject`` has its baggage removed.
    """

    def __init__(self, inner: TextMapPropagator) -> None:
        self._inner = inner

    @property
    def inner(self) -> TextMapPropagator:
        return self._inner

    def extract(
        self,
        carrier: CarrierT,
        context: Optional[Context] = None,
        getter: Getter[CarrierT] = default_getter,
    ) -> Context:
        return self._inner.extract(carrier, context=context, getter=getter)

    def inject(
        self,
        carrier: CarrierT,
        context: Optional[Context] = None,
        setter: Setter[CarrierT] = default_setter,
    ) -> None:
        outbound = baggage.clear(context if context is not None else get_current())
        self._inner.inject(carrier, context=outbound, setter=setter)

    @property
    def fields(self) -> Set[str]:
        return {field for field in self._inner.fields if field.lower() != _BAGGAGE_FIELD}


def install_outbound_trace_policy() -> None:
    """Make the global propagator inject trace ids only. Idempotent.

    Call once at process start, after ``opentelemetry-instrument`` has set the
    global propagator and before the first outbound request. Each call site
    (``opentelemetry.propagate.inject``, the httpx instrumentors, the MCP SDK,
    Strands) reads the global propagator per request, so the wrapper covers
    them all.
    """
    current = propagate.get_global_textmap()
    if isinstance(current, OutboundWithoutBaggage):
        return
    propagate.set_global_textmap(OutboundWithoutBaggage(current))
