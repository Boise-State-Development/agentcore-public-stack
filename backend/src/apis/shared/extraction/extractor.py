"""The constrained model call: a document and a schema, and nothing else.

``docs/specs/structured-extraction.md`` §5, §6, D1, D11, D15. This is the side
channel the whole design turns on, in the same sense ``session_title`` and
``tool_summaries`` are: its own request, its own messages, no conversation
appended to and none read. It therefore has **no cacheable prefix to bust**,
which is what lets the field schema carry real types and enums without any of
the 30k–150k-token prefix re-writes a per-session ``toolConfig`` change would
cost.

WHY A DEDICATED MODEL INSTANCE (D15)
------------------------------------
``BedrockModel(strict_tools=True)`` is a **config** flag: it adds
``strict: true`` to *every* tool spec in that model's requests and restricts
the JSON Schema features every one of them may use, and a schema using an
unsupported feature fails the whole request with a ``ValidationException``. Our
agents carry MCP tool schemas we neither author nor can fix, where ``oneOf`` is
ordinary — so setting this on the chat agent's model would let a third party's
tool definition fail an entire turn. The extraction path constructs its own
instance and shares nothing with ``agent_factory``'s. A test asserts this is
the only place in the tree that sets it.

WHY NOT THE AGENT-LEVEL PATH
----------------------------
Strands also offers ``Agent(structured_output_model=…)``, which
``register_dynamic_tool``s the schema into *that agent's* registry — putting it
in the turn's own ``toolConfig`` — and appends a forcing prompt to the
conversation. That is both halves of what D1 refuses. Only the model-level
``BedrockModel.structured_output`` is used here.

THE FALLBACK IS MARKED, NEVER SILENT (D11)
------------------------------------------
Grammar compilation is a latency cliff: a new schema takes up to a few minutes
on first use, then is cached 24 hours per account for identical schemas. On a
timeout, on a ``ValidationException`` naming an unsupported schema feature, or
on the ``ValueError`` Strands raises when the model answers without calling the
forced tool, the call retries **once without** ``strict_tools`` — same prompt,
same Pydantic validation on return — and the run is marked
``schema_enforced=False``. That marker reaches the SSE event, the stored row
and the artifact header, because a reviewer has to be able to tell types that
were guaranteed from types that were merely requested.

NO TURN-SCOPED STATE
--------------------
:func:`extract_fields` is a function of ``(document text, field set)`` and
holds nothing. A later change may run it N times concurrently inside one tool
call to fan out over several documents (G5); nothing here assumes it is the
only extraction in flight, and nothing is keyed on "the current turn".
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type

from pydantic import BaseModel, ValidationError

from .field_sets import FieldSet
from .schema import model_for_field_set

logger = logging.getLogger(__name__)

#: Seconds the constrained call may take before the unenforced retry. Generous
#: because the first use of a new schema pays grammar compilation ("up to a few
#: minutes", §6.4) — and the retry is a downgrade, so timing out early would
#: trade a guaranteed schema for a requested one to save a wait nobody is
#: watching. The tool runs mid-turn, never before the first token.
STRICT_TIMEOUT_SECONDS = 210.0

#: Seconds for the unenforced retry. Shorter: there is no grammar to compile.
FALLBACK_TIMEOUT_SECONDS = 120.0

#: Characters of document text one call may carry. The document is already
#: bounded upstream by ``document_read``'s source-size limit; this is the
#: second line, and it is applied per call so a `pages` narrowing is what the
#: caller reaches for rather than this silently cutting the tail.
MAX_DOCUMENT_CHARS = 400_000

_SYSTEM_PROMPT = (
    "You extract specific named fields from a single document, exactly as the "
    "document states them.\n\n"
    "Rules, in order of importance:\n"
    "1. Every value must come from the document in front of you. Never infer a "
    "value, never estimate one, never carry one over from a similar document, "
    "and never repeat a value from another field because it looks plausible.\n"
    "2. If the document does not state a field, set its value to null and its "
    "quote to null. A null is a correct, useful answer. A guess is not, and it "
    "is worse than a blank because someone will rely on it.\n"
    "3. For every value you do give, quote the passage you took it from "
    "verbatim — copied character for character from the document, including its "
    "own punctuation and casing. Do not tidy it, do not join lines, do not add "
    "quotation marks of your own, and do not paraphrase. The quote is checked "
    "against the document automatically, and a reconstructed quote fails that "
    "check even when the value is right.\n"
    "4. Quote the shortest passage that actually carries the value — ideally "
    "the sentence containing it.\n"
    "5. Give the value as the document expresses it. Keep currency symbols and "
    "thousands separators as written. Do not convert units or reformat numbers."
)


class ExtractionError(RuntimeError):
    """The constrained call could not produce a validated result.

    A conversational error from the tool. Raised only when both the strict call
    and the unenforced retry failed, so by this point the honest answer to the
    user is that extraction did not run — not a table of nulls, which would be
    indistinguishable from a document that genuinely says nothing.
    """


@dataclass(frozen=True)
class ExtractionResult:
    """One run's typed answer, plus whether the schema was actually enforced."""

    #: ``{field_name: {"value": …, "quote": …}}`` in the field set's order.
    rows: Dict[str, Dict[str, Any]]
    #: False when the run fell back to an unenforced request (D11).
    schema_enforced: bool
    model_id: str
    #: Token usage for the metering row (D12), empty when the SDK reported none.
    usage: Dict[str, int]


