"""Per-owner byte cap accounting for managed knowledge bases.

Managed storage is billed at $5.00/GB-month, roughly 35x what S3 Vectors costs
today. At the measured average of 1.13 MB per user that is about $169/month across
the fleet — but nothing structural stops one user uploading far more, and 30,000
users at 100 MB each would be 3 TB, or about $15,000/month. The cap is what turns
"unlikely" into "impossible".

Why an accumulator instead of the obvious condition
---------------------------------------------------
The natural way to express this is::

    ConditionExpression="storedBytes + reservedBytes + :n <= :cap"

**DynamoDB rejects that.** Condition expressions compare operands; they cannot do
arithmetic. Verified directly: the parser fails with ``Cannot parse condition
starting at:+ reserved <= :cap``.

So the arithmetic is moved to the client, where it is free. A single
``totalBytes`` accumulator is maintained as the invariant
``totalBytes == storedBytes + reservedBytes``, and the guard compares it against a
**literal computed before the call**::

    ADD totalBytes :n, reservedBytes :n
    CONDITION totalBytes <= :max_before      where :max_before = cap - n

That is a single atomic conditional update, so N concurrent reservations cannot
collectively overshoot. The alternative — read, compute, write — has a window
between the read and the write in which another writer commits, which is exactly
the race a cap exists to prevent.

Reserve / commit / release, not just "add"
-----------------------------------------
Ingestion is not instantaneous: a 50 KiB PDF measured 68-264 seconds. Counting
bytes only on success would let a user start unlimited concurrent uploads that are
each individually under the cap and collectively far over it. So bytes are reserved
up front, converted to stored on success, and returned on failure. A crash between
reserve and commit leaks a reservation, which is the safe direction — it
under-permits rather than over-permits, and the reconciler can recover it.

Sizing
------
Size always comes from an S3 ``HEAD`` on the stored object, never from a
client-reported value: a client that under-reports its own size would defeat the
cap entirely. Bedrock's ``RawDataSize`` metric is deliberately **not** used for
enforcement — it returned 0 datapoints for a directly-ingested document during
evaluation and remains unconfirmed. Enforcing against a metric that is sometimes
absent would fail open.

Import weight
-------------
Module-level imports are stdlib only; ``boto3`` is function-local, so this module
can be imported into a size-constrained Lambda image for free.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal
from typing import Optional

from apis.shared.kb_backend.metrics import emit_count

logger = logging.getLogger(__name__)

METRIC_BYTE_CAP_REJECTED = "KbByteCapRejected"

#: Defaults mirror the CDK config (Requirement 12.2). Both are read from the
#: environment so an operator can tune them without a code change; the fallbacks
#: keep local runs working.
#:
#: 100 MB is deliberately BELOW the existing 1 GB user-files precedent. At $5.00
#: per GB-month that precedent would permit roughly $150,000/month across the
#: fleet, which is not a limit so much as a formality.
DEFAULT_PER_OWNER_BYTES = 100 * 1024 * 1024
DEFAULT_PER_OWNER_ELEVATED_BYTES = 1024 * 1024 * 1024
DEFAULT_PER_KB_CEILING_BYTES = 500 * 1024 * 1024


class ByteCapExceeded(Exception):
    """A reservation would take the owner over their cap.

    Carries the numbers so the caller can render a plain-language message with the
    option to request an elevated tier, rather than a bare failure (Requirement
    12.12). A user who cannot see how far over they are cannot act on it.
    """

    def __init__(self, requested: int, cap: int, already_used: Optional[int] = None) -> None:
        self.requested = requested
        self.cap = cap
        self.already_used = already_used
        super().__init__(
            f"reserving {requested} bytes would exceed the {cap}-byte cap"
            + (f" (already using {already_used})" if already_used is not None else "")
        )


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not an integer; falling back to {default}")
        return default


def per_owner_cap(elevated: bool = False) -> int:
    """The owner's total allowance in bytes.

    Which tier a user belongs to is the caller's decision: RBAC already owns role
    resolution and this module should not grow a second opinion about it.
    """
    if elevated:
        return _env_int("MANAGED_KB_PER_OWNER_ELEVATED_BYTES", DEFAULT_PER_OWNER_ELEVATED_BYTES)
    return _env_int("MANAGED_KB_PER_OWNER_DEFAULT_BYTES", DEFAULT_PER_OWNER_BYTES)


def per_kb_ceiling() -> int:
    """Ceiling for a single knowledge base, independent of the owner's total.

    Stops one knowledge base consuming an entire elevated allowance and starving
    the owner's others.
    """
    return _env_int("MANAGED_KB_PER_KB_CEILING_BYTES", DEFAULT_PER_KB_CEILING_BYTES)


def _table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"])


def object_size_bytes(bucket: str, key: str) -> int:
    """Authoritative size, from S3 rather than from the client.

    A client-reported size is an input, and an input that can lower its own cost is
    not a measurement.
    """
    import boto3

    response = boto3.client("s3").head_object(Bucket=bucket, Key=key)
    return int(response["ContentLength"])


def reserve(
    assistant_id: str,
    app_kb_id: str,
    n_bytes: int,
    cap: int,
) -> None:
    """Reserve ``n_bytes`` against the cap, atomically.

    Raises :class:`ByteCapExceeded` if the reservation would breach the cap. The
    comparison is against ``cap - n_bytes``, computed here, because DynamoDB cannot
    add inside a condition — see the module docstring.

    ``attribute_not_exists`` covers the first reservation on a record that has
    never held bytes, so a fresh knowledge base does not need initialising.
    """
    from botocore.exceptions import ClientError

    from apis.shared.kb_backend.records import kb_pk, kb_sk

    if n_bytes < 0:
        raise ValueError("n_bytes must not be negative")
    if n_bytes == 0:
        return
    if n_bytes > cap:
        # Cannot fit even into an empty allowance; no point issuing the write.
        emit_count(METRIC_BYTE_CAP_REJECTED)
        raise ByteCapExceeded(requested=n_bytes, cap=cap)

    try:
        _table().update_item(
            Key={"PK": kb_pk(assistant_id), "SK": kb_sk(app_kb_id)},
            UpdateExpression="ADD #total :n, #reserved :n",
            ConditionExpression="attribute_not_exists(#total) OR #total <= :max_before",
            ExpressionAttributeNames={
                # `total` is a DynamoDB reserved keyword, so these are aliased.
                "#total": "totalBytes",
                "#reserved": "reservedBytes",
            },
            ExpressionAttributeValues={
                ":n": Decimal(n_bytes),
                ":max_before": Decimal(cap - n_bytes),
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            emit_count(METRIC_BYTE_CAP_REJECTED)
            raise ByteCapExceeded(requested=n_bytes, cap=cap) from exc
        raise


def commit(assistant_id: str, app_kb_id: str, n_bytes: int) -> None:
    """Convert a reservation into stored bytes.

    ``totalBytes`` is untouched: the bytes were already counted at reserve time.
    Adding here as well would double-count and shrink the owner's allowance on
    every successful upload.
    """
    from apis.shared.kb_backend.records import kb_pk, kb_sk

    if n_bytes == 0:
        return
    _table().update_item(
        Key={"PK": kb_pk(assistant_id), "SK": kb_sk(app_kb_id)},
        UpdateExpression="ADD #reserved :neg, #stored :n",
        ExpressionAttributeNames={"#reserved": "reservedBytes", "#stored": "storedBytes"},
        ExpressionAttributeValues={":neg": Decimal(-n_bytes), ":n": Decimal(n_bytes)},
    )


def release(assistant_id: str, app_kb_id: str, n_bytes: int) -> None:
    """Return a reservation after a failed ingestion.

    Decrements both the reservation and the accumulator, restoring the allowance
    exactly. Not releasing would silently shrink the owner's cap with every failed
    upload until they could not upload at all — a leak that presents as "the
    product stopped working" long after the failures that caused it.
    """
    from apis.shared.kb_backend.records import kb_pk, kb_sk

    if n_bytes == 0:
        return
    _table().update_item(
        Key={"PK": kb_pk(assistant_id), "SK": kb_sk(app_kb_id)},
        UpdateExpression="ADD #reserved :neg, #total :neg",
        ExpressionAttributeNames={"#reserved": "reservedBytes", "#total": "totalBytes"},
        ExpressionAttributeValues={":neg": Decimal(-n_bytes)},
    )


def reserve_snapshot(
    assistant_id: str,
    app_kb_id: str,
    total_bytes: int,
    cap: int,
) -> None:
    """Reserve a whole migration corpus up front (Requirement 12.11/12.12).

    Migration is the largest byte-adding operation in the system and the only one
    that runs unattended, which makes it both the easiest place to forget the check
    and the worst. Reserving per-document as the worker progresses would let a
    migration run for an hour and then stop halfway, leaving a half-populated
    managed knowledge base and an owner over their cap with no way back.

    So the entire snapshot is reserved *before* the migration enters ``shadow``. A
    corpus that cannot fit fails immediately, with numbers the caller can turn into
    "this needs an elevated tier" rather than a stack trace.

    Deliberately the same conditional write as :func:`reserve`; the distinction is
    the caller's contract, not the mechanism.
    """
    reserve(assistant_id, app_kb_id, total_bytes, cap)
