"""Lazy provisioning of a Managed Knowledge Base, and its CUSTOM connector.

A knowledge base is created in AWS the first time a document is actually ready to
be indexed — never when an assistant is created (Requirement 7.1). Creation was
measured at 47–124 s to ``ACTIVE`` (n=7), so this never sits on an interactive
path, and every call here is issued off the event loop (Requirement 20.7).

Order of operations, which is the whole design
----------------------------------------------
The KB_Record is written in ``provisioning`` **before** the first AWS call and the
returned identifiers are attached afterwards with a conditional update
(Requirement 7.3). Reversing those two steps looks harmless and is not: a crash
between ``CreateKnowledgeBase`` returning and the record being written would leave
a billed AWS resource that no record points at and no code can find. Nothing
raises, nothing alarms, and the only evidence is the invoice.

Written record-first, the same crash leaves a ``provisioning`` record that is a
durable **retry anchor** (Requirement 7.8): the Reconciler can match it against
the orphan and adopt it, and a plain retry of this function reuses the persisted
``clientToken`` so AWS deduplicates rather than creating a second knowledge base.

Four details that are each a defect if omitted
----------------------------------------------
1. **The ``clientToken`` is built, not interpolated.** The API's minimum is **33
   characters** — verified in the packaged service model, ``ClientToken`` has
   ``min: 33``, ``max: 256``, ``pattern: [a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}``. The
   natural ``{id}-{variant}-kb`` template is 31 characters and fails *client-side*
   validation, before a request is ever sent. :func:`build_client_token`
   constructs one that cannot be too short, and :func:`validate_client_token`
   refuses one that is.

2. **"Unable to verify the specified embedding model" is retryable.** It was
   observed against a model confirmed ``ACTIVE`` and directly invokable: it is IAM
   eventual consistency, not a configuration error. Treated as fatal, lazy
   provisioning fails intermittently while pointing at the wrong cause — an
   operator reads the message and goes to check the model.

3. **``dataDeletionPolicy: RETAIN`` at creation.** The documented remedy for the
   ``DELETE_UNSUCCESSFUL`` state, which the dev account has already been sitting
   in since 2025-11-24. Set deliberately up front, not as incident response.

4. **``imageExtractionStatus: ENABLED``.** Opt-in. Left at its default, chart and
   image content is never described and never indexed — a silent loss of a
   capability being paid for, with no error anywhere.

Why ``storageConfiguration`` is absent
--------------------------------------
There is no vector store to provision: that is the point of a managed knowledge
base. Sending ``storageConfiguration`` at all is rejected (Requirement 8.2), so it
is omitted entirely rather than passed empty.

Reconciling "``managedKnowledgeBaseConfiguration={}``" with the embedding pin
----------------------------------------------------------------------------
Requirement 8.1 records that ``managedKnowledgeBaseConfiguration`` has **no
required members**, so ``{}`` is a valid value. Requirement 8.5 separately pins
``embeddingModelType: CUSTOM`` to ``amazon.titan-embed-text-v2:0`` at float32 and
1024 dimensions. Those are not in conflict: the packaged service model puts
``embeddingModelType`` / ``embeddingModelArn`` /
``embeddingModelConfiguration.bedrockEmbeddingModelConfiguration`` inside
``managedKnowledgeBaseConfiguration`` as optional members, so the pin goes there.
It is pinned rather than left to the service default because the choice is
**immutable after creation** (Requirement 8.8) and because keeping today's Titan
v2 embedding preserves continuity with the legacy corpus.

Import boundary
---------------
Module-level imports are stdlib plus this package's own stdlib-only modules.
``boto3`` is imported inside the function that builds a client, so importing this
module into a size-constrained Lambda image costs nothing. See
``tests/architecture/test_kb_backend_boundary.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from apis.shared.kb_backend.metrics import emit_count

logger = logging.getLogger(__name__)

# ── Immutable embedding configuration (Requirement 8.5, 8.8) ─────────────────
#
# Immutable after creation: Bedrock rejects a change, so a drift here is not a
# migration but a rebuild. Recorded on the KB_Record too, so a mismatch is
# detectable rather than mysterious.
EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIMENSIONS = 1024

#: The service model's ``EmbeddingDataType`` enum is ``['FLOAT32', 'BINARY']`` —
#: upper case. "float32" is rejected.
EMBEDDING_DATA_TYPE = "FLOAT32"
EMBEDDING_MODEL_TYPE = "CUSTOM"

# ── Knowledge base and data source shapes ────────────────────────────────────
KNOWLEDGE_BASE_TYPE = "MANAGED"

#: Classic ``CUSTOM`` / ``S3`` / ``WEB`` at the top level are rejected with
#: "Unsupported data source type for MANAGED knowledge base type". The real
#: connector type nests one level down, in ``connectorParameters``.
DATA_SOURCE_TYPE = "MANAGED_KNOWLEDGE_BASE_CONNECTOR"
CONNECTOR_TYPE = "CUSTOM"
CONNECTOR_VERSION = "1"

#: Requirement 8.7. ``RETAIN`` at creation time, not later.
DATA_DELETION_POLICY = "RETAIN"

#: Requirement 8.6. Opt-in; the default indexes no image or chart content.
IMAGE_EXTRACTION_STATUS = "ENABLED"

# ── clientToken (Requirement 7.5, 7.6) ──────────────────────────────────────
CLIENT_TOKEN_MIN_LENGTH = 33
CLIENT_TOKEN_MAX_LENGTH = 256

#: Anchored copy of the service model's pattern. Note what it permits and does
#: not: the first and last characters must be alphanumeric, so a token may not
#: begin or end with a hyphen, though hyphens may run consecutively inside.
CLIENT_TOKEN_PATTERN = re.compile(r"^[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}$")

#: Length of the deterministic digest suffix. 40 hex characters alone clears the
#: 33-character minimum, so a token is long enough even when the caller's parts
#: sanitize away to nothing.
_DIGEST_CHARS = 40

# ── Retry classification (Requirement 7.7) ──────────────────────────────────
#
# Matched against the *message*, lower-cased, because this failure arrives as an
# ordinary validation-shaped error and is indistinguishable from a real
# misconfiguration by error code alone.
RETRYABLE_MESSAGE_FRAGMENTS = (
    "unable to verify the specified embedding model",
    "unable to verify the embedding model",
)

#: Transport-level errors that are retryable for the usual reasons. Deliberately
#: narrow: a genuine ``ValidationException`` must fail fast and loudly, because
#: retrying a malformed request just delays the report by a minute.
RETRYABLE_ERROR_CODES = (
    "ThrottlingException",
    "TooManyRequestsException",
    "InternalServerException",
    "ServiceUnavailableException",
)

MAX_PROVISION_ATTEMPTS = 5
_MAX_BACKOFF_SECONDS = 30.0

METRIC_PROVISION_RETRIED = "KbProvisionRetried"
METRIC_PROVISION_ADOPTED = "KbProvisionAdopted"


class ProvisioningError(RuntimeError):
    """Provisioning could not complete."""


class RetryableProvisioningError(ProvisioningError):
    """Provisioning failed for a reason that will plausibly clear on its own."""


class ProvisioningInProgress(RetryableProvisioningError):
    """Another worker owns this provisioning and has not finished.

    Retryable rather than fatal, and deliberately *not* an attempt to provision
    anyway: two workers creating one knowledge base each is exactly the
    duplication Requirement 7.4 forbids.
    """


@dataclass(frozen=True)
class ProvisionedKnowledgeBase:
    """The identifiers a caller needs, plus how they were obtained.

    ``created`` distinguishes "this call made the AWS resource" from "this call
    found one already recorded", which is what makes idempotency assertable
    instead of assumed.
    """

    aws_kb_id: str
    aws_data_source_id: str
    client_token: str
    created: bool = False


# ── Payload builders ─────────────────────────────────────────────────────────
def build_client_token(*parts: Any) -> str:
    """Build a ``clientToken`` that satisfies the API's constraints by construction.

    Deterministic in its inputs, so a retry of the same provisioning produces the
    same token and AWS deduplicates the create rather than making a second
    knowledge base. That property is the reason the token is also persisted on the
    KB_Record: a *later* process, with no memory of this one, must be able to
    reproduce the retry.

    The caller's parts are sanitized to the permitted alphabet and a digest of the
    original seed is appended. The digest is not decoration — it is what
    guarantees the 33-character minimum regardless of how short the inputs are,
    which is the failure the natural ``{id}-{variant}-kb`` template walks into at
    31 characters.
    """
    seed = "-".join(str(part) for part in parts if str(part) != "")
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]

    sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", seed).strip("-")
    token = f"{sanitized}-{digest}" if sanitized else digest

    if len(token) > CLIENT_TOKEN_MAX_LENGTH:
        # Truncate from the left of the *prefix*, never the digest: the digest is
        # what carries the uniqueness, so a token trimmed to its prefix could
        # collide with a different knowledge base's.
        keep = CLIENT_TOKEN_MAX_LENGTH - len(digest) - 1
        token = f"{sanitized[:keep].rstrip('-')}-{digest}"

    validate_client_token(token)
    return token


def validate_client_token(token: str) -> None:
    """Refuse a token the API would refuse, with a message saying which rule.

    Checked locally because botocore validates ``min``/``max``/``pattern``
    client-side: a short token never reaches AWS, so there is no service error to
    read and no request id to quote. Raising here, naming the length, is the
    difference between a one-line fix and an afternoon.
    """
    if not isinstance(token, str):
        raise ValueError(f"clientToken must be a string, got {type(token).__name__}")
    if len(token) < CLIENT_TOKEN_MIN_LENGTH:
        raise ValueError(
            f"clientToken must be at least {CLIENT_TOKEN_MIN_LENGTH} characters "
            f"(the API's documented minimum); got {len(token)}: {token!r}. Build "
            f"tokens with build_client_token() rather than interpolating a "
            f"template — the natural '{{id}}-{{variant}}-kb' form is 31 characters "
            f"and fails botocore's client-side validation before any request."
        )
    if len(token) > CLIENT_TOKEN_MAX_LENGTH:
        raise ValueError(
            f"clientToken must be at most {CLIENT_TOKEN_MAX_LENGTH} characters; "
            f"got {len(token)}"
        )
    if not CLIENT_TOKEN_PATTERN.match(token):
        raise ValueError(
            f"clientToken {token!r} does not match the API's pattern "
            f"{CLIENT_TOKEN_PATTERN.pattern}: it must begin and end with an "
            f"alphanumeric character"
        )


def embedding_model_arn(region: Optional[str] = None) -> str:
    """ARN of the pinned embedding model.

    Foundation-model ARNs carry no account, hence the empty account segment.
    """
    return f"arn:aws:bedrock:{region or _region()}::foundation-model/{EMBEDDING_MODEL_ID}"


def build_tags(
    app_kb_id: str,
    owner_user_id: str,
    project_prefix: Optional[str] = None,
    environment: Optional[str] = None,
) -> Dict[str, str]:
    """Tags the Reconciler and the teardown script both read (Requirement 20.11).

    Delegates to :mod:`apis.shared.kb_backend.tags`, which owns the key names and
    the value resolution. Kept as a thin wrapper because the provisioning saga is
    the only caller and this is where a reader looks for it.

    ⚠️ This function used to build the tags itself, with keys ``prefix``/``env``
    and values from ``PROJECT_PREFIX``/``ENVIRONMENT`` — neither of which the
    provisioning Lambda receives. Every knowledge base would have been tagged with
    the hardcoded defaults, the teardown script (which read a different pair of
    variables) would have matched nothing, and two environments in one account
    would have claimed each other's corpora. See the ``tags`` module docstring.
    """
    from apis.shared.kb_backend.tags import build_tags as _canonical

    return _canonical(app_kb_id, owner_user_id, project_prefix, environment)


def knowledge_base_payload(
    name: str,
    role_arn: str,
    client_token: str,
    *,
    description: Optional[str] = None,
    tags: Optional[Mapping[str, str]] = None,
    region: Optional[str] = None,
    kms_key_arn: Optional[str] = None,
) -> Dict[str, Any]:
    """The exact ``CreateKnowledgeBase`` request (Requirements 8.1, 8.2, 8.5).

    ``storageConfiguration`` is absent by construction — there is no key to
    accidentally set to ``None``, because a managed knowledge base has no vector
    store and sending one is rejected.
    """
    validate_client_token(client_token)

    managed: Dict[str, Any] = {
        # Requirement 8.5: pinned, and immutable from here on.
        "embeddingModelType": EMBEDDING_MODEL_TYPE,
        "embeddingModelArn": embedding_model_arn(region),
        "embeddingModelConfiguration": {
            "bedrockEmbeddingModelConfiguration": {
                "dimensions": EMBEDDING_DIMENSIONS,
                "embeddingDataType": EMBEDDING_DATA_TYPE,
            }
        },
    }
    if kms_key_arn:
        # Requirement 20.5, only where customer-managed encryption is required.
        managed["serverSideEncryptionConfiguration"] = {"kmsKeyArn": kms_key_arn}

    payload: Dict[str, Any] = {
        "name": name,
        "roleArn": role_arn,
        "clientToken": client_token,
        "knowledgeBaseConfiguration": {
            "type": KNOWLEDGE_BASE_TYPE,
            "managedKnowledgeBaseConfiguration": managed,
        },
    }
    if description:
        payload["description"] = description
    if tags:
        payload["tags"] = dict(tags)
    return payload


def data_source_payload(
    knowledge_base_id: str,
    name: str,
    client_token: str,
    *,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """The exact ``CreateDataSource`` request (Requirements 8.3, 8.4, 8.6, 8.7).

    Note the two-level nesting of the connector type. Putting ``CUSTOM`` at the
    top level — which is what every pre-managed example does — is rejected with
    "Unsupported data source type for MANAGED knowledge base type".
    """
    validate_client_token(client_token)

    payload: Dict[str, Any] = {
        "knowledgeBaseId": knowledge_base_id,
        "name": name,
        "clientToken": client_token,
        # Requirement 8.7 — at creation, because it cannot rescue a knowledge base
        # that is already stuck in DELETE_UNSUCCESSFUL.
        "dataDeletionPolicy": DATA_DELETION_POLICY,
        "dataSourceConfiguration": {
            "type": DATA_SOURCE_TYPE,
            "managedKnowledgeBaseConnectorConfiguration": {
                "connectorParameters": {
                    "type": CONNECTOR_TYPE,
                    "version": CONNECTOR_VERSION,
                },
                # Requirement 8.6 — opt-in, and silent when omitted.
                "mediaExtractionConfiguration": {
                    "imageExtractionConfiguration": {
                        "imageExtractionStatus": IMAGE_EXTRACTION_STATUS
                    }
                },
            },
        },
    }
    if description:
        payload["description"] = description
    return payload


# ── Retry classification ─────────────────────────────────────────────────────
def is_retryable_error(exc: BaseException) -> bool:
    """Whether ``exc`` should be retried rather than surfaced (Requirement 7.7).

    The embedding-verification message is the interesting case. It arrives looking
    like a configuration error, which is why a first reading of it produces code
    that fails the whole provisioning and tells the operator to check a model that
    is demonstrably fine. It is IAM eventual consistency: the role's
    ``bedrock:InvokeModel`` grant has not propagated yet.
    """
    message = str(exc).lower()
    if any(fragment in message for fragment in RETRYABLE_MESSAGE_FRAGMENTS):
        return True

    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        code = response.get("Error", {}).get("Code")
        if code in RETRYABLE_ERROR_CODES:
            return True
    return False


# ── Clients and clocks ───────────────────────────────────────────────────────
def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def bedrock_agent_client():
    """A ``bedrock-agent`` control-plane client. Imported lazily, deliberately."""
    import boto3

    return boto3.client("bedrock-agent", region_name=_region())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


async def _call(
    operation: Callable[..., Any],
    payload: Mapping[str, Any],
    *,
    what: str,
    max_attempts: int = MAX_PROVISION_ATTEMPTS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Any:
    """Invoke a synchronous boto3 operation off the event loop, with retries.

    ``asyncio.to_thread`` rather than a direct call because this runs inside the
    async request path and ``CreateKnowledgeBase`` blocks for 47–124 s
    (Requirement 20.7). Called directly it would stall every other coroutine on
    the loop — including the health check that decides whether the task is alive.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return await asyncio.to_thread(lambda: operation(**payload))
        except Exception as exc:
            if attempt >= max_attempts or not is_retryable_error(exc):
                raise
            delay = min(2.0**attempt, _MAX_BACKOFF_SECONDS)
            logger.warning(
                f"{what} failed with a retryable error (attempt {attempt}/"
                f"{max_attempts}, retrying in {delay}s): {exc}"
            )
            emit_count(METRIC_PROVISION_RETRIED, dimensions={"operation": what})
            await sleep(delay)

    # Unreachable: the loop either returns or raises.
    raise ProvisioningError(f"{what} exhausted {max_attempts} attempts")


