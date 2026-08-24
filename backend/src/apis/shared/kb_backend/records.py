"""KB_Record persistence for the managed knowledge base migration.

Records live in the **existing** assistants table as siblings of the assistant's
``METADATA`` row, preserving the adjacency-list convention::

    PK = AST#{assistant_id}
    SK = KB#{app_kb_id}                        # app_kb_id == assistant_id this phase
    SK = KBTOMB#{app_kb_id}                    # whole-KB tombstone
    SK = KBTOMB#{app_kb_id}#DOC#{document_id}  # per-document tombstone

Three invariants are load-bearing. Each is enforced here rather than left to
callers, because each fails silently when violated:

**1. Absence means legacy.** ``retrievalEngine`` is written *only* as
``"managed"``. Nothing here ever writes ``"s3vectors"`` onto a record that did
not already carry it. That is what makes this migration zero-backfill: every
existing knowledge base is already correct by virtue of having no opinion, and
rollback is a single attribute removal rather than a data rewrite. A backfill
that "helpfully" stamped the legacy value on 1,692 records would convert a
pointer flip into a migration of its own.

**2. Every transition is conditional.** These functions are called from a
dispatcher that fans out to concurrent workers, so a read-then-write would let
two workers both believe they won. Each transition therefore carries a DynamoDB
``ConditionExpression`` and surfaces the loss as :class:`TransitionLost` rather
than an opaque ``ClientError``.

**3. Sparse work keys are removed, not just ignored.** ``GSI7_PK``/``GSI7_SK``
exist only while a record is eligible for background work. On reaching a terminal
state they are ``REMOVE``d, so an ineligible knowledge base is invisible to the
dispatcher's query *by physics* rather than by filter. This matters more than the
usual sparse-index argument because the dispatcher creates and deletes billed AWS
resources: a missing key can only ever mean "do nothing", whereas a stale key
means "act on something nobody asked you to act on".

Import boundary
---------------
This module deliberately talks to DynamoDB through the raw table resource instead
of importing ``apis.shared.assistants``. That package's ``__init__`` imports
``rag_service``, which imports the embeddings stack at module scope; pulling it
into the migration Lambda image would blow the image-size budget. The same
constraint is why ``apis/app_api/kb_sync/records.py`` is written this way, and
this module follows it: **module-level imports are stdlib only**, and ``boto3``
is imported inside the functions that need it. ``kb_backend/__init__.py`` is
intentionally empty so importing a submodule pulls in nothing else.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

# ── Engines ──────────────────────────────────────────────────────────────────
#
# LEGACY is never persisted. It is the value `resolve_engine` returns for a
# record that carries no `retrievalEngine` attribute, which is every record that
# predates this feature.
ENGINE_LEGACY = "s3vectors"
ENGINE_MANAGED = "managed"

# ── Provisioning ─────────────────────────────────────────────────────────────
PROVISIONING = "provisioning"
ACTIVE = "active"
FAILED = "failed"
DELETING = "deleting"

# ── Migration states ─────────────────────────────────────────────────────────
SHADOW = "shadow"
VERIFY = "verify"
PROMOTE = "promote"
RETAIN = "retain"
MIGRATION_FAILED = "failed"

#: Reserved in the enum so a stored value round-trips, but never entered in this
#: phase. Reclaiming legacy vectors is explicitly a follow-up spec; a worker that
#: found itself here would delete data this phase has promised to retain.
RECLAIM = "reclaim"

#: States that keep a record in the dispatcher's queue. Work keys are written on
#: entering one of these.
WORK_ELIGIBLE_STATES = frozenset({SHADOW, VERIFY, PROMOTE})

#: States that take a record out of the queue for good. Work keys are removed on
#: entering one of these. ``RETAIN`` is the terminal state this phase reaches;
#: ``MIGRATION_FAILED`` is terminal too and leaves the record on legacy, which
#: keeps working.
TERMINAL_STATES = frozenset({RETAIN, MIGRATION_FAILED})

ALL_MIGRATION_STATES = frozenset(
    {SHADOW, VERIFY, PROMOTE, RETAIN, MIGRATION_FAILED, RECLAIM}
)


class TransitionLost(Exception):
    """A conditional write was rejected because the guard did not hold.

    Raised instead of leaking ``ConditionalCheckFailedException`` so callers can
    tell "another worker got there first, do nothing" apart from a real error.
    Losing a race is normal and must not be logged as a failure.
    """


class ReclaimNotSupported(Exception):
    """Refuses an attempt to enter ``reclaim``, which this phase never does."""


# ── Keys ─────────────────────────────────────────────────────────────────────
def kb_pk(assistant_id: str) -> str:
    return f"AST#{assistant_id}"


def kb_sk(app_kb_id: str) -> str:
    return f"KB#{app_kb_id}"


def kb_tombstone_sk(app_kb_id: str) -> str:
    return f"KBTOMB#{app_kb_id}"


def document_tombstone_sk(app_kb_id: str, document_id: str) -> str:
    return f"KBTOMB#{app_kb_id}#DOC#{document_id}"


def work_pk(state: str) -> str:
    return f"KBWORK#{state}"


def _table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"])


# ── Model ────────────────────────────────────────────────────────────────────
@dataclass
class KbRecord:
    """A knowledge base's control-plane state.

    A dataclass rather than a Pydantic model on purpose: this module is imported
    by a size-constrained Lambda image and has no need for validation machinery
    it would then have to carry.
    """

    app_kb_id: str
    owner_user_id: str
    visibility: str = "PRIVATE"

    # Absent means legacy. Only ever ENGINE_MANAGED when present.
    retrieval_engine: Optional[str] = None

    provisioning_state: str = PROVISIONING
    aws_kb_id: Optional[str] = None
    aws_data_source_id: Optional[str] = None

    # Immutable after creation: Bedrock rejects changing either, so they are
    # recorded to make a mismatch detectable rather than mysterious.
    embedding_model_id: str = "amazon.titan-embed-text-v2:0"
    embedding_dimensions: int = 1024

    # Captured at creation because a corpus indexed without image extraction is
    # not comparable to one indexed with it.
    parser_config: Dict[str, Any] = field(default_factory=dict)
    image_extraction: bool = False

    stored_bytes: int = 0
    reserved_bytes: int = 0
    last_retrieved_at: Optional[str] = None

    migration_state: Optional[str] = None
    migration_generation: int = 0
    migration_lease_until: Optional[str] = None
    migration_progress: Dict[str, Any] = field(default_factory=dict)
    migration_error: Optional[str] = None

    promoted_at: Optional[str] = None
    rolled_back_at: Optional[str] = None
    retain_until: Optional[str] = None

    pinned: bool = False
    exempt_from_reclaim: bool = False

    client_token: Optional[str] = None

    def to_item(self, assistant_id: str) -> Dict[str, Any]:
        """Serialize for DynamoDB, omitting absent optionals.

        Optionals are omitted rather than written as ``None`` so that "has no
        opinion" stays distinguishable from "explicitly null". ``retrievalEngine``
        depends on that distinction.
        """
        item: Dict[str, Any] = {
            "PK": kb_pk(assistant_id),
            "SK": kb_sk(self.app_kb_id),
            "appKbId": self.app_kb_id,
            "ownerUserId": self.owner_user_id,
            "visibility": self.visibility,
            "provisioningState": self.provisioning_state,
            "embeddingModelId": self.embedding_model_id,
            "embeddingDimensions": Decimal(self.embedding_dimensions),
            "parserConfig": self.parser_config,
            "imageExtraction": self.image_extraction,
            "storedBytes": Decimal(self.stored_bytes),
            "reservedBytes": Decimal(self.reserved_bytes),
            "migrationGeneration": Decimal(self.migration_generation),
            "pinned": self.pinned,
            "exemptFromReclaim": self.exempt_from_reclaim,
        }

        optional = {
            "retrievalEngine": self.retrieval_engine,
            "awsKbId": self.aws_kb_id,
            "awsDataSourceId": self.aws_data_source_id,
            "lastRetrievedAt": self.last_retrieved_at,
            "migrationState": self.migration_state,
            "migrationLeaseUntil": self.migration_lease_until,
            "migrationError": self.migration_error,
            "promotedAt": self.promoted_at,
            "rolledBackAt": self.rolled_back_at,
            "retainUntil": self.retain_until,
            "clientToken": self.client_token,
        }
        item.update({k: v for k, v in optional.items() if v is not None})

        if self.migration_progress:
            item["migrationProgress"] = self.migration_progress

        return item


def resolve_engine(item: Optional[Mapping[str, Any]]) -> str:
    """Return the backend that should serve this record.

    The whole migration rests on this function's default. A record with no
    ``retrievalEngine`` attribute — which is every knowledge base that existed
    before this feature — resolves to the legacy backend. Nothing had to be
    written to make that true, and nothing has to be unwritten to roll back.

    A missing record resolves to legacy for the same reason: the absence of an
    opinion is an answer, not an error.
    """
    if not item:
        return ENGINE_LEGACY
    return ENGINE_MANAGED if item.get("retrievalEngine") == ENGINE_MANAGED else ENGINE_LEGACY


# ── Reads ────────────────────────────────────────────────────────────────────
def get_kb_record(assistant_id: str, app_kb_id: str) -> Optional[Dict[str, Any]]:
    response = _table().get_item(Key={"PK": kb_pk(assistant_id), "SK": kb_sk(app_kb_id)})
    return response.get("Item")


def query_due_work(state: str, now_iso: str, limit: int = 20) -> list:
    """Records in ``state`` whose ``dueAt`` has passed, oldest first.

    Reads the sparse index, so records that have left the queue are not returned
    because they have no key — not because they were filtered out.
    """
    from boto3.dynamodb.conditions import Key

    response = _table().query(
        IndexName="KbWorkIndex",
        KeyConditionExpression=Key("GSI7_PK").eq(work_pk(state)) & Key("GSI7_SK").lte(now_iso),
        Limit=limit,
    )
    return response.get("Items", [])


# ── Transitions ──────────────────────────────────────────────────────────────
def _conditional(operation, **kwargs):
    """Run a conditional write, translating a failed guard into TransitionLost."""
    from botocore.exceptions import ClientError

    try:
        return operation(**kwargs)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise TransitionLost(
                "conditional write rejected; another writer won or the "
                "precondition no longer holds"
            ) from exc
        raise


def create_provisioning(
    assistant_id: str,
    record: KbRecord,
) -> Dict[str, Any]:
    """Create the record, exactly once.

    ``attribute_not_exists(PK)`` makes this idempotent under concurrency: two
    callers racing to enrol the same knowledge base produce one record and one
    :class:`TransitionLost`, rather than one silently overwriting the other's
    ``clientToken`` and orphaning a half-created AWS knowledge base.
    """
    item = record.to_item(assistant_id)
    _conditional(
        _table().put_item,
        Item=item,
        ConditionExpression="attribute_not_exists(PK) AND attribute_not_exists(SK)",
    )
    return item


def attach_aws_ids(
    assistant_id: str,
    app_kb_id: str,
    aws_kb_id: str,
    aws_data_source_id: str,
    now_iso: str,
) -> None:
    """Record the AWS identifiers and mark the record active.

    Guarded on still being ``provisioning`` so a late-returning create cannot
    overwrite identifiers belonging to a newer generation.
    """
    _conditional(
        _table().update_item,
        Key={"PK": kb_pk(assistant_id), "SK": kb_sk(app_kb_id)},
        UpdateExpression=(
            "SET awsKbId = :kb, awsDataSourceId = :ds, "
            "provisioningState = :active, updatedAt = :now"
        ),
        ConditionExpression="provisioningState = :provisioning",
        ExpressionAttributeValues={
            ":kb": aws_kb_id,
            ":ds": aws_data_source_id,
            ":active": ACTIVE,
            ":provisioning": PROVISIONING,
            ":now": now_iso,
        },
    )


def promote_engine(
    assistant_id: str,
    app_kb_id: str,
    generation: int,
    now_iso: str,
) -> None:
    """Flip the record to the managed backend. The single cutover write.

    Three guards, all necessary:

    * ``migrationState = promote`` — only a record that reached the cutover step
      may cut over.
    * ``migrationGeneration = :gen`` — a worker whose lease expired and whose
      generation has been superseded cannot promote on stale information.
    * ``migrationProgress.migrated = migrationProgress.total`` — the catch-up
      pass has converged. Without this, promotion could strand documents written
      during migration on a backend nobody reads any more. Comparing two
      document paths keeps the check atomic with the write; passing the total in
      as a value would let it go stale between read and write.

    ``total`` is a DynamoDB reserved keyword, so the progress paths are aliased
    through ``ExpressionAttributeNames``. Without the aliases the whole condition
    is rejected as a ``ValidationException`` — loudly, which is the good case, but
    only because it never validates at all.

    Because this is one conditional write, rollback is symmetric: see
    :func:`rollback_engine`.
    """
    _conditional(
        _table().update_item,
        Key={"PK": kb_pk(assistant_id), "SK": kb_sk(app_kb_id)},
        UpdateExpression="SET retrievalEngine = :managed, promotedAt = :now",
        ConditionExpression=(
            "migrationState = :promote "
            "AND migrationGeneration = :gen "
            "AND #progress.#migrated = #progress.#total"
        ),
        ExpressionAttributeNames={
            "#progress": "migrationProgress",
            "#migrated": "migrated",
            "#total": "total",
        },
        ExpressionAttributeValues={
            ":managed": ENGINE_MANAGED,
            ":promote": PROMOTE,
            ":gen": Decimal(generation),
            ":now": now_iso,
        },
    )


def rollback_engine(assistant_id: str, app_kb_id: str, now_iso: str) -> None:
    """Return the record to the legacy backend by REMOVING the engine attribute.

    Note the ``REMOVE``. Rollback restores the original *shape*, not a written
    legacy value, so a rolled-back record is byte-indistinguishable from one that
    never migrated. Writing ``"s3vectors"`` here would work today and quietly
    break the "absence means legacy" invariant that lets this feature ship
    without touching 1,692 existing records.

    Guarded on currently being managed so a double rollback is a no-op loss
    rather than a spurious ``rolledBackAt`` bump.
    """
    _conditional(
        _table().update_item,
        Key={"PK": kb_pk(assistant_id), "SK": kb_sk(app_kb_id)},
        UpdateExpression="REMOVE retrievalEngine SET rolledBackAt = :now",
        ConditionExpression="retrievalEngine = :managed",
        ExpressionAttributeValues={":managed": ENGINE_MANAGED, ":now": now_iso},
    )


def set_migration_state(
    assistant_id: str,
    app_kb_id: str,
    new_state: str,
    generation: int,
    due_at: Optional[str] = None,
    expected_states: Optional[Iterable[str]] = None,
    error: Optional[str] = None,
) -> None:
    """Move to ``new_state``, maintaining the sparse work keys.

    Entering a work-eligible state writes ``GSI7_PK``/``GSI7_SK``; entering a
    terminal state ``REMOVE``s them. The removal is the point: it is what takes
    the record out of the dispatcher's queue, and skipping it would leave a
    finished knowledge base being handed to workers forever.

    ``expected_states`` guards the transition against a concurrent writer that
    has already moved the record on. The generation is always guarded.
    """
    if new_state == RECLAIM:
        raise ReclaimNotSupported(
            "reclaim is reserved but never entered in this phase; reclaiming "
            "legacy vectors is a follow-up spec"
        )
    if new_state not in ALL_MIGRATION_STATES:
        raise ValueError(f"unknown migration state: {new_state!r}")
    if new_state in WORK_ELIGIBLE_STATES and not due_at:
        raise ValueError(f"{new_state} is work-eligible and requires due_at")

    values: Dict[str, Any] = {
        ":state": new_state,
        ":gen": Decimal(generation),
    }
    sets = ["migrationState = :state"]
    removes = []

    if new_state in TERMINAL_STATES:
        # Leaving the queue: the keys must go, not merely be ignored.
        removes.extend(["GSI7_PK", "GSI7_SK"])
    else:
        sets.extend(["GSI7_PK = :wpk", "GSI7_SK = :wsk"])
        values[":wpk"] = work_pk(new_state)
        values[":wsk"] = due_at

    if error is not None:
        sets.append("migrationError = :err")
        values[":err"] = error

    expression = f"SET {', '.join(sets)}"
    if removes:
        expression += f" REMOVE {', '.join(removes)}"

    condition = "migrationGeneration = :gen"
    if expected_states is not None:
        expected = list(expected_states)
        if not expected:
            raise ValueError("expected_states must be non-empty when provided")
        placeholders = []
        for index, state in enumerate(expected):
            placeholder = f":exp{index}"
            placeholders.append(placeholder)
            values[placeholder] = state
        condition += f" AND migrationState IN ({', '.join(placeholders)})"

    _conditional(
        _table().update_item,
        Key={"PK": kb_pk(assistant_id), "SK": kb_sk(app_kb_id)},
        UpdateExpression=expression,
        ConditionExpression=condition,
        ExpressionAttributeValues=values,
    )


def acquire_lease(
    assistant_id: str,
    app_kb_id: str,
    lease_until: str,
    now_iso: str,
) -> None:
    """Take the worker lease, or lose the race.

    The guard admits exactly two situations: no lease has ever been taken, or the
    existing lease has expired. A live lease held by another worker rejects,
    which is what stops two workers migrating the same knowledge base and
    double-ingesting its corpus.

    ISO-8601 UTC strings compare correctly lexicographically, so the expiry test
    is a plain string comparison and stays atomic with the write.
    """
    _conditional(
        _table().update_item,
        Key={"PK": kb_pk(assistant_id), "SK": kb_sk(app_kb_id)},
        UpdateExpression="SET migrationLeaseUntil = :until",
        ConditionExpression=(
            "attribute_not_exists(migrationLeaseUntil) OR migrationLeaseUntil < :now"
        ),
        ExpressionAttributeValues={":until": lease_until, ":now": now_iso},
    )