def _build_model(model_id: str, *, strict: bool):
    """A ``BedrockModel`` for the constrained call, built per call.

    Imported lazily so this module stays importable (and the parser and schema
    builder stay unit-testable) without Strands or boto on the path.
    """
    from strands.models import BedrockModel

    return BedrockModel(model_id=model_id, strict_tools=True) if strict else BedrockModel(
        model_id=model_id
    )


def _prompt(field_set: FieldSet, document_text: str, filename: str) -> List[Dict[str, Any]]:
    """The one user message the constrained call carries.

    The document and the field set's own instructions. Deliberately not the
    conversation: extraction on turn 40 of a long thread costs exactly what it
    costs on turn 1, and nothing the user said earlier can talk the extractor
    out of reading the document.
    """
    wanted = "\n".join(
        f"- {field.name} ({field.type}): {field.find}"
        + (f" One of: {', '.join(field.enum)}." if field.enum else "")
        + (" May legitimately be absent." if field.optional else "")
        for field in field_set.fields
    )
    return [
        {
            "role": "user",
            "content": [
                {
                    "text": (
                        f"Document: {filename}\n"
                        f"Field set: {field_set.title} (version {field_set.version})\n\n"
                        f"Fields to find:\n{wanted}\n\n"
                        "--- BEGIN DOCUMENT ---\n"
                        f"{document_text}\n"
                        "--- END DOCUMENT ---"
                    )
                }
            ],
        }
    ]


def _normalize_usage(usage: Any) -> Dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    normalized = {
        "inputTokens": int(usage.get("inputTokens") or 0),
        "outputTokens": int(usage.get("outputTokens") or 0),
        "cacheReadInputTokens": int(usage.get("cacheReadInputTokens") or 0),
        "cacheWriteInputTokens": int(usage.get("cacheWriteInputTokens") or 0),
    }
    # Strands seeds usage at zero and overwrites it when the provider's
    # metadata chunk arrives, so an all-zero reading is "not reported yet"
    # rather than a real measurement. Returning {} for it keeps the caller
    # from metering a call at zero tokens, which would price to $0 and look
    # exactly like a model with no catalog row.
    return normalized if (normalized["inputTokens"] or normalized["outputTokens"]) else {}


def _usage_from(event: Dict[str, Any]) -> Dict[str, int]:
    """Token usage out of one ``process_stream`` event, or ``{}``.

    **The shapes here are Strands', not the provider's**, and getting them
    wrong is silent: ``_meter`` skips an empty reading, so a missed usage block
    writes no ``C#`` row at all — worse than the unpriced-model case, because
    not even ``UnmeteredModelCall`` fires to say so.

    Two places carry it, read in order of reliability:

    * ``event["stop"]`` — the final ``ModelStopReason``, a 4-tuple of
      ``(stop_reason, message, usage, metrics)``. This is the authoritative
      one: it is emitted once per call, after the provider's metadata chunk
      has been folded in.
    * ``event["event"]["metadata"]["usage"]`` — a ``ModelStreamChunkEvent``
      wrapping the provider's raw metadata chunk. The wrapper key is
      ``"event"``, not ``"metadata"``; the metadata is one level further in.

    Best-effort: raising here would lose a good extraction over bookkeeping.
    """
    try:
        stop = event.get("stop")
        if isinstance(stop, (tuple, list)) and len(stop) >= 3:
            usage = _normalize_usage(stop[2])
            if usage:
                return usage

        chunk = event.get("event")
        if isinstance(chunk, dict):
            metadata = chunk.get("metadata")
            if isinstance(metadata, dict):
                usage = _normalize_usage(metadata.get("usage"))
                if usage:
                    return usage
    except Exception:  # noqa: BLE001 - metering must never fail an extraction
        logger.debug("extraction usage not readable", exc_info=True)
    return {}