# ── The saga ─────────────────────────────────────────────────────────────────
#: Statuses a knowledge base can hold while still on its way to usable.
KB_PENDING_STATUSES = ("CREATING", "UPDATING")

#: The status the data-source create requires.
KB_ACTIVE_STATUS = "ACTIVE"

#: Terminal-bad statuses. Waiting on these would burn the whole budget to reach
#: the same conclusion the first poll already supports.
KB_FAILED_STATUSES = ("FAILED", "DELETING", "DELETE_UNSUCCESSFUL")

#: Ceiling on the wait. The measured range to ACTIVE is 47-124 s (n=7), so this
#: is roughly 2.5x the observed worst case — comfortably inside the worker's
#: 15-minute Lambda timeout, and short enough that a genuinely stuck creation is
#: reported within one migration step rather than silently holding a lease.
KB_ACTIVE_WAIT_SECONDS = 300.0

#: Poll interval. Not a tuning knob worth an env var: the operation being waited
#: on takes tens of seconds, so anything finer just adds API calls.
KB_ACTIVE_POLL_SECONDS = 5.0


class KnowledgeBaseNotReady(Exception):
    """A knowledge base did not reach ``ACTIVE`` within the wait budget."""


async def _wait_for_knowledge_base_active(
    client: Any,
    aws_kb_id: str,
    *,
    what: str,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    budget_seconds: Optional[float] = None,
    interval_seconds: Optional[float] = None,
) -> str:
    """Block until ``aws_kb_id`` is ``ACTIVE``, or raise.

    ``CreateKnowledgeBase`` returns while the knowledge base is still
    ``CREATING``; anything that touches it before ``ACTIVE`` is refused with a
    ``ConflictException`` telling you to wait. Retrying the *dependent* call is
    the wrong shape — it burns attempts on a precondition rather than waiting for
    it — so the precondition is waited on directly.

    Budget and interval are resolved at call time, never bound as default
    arguments: a module-level default is captured at import and silently ignores a
    test's override, which has already cost this feature a 33-second test.
    """
    budget = KB_ACTIVE_WAIT_SECONDS if budget_seconds is None else budget_seconds
    interval = KB_ACTIVE_POLL_SECONDS if interval_seconds is None else interval_seconds

    waited = 0.0
    last_status = "UNKNOWN"
    while True:
        described = await asyncio.to_thread(
            lambda: client.get_knowledge_base(knowledgeBaseId=aws_kb_id)
        )
        last_status = str(
            (described.get("knowledgeBase") or {}).get("status") or "UNKNOWN"
        )
        if last_status == KB_ACTIVE_STATUS:
            if waited:
                logger.info(
                    f"kb {aws_kb_id} reached {KB_ACTIVE_STATUS} after {waited:.0f}s; "
                    f"proceeding to {what}"
                )
            return last_status
        if last_status in KB_FAILED_STATUSES:
            # Failing here rather than waiting out the budget: the status is
            # terminal, so the only thing more waiting buys is a later report.
            raise KnowledgeBaseNotReady(
                f"kb {aws_kb_id} is {last_status}, which will never reach "
                f"{KB_ACTIVE_STATUS}; refusing {what}"
            )
        if waited >= budget:
            raise KnowledgeBaseNotReady(
                f"kb {aws_kb_id} was still {last_status} after {waited:.0f}s "
                f"(budget {budget:.0f}s); refusing {what}. The migration is "
                f"resumable: the clientToken is deterministic, so the next attempt "
                f"adopts this knowledge base rather than creating another."
            )
        await sleep(interval)
        waited += interval


