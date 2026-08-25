"""Tombstoned deletion sagas for managed knowledge bases.

Every delete here either completes or leaves a durable, retryable work item. That
is the whole requirement (Requirement 13), and it exists because a failed delete
of a managed knowledge base is not a crash — it is a **silent recurring bill**.

The ordering is the mechanism
-----------------------------
1. Write the Tombstone to DynamoDB.
2. *Then* call AWS.
3. Poll until AWS reports the resource genuinely absent.
4. *Only then* clear the Tombstone.

Reversing steps 1 and 2 looks equivalent and is not. A crash between the AWS call
and the database write leaves a half-deleted, still-billed resource that no record
points at, nothing alarms on, and no code will ever revisit. Written
tombstone-first, the same crash leaves a row that :func:`iter_tombstones` finds
(Requirement 13.8) and that a later pass can retry.

Clearing early is the same defect wearing the opposite hat: a tombstone cleared on
the strength of an *accepted* delete call describes a resource AWS may still be
holding, and holding it is what costs money. So :func:`clear_kb_tombstone` is
never called on the accept path — it is reachable only after
:func:`confirm_knowledge_base_absent` has returned true.

No TTL. Deliberately.
---------------------
A Tombstone is cleared by confirmed deletion or it stays. Attaching a TTL would
let DynamoDB quietly remove the evidence of a delete that never finished, which
recreates precisely the silent-leak class this module exists to close. The same
reasoning bans a TTL on the KB_Record itself (Requirement 13.6):
:func:`remove_kb_record` refuses outright unless the caller can show confirmation.

"Accepted" is not "gone"
------------------------
``DeleteKnowledgeBase`` returns ``status: DELETING`` and the resource lives on for
a measured **2-6 minutes**. There is no waiter, so absence is established by
polling ``ListKnowledgeBases`` until the identifier stops appearing
(Requirement 13.4), with a window comfortably past the observed worst case.

``DELETE_UNSUCCESSFUL`` is a terminal *operator* state, not a completed delete
(Requirement 13.7). The dev account has contained one since 2025-11-24 that no
reconciler would ever have noticed. Observing it stops the poll, records the state
on the Tombstone, and leaves the Tombstone standing.

Why the tag filter costs a describe call per knowledge base
-----------------------------------------------------------
``ListKnowledgeBases`` has no tag-filter parameter and its summaries carry neither
``knowledgeBaseArn`` nor ``createdAt`` — verified against the packaged service
model, where ``KnowledgeBaseSummary`` is
``{knowledgeBaseId, name, description, status, updatedAt}``. Both of those are
needed: the ARN to read tags, and ``createdAt`` for the Reconciler's age gate. So
:func:`iter_project_knowledge_bases` pages the list and calls
``GetKnowledgeBase`` per entry. The alternative — synthesizing the ARN from the
region and account — trades a read call for a brittle string, and the caller here
is a daily job.

Import boundary
---------------
Module-level imports are stdlib plus this package's own stdlib-only modules;
``boto3`` and ``botocore`` are function-local. Nothing here imports
``apis.shared.assistants``, whose ``__init__`` pulls in the embeddings stack and
would blow the migration Lambda image budget. Enforced by
``tests/architecture/test_kb_backend_boundary.py``.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional

from apis.shared.kb_backend.metrics import emit_count
from apis.shared.kb_backend.records import (
    document_tombstone_sk,
    kb_pk,
    kb_sk,
    kb_tombstone_sk,
)

logger = logging.getLogger(__name__)

# ── Intents ──────────────────────────────────────────────────────────────────
#
# Recorded on the Tombstone so a retry knows which saga to resume without having
# to infer it from the sort key's shape.
INTENT_DELETE_KB = "delete_kb"
INTENT_DELETE_DOCUMENT = "delete_document"

#: Attribute name flagging a tombstone whose ``PK`` is *not* a real assistant
#: partition.
#:
#: The reconciler deletes orphans — knowledge bases with no KB_Record — and an
#: orphan by definition carries no assistant id to anchor on, so its tombstone
#: lands in a partition derived from whatever identifier the tags did preserve.
#: That item is a genuine work record and must be kept, but a reader must not
#: mistake the partition for an assistant that exists, and
#: ``iter_tombstones(<real assistant id>)`` will never return it. This attribute
#: says so on the item, in place of a comment nobody triaging at 3am will read.
SYNTHETIC_PARTITION = "syntheticPartition"

# ── AWS states ───────────────────────────────────────────────────────────────
#
# Copied from the packaged service model's ``KnowledgeBaseStatus`` enum:
# ``CREATING | ACTIVE | DELETING | UPDATING | FAILED | DELETE_UNSUCCESSFUL |
# UPDATE_UNSUCCESSFUL``.
KB_STATUS_DELETING = "DELETING"
KB_STATUS_DELETE_UNSUCCESSFUL = "DELETE_UNSUCCESSFUL"

#: From ``DocumentStatus``. A document AWS reports ``NOT_FOUND`` is gone; anything
#: else — including ``DELETING`` and ``DELETE_IN_PROGRESS`` — is still present.
DOCUMENT_STATUS_NOT_FOUND = "NOT_FOUND"

# ── Poll windows (Requirement 13.4) ──────────────────────────────────────────
#
# Deletion was measured at 2-6 minutes, so the floor is 360 s and this sits above
# it. These are read *at call time* rather than bound as default arguments,
# because a default argument is evaluated once at import and cannot be patched:
# an earlier version of a sibling poller bound its timeout that way and a test
# that shortened the window had no effect at all, silently waiting the full
# production timeout instead. See `wait_until_retrievable` in the ingestion
# consumer for the same note.
KB_DELETE_POLL_TIMEOUT_SECONDS = 480.0
KB_DELETE_POLL_INTERVAL_SECONDS = 10.0

DOCUMENT_DELETE_POLL_TIMEOUT_SECONDS = 120.0
DOCUMENT_DELETE_POLL_INTERVAL_SECONDS = 2.0

#: ``ListKnowledgeBases`` page size. The list is always paged to exhaustion; this
#: only trades call count against payload size.
LIST_PAGE_SIZE = 100

# ── Metrics ──────────────────────────────────────────────────────────────────
METRIC_TOMBSTONE_WRITTEN = "KbTombstoneWritten"
METRIC_TOMBSTONE_CLEARED = "KbTombstoneCleared"

#: A delete that was accepted but never confirmed. Sustained non-zero is the only
#: signal that the delete saga is leaking paid resources.
METRIC_TOMBSTONE_SURVIVED = "KbTombstoneSurvived"

#: Requirement 13.7. Needs an alarm, not a dashboard: nothing clears this state
#: on its own.
METRIC_DELETE_UNSUCCESSFUL = "KbDeleteUnsuccessful"


class TombstoneError(RuntimeError):
    """A tombstoned delete could not be completed."""


class DeleteNotConfirmed(TombstoneError):
    """AWS never reported the resource absent within the poll window.

    Retryable. The Tombstone is deliberately left in place, so the work item
    outlives this process.
    """


class DeleteUnsuccessful(TombstoneError):
    """AWS reported ``DELETE_UNSUCCESSFUL`` (Requirement 13.7).

    Distinct from :class:`DeleteNotConfirmed` because it is *not* a matter of
    waiting longer. It is an actionable operator state that persists until someone
    intervenes, and it must never be mistaken for a completed delete.
    """


class ServiceRoleStillInUse(TombstoneError):
    """Refuses to delete a service role that still has knowledge bases.

    Requirement 13.5. Removing the role first is a documented route *into*
    ``DELETE_UNSUCCESSFUL``: the pending deletion needs the role it was created
    with, and without it the knowledge base can be neither deleted nor recovered.
    """


class RecordRemovalRefused(TombstoneError):
    """Refuses to remove a KB_Record before AWS confirmed the deletion.

    Requirement 13.6. The record is the only pointer to the AWS identifiers, so
    dropping it early converts a retryable delete into an untraceable one.
    """


@dataclass(frozen=True)
class KnowledgeBaseFacts:
    """What AWS says about one knowledge base.

    ``created_at`` is **AWS's own** ``createdAt``, carried through unmodified. The
    Reconciler's age gate depends on that provenance: substituting the time this
    process happened to look would make a reconciler that was down for a week
    treat every knowledge base in the account as brand new (Requirement 14.3).
    """

    kb_id: str
    name: str
    status: str
    arn: Optional[str] = None
    created_at: Optional[Any] = None
    tags: Mapping[str, str] = None  # type: ignore[assignment]


@dataclass(frozen=True)
class DeleteOutcome:
    """The result of one saga run.

    ``confirmed`` means AWS reported the resource absent — the only condition
    under which the Tombstone was cleared. ``tombstone_cleared`` is reported
    separately rather than inferred so a test can catch the two drifting apart.
    """

    confirmed: bool
    tombstone_cleared: bool
    already_absent: bool = False
    delete_unsuccessful: bool = False
    polls: int = 0


# ── DynamoDB plumbing ────────────────────────────────────────────────────────
def _table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"])


def _now_iso() -> str:
    from apis.shared.timestamps import utc_now_iso

    return utc_now_iso()


# ── Tombstone writes ─────────────────────────────────────────────────────────
def _write_tombstone(
    assistant_id: str,
    sort_key: str,
    intent: str,
    attributes: Mapping[str, Any],
) -> Dict[str, Any]:
    """Upsert a Tombstone, preserving the original ``createdAt`` and counting attempts.

    An upsert rather than a ``put_item`` because a retried saga must not restart
    the clock. ``createdAt`` is written through ``if_not_exists`` so it records
    when the delete was *first* attempted — the number an operator triaging a
    stuck tombstone actually wants — while ``attempts`` accumulates with ``ADD``,
    which is atomic and needs no read.

    No ``ttl`` attribute is written, and none may be added. See the module
    docstring: expiry would silently discard the evidence of an unfinished delete.
    """
    now = _now_iso()
    values: Dict[str, Any] = {
        ":intent": intent,
        ":now": now,
        ":one": Decimal(1),
    }
    sets = [
        "intent = :intent",
        "createdAt = if_not_exists(createdAt, :now)",
        "updatedAt = :now",
    ]
    for index, (key, value) in enumerate(sorted(attributes.items())):
        if value is None:
            continue
        placeholder = f":a{index}"
        sets.append(f"{key} = {placeholder}")
        values[placeholder] = value

    _table().update_item(
        Key={"PK": kb_pk(assistant_id), "SK": sort_key},
        UpdateExpression=f"SET {', '.join(sets)} ADD attempts :one",
        ExpressionAttributeValues=values,
    )
    emit_count(METRIC_TOMBSTONE_WRITTEN, dimensions={"intent": intent})
    return {"PK": kb_pk(assistant_id), "SK": sort_key, "intent": intent, "createdAt": now}


def write_kb_tombstone(
    assistant_id: str,
    app_kb_id: str,
    aws_kb_id: Optional[str] = None,
    aws_data_source_id: Optional[str] = None,
    extra_attributes: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Mark a whole-knowledge-base delete as intended. Call this *before* AWS.

    ``extra_attributes`` lets a caller that is not deleting on behalf of a known
    assistant say so on the item itself — see ``SYNTHETIC_PARTITION`` below.
    """
    attributes: Dict[str, Any] = {
        "appKbId": app_kb_id,
        "awsKbId": aws_kb_id,
        "awsDataSourceId": aws_data_source_id,
    }
    if extra_attributes:
        attributes.update(extra_attributes)
    return _write_tombstone(
        assistant_id,
        kb_tombstone_sk(app_kb_id),
        INTENT_DELETE_KB,
        attributes,
    )


