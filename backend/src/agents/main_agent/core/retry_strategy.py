"""Retry strategy that widens Strands' throttling-only retry to Bedrock's
transient service faults.

WHY THIS EXISTS
Strands' ``BedrockModel`` maps exactly one error code to a retryable
exception — ``ThrottlingException`` becomes ``ModelThrottledException``
(see ``strands/models/bedrock.py``). Every other modeled Bedrock error,
including the transient server-side faults AWS explicitly documents as
"retry the request", re-raises as a raw ``botocore.exceptions.ClientError``.
The stock ``ModelRetryStrategy.is_retryable`` only matches
``ModelThrottledException``, so those faults reach the user as a
conversational error on the first occurrence with no second attempt.

Observed in prod 2026-08-31 on session ``5f34d2b0``: a ``ConverseStream``
call carrying two PDFs failed with ``ServiceUnavailableException`` after
95.6s, was never retried, charged the user for 56,440 uncached input tokens,
and returned zero output. The user's attachments were consumed by that dead
turn, which then cascaded into a re-upload loop.

WHY MID-STREAM FAILURES ARE DELIBERATELY NOT RETRIED
``EventStreamError`` (a ``ClientError`` subclass) is raised while iterating
``response["stream"]`` — i.e. after ``converse_stream`` returned and, in
general, after chunks have already been handed to the callback and forwarded
to the SSE client. Retrying there restarts generation from scratch and the
user sees the abandoned prefix followed by a second, full response. A plain
``ClientError`` from the ``converse_stream`` call itself means the request
was rejected before the stream opened, so nothing was emitted and a retry is
invisible. We retry only the latter. ``ModelThrottledException`` keeps the
SDK's existing semantics unchanged — this strategy only ever widens the
retryable set, never narrows it.
"""

import logging
from typing import Optional

import openai
from botocore.exceptions import ClientError, EventStreamError
from strands import ModelRetryStrategy

from apis.shared.models.stream_output import streamed_partial_output

logger = logging.getLogger(__name__)


# Bedrock error codes that are safe and worthwhile to retry when they are
# raised before the response stream opens. All of these are documented by
# AWS as transient server-side conditions.
#
# ThrottlingException is listed for completeness: Strands normally converts
# it to ModelThrottledException (already handled by the superclass), but the
# lowercase `throttlingException` spelling it also checks for shows the code
# is not stable across services, so matching here costs nothing and closes
# the gap if a spelling slips past that mapper.
RETRYABLE_BEDROCK_ERROR_CODES = frozenset(
    {
        "ServiceUnavailableException",  # 503 — the prod failure this fixes
        "InternalServerException",  # 500
        "ModelNotReadyException",  # model warming up; AWS says retry
        "ModelTimeoutException",  # request timed out inside Bedrock
        "ThrottlingException",
        "throttlingException",
        "TooManyRequestsException",
        "RequestTimeout",
        "RequestTimeoutException",
    }
)


def bedrock_error_code(exception: BaseException) -> Optional[str]:
    """Return the modeled Bedrock/botocore error code, or ``None``.

    Only reads ``ClientError.response``; anything else (including a
    ``ClientError`` with a malformed response payload) yields ``None`` so the
    caller falls through to "not retryable".
    """
    if not isinstance(exception, ClientError):
        return None
    try:
        code = exception.response.get("Error", {}).get("Code")
    except AttributeError:  # pragma: no cover - defensive
        return None
    return code if isinstance(code, str) else None


class BedrockTransientRetryStrategy(ModelRetryStrategy):
    """``ModelRetryStrategy`` that also retries pre-stream Bedrock faults.

    Everything except the retryable-exception predicate is inherited: the same
    exponential backoff, the same ``max_attempts`` budget, the same reset on a
    successful call. See the module docstring for the mid-stream carve-out.
    """

    def is_retryable(self, exception: Exception) -> bool:
        """Whether ``exception`` should trigger another model attempt."""
        if super().is_retryable(exception):
            return True

        # Mid-stream failure: chunks may already be on the wire. Restarting
        # would duplicate visible output, so surface it as an error instead.
        if isinstance(exception, EventStreamError):
            logger.info(
                "Not retrying mid-stream Bedrock failure (code=%s); "
                "partial output may already have reached the client",
                bedrock_error_code(exception),
            )
            return False

        code = bedrock_error_code(exception)
        if code in RETRYABLE_BEDROCK_ERROR_CODES:
            logger.warning("Retrying transient Bedrock fault: %s", code)
            return True

        return False


# Server-side faults the OpenAI Responses API reports with no HTTP status:
# they arrive as an `error` / `response.failed` event inside a 200 stream, so
# the OpenAI client's own retries (which only see HTTP responses) never fire.
# Matched against the lowercased message. Both entries are the exact texts
# seen in prod on GPT-6 Sol and Luna (2026-09/10).
_OPENAI_SERVER_FAULT_MARKERS = (
    "server had an error while processing your request",
    "temporarily unavailable",
)

# Error codes the Responses API puts on those same stream events.
_OPENAI_SERVER_FAULT_CODES = frozenset({"server_error", "service_unavailable"})


def is_transient_openai_fault(exception: BaseException) -> bool:
    """Whether an OpenAI-family error is a transient fault on the provider's side.

    Covers both ways it arrives: as an HTTP error before the stream opens
    (5xx, connection reset, timeout) and as an error event inside an open
    stream, which carries a code or message but no status. A 4xx is never
    transient here; 429 is already ``ModelThrottledException`` by the time it
    reaches the strategy.
    """
    if isinstance(exception, (openai.InternalServerError, openai.APIConnectionError)):
        return True
    if isinstance(exception, openai.APIStatusError):
        return False

    code = getattr(exception, "code", None)
    if isinstance(code, str) and code.lower() in _OPENAI_SERVER_FAULT_CODES:
        return True

    message = str(exception).lower()
    return any(marker in message for marker in _OPENAI_SERVER_FAULT_MARKERS)


class ResponsesTransientRetryStrategy(ModelRetryStrategy):
    """Retry strategy for OpenAI Responses models served from ``bedrock-runtime``.

    Before this existed the provider got no strategy at all, and Strands reads
    ``retry_strategy=None`` as "retries off" — so even ``ModelThrottledException``,
    which the stock strategy retries, reached the user on the first failure as
    "Agent force-stopped". A staff member's prod sessions in Sept-Oct 2026
    failed that way on both GPT-6 messages listed above.

    Retries throttling and transient server faults, but only while the failed
    call has shown the user nothing. The model wrapper tags a failure that
    came after visible output (``apis.shared.models.stream_output``); retrying
    that one would print the abandoned prefix followed by a whole new answer.
    Backoff and attempt budget are inherited unchanged.
    """

    def is_retryable(self, exception: Exception) -> bool:
        """Whether ``exception`` should trigger another model attempt."""
        if streamed_partial_output(exception):
            logger.info(
                "Not retrying mid-stream model failure (%s); partial output "
                "already reached the client",
                type(exception).__name__,
            )
            return False

        if super().is_retryable(exception):
            return True

        if is_transient_openai_fault(exception):
            logger.warning(
                "Retrying transient OpenAI Responses fault: %s: %s",
                type(exception).__name__,
                exception,
            )
            return True

        return False