def _complete(item: Mapping[str, Any]) -> bool:    return bool(item.get("awsKbId")) and bool(item.get("awsDataSourceId"))


def _resource_name(app_kb_id: str, project_prefix: Optional[str] = None) -> str:
    """The knowledge base's AWS name.

    Resolved through the same helper as the tags, so a knowledge base's name and
    its ``ManagedKbPrefix`` tag can never disagree. The name is only a convention —
    every filter in this feature matches on tags — but a name that says ``prod``
    while the tag says ``dev`` is the kind of thing an operator reads once and
    trusts.
    """
    from apis.shared.kb_backend.tags import tag_prefix

    return f"{tag_prefix(project_prefix)}-kb-{app_kb_id}"


async def provision_managed_kb(
    assistant_id: str,
    app_kb_id: Optional[str] = None,
    owner_user_id: str = "",
    *,
    role_arn: Optional[str] = None,
    client=None,
    region: Optional[str] = None,
    kms_key_arn: Optional[str] = None,
    project_prefix: Optional[str] = None,
    environment: Optional[str] = None,
    max_attempts: int = MAX_PROVISION_ATTEMPTS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    # How long to wait for the knowledge base to reach ACTIVE before creating its
    # data source. Optional-None rather than a bound module constant so a test can
    # actually override them (see _wait_for_knowledge_base_active).
    budget_seconds: Optional[float] = None,
    interval_seconds: Optional[float] = None,
) -> ProvisionedKnowledgeBase:
    """Provision, or adopt, the managed knowledge base for ``app_kb_id``.

    Safe to call repeatedly and concurrently. Three paths, in the order they are
    tried:

    * **Already provisioned** — the record carries both identifiers, so this
      returns them and calls nothing.
    * **Resuming** — a record exists in ``provisioning``, which is what a crash
      between the AWS create and the conditional update leaves behind. Its
      persisted ``clientToken`` is reused, so the retried ``CreateKnowledgeBase``
      is deduplicated by AWS and no second knowledge base appears.
    * **Fresh** — the record is written first, then AWS is called.

    Losing the ``create_provisioning`` race raises
    :class:`ProvisioningInProgress` rather than proceeding. The winner is already
    creating the knowledge base; a loser that pressed on with its own token would
    create a second one and only one of them could ever be recorded.
    """
    from apis.shared.kb_backend import records as r

    app_kb_id = app_kb_id or assistant_id
    role_arn = role_arn or os.environ.get("MANAGED_KB_SERVICE_ROLE_ARN")
    if not role_arn:
        raise ProvisioningError(
            "no Bedrock knowledge base service role: pass role_arn or set "
            "MANAGED_KB_SERVICE_ROLE_ARN"
        )

    client = client or bedrock_agent_client()
    kb_token = build_client_token(app_kb_id, "knowledge-base")
    ds_token = build_client_token(app_kb_id, "data-source")

    existing = await asyncio.to_thread(r.get_kb_record, assistant_id, app_kb_id)

    if existing and _complete(existing):
        # Idempotent: nothing to create, and nothing to write.
        return ProvisionedKnowledgeBase(
            aws_kb_id=existing["awsKbId"],
            aws_data_source_id=existing["awsDataSourceId"],
            client_token=existing.get("clientToken") or kb_token,
            created=False,
        )

    if existing:
        # The retry-anchor path. Reuse the persisted token: it is the only thing
        # that makes the re-create idempotent on AWS's side.
        kb_token = existing.get("clientToken") or kb_token
        emit_count(METRIC_PROVISION_ADOPTED, dimensions={"appKbId": app_kb_id})
        logger.info(
            f"resuming provisioning for kb {app_kb_id} from its existing "
            f"{existing.get('provisioningState')} record"
        )
    else:
        record = r.KbRecord(
            app_kb_id=app_kb_id,
            owner_user_id=owner_user_id,
            provisioning_state=r.PROVISIONING,
            client_token=kb_token,
            embedding_model_id=EMBEDDING_MODEL_ID,
            embedding_dimensions=EMBEDDING_DIMENSIONS,
            image_extraction=True,
            parser_config={
                "imageExtractionStatus": IMAGE_EXTRACTION_STATUS,
                "connectorType": CONNECTOR_TYPE,
                "embeddingDataType": EMBEDDING_DATA_TYPE,
            },
        )
        try:
            # DDB before AWS. See the module docstring; this ordering is the
            # difference between a retry anchor and an untraceable paying resource.
            await asyncio.to_thread(r.create_provisioning, assistant_id, record)
        except r.TransitionLost as exc:
            other = await asyncio.to_thread(r.get_kb_record, assistant_id, app_kb_id)
            if other and _complete(other):
                return ProvisionedKnowledgeBase(
                    aws_kb_id=other["awsKbId"],
                    aws_data_source_id=other["awsDataSourceId"],
                    client_token=other.get("clientToken") or kb_token,
                    created=False,
                )
            raise ProvisioningInProgress(
                f"another worker is provisioning kb {app_kb_id}; retry later "
                f"rather than creating a second knowledge base"
            ) from exc

    name = _resource_name(app_kb_id, project_prefix)

    aws_kb_id = (existing or {}).get("awsKbId")
    if not aws_kb_id:
        response = await _call(
            client.create_knowledge_base,
            knowledge_base_payload(
                name=name,
                role_arn=role_arn,
                client_token=kb_token,
                description=f"Managed knowledge base for {app_kb_id}",
                tags=build_tags(app_kb_id, owner_user_id, project_prefix, environment),
                region=region,
                kms_key_arn=kms_key_arn,
            ),
            what="CreateKnowledgeBase",
            max_attempts=max_attempts,
            sleep=sleep,
        )
        aws_kb_id = response["knowledgeBase"]["knowledgeBaseId"]

    # CreateKnowledgeBase returns as soon as the knowledge base is CREATING, not
    # when it is usable — this module's own header records 47–124 s to ACTIVE
    # (n=7). CreateDataSource against a CREATING knowledge base is refused:
    #
    #   ConflictException: The Knowledge Base is not in a valid status.
    #   Wait for the knowledge base to reach a valid status and try again.
    #
    # `ConflictException` is deliberately absent from RETRYABLE_ERROR_CODES — a
    # genuine conflict must fail fast — and `_call`'s backoff tops out around 60 s
    # anyway, short of the measured upper bound. So the wait is explicit rather
    # than a widened retry set.
    #
    # This is also why the failure orphaned a knowledge base on first run: the
    # create succeeded, the data source did not, and `attach_aws_ids` never ran, so
    # nothing recorded the id. The deterministic `clientToken` means a retry adopts
    # that knowledge base rather than creating a second one, and the tags written
    # at create make it discoverable by the reconciler — but the orphan existed at
    # all only because of this missing wait.
    await _wait_for_knowledge_base_active(
        client,
        aws_kb_id,
        what="CreateDataSource",
        sleep=sleep,
        budget_seconds=budget_seconds,
        interval_seconds=interval_seconds,
    )

    aws_data_source_id = (existing or {}).get("awsDataSourceId")
    if not aws_data_source_id:
        ds_response = await _call(
            client.create_data_source,
            data_source_payload(
                knowledge_base_id=aws_kb_id,
                name=name,
                client_token=ds_token,
                description=f"CUSTOM connector for {app_kb_id}",
            ),
            what="CreateDataSource",
            max_attempts=max_attempts,
            sleep=sleep,
        )
        aws_data_source_id = ds_response["dataSource"]["dataSourceId"]

    try:
        await asyncio.to_thread(
            r.attach_aws_ids,
            assistant_id,
            app_kb_id,
            aws_kb_id,
            aws_data_source_id,
            _now_iso(),
        )
    except r.TransitionLost:
        # Another worker attached first, or the record has already left
        # `provisioning`. Its identifiers win: they are the ones every reader
        # will see, so returning our own would hand back a knowledge base that
        # no record points at.
        current = await asyncio.to_thread(r.get_kb_record, assistant_id, app_kb_id)
        if current and _complete(current):
            return ProvisionedKnowledgeBase(
                aws_kb_id=current["awsKbId"],
                aws_data_source_id=current["awsDataSourceId"],
                client_token=current.get("clientToken") or kb_token,
                created=False,
            )
        raise

    return ProvisionedKnowledgeBase(
        aws_kb_id=aws_kb_id,
        aws_data_source_id=aws_data_source_id,
        client_token=kb_token,
        created=True,
    )


__all__ = [
    "CLIENT_TOKEN_MAX_LENGTH",
    "CLIENT_TOKEN_MIN_LENGTH",
    "CLIENT_TOKEN_PATTERN",
    "CONNECTOR_TYPE",
    "CONNECTOR_VERSION",
    "DATA_DELETION_POLICY",
    "DATA_SOURCE_TYPE",
    "EMBEDDING_DATA_TYPE",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_MODEL_ID",
    "EMBEDDING_MODEL_TYPE",
    "IMAGE_EXTRACTION_STATUS",
    "KnowledgeBaseNotReady",
    "KNOWLEDGE_BASE_TYPE",
    "ProvisionedKnowledgeBase",
    "ProvisioningError",
    "ProvisioningInProgress",
    "RetryableProvisioningError",
    "build_client_token",
    "build_tags",
    "data_source_payload",
    "embedding_model_arn",
    "is_retryable_error",
    "knowledge_base_payload",
    "provision_managed_kb",
    "validate_client_token",
]