async def _run_once(
    model: Any,
    output_model: Type[BaseModel],
    prompt: List[Dict[str, Any]],
    timeout: float,
) -> Tuple[BaseModel, Dict[str, int]]:
    """One ``structured_output`` pass. Returns the validated model and usage."""

    async def _drive() -> Tuple[BaseModel, Dict[str, int]]:
        output: Optional[BaseModel] = None
        usage: Dict[str, int] = {}
        async for event in model.structured_output(
            output_model, prompt, system_prompt=_SYSTEM_PROMPT
        ):
            if not isinstance(event, dict):
                continue
            # Overwrite rather than keep the first: the authoritative reading
            # is on the final `stop` event, which arrives last.
            reading = _usage_from(event)
            if reading:
                usage = reading
            if "output" in event:
                output = event["output"]
        if output is None:
            raise ExtractionError("the extraction call produced no structured output")
        return output, usage

    return await asyncio.wait_for(_drive(), timeout=timeout)


#: Bedrock's own words when a schema is too big or too complex to compile.
#: **Measured on dev, 2026-10-20** — each of these is a distinct error, none is
#: documented, and the first arrives in under a second while the third can take
#: ninety:
#:
#:   * "parameters with union types"  — more than 16 nullable/anyOf parameters.
#:   * "cause performance issues"     — a fast pre-check on overall complexity.
#:   * "too complex for compilation"  — the same verdict, reached later.
#:   * "grammar size"                 — compiled past the 300MB ceiling.
#:
#: These are **authoring** failures, not transient ones, and they are the
#: reason the unenforced fallback must not swallow everything a
#: ``ValidationException`` can mean: a field set that is too wide would
#: otherwise downgrade to unenforced on *every* run, forever, and the office
#: that authored it would never be told. See :func:`_capacity_failure`.
_CAPACITY_MARKERS = (
    "parameters with union types",
    "cause performance issues",
    "too complex for compilation",
    "grammar size",
)


class SchemaTooComplexError(ExtractionError):
    """The field set is wider or more complex than Bedrock will compile.

    Separate from :class:`ExtractionError` so the tool can tell the model this
    is the *author's* problem to fix, not the user's request to retry.
    """


def _capacity_failure(exc: BaseException) -> bool:
    """Whether *exc* says the schema itself is too big to compile."""
    message = str(exc)
    return any(marker in message for marker in _CAPACITY_MARKERS)


def _should_fall_back(exc: BaseException) -> bool:
    """Whether *exc* is one of D11's marked-fallback causes.

    Narrow on purpose, in two directions.

    A credentials error, a throttle or an access denial is not a reason to
    retry without enforcement — it is a reason to fail, because retrying would
    turn an infrastructure problem into a quietly weaker result.

    And a **capacity** failure is not a reason either, which is the correction
    measurement forced: the spec's D11 would have retried unenforced on any
    schema-shaped ``ValidationException``, so a field set one field too wide
    would have produced unenforced types on every run for the life of the
    deployment, with only a ``schemaEnforced: false`` marker to show for it.
    That is a fixable authoring mistake wearing the costume of a transient
    fault. It raises instead.
    """
    if _capacity_failure(exc):
        return False
    if isinstance(exc, asyncio.TimeoutError):
        # Grammar compilation on a schema Bedrock has not seen (§6.4).
        return True
    if isinstance(exc, ValueError) and not isinstance(exc, ValidationError):
        # Strands raises this when the model answered without calling the
        # forced tool, or returned no usable tool input.
        return True
    name = type(exc).__name__
    if name == "ValidationException":
        return True
    message = str(exc)
    return "ValidationException" in message and (
        "schema" in message.lower() or "strict" in message.lower()
    )


