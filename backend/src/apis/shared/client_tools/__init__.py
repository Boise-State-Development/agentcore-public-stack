"""Client-executed tools: tools the *client* runs, with the agent loop on the server.

A terminal client can run commands, read files and list directories on the
user's machine. The server cannot, and must not try. What the server can do is
own the agent loop, so the protocol is:

1. The client declares the tools it can execute on each turn
   (``InvocationRequest.client_tools``, a list of :class:`ClientToolSpec`).
2. ``build_client_tools`` turns each spec into a Strands tool whose body raises
   an interrupt through ``ToolContext`` — the same mechanism
   ``ask_user_question`` uses, so the paused-turn snapshot, SSE extraction and
   the resume route all already exist.
3. ``stream_coordinator`` emits one ``client_tool_call`` SSE event per pending
   interrupt. The client runs the tool and posts the result back as an
   ``interrupt_responses`` entry.
4. On resume, Strands hands the client's response to the tool body, which
   returns it as the tool result. The model continues.

What never happens: a client tool executes anywhere but on the client that
declared it. The web app never sends ``client_tools``, so nothing in this
module runs for a web turn.

Trust boundary
--------------
The spec list is request input from an authenticated user. Names, descriptions
and schemas are bounded (:data:`MAX_CLIENT_TOOLS`, :data:`MAX_DESCRIPTION_CHARS`,
:data:`MAX_SCHEMA_BYTES`) because they are written into the model's
``toolConfig`` and therefore into the prompt. Results are bounded too
(:data:`MAX_RESULT_CHARS`), for the same reason. Beyond size, the server trusts
nothing about them: whether a command is safe to run is the client's decision,
made with the user present, which is why the TUI asks before executing.

Cost notes
----------
Each spec adds a tool to ``toolConfig``, which is part of the cached prompt
prefix. A client that sends the *same* specs every turn keeps the prefix
stable; the digest (:func:`client_tools_digest`) is a cache-key element so a
changed set rebuilds the agent rather than reusing one with stale tools.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

#: Caps. Each tool is prompt text; a client is not a plugin marketplace.
MAX_CLIENT_TOOLS = 16
MAX_NAME_CHARS = 64
MAX_DESCRIPTION_CHARS = 1024
MAX_SCHEMA_BYTES = 8 * 1024
#: The client's result is appended to the conversation as a tool result, so it
#: is capped here regardless of what the client did.
MAX_RESULT_CHARS = 32_000

#: Bedrock's tool-name rule, and also what keeps a spec name from colliding
#: with anything the registry owns: every client tool name is prefixed.
_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")

#: Prefix on the wire-visible tool name. The model sees ``local_run_command``;
#: the client declared ``run_command``. The prefix guarantees no collision with
#: registry, gateway or MCP tool names, and tells the model where the tool runs.
TOOL_NAME_PREFIX = "local_"

#: Interrupt reason discriminator, mirrored by the SSE event name.
INTERRUPT_TYPE = "client_tool_call"


class ClientToolSpec(BaseModel):
    """One tool the client can execute, as declared on the request."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    name: str = Field(..., description="Identifier, [a-zA-Z][a-zA-Z0-9_]*, max 64 chars")
    description: str = Field(..., description="What the tool does and when to use it; shown to the model")
    input_schema: Dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
        alias="inputSchema",
        description="JSON Schema for the arguments, as Bedrock's toolSpec.inputSchema.json",
    )

    @field_validator("name")
    @classmethod
    def _name_shape(cls, value: str) -> str:
        if not _NAME_RE.match(value):
            raise ValueError("client tool name must match [a-zA-Z][a-zA-Z0-9_]* and be at most 64 characters")
        return value

    @field_validator("description")
    @classmethod
    def _description_bounds(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("client tool description must not be empty")
        if len(text) > MAX_DESCRIPTION_CHARS:
            raise ValueError(f"client tool description exceeds {MAX_DESCRIPTION_CHARS} characters")
        return text

    @field_validator("input_schema")
    @classmethod
    def _schema_bounds(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError('client tool input_schema must be a JSON Schema with "type": "object"')
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode()) > MAX_SCHEMA_BYTES:
            raise ValueError(f"client tool input_schema exceeds {MAX_SCHEMA_BYTES} bytes")
        return value

    @property
    def wire_name(self) -> str:
        """The name the model sees and the client receives in ``client_tool_call``."""
        return f"{TOOL_NAME_PREFIX}{self.name}"


def validate_client_tools(specs: Optional[List[ClientToolSpec]]) -> List[ClientToolSpec]:
    """Dedupe and cap a request's spec list. Raises ``ValueError`` on overflow."""
    if not specs:
        return []
    if len(specs) > MAX_CLIENT_TOOLS:
        raise ValueError(f"at most {MAX_CLIENT_TOOLS} client tools may be declared per turn")
    seen: set[str] = set()
    out: List[ClientToolSpec] = []
    for spec in specs:
        if spec.name in seen:
            raise ValueError(f"client tool declared twice: {spec.name}")
        seen.add(spec.name)
        out.append(spec)
    return out


def client_tools_digest(specs: Optional[List[ClientToolSpec]]) -> str:
    """A stable digest of the declared tools, for the agent cache key.

    Empty string for none, so a turn without client tools keys exactly as it
    did before the field existed.
    """
    if not specs:
        return ""
    material = json.dumps(
        [spec.model_dump(by_alias=True) for spec in sorted(specs, key=lambda s: s.name)],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Wire event
# ---------------------------------------------------------------------------


class ClientToolCallEvent(BaseModel):
    """SSE event telling the client to run one of its tools and post the result.

    ``arguments`` is the model's tool input, passed through as-is. The client
    answers with an ``interrupt_responses`` entry whose ``response`` is a
    :class:`ClientToolResult`-shaped dict.
    """

    model_config = ConfigDict(populate_by_name=True)

    type: str = INTERRUPT_TYPE
    interrupt_id: str = Field(..., alias="interruptId")
    tool_use_id: str = Field(..., alias="toolUseId")
    tool_name: str = Field(..., alias="toolName", description="The client's declared name, without the prefix")
    arguments: Dict[str, Any] = Field(default_factory=dict)

    def to_sse_format(self) -> str:
        payload = self.model_dump(by_alias=True, exclude_none=True)
        return f"event: {INTERRUPT_TYPE}\ndata: {json.dumps(payload)}\n\n"


class ClientToolResult(BaseModel):
    """What the client posts back. Tolerant: a bare string is accepted upstream."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    output: str = ""
    is_error: bool = Field(default=False, alias="isError")
    #: Set by the client when the user refused to run the tool. Distinct from an
    #: error so the model can ask differently rather than retry.
    declined: bool = False


def coerce_result(response: Any) -> ClientToolResult:
    """Accept whatever the client sent and make a :class:`ClientToolResult` of it.

    Rejecting a malformed answer would strand the turn paused with no way out,
    so anything unrecognised becomes an error result that says so.
    """
    if isinstance(response, ClientToolResult):
        result = response
    elif isinstance(response, str):
        result = ClientToolResult(output=response)
    elif isinstance(response, dict):
        try:
            result = ClientToolResult.model_validate(response)
        except Exception as exc:  # noqa: BLE001 - never raise out of a resume
            result = ClientToolResult(output=f"client returned an unreadable result: {exc}", is_error=True)
    else:
        result = ClientToolResult(output=f"client returned an unreadable result of type {type(response).__name__}", is_error=True)

    if len(result.output) > MAX_RESULT_CHARS:
        kept = result.output[:MAX_RESULT_CHARS]
        result = result.model_copy(update={"output": f"{kept}\n… [truncated: {len(result.output):,} characters total]"})
    return result


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_client_tools(specs: Optional[List[ClientToolSpec]]) -> list:
    """One interrupting Strands tool per spec. Empty list for none.

    Imported lazily so this module stays importable in contexts without the
    Strands SDK (app-api validates the request shape but never builds tools).
    """
    specs = validate_client_tools(specs)
    if not specs:
        return []

    from strands import tool as strands_tool
    from strands.types.tools import ToolContext

    built = []
    for spec in specs:
        built.append(_make_tool(spec, strands_tool, ToolContext))
    logger.info("Built %d client-executed tool(s): %s", len(built), ", ".join(s.wire_name for s in specs))
    return built


def _make_tool(spec: ClientToolSpec, strands_tool: Any, tool_context_type: Any) -> Any:
    """Build one tool. A closure per spec so each carries its own name/schema.

    Strands validates a call's input against the *Python signature* of the
    decorated function (it builds a Pydantic model from the parameters), not
    against the ``inputSchema`` passed to the decorator — that only shapes
    what the model sees. A body declared as ``**arguments`` therefore rejected
    every real call with "Field required: arguments" before the interrupt could
    fire. The body is given one keyword-only parameter per schema property,
    with ``Any`` annotations and defaults for the optional ones, so the
    validator accepts exactly what the schema advertises.
    """
    properties = spec.input_schema.get("properties") or {}
    required = set(spec.input_schema.get("required") or ())
    names = [name for name in properties if isinstance(name, str) and name.isidentifier() and name != "tool_context"]

    def _run(tool_context: Any, **arguments: Any) -> Dict[str, Any]:
        tool_use = getattr(tool_context, "tool_use", None) or {}
        tool_use_id = tool_use.get("toolUseId", "")
        # Optional parameters the model left unset arrive as None (the
        # synthesised default); drop them so the client sees only what was
        # actually passed. A model that wants to send null explicitly cannot,
        # but no declared schema here has a nullable argument.
        args = {key: value for key, value in arguments.items() if value is not None}
        logger.info("Pausing for client tool %s tool_use_id=%s", spec.wire_name, tool_use_id)
        response = tool_context.interrupt(
            name=INTERRUPT_TYPE,
            reason={
                "type": INTERRUPT_TYPE,
                "toolUseId": tool_use_id,
                "toolName": spec.name,
                "arguments": args,
            },
        )
        result = coerce_result(response)
        if result.declined:
            return {
                "status": "error",
                "content": [{"text": "The user declined to run this tool. Do not retry it; ask them or proceed without it."}],
            }
        return {
            "status": "error" if result.is_error else "success",
            "content": [{"text": result.output or "(no output)"}],
        }

    parameters = [inspect.Parameter("tool_context", inspect.Parameter.KEYWORD_ONLY, annotation=tool_context_type)]
    for name in names:
        default = inspect.Parameter.empty if name in required else None
        parameters.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=Any))
    _run.__signature__ = inspect.Signature(parameters, return_annotation=Dict[str, Any])  # type: ignore[attr-defined]
    _run.__annotations__ = {"tool_context": tool_context_type, **{name: Any for name in names}, "return": Dict[str, Any]}
    _run.__name__ = spec.wire_name
    _run.__doc__ = f"{spec.description}\n\nRuns on the user's own machine; the user is asked before it executes."

    return strands_tool(
        name=spec.wire_name,
        description=_run.__doc__,
        inputSchema={"json": spec.input_schema},
        context=True,
    )(_run)
