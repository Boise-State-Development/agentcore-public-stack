"""One retry strategy for every model provider, and the only retry layer.

WHY THIS EXISTS
Strands' stock ``ModelRetryStrategy`` retries ``ModelThrottledException`` and
nothing else, and the providers disagree about what reaches it:

- ``BedrockModel`` maps only ``ThrottlingException``; every other modeled
  Bedrock fault, including the transient ones AWS documents as "retry the
  request", re-raises as a raw ``botocore.exceptions.ClientError``. Prod
  session ``5f34d2b0`` (2026-08-31): a ``ServiceUnavailableException`` after
  95.6s, never retried, 56,440 uncached input tokens billed for zero output.
- The OpenAI family (bedrock-runtime Responses, Mantle, OpenAI direct) reports
  server faults as events inside an HTTP 200 stream. GPT-6 Sol and Luna did
  this in prod through Sept-Oct 2026 ("The server had an error while
  processing your request", "The service is temporarily unavailable").
- Gemini maps ``UNAVAILABLE`` / ``RESOURCE_EXHAUSTED`` and re-raises 5xx raw.

And for every provider except Bedrock the factory used to pass
``retry_strategy=None``, which Strands reads as retries OFF, so those GPT-6
faults reached users as "Agent force-stopped" on the first attempt.

WHY THIS IS THE ONLY LAYER
botocore and the OpenAI client each retry on their own (3 and 3 attempts by
default), and those retries compounded with this layer's: up to 12 calls per
model invocation on Bedrock. The factory now holds both to a single attempt,
so this strategy covers what they used to (throttles, 5xx, connection resets
and timeouts) and the total is one knob: ``sdk_max_attempts``. Gemini's
client never retries unless configured.

WHY A FAILURE AFTER VISIBLE OUTPUT IS NEVER RETRIED
A retry restarts generation from scratch. When the failed call already
streamed text, the user sees the abandoned prefix followed by a second, full
answer. The factory wraps every model's stream to tag such a failure
(``apis.shared.models.stream_output``); that tag wins over everything below.
``EventStreamError`` is also excluded outright: Bedrock raises it while
iterating the response, after chunks may already be on the wire.
"""

import logging
from typing import Optional

import openai
from botocore.exceptions import ClientError, ConnectionError as BotoConnectionError
from botocore.exceptions import EventStreamError, HTTPClientError
from google.genai import errors as genai_errors
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

# HTTP statuses botocore's standard mode retries regardless of error code.
# Covered here because the transport no longer retries them.
_RETRYABLE_HTTP_STATUSES = frozenset({500, 502, 503, 504})

# Server-side faults the OpenAI Responses API reports with no HTTP status:
# they arrive as an `error` / `response.failed` event inside a 200 stream.
# Matched against the lowercased message. Both entries are the exact texts
# seen in prod on GPT-6 Sol and Luna (2026-09/10).
_OPENAI_SERVER_FAULT_MARKERS = (
    "server had an error while processing your request",
    "temporarily unavailable",
)

# Error codes the Responses API puts on those same stream events.
_OPENAI_SERVER_FAULT_CODES = frozenset({"server_error", "service_unavailable"})


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


def is_transient_bedrock_fault(exception: BaseException) -> bool:
    """Whether a botocore error is one its own standard retry mode would retry.

    A modeled transient code, a 5xx, or a connection failure / timeout.
    """
    if isinstance(exception, (BotoConnectionError, HTTPClientError)):
        return True
    if bedrock_error_code(exception) in RETRYABLE_BEDROCK_ERROR_CODES:
        return True
    if isinstance(exception, ClientError):
        status = exception.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return status in _RETRYABLE_HTTP_STATUSES
    return False


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


def is_transient_gemini_fault(exception: BaseException) -> bool:
    """Whether a Gemini error is a 5xx. Strands already maps its 429/503 statuses."""
    return isinstance(exception, genai_errors.ServerError)


class TransientModelRetryStrategy(ModelRetryStrategy):
    """``ModelRetryStrategy`` that retries every provider's transient faults.

    Everything except the retryable-exception predicate is inherited: the same
    exponential backoff, the same ``max_attempts`` budget, the same reset on a
    successful call. See the module docstring for what is excluded and why.
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

        # Mid-stream failure: chunks may already be on the wire. Restarting
        # would duplicate visible output, so surface it as an error instead.
        if isinstance(exception, EventStreamError):
            logger.info(
                "Not retrying mid-stream Bedrock failure (code=%s); "
                "partial output may already have reached the client",
                bedrock_error_code(exception),
            )
            return False

        if super().is_retryable(exception):
            return True

        if (
            is_transient_bedrock_fault(exception)
            or is_transient_openai_fault(exception)
            or is_transient_gemini_fault(exception)
        ):
            logger.warning(
                "Retrying transient model fault: %s: %s",
                type(exception).__name__,
                exception,
            )
            return True

        return False