async def extract_fields(
    field_set: FieldSet,
    document_text: str,
    *,
    filename: str,
    model_id: str,
) -> ExtractionResult:
    """Run *field_set* over *document_text* on its own constrained request.

    Pure with respect to the turn: no session, no user, no conversation, and no
    state kept between calls. Safe to run concurrently with itself.

    Raises:
        ExtractionError: neither the strict call nor the unenforced retry
            produced a validated result, or the model id is unset.
    """
    if not model_id:
        raise ExtractionError(
            "DOCUMENT_EXTRACT_MODEL_ID is not set on this runtime, so there is "
            "no model to run the extraction on."
        )
    text = (document_text or "").strip()
    if not text:
        raise ExtractionError(
            "the document has no readable text layer, so there is nothing to "
            "extract from. A scanned PDF needs OCR first."
        )
    if len(text) > MAX_DOCUMENT_CHARS:
        raise ExtractionError(
            f"the document is {len(text)} characters, over the "
            f"{MAX_DOCUMENT_CHARS}-character limit for one extraction. Narrow "
            "it with the `pages` argument and run the field set per section."
        )

    output_model = model_for_field_set(field_set)
    prompt = _prompt(field_set, text, filename)

    try:
        output, usage = await _run_once(
            _build_model(model_id, strict=True),
            output_model,
            prompt,
            STRICT_TIMEOUT_SECONDS,
        )
        enforced = True
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified by _should_fall_back
        if _capacity_failure(exc):
            raise SchemaTooComplexError(
                f"the field set '{field_set.name}' is too complex for Bedrock to "
                f"compile a constrained schema from ({exc}). This is a limit on "
                "the field set itself, not on the document — it needs splitting "
                "into two smaller sets by whoever maintains the skill."
            ) from exc
        if not _should_fall_back(exc):
            raise ExtractionError(f"the extraction call failed: {exc}") from exc
        logger.warning(
            "document_extract: strict schema call failed for field set %r (%s: %s); "
            "retrying unenforced and marking the run",
            field_set.name,
            type(exc).__name__,
            exc,
        )
        try:
            output, usage = await _run_once(
                _build_model(model_id, strict=False),
                output_model,
                prompt,
                FALLBACK_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as retry_exc:  # noqa: BLE001
            raise ExtractionError(
                f"the extraction call failed, with and without schema "
                f"enforcement: {retry_exc}"
            ) from retry_exc
        enforced = False

    # Read back through the field set's own order, so the rows are ordered by
    # the authored table and not by whatever order the model emitted.
    rows: Dict[str, Dict[str, Any]] = {}
    for field in field_set.fields:
        answer = _answer_for(output, field.name)
        rows[field.name] = {
            "value": getattr(answer, "value", None),
            "quote": getattr(answer, "quote", None),
        }

    return ExtractionResult(
        rows=rows,
        schema_enforced=enforced,
        model_id=model_id,
        usage=usage,
    )


def _answer_for(output: BaseModel, field_name: str) -> Any:
    """One field's answer off the validated model, found by its **alias**.

    The alias is looked up first, and ``getattr`` is only the fallback. That
    order is load-bearing rather than tidy: a field whose authored name
    collides with a ``BaseModel`` attribute is bound to a renamed Python
    attribute while keeping the authored name as its alias (see
    ``schema._attribute_name``) — and for a name like ``json`` or ``copy``,
    ``getattr(output, "json")`` happily returns Pydantic's own deprecated
    *method*, which is not ``None``, so an attribute-first lookup silently
    returns a bound method in place of the extracted value.
    """
    for name, info in type(output).model_fields.items():
        if info.alias == field_name or name == field_name:
            return getattr(output, name, None)
    return None


__all__ = [
    "FALLBACK_TIMEOUT_SECONDS",
    "MAX_DOCUMENT_CHARS",
    "STRICT_TIMEOUT_SECONDS",
    "ExtractionError",
    "ExtractionResult",
    "SchemaTooComplexError",
    "extract_fields",
]