def write_document_tombstone(
    assistant_id: str,
    app_kb_id: str,
    document_id: str,
    aws_kb_id: Optional[str] = None,
    aws_data_source_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Mark a single-document delete as intended. Call this *before* AWS."""
    return _write_tombstone(
        assistant_id,
        document_tombstone_sk(app_kb_id, document_id),
        INTENT_DELETE_DOCUMENT,
        {
            "appKbId": app_kb_id,
            "documentId": document_id,
            "awsKbId": aws_kb_id,
            "awsDataSourceId": aws_data_source_id,
        },
    )


def record_tombstone_error(
    assistant_id: str,
    sort_key: str,
    error: str,
    aws_status: Optional[str] = None,
) -> None:
    """Annotate a surviving Tombstone with why it survived.

    Never raises. The saga has already failed by the time this is reached, and
    losing the annotation is strictly better than replacing a precise failure with
    a DynamoDB error from the bookkeeping.
    """
    sets = ["lastError = :err", "updatedAt = :now"]
    values: Dict[str, Any] = {":err": error[:1024], ":now": _now_iso()}
    if aws_status:
        sets.append("awsStatus = :status")
        values[":status"] = aws_status

    try:
        _table().update_item(
            Key={"PK": kb_pk(assistant_id), "SK": sort_key},
            UpdateExpression=f"SET {', '.join(sets)}",
            ExpressionAttributeValues=values,
        )
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not mask the real failure
        logger.warning(f"could not annotate tombstone {sort_key}: {exc}")


def _clear(assistant_id: str, sort_key: str, intent: str) -> bool:
    _table().delete_item(Key={"PK": kb_pk(assistant_id), "SK": sort_key})
    emit_count(METRIC_TOMBSTONE_CLEARED, dimensions={"intent": intent})
    return True


def clear_kb_tombstone(assistant_id: str, app_kb_id: str, confirmed_absent: bool) -> bool:
    """Clear a knowledge-base Tombstone. Refuses unless AWS confirmed absence.

    ``confirmed_absent`` is a required positional argument rather than a keyword
    with a convenient default, because the failure mode being guarded against is a
    caller who *forgot* the confirmation step. A default of ``True`` would make
    the unsafe call the short one; there is no default at all, so the caller has
    to state what it knows.
    """
    if not confirmed_absent:
        raise TombstoneError(
            f"refusing to clear the tombstone for kb {app_kb_id}: AWS has not "
            f"confirmed the knowledge base is absent. An accepted delete call is "
            f"not a completed deletion (Requirement 13.3), and clearing here "
            f"would discard the only work item for a resource still being billed."
        )
    return _clear(assistant_id, kb_tombstone_sk(app_kb_id), INTENT_DELETE_KB)


def clear_document_tombstone(
    assistant_id: str,
    app_kb_id: str,
    document_id: str,
    confirmed_absent: bool,
) -> bool:
    """Clear a document Tombstone. Refuses unless AWS confirmed absence."""
    if not confirmed_absent:
        raise TombstoneError(
            f"refusing to clear the tombstone for document {document_id}: AWS has "
            f"not confirmed it is absent"
        )
    return _clear(
        assistant_id, document_tombstone_sk(app_kb_id, document_id), INTENT_DELETE_DOCUMENT
    )


def iter_tombstones(assistant_id: str) -> List[Dict[str, Any]]:
    """Surviving Tombstones for one assistant, as retryable work items (Req 13.8).

    Keyed on the ``KBTOMB#`` prefix, so a whole-KB tombstone and its documents'
    tombstones come back together and in that order — which is the order a retry
    wants them.
    """
    from boto3.dynamodb.conditions import Key

    response = _table().query(
        KeyConditionExpression=Key("PK").eq(kb_pk(assistant_id))
        & Key("SK").begins_with("KBTOMB#")
    )
    return response.get("Items", [])


def remove_kb_record(assistant_id: str, app_kb_id: str, confirmed_absent: bool) -> None:
    """Delete the KB_Record. Refuses unless AWS confirmed the deletion (Req 13.6).

    The record holds the only mapping from ``App_KB_Id`` to the AWS identifiers.
    Removing it while AWS still holds the knowledge base turns a resource that a
    tombstone could still find into one nothing can address — the exact leak this
    module exists to prevent, produced by the cleanup step rather than the crash.
    """
    if not confirmed_absent:
        raise RecordRemovalRefused(
            f"refusing to remove the KB_Record for {app_kb_id} before AWS confirms "
            f"deletion (Requirement 13.6); the record is the only pointer to the "
            f"AWS identifiers"
        )
    _table().delete_item(Key={"PK": kb_pk(assistant_id), "SK": kb_sk(app_kb_id)})


# ── AWS listing, tag-filtered and paginated (Requirement 14.1) ───────────────
def project_tag_filter(
    project_prefix: Optional[str] = None,
    environment: Optional[str] = None,
) -> Dict[str, str]:
    """The tags that identify this platform's knowledge bases.

    Mirrors ``provisioning.build_tags``, which writes them. Only ``prefix`` and
    ``env`` are matched: ``appKbId`` and ``ownerUserId`` vary per resource and are
    identity, not scope.
    """
    return {
        "prefix": project_prefix or os.environ.get("PROJECT_PREFIX", "agentcore"),
        "env": environment or os.environ.get("ENVIRONMENT", "dev"),
    }


def matches_project_tags(tags: Optional[Mapping[str, str]], expected: Mapping[str, str]) -> bool:
    """True when every expected tag is present with the expected value.

    Absent or empty tags never match. An untagged knowledge base is out of scope
    by construction, which is the conservative direction: this predicate gates
    deletion, so a false negative leaves a resource alone while a false positive
    deletes someone else's.
    """
    if not tags:
        return False
    return all(tags.get(key) == value for key, value in expected.items())


def iter_knowledge_base_summaries(client, page_size: Optional[int] = None) -> Iterator[Dict[str, Any]]:
    """Every ``KnowledgeBaseSummary`` in the account, paging to exhaustion.

    Hand-rolled paging rather than ``get_paginator`` so that a stubbed client in a
    test is a plain object with one method, not something that has to satisfy
    botocore's paginator protocol. Reading only the first page would make the
    Reconciler's judgement depend on account size: every knowledge base past page
    one would look like a missing-vector record and every orphan there would go
    unbilled-for-ever.
    """
    if page_size is None:
        page_size = LIST_PAGE_SIZE

    token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"maxResults": page_size}
        if token:
            kwargs["nextToken"] = token
        response = client.list_knowledge_bases(**kwargs)
        for summary in response.get("knowledgeBaseSummaries") or []:
            yield summary
        token = response.get("nextToken")
        if not token:
            return


def describe_knowledge_base(client, kb_id: str) -> Optional[Dict[str, Any]]:
    """``GetKnowledgeBase``, or ``None`` if it has already gone.

    A ``ResourceNotFoundException`` between the list and the describe is normal —
    something else deleted it, or this saga's own earlier attempt finally landed —
    and means exactly what the caller wants to know.
    """
    from botocore.exceptions import ClientError

    try:
        response = client.get_knowledge_base(knowledgeBaseId=kb_id)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise
    return response.get("knowledgeBase") or None


def knowledge_base_tags(client, arn: str) -> Dict[str, str]:
    """Tags for one knowledge base. A read failure yields ``{}``, never a match.

    Failing closed matters here: ``{}`` cannot satisfy
    :func:`matches_project_tags`, so a knowledge base whose tags could not be read
    is left alone rather than deleted on the strength of a failed lookup.
    """
    from botocore.exceptions import ClientError

    try:
        return dict((client.list_tags_for_resource(resourceArn=arn) or {}).get("tags") or {})
    except ClientError as exc:
        logger.warning(f"could not read tags for {arn}: {exc}")
        return {}


def iter_project_knowledge_bases(
    client,
    project_prefix: Optional[str] = None,
    environment: Optional[str] = None,
    page_size: Optional[int] = None,
) -> Iterator[KnowledgeBaseFacts]:
    """This project's knowledge bases, with AWS's ``createdAt`` and status.

    Paginated (Requirement 14.1) and tag-filtered. The filter is applied to tags
    read from AWS rather than to the name, because a name is a convention this
    code chose and a tag is a fact recorded on the resource: a knowledge base
    created by an older naming scheme is still ours, and one that merely happens
    to share our prefix is not.
    """
    expected = project_tag_filter(project_prefix, environment)

    for summary in iter_knowledge_base_summaries(client, page_size=page_size):
        kb_id = summary.get("knowledgeBaseId")
        if not kb_id:
            continue

        described = describe_knowledge_base(client, kb_id)
        if described is None:
            continue

        arn = described.get("knowledgeBaseArn")
        tags = knowledge_base_tags(client, arn) if arn else {}
        if not matches_project_tags(tags, expected):
            continue

        yield KnowledgeBaseFacts(
            kb_id=kb_id,
            name=described.get("name") or summary.get("name") or "",
            status=described.get("status") or summary.get("status") or "",
            arn=arn,
            # AWS's own timestamp, untouched. See KnowledgeBaseFacts.
            created_at=described.get("createdAt"),
            tags=tags,
        )


# ── Confirmation by polling (Requirement 13.3, 13.4) ─────────────────────────
def confirm_knowledge_base_absent(
    client,
    aws_kb_id: str,
    timeout_seconds: Optional[float] = None,
    interval_seconds: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> DeleteOutcome:
    """Poll ``ListKnowledgeBases`` until ``aws_kb_id`` stops appearing.

    Absence is established from the **list**, not from the delete call's return
    value and not from a single ``GetKnowledgeBase``: the delete is asynchronous
    and returns ``DELETING`` while the resource is still there and still billed.

    Returns as soon as the identifier is gone. Raises :class:`DeleteUnsuccessful`
    the moment ``DELETE_UNSUCCESSFUL`` is observed — that state does not resolve
    by waiting, so continuing to poll would burn the window and then report the
    wrong reason. Raises :class:`DeleteNotConfirmed` on timeout.

    Both windows resolve from the module constants *at call time*. Bound as
    default arguments they would be fixed at import and unpatchable, and a test
    that shortened them would sit through the full production wait while
    appearing to pass.
    """
    if timeout_seconds is None:
        timeout_seconds = KB_DELETE_POLL_TIMEOUT_SECONDS
    if interval_seconds is None:
        interval_seconds = KB_DELETE_POLL_INTERVAL_SECONDS

    deadline = monotonic() + timeout_seconds
    polls = 0

    while True:
        polls += 1
        present: Optional[Dict[str, Any]] = None
        for summary in iter_knowledge_base_summaries(client):
            if summary.get("knowledgeBaseId") == aws_kb_id:
                present = summary
                break

        if present is None:
            return DeleteOutcome(confirmed=True, tombstone_cleared=False, polls=polls)

        status = present.get("status") or ""
        if status == KB_STATUS_DELETE_UNSUCCESSFUL:
            emit_count(METRIC_DELETE_UNSUCCESSFUL)
            raise DeleteUnsuccessful(
                f"knowledge base {aws_kb_id} is in {KB_STATUS_DELETE_UNSUCCESSFUL}. "
                f"This is an operator state, not a completed delete: it does not "
                f"clear on its own and the resource is still billed. The tombstone "
                f"is being left in place as the work item."
            )

        if monotonic() >= deadline:
            raise DeleteNotConfirmed(
                f"knowledge base {aws_kb_id} still present after {timeout_seconds}s "
                f"(last status {status!r}) across {polls} polls; leaving the "
                f"tombstone as a retryable work item"
            )

        sleep(interval_seconds)


def confirm_document_absent(
    client,
    aws_kb_id: str,
    aws_data_source_id: str,
    document_id: str,
    timeout_seconds: Optional[float] = None,
    interval_seconds: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> DeleteOutcome:
    """Poll ``GetKnowledgeBaseDocuments`` until the document reports ``NOT_FOUND``.

    Only ``NOT_FOUND`` (or an empty detail list) counts as absent. ``DELETING``
    and ``DELETE_IN_PROGRESS`` are explicitly *present*: treating them as done is
    the document-scale version of trusting the accepted delete call.
    """
    from apis.shared.kb_backend.managed_backend import document_identifier

    if timeout_seconds is None:
        timeout_seconds = DOCUMENT_DELETE_POLL_TIMEOUT_SECONDS
    if interval_seconds is None:
        interval_seconds = DOCUMENT_DELETE_POLL_INTERVAL_SECONDS

    deadline = monotonic() + timeout_seconds
    polls = 0

    while True:
        polls += 1
        response = client.get_knowledge_base_documents(
            knowledgeBaseId=aws_kb_id,
            dataSourceId=aws_data_source_id,
            documentIdentifiers=[document_identifier(document_id)],
        )
        details = response.get("documentDetails") or []
        statuses = {detail.get("status") for detail in details}

        if not details or statuses <= {DOCUMENT_STATUS_NOT_FOUND}:
            return DeleteOutcome(confirmed=True, tombstone_cleared=False, polls=polls)

        if monotonic() >= deadline:
            raise DeleteNotConfirmed(
                f"document {document_id} still present in kb {aws_kb_id} after "
                f"{timeout_seconds}s (statuses {sorted(s for s in statuses if s)}); "
                f"leaving the tombstone as a retryable work item"
            )

        sleep(interval_seconds)


# ── Sagas ────────────────────────────────────────────────────────────────────
def delete_knowledge_base(
    assistant_id: str,
    app_kb_id: str,
    aws_kb_id: str,
    aws_data_source_id: Optional[str] = None,
    client=None,
    remove_record: bool = False,
    extra_attributes: Optional[Mapping[str, Any]] = None,
    timeout_seconds: Optional[float] = None,
    interval_seconds: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> DeleteOutcome:
    """Delete a knowledge base under a Tombstone.

    The four steps run strictly in order, and the order is the guarantee:

    1. **Tombstone first.** Written before any AWS call, so a crash anywhere below
       leaves a work item rather than a resource nothing knows about.
    2. **Ask AWS.** ``ResourceNotFoundException`` is success, not failure — an
       earlier attempt got there, and the tombstone should still be cleared.
    3. **Confirm by polling.** The accepted call is ignored as evidence.
    4. **Clear the Tombstone**, and only now, optionally, the KB_Record.

    On any failure the Tombstone survives, annotated with the reason, and the
    exception propagates so the invocation fails and its retry or DLQ fires.
    """
    from apis.shared.kb_backend.managed_backend import bedrock_agent_client
    from botocore.exceptions import ClientError

    if client is None:
        client = bedrock_agent_client()

    sort_key = kb_tombstone_sk(app_kb_id)

    # Step 1. Before AWS. Always.
    write_kb_tombstone(
        assistant_id,
        app_kb_id,
        aws_kb_id,
        aws_data_source_id,
        extra_attributes=extra_attributes,
    )

    already_absent = False
    try:
        # Step 2.
        try:
            client.delete_knowledge_base(knowledgeBaseId=aws_kb_id)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
            already_absent = True
            logger.info(
                f"knowledge base {aws_kb_id} was already absent; treating the "
                f"delete as complete and clearing its tombstone"
            )

        # Step 3. "Accepted" is not "gone" — establish absence from the list.
        outcome = confirm_knowledge_base_absent(
            client,
            aws_kb_id,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            sleep=sleep,
            monotonic=monotonic,
        )
    except DeleteUnsuccessful as exc:
        record_tombstone_error(
            assistant_id, sort_key, str(exc), aws_status=KB_STATUS_DELETE_UNSUCCESSFUL
        )
        raise
    except Exception as exc:
        emit_count(METRIC_TOMBSTONE_SURVIVED, dimensions={"intent": INTENT_DELETE_KB})
        record_tombstone_error(assistant_id, sort_key, str(exc))
        raise

    # Step 4. Reachable only with confirmation in hand.
    clear_kb_tombstone(assistant_id, app_kb_id, outcome.confirmed)

    if remove_record:
        remove_kb_record(assistant_id, app_kb_id, outcome.confirmed)

    return DeleteOutcome(
        confirmed=True,
        tombstone_cleared=True,
        already_absent=already_absent,
        polls=outcome.polls,
    )


def delete_document(
    assistant_id: str,
    app_kb_id: str,
    document_id: str,
    aws_kb_id: str,
    aws_data_source_id: str,
    client=None,
    timeout_seconds: Optional[float] = None,
    interval_seconds: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> DeleteOutcome:
    """Delete one document under a Tombstone. Same ordering as the KB saga."""
    from apis.shared.kb_backend.managed_backend import (
        bedrock_agent_client,
        document_identifier,
    )

    if client is None:
        client = bedrock_agent_client()

    sort_key = document_tombstone_sk(app_kb_id, document_id)

    # Step 1. Before AWS. Always.
    write_document_tombstone(
        assistant_id, app_kb_id, document_id, aws_kb_id, aws_data_source_id
    )

    try:
        client.delete_knowledge_base_documents(
            knowledgeBaseId=aws_kb_id,
            dataSourceId=aws_data_source_id,
            documentIdentifiers=[document_identifier(document_id)],
        )
        outcome = confirm_document_absent(
            client,
            aws_kb_id,
            aws_data_source_id,
            document_id,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
            sleep=sleep,
            monotonic=monotonic,
        )
    except Exception as exc:
        emit_count(METRIC_TOMBSTONE_SURVIVED, dimensions={"intent": INTENT_DELETE_DOCUMENT})
        record_tombstone_error(assistant_id, sort_key, str(exc))
        raise

    clear_document_tombstone(assistant_id, app_kb_id, document_id, outcome.confirmed)
    return DeleteOutcome(confirmed=True, tombstone_cleared=True, polls=outcome.polls)


# ── Service-role teardown guard (Requirement 13.5) ───────────────────────────
def knowledge_bases_using_role(
    client,
    role_arn: str,
    project_prefix: Optional[str] = None,
    environment: Optional[str] = None,
) -> List[str]:
    """Identifiers of this project's knowledge bases still using ``role_arn``.

    Read from ``GetKnowledgeBase``'s ``roleArn`` rather than from our own records,
    because the question is what AWS still believes — and a knowledge base our
    database has forgotten is exactly the one that makes deleting the role
    dangerous.
    """
    outstanding: List[str] = []
    for facts in iter_project_knowledge_bases(
        client, project_prefix=project_prefix, environment=environment
    ):
        described = describe_knowledge_base(client, facts.kb_id)
        if described is None:
            continue
        if described.get("roleArn") == role_arn:
            outstanding.append(facts.kb_id)
    return outstanding


def assert_service_role_deletable(
    client,
    role_arn: str,
    project_prefix: Optional[str] = None,
    environment: Optional[str] = None,
) -> None:
    """Raise unless every knowledge base using ``role_arn`` is confirmed absent.

    Called by teardown before it touches the role. A knowledge base mid-``DELETING``
    still counts as present: it needs the role to finish, and pulling the role out
    from under it is a documented route into ``DELETE_UNSUCCESSFUL``, which is
    unrecoverable without support.
    """
    outstanding = knowledge_bases_using_role(
        client, role_arn, project_prefix=project_prefix, environment=environment
    )
    if outstanding:
        raise ServiceRoleStillInUse(
            f"refusing to delete service role {role_arn}: {len(outstanding)} "
            f"knowledge base(s) still reference it ({', '.join(sorted(outstanding))}). "
            f"Delete them and confirm their absence first (Requirement 13.5); "
            f"removing the role while one is still DELETING can strand it in "
            f"{KB_STATUS_DELETE_UNSUCCESSFUL}."
        )


__all__ = [
    "DOCUMENT_DELETE_POLL_INTERVAL_SECONDS",
    "DOCUMENT_DELETE_POLL_TIMEOUT_SECONDS",
    "DOCUMENT_STATUS_NOT_FOUND",
    "INTENT_DELETE_DOCUMENT",
    "INTENT_DELETE_KB",
    "KB_DELETE_POLL_INTERVAL_SECONDS",
    "KB_DELETE_POLL_TIMEOUT_SECONDS",
    "KB_STATUS_DELETE_UNSUCCESSFUL",
    "KB_STATUS_DELETING",
    "LIST_PAGE_SIZE",
    "METRIC_DELETE_UNSUCCESSFUL",
    "METRIC_TOMBSTONE_CLEARED",
    "METRIC_TOMBSTONE_SURVIVED",
    "METRIC_TOMBSTONE_WRITTEN",
    "SYNTHETIC_PARTITION",
    "DeleteNotConfirmed",
    "DeleteOutcome",
    "DeleteUnsuccessful",
    "KnowledgeBaseFacts",
    "RecordRemovalRefused",
    "ServiceRoleStillInUse",
    "TombstoneError",
    "assert_service_role_deletable",
    "clear_document_tombstone",
    "clear_kb_tombstone",
    "confirm_document_absent",
    "confirm_knowledge_base_absent",
    "delete_document",
    "delete_knowledge_base",
    "describe_knowledge_base",
    "iter_knowledge_base_summaries",
    "iter_project_knowledge_bases",
    "iter_tombstones",
    "knowledge_base_tags",
    "knowledge_bases_using_role",
    "matches_project_tags",
    "project_tag_filter",
    "record_tombstone_error",
    "remove_kb_record",
    "write_document_tombstone",
    "write_kb_tombstone",
]
