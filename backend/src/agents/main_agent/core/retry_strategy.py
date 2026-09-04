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

from botocore.exceptions import ClientError, EventStreamError
from strands import ModelRetryStrategy

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
