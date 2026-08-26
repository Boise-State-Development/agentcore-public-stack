"""Daily reconciler for managed knowledge bases.

Joins a paginated, tag-filtered ``ListKnowledgeBases`` against the KB_Records and
acts on the three ways the two sides can disagree. It exists because a managed
knowledge base is a **runtime-created, billed** resource with no CloudFormation
parent: nothing else in the system would ever notice one that our database has
forgotten about.

The join table
--------------
=============  ============================================================
Side           Action
=============  ============================================================
AWS only       Orphan. Delete **only if AWS's own ``createdAt`` is >24 h old**
Record only    Mark ``vectorState: missing``. **Never delete the record**
Both           Refresh ``storedBytes`` for quota accounting
=============  ============================================================

Two of those three rows are counter-intuitive, and each is the way it is because
the intuitive version destroys something.

**Age-gate on AWS's ``createdAt``, never on discovery time.** The tempting
implementation records when the reconciler first *saw* an unknown knowledge base
and waits 24 hours from there. That is wrong in both directions. A reconciler that
was down for a week comes back and treats every knowledge base in the account as
newly discovered — so either it waits another 24 hours on genuine week-old
orphans, or, if the comparison is written the other way round, it deletes every
knowledge base that is mid-provisioning right now, including creates that are 40
seconds old and about to succeed. AWS's ``createdAt`` is a fact about the
resource, is identical on every run, and does not depend on this process's uptime.
It is obtained from ``GetKnowledgeBase``, because ``KnowledgeBaseSummary`` does
not carry it.

**A record with no AWS knowledge base is a stale pointer, not a dead corpus.** It
means the *vectors* are gone. The source bytes are still in S3 and the ``DOC#``
records are still valid and still ``complete``, so the knowledge base can be
rebuilt from them on the next ingest, and the owner never has to re-upload
anything. The record is the only pointer to that recoverable corpus, so deleting
it is the one action here that loses user data — which is why
:func:`mark_vector_state_missing` is the entire response and no code path in this
module removes a KB_Record.

Report-only, and armed separately
---------------------------------
This ships **disarmed** (Requirement 14.7, 19.7). It logs exactly what it would
have deleted and deletes nothing, and it runs that way for weeks so its judgement
can be checked against real data before it is trusted with a delete. Arming is one
flag, ``MANAGED_KB_RECONCILER_ARMED``, and an **empty string reads as off**
(Requirement 19.8) — an unset GitHub Actions variable expands to ``""``, which is
how a flag that is obviously off ends up looking truthy to ``if os.environ.get``.

The per-run limit applies in **both** modes, so the report says what an armed run
would actually do. A report listing 500 intended deletions from a run that would
only ever perform 25 is a misleading artifact, and the whole point of the
report-only period is that the artifact can be trusted.

Import boundary
---------------
Module-level imports are stdlib plus the stdlib-only ``kb_backend`` modules;
``boto3`` is function-local, and nothing here reaches ``apis.shared.assistants``.
DynamoDB is accessed through the raw table resource, matching
``kb_migration/ingestion_consumer.py`` and ``kb_sync/records.py``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, Iterator, List, Optional

from apis.shared.kb_backend.metrics import emit_count, emit_fleet_gauges
from apis.shared.kb_backend.records import kb_pk, kb_sk

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Flags ────────────────────────────────────────────────────────────────────
#
# The arming flag. Absent, empty, or anything not in the truthy set means the
# reconciler reports and deletes nothing.
FLAG_RECONCILER_ARMED = "MANAGED_KB_RECONCILER_ARMED"

#: Recognised affirmative spellings. Everything else — including ``""``, ``"0"``,
#: ``"false"`` and ``"off"`` — is off. An allow-list rather than a truthiness test
#: because the failure being designed around is a value that is *present but
#: empty*: ``bool("")`` is correct by luck, ``bool("false")`` is not.
_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})

# ── Tunables, resolved at call time ──────────────────────────────────────────
#
# Read inside the functions that use them rather than bound as default arguments.
# A default argument is evaluated once at import, so it cannot be patched and a
# test that overrides it silently gets the production value instead.

#: Requirement 14.4. An orphan younger than this is very likely an in-flight
#: create: provisioning to ``ACTIVE`` was measured at 47-124 s, and the record is
#: written before the AWS call, so the only window in which a legitimate create
#: looks like an orphan is the moments between the two. 24 hours is far wider than
#: needed, which is the correct direction for a destructive action.
ORPHAN_MIN_AGE_HOURS = 24.0

#: Requirement 14.8. Bounds the destructive work of a single run, so a bug in the
#: join — or a tag filter that suddenly matches more than it should — costs at
#: most this many knowledge bases before someone sees the report.
MAX_DELETIONS_PER_RUN = 25

#: Hard ceiling on :func:`max_deletions_per_run`, above which the env var is
#: ignored. Deleting more than this in one pass is not an operation that should be
#: reachable by editing a variable; it should require repeated, observed runs.
MAX_DELETIONS_CEILING = 100

#: Bounds the join itself. A reconciler that walked an unbounded account would
#: time out mid-pass and produce a partial report indistinguishable from a
#: complete one.
MAX_KNOWLEDGE_BASES_PER_RUN = 2000

# ── Vector state ─────────────────────────────────────────────────────────────
#
# Written on a record whose AWS knowledge base has gone. Not a failure state: the
# corpus is intact and the next ingest re-provisions.
VECTOR_STATE_MISSING = "missing"

# ── Metrics ──────────────────────────────────────────────────────────────────
METRIC_ORPHANS_FOUND = "KbOrphansFound"
METRIC_ORPHANS_DELETED = "KbOrphansDeleted"
METRIC_VECTORS_MISSING = "KbVectorsMissing"
METRIC_RECONCILER_LIMIT_REACHED = "KbReconcilerLimitReached"


@dataclass
class PlannedDeletion:
    """An orphan the reconciler intends to delete, and why it is eligible."""

    kb_id: str
    name: str
    status: str
    created_at: Optional[str]
    age_hours: Optional[float]
    performed: bool = False
    error: Optional[str] = None


@dataclass
class ReconcileReport:
    """What one run found and what it did.

    ``armed`` is on the report rather than only in the logs so a stored artifact
    is self-describing: an operator reading last night's output should not have to
    go and check what the flag was set to at the time.
    """

    armed: bool = False
    aws_knowledge_bases: int = 0
    records: int = 0
    matched: int = 0
    orphans: int = 0
    planned_deletions: List[PlannedDeletion] = field(default_factory=list)
    skipped_too_young: List[str] = field(default_factory=list)
    marked_missing: List[str] = field(default_factory=list)
    refreshed_bytes: List[str] = field(default_factory=list)
    limit_reached: bool = False

    #: Fleet gauges (Requirement 22.1), accumulated over the record side of the
    #: join. Computed from each KB_Record as it was read, so a ``storedBytes``
    #: refresh performed later in the same pass lands in the *next* pass's gauge —
    #: acceptable for a daily number, and cheaper than a second full scan.
    stored_bytes: int = 0
    idle_bytes: int = 0
    #: Knowledge bases with no recorded activity at all: never retrieved and their
    #: agent never used. Reported so ``KbIdleGB`` can be read honestly — these are
    #: unmeasured, not idle, and counting them as idle would make every freshly
    #: provisioned corpus look abandoned.
    unmeasured_idleness: int = 0

    @property
    def deletions_performed(self) -> int:
        return sum(1 for planned in self.planned_deletions if planned.performed)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "armed": self.armed,
            "mode": "armed" if self.armed else "report-only",
            "awsKnowledgeBases": self.aws_knowledge_bases,
            "records": self.records,
            "matched": self.matched,
            "orphans": self.orphans,
            "plannedDeletions": [
                {
                    "knowledgeBaseId": planned.kb_id,
                    "name": planned.name,
                    "status": planned.status,
                    "createdAt": planned.created_at,
                    "ageHours": planned.age_hours,
                    "performed": planned.performed,
                    "error": planned.error,
                }
                for planned in self.planned_deletions
            ],
            "deletionsPerformed": self.deletions_performed,
            "skippedTooYoung": self.skipped_too_young,
            "markedMissing": self.marked_missing,
            "refreshedBytes": self.refreshed_bytes,
            "limitReached": self.limit_reached,
            "storedBytes": self.stored_bytes,
            "idleBytes": self.idle_bytes,
            "unmeasuredIdleness": self.unmeasured_idleness,
        }


# ── Flag and tunable readers ─────────────────────────────────────────────────
def reconciler_armed() -> bool:
    """Whether the reconciler may delete. Defaults to **off**.

    An empty string is off (Requirement 19.8). This is not defensive
    over-engineering: an unset repository or environment variable expands to the
    empty string in GitHub Actions, and this repo has been bitten by that before —
    a flag nobody set looking set, in the one component whose mistakes are
    irreversible.
    """
    raw = os.environ.get(FLAG_RECONCILER_ARMED)
    if not raw:
        return False
    return raw.strip().lower() in _TRUTHY


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a number; falling back to {default}")
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} is not an integer; falling back to {default}")
        return default


def orphan_min_age_hours() -> float:
    return _env_float("MANAGED_KB_ORPHAN_MIN_AGE_HOURS", ORPHAN_MIN_AGE_HOURS)


def max_deletions_per_run() -> int:
    """The per-run deletion bound, clamped so the environment cannot lift it.

    The env var may lower the limit but not raise it past
    :data:`MAX_DELETIONS_CEILING` (Requirement 14.8). A bound that any environment
    variable can set to a million is not a bound, and this is the one limit whose
    failure mode is irreversible: it is what stops a single bad run — a wrong tag
    filter, a botched migration — from deleting an account's worth of user
    knowledge bases before anyone reads the report.
    """
    requested = _env_int("MANAGED_KB_RECONCILER_MAX_DELETIONS", MAX_DELETIONS_PER_RUN)
    if requested > MAX_DELETIONS_CEILING:
        logger.warning(
            f"MANAGED_KB_RECONCILER_MAX_DELETIONS={requested} exceeds the ceiling of "
            f"{MAX_DELETIONS_CEILING}; clamping. Run the reconciler repeatedly rather "
            f"than raising this."
        )
        return MAX_DELETIONS_CEILING
    return max(requested, 0)


def max_knowledge_bases_per_run() -> int:
    return _env_int("MANAGED_KB_RECONCILER_MAX_SCANNED", MAX_KNOWLEDGE_BASES_PER_RUN)


# ── DynamoDB plumbing ────────────────────────────────────────────────────────
def _table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["DYNAMODB_ASSISTANTS_TABLE_NAME"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    from apis.shared.timestamps import utc_now_iso

    return utc_now_iso()


def iter_kb_records() -> Iterator[Dict[str, Any]]:
    """Every KB_Record in the table, paging the scan to exhaustion.

    A scan, because the ``KbWorkIndex`` GSI is *sparse* and deliberately holds
    only records that are eligible for migration work — the records this join
    cares most about are precisely the ones absent from it. Paged to exhaustion
    for the same reason the AWS list is: a truncated read makes every unread
    record look like an orphan on the AWS side.

    ``KBTOMB#`` sort keys do not match ``begins_with(SK, "KB#")``, so tombstones
    are excluded by the key prefix rather than filtered afterwards.
    """
    from boto3.dynamodb.conditions import Attr

    table = _table()
    kwargs: Dict[str, Any] = {"FilterExpression": Attr("SK").begins_with("KB#")}
    while True:
        response = table.scan(**kwargs)
        for item in response.get("Items") or []:
            yield item
        start = response.get("LastEvaluatedKey")
        if not start:
            return
        kwargs["ExclusiveStartKey"] = start


# ── Age gate (Requirement 14.3, 14.4) ────────────────────────────────────────
def parse_aws_timestamp(value: Any) -> Optional[datetime]:
    """Coerce AWS's ``createdAt`` to an aware UTC datetime, or ``None``.

    boto3 hands back a ``datetime`` here, but a value that has been through a
    stubbed client, an EventBridge payload or a JSON round-trip arrives as a
    string or an epoch number. All three are accepted; anything unparseable
    returns ``None``, which the age gate treats as *not old enough* rather than
    guessing.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float, Decimal)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        from apis.shared.timestamps import from_iso

        try:
            return from_iso(value)
        except ValueError:
            return None
    return None


def orphan_age_hours(created_at: Any, now: Optional[datetime] = None) -> Optional[float]:
    """Hours since **AWS's** ``createdAt``, or ``None`` if it cannot be read."""
    created = parse_aws_timestamp(created_at)
    if created is None:
        return None
    return ((now or _now()) - created).total_seconds() / 3600.0


def orphan_is_deletable(
    created_at: Any,
    now: Optional[datetime] = None,
    min_age_hours: Optional[float] = None,
) -> bool:
    """Whether an orphan has existed in AWS long enough to be deleted.

    The input is AWS's ``createdAt``. It is deliberately not "when did we first
    see this": see the module docstring. Passing a discovery timestamp here would
    type-check, run, pass a naive test, and delete in-flight creates in
    production.

    A missing or unparseable ``createdAt`` returns ``False``. Failing closed is
    the only safe direction for a destructive action: an orphan left one more day
    costs pennies, and a knowledge base deleted 40 seconds into its creation costs
    a user their upload.
    """
    if min_age_hours is None:
        min_age_hours = orphan_min_age_hours()

    created = parse_aws_timestamp(created_at)
    if created is None:
        return False
    return (now or _now()) - created > timedelta(hours=min_age_hours)


# ── Record-side actions ──────────────────────────────────────────────────────
def mark_vector_state_missing(assistant_id: str, app_kb_id: str) -> None:
    """Record that the AWS knowledge base behind this record has gone.

    **This never deletes the record**, and there is deliberately no function in
    this module that does. The vectors are gone; the corpus is not. The uploaded
    bytes are still in S3 and the ``DOC#`` records still describe them, so the
    next ingest re-provisions a knowledge base and re-indexes from the documents
    already present. The record carries the only mapping from ``App_KB_Id`` to
    that corpus, so removing it would turn a recoverable, invisible-to-the-user
    situation into permanent data loss.

    ``awsKbId``/``awsDataSourceId`` are left in place rather than cleared: they
    are the evidence of which AWS resource vanished, and provisioning already
    treats a record it cannot find in AWS as needing a fresh create.
    """
    _table().update_item(
        Key={"PK": kb_pk(assistant_id), "SK": kb_sk(app_kb_id)},
        UpdateExpression=(
            "SET vectorState = :missing, vectorStateObservedAt = :now, updatedAt = :now"
        ),
        ExpressionAttributeValues={":missing": VECTOR_STATE_MISSING, ":now": _now_iso()},
    )
    emit_count(METRIC_VECTORS_MISSING)


def refresh_stored_bytes(assistant_id: str, app_kb_id: str, stored_bytes: int) -> None:
    """Re-anchor quota accounting, and clear any stale ``vectorState``.

    The ``REMOVE`` matters: a record marked ``missing`` on an earlier run that has
    since been re-provisioned would otherwise stay marked for ever, and the UI
    would keep telling its owner their knowledge base is broken after it was
    fixed.
    """
    _table().update_item(
        Key={"PK": kb_pk(assistant_id), "SK": kb_sk(app_kb_id)},
        UpdateExpression=(
            "SET storedBytes = :bytes, updatedAt = :now "
            "REMOVE vectorState, vectorStateObservedAt"
        ),
        ExpressionAttributeValues={":bytes": Decimal(int(stored_bytes)), ":now": _now_iso()},
    )


def stored_bytes_from_s3(assistant_id: str, bucket: Optional[str] = None, s3_client=None) -> Optional[int]:
    """Total size of an assistant's uploaded documents, straight from S3.

    S3 rather than a client-reported or previously-stored value, for the same
    reason the byte cap uses a ``HEAD``: this number gates a $150,000/month
    exposure at full adoption, and the only trustworthy source for it is the
    service holding the bytes.

    Returns ``None`` when no bucket is configured or the listing fails, and the
    caller then leaves ``storedBytes`` alone. Writing a zero on a failed listing
    would silently hand every owner their whole allowance back.
    """
    bucket = bucket or os.environ.get("S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME")
    if not bucket:
        return None

    if s3_client is None:
        import boto3

        s3_client = boto3.client("s3")

    prefix = f"assistants/{assistant_id}/documents/"
    total = 0
    token: Optional[str] = None
    try:
        while True:
            kwargs: Dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = s3_client.list_objects_v2(**kwargs)
            for obj in response.get("Contents") or []:
                total += int(obj.get("Size") or 0)
            if not response.get("IsTruncated"):
                return total
            token = response.get("NextContinuationToken")
            if not token:
                return total
    except Exception as exc:  # noqa: BLE001 - a failed listing must not zero the quota
        logger.warning(f"could not total stored bytes for {assistant_id}: {exc}")
        return None


# ── The run ──────────────────────────────────────────────────────────────────
def reconcile(
    client=None,
    project_prefix: Optional[str] = None,
    environment: Optional[str] = None,
    armed: Optional[bool] = None,
    now: Optional[datetime] = None,
    stored_bytes_resolver: Optional[Callable[[str], Optional[int]]] = None,
) -> ReconcileReport:
    """One reconciliation pass.

    ``armed`` defaults to :func:`reconciler_armed`, i.e. to the flag, i.e. to off.
    It is an argument only so a test can exercise the armed path without mutating
    process environment — never so a caller can conveniently turn deletion on.
    """
    from apis.shared.kb_backend import tombstones as tb

    if client is None:
        from apis.shared.kb_backend.managed_backend import bedrock_agent_client

        client = bedrock_agent_client()

    if armed is None:
        armed = reconciler_armed()
    if now is None:
        now = _now()

    report = ReconcileReport(armed=armed)
    scan_limit = max_knowledge_bases_per_run()
    delete_limit = max_deletions_per_run()
    min_age = orphan_min_age_hours()

    # ── record side ──────────────────────────────────────────────────────────
    # Keyed by awsKbId, because that is the identifier the AWS side reports. A
    # record with no awsKbId has not finished provisioning and is not evidence of
    # anything: it is skipped rather than counted as a missing-vector record,
    # which would mark every in-flight create broken.
    records_by_aws_id: Dict[str, Dict[str, Any]] = {}
    unprovisioned = 0
    for item in iter_kb_records():
        report.records += 1
        _accumulate_gauges(item, report, now)
        aws_kb_id = item.get("awsKbId")
        if aws_kb_id:
            records_by_aws_id[str(aws_kb_id)] = item
        else:
            unprovisioned += 1

    # ── AWS side ─────────────────────────────────────────────────────────────
    seen_aws_ids: set = set()
    orphan_facts: List[tb.KnowledgeBaseFacts] = []

    for facts in tb.iter_project_knowledge_bases(
        client, project_prefix=project_prefix, environment=environment
    ):
        if report.aws_knowledge_bases >= scan_limit:
            report.limit_reached = True
            logger.warning(
                f"stopping the AWS walk at {scan_limit} knowledge bases; this run's "
                f"join is partial and no deletion decision is made beyond this point"
            )
            break

        report.aws_knowledge_bases += 1
        seen_aws_ids.add(facts.kb_id)

        record = records_by_aws_id.get(facts.kb_id)
        if record is None:
            orphan_facts.append(facts)
        else:
            report.matched += 1
            _reconcile_matched(record, stored_bytes_resolver, report)

    # ── record only: mark missing, never delete (Requirement 14.5) ────────────
    #
    # Only meaningful when the AWS walk completed. On a truncated walk an unmatched
    # record may simply be one this run never reached.
    if not report.limit_reached:
        for aws_kb_id, record in sorted(records_by_aws_id.items()):
            if aws_kb_id in seen_aws_ids:
                continue
            app_kb_id = str(record.get("appKbId") or "")
            assistant_id = _assistant_id_of(record)
            if not app_kb_id or not assistant_id:
                logger.warning(f"skipping malformed KB_Record {record.get('PK')}/{record.get('SK')}")
                continue
            logger.info(
                f"KB_Record {app_kb_id} points at awsKbId {aws_kb_id}, which AWS does "
                f"not have. Marking vectorState={VECTOR_STATE_MISSING}. The record is "
                f"NOT deleted: its documents are still valid and the knowledge base "
                f"rebuilds from them on the next ingest."
            )
            mark_vector_state_missing(assistant_id, app_kb_id)
            report.marked_missing.append(app_kb_id)

    # ── AWS only: orphans (Requirements 14.2, 14.3, 14.4) ────────────────────
    report.orphans = len(orphan_facts)
    if report.orphans:
        emit_count(METRIC_ORPHANS_FOUND, value=report.orphans)

    for facts in orphan_facts:
        age = orphan_age_hours(facts.created_at, now=now)
        # The gate reads AWS's createdAt. Never the time of discovery.
        if not orphan_is_deletable(facts.created_at, now=now, min_age_hours=min_age):
            report.skipped_too_young.append(facts.kb_id)
            logger.info(
                f"orphan {facts.kb_id} ({facts.name}) has no KB_Record but AWS "
                f"reports createdAt={facts.created_at!r} "
                f"(age={age if age is None else round(age, 2)}h < {min_age}h); "
                f"leaving it alone — it is most likely an in-flight create"
            )
            continue

        planned = PlannedDeletion(
            kb_id=facts.kb_id,
            name=facts.name,
            status=facts.status,
            created_at=str(facts.created_at) if facts.created_at is not None else None,
            age_hours=None if age is None else round(age, 2),
        )

        # The limit applies whether or not we are armed, so the report describes
        # what an armed run would really do.
        if len(report.planned_deletions) >= delete_limit:
            report.limit_reached = True
            emit_count(METRIC_RECONCILER_LIMIT_REACHED)
            logger.warning(
                f"per-run deletion limit of {delete_limit} reached; {facts.kb_id} and "
                f"any further orphans are left for the next run"
            )
            break

        report.planned_deletions.append(planned)

        if facts.status == tb.KB_STATUS_DELETE_UNSUCCESSFUL:
            # Requirement 13.7 / the tombstone table's fourth row. Retrying the
            # delete does not help and the state does not clear on its own, so it
            # is surfaced as an operator state instead of being counted as work.
            planned.error = tb.KB_STATUS_DELETE_UNSUCCESSFUL
            emit_count(tb.METRIC_DELETE_UNSUCCESSFUL)
            logger.error(
                f"orphan {facts.kb_id} is in {tb.KB_STATUS_DELETE_UNSUCCESSFUL} and "
                f"needs operator action; it will not delete by retrying and it is "
                f"still being billed"
            )
            continue

        if not armed:
            # Report-only. This is the shipped mode and it performs no deletes.
            logger.warning(
                f"[report-only] WOULD delete orphan knowledge base {facts.kb_id} "
                f"({facts.name}), AWS createdAt={facts.created_at!r}, "
                f"age={planned.age_hours}h. Set {FLAG_RECONCILER_ARMED} to arm."
            )
            continue

        try:
            _delete_orphan(facts, client)
            planned.performed = True
            emit_count(METRIC_ORPHANS_DELETED)
            logger.info(f"deleted orphan knowledge base {facts.kb_id}")
        except Exception as exc:  # noqa: BLE001 - one bad orphan must not end the run
            planned.error = str(exc)
            logger.error(f"failed to delete orphan {facts.kb_id}: {exc}", exc_info=True)

    emit_fleet_gauges(
        kb_count=report.records,
        stored_bytes=report.stored_bytes,
        idle_bytes=report.idle_bytes,
        unmeasured=report.unmeasured_idleness,
    )

    logger.info(
        f"reconcile complete: mode={'armed' if armed else 'report-only'} "
        f"aws={report.aws_knowledge_bases} records={report.records} "
        f"matched={report.matched} orphans={report.orphans} "
        f"planned={len(report.planned_deletions)} "
        f"performed={report.deletions_performed} "
        f"markedMissing={len(report.marked_missing)} "
        f"unprovisioned={unprovisioned} "
        f"storedGB={report.stored_bytes / 1_000_000_000:.3f} "
        f"idleGB={report.idle_bytes / 1_000_000_000:.3f} "
        f"unmeasured={report.unmeasured_idleness}"
    )
    return report


def _accumulate_gauges(
    record: Dict[str, Any],
    report: ReconcileReport,
    now: Optional[datetime] = None,
) -> None:
    """Fold one KB_Record into the fleet gauges (Requirements 22.1, 22.5).

    The reconciler is where this belongs because it is already the one pass that
    walks every knowledge base; a second sweep to count them would double a scan
    that exists.

    Idleness comes from :func:`idleness.idle_days`, which takes the **maximum** of
    the knowledge base's own ``lastRetrievedAt`` and its bound agents'
    ``lastUsedAt``. Never retrieval alone: an agent can be invoked all day and
    retrieve nothing, because retrieval only fires when the query matches, so a
    corpus judged by retrieval alone looks abandoned exactly when its agent is
    busiest with questions the documents do not answer.

    A record with no activity signal at all counts toward ``unmeasured_idleness``
    and **not** toward idle bytes. It is unmeasured, not idle — that is what a
    knowledge base provisioned an hour ago looks like.
    """
    from apis.shared.kb_backend.idleness import idle_days

    stored = int(record.get("storedBytes") or 0)
    report.stored_bytes += stored

    assistant_id = _assistant_id_of(record)
    if not assistant_id:
        return

    try:
        days = idle_days(assistant_id, record, now=_iso_or_none(now))
    except Exception as exc:  # noqa: BLE001 - a gauge must not end the pass
        logger.warning(f"could not compute idleness for {assistant_id}: {exc}")
        return

    if days is None:
        report.unmeasured_idleness += 1
    elif days >= idle_threshold_days():
        report.idle_bytes += stored


def _iso_or_none(moment: Optional[datetime]) -> Optional[str]:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def idle_threshold_days() -> int:
    """Days without a sign of life before bytes count as idle.

    A **reporting** threshold. Nothing reclaims in this phase, and the number the
    follow-up spec eventually evicts on should come from the distribution this
    metric records rather than being inherited from this default.
    """
    from apis.shared.kb_backend.metrics import IDLE_THRESHOLD_DAYS

    return _env_int("KB_IDLE_THRESHOLD_DAYS", IDLE_THRESHOLD_DAYS)


def _assistant_id_of(record: Dict[str, Any]) -> str:
    """Recover the assistant id from the record's ``PK``."""
    pk = str(record.get("PK") or "")
    return pk[len("AST#") :] if pk.startswith("AST#") else ""


def _reconcile_matched(
    record: Dict[str, Any],
    stored_bytes_resolver: Optional[Callable[[str], Optional[int]]],
    report: ReconcileReport,
) -> None:
    """Both sides agree: refresh stored bytes (Requirement 14.6).

    Written only when the number actually changed, or when a stale
    ``vectorState`` needs clearing. A daily no-op write per knowledge base would
    be pure cost and would churn ``updatedAt`` on records nothing happened to.
    """
    app_kb_id = str(record.get("appKbId") or "")
    assistant_id = _assistant_id_of(record)
    if not app_kb_id or not assistant_id:
        return

    resolver = stored_bytes_resolver or stored_bytes_from_s3
    actual = resolver(assistant_id)
    if actual is None:
        return

    current = int(record.get("storedBytes") or 0)
    stale_state = record.get("vectorState") is not None
    if actual == current and not stale_state:
        return

    refresh_stored_bytes(assistant_id, app_kb_id, actual)
    report.refreshed_bytes.append(app_kb_id)


def _delete_orphan(facts, client) -> None:
    """Delete an orphan through the tombstoned saga.

    Through the saga rather than a bare ``DeleteKnowledgeBase`` because an orphan
    is by definition a resource a previous delete failed to remove, so the one
    thing it must not do is fail silently a second time. The saga writes the
    tombstone first, polls until AWS reports the knowledge base absent, and clears
    the tombstone only then.

    An orphan has no KB_Record — that is what makes it an orphan — so there is no
    assistant id to anchor its tombstone on. It is anchored on the ``appKbId`` tag
    the provisioner wrote, falling back to the AWS identifier, and the item is
    flagged ``syntheticPartition`` so nobody reads that ``PK`` as a real assistant
    and nobody expects ``iter_tombstones(<assistant id>)`` to surface it.
    ``anchorSource`` records which of the two identifiers was available, because
    when this item is being triaged that is the first question.

    ``remove_record`` is left false: there is no record to remove, and this module
    never removes one.
    """
    from apis.shared.kb_backend import tombstones as tb
    from apis.shared.kb_backend.tags import TAG_KEY_APP_KB_ID

    tags = facts.tags or {}
    # The AWS tag, whose key is owned by `kb_backend.tags` — not the KB_Record
    # attribute, which is separately named `appKbId` and stays that way.
    tagged = tags.get(TAG_KEY_APP_KB_ID)
    anchor = tagged or facts.kb_id
    tb.delete_knowledge_base(
        anchor,
        anchor,
        facts.kb_id,
        client=client,
        remove_record=False,
        extra_attributes={
            tb.SYNTHETIC_PARTITION: True,
            "anchorSource": f"tag:{TAG_KEY_APP_KB_ID}" if tagged else "aws:knowledgeBaseId",
            "orphanKbName": facts.name or None,
        },
    )


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Scheduled entry point. Returns the report so it lands in the invocation log.

    The invocation event is deliberately **not** consulted for arming. The
    environment flag is the only way to arm (Requirement 19.7), because an event
    payload is the one input an operator does not review: an EventBridge target
    carrying a constant ``{"armed": true}``, or any principal holding
    ``lambda:InvokeFunction``, would delete billed user resources while every
    piece of reviewable configuration still said report-only, leaving nothing
    behind but an ``Invoke`` in CloudTrail. If the event disagrees with the flag,
    the flag wins, and the disagreement is logged rather than honoured.
    """
    requested = (event or {}).get("armed")
    if requested is not None:
        logger.warning(
            f"ignoring armed={requested!r} from the invocation event: arming is "
            f"controlled only by {FLAG_RECONCILER_ARMED}"
        )
    report = reconcile()
    return {"statusCode": 200, "report": report.to_dict()}


__all__ = [
    "FLAG_RECONCILER_ARMED",
    "MAX_DELETIONS_CEILING",
    "MAX_DELETIONS_PER_RUN",
    "MAX_KNOWLEDGE_BASES_PER_RUN",
    "METRIC_ORPHANS_DELETED",
    "METRIC_ORPHANS_FOUND",
    "METRIC_RECONCILER_LIMIT_REACHED",
    "METRIC_VECTORS_MISSING",
    "ORPHAN_MIN_AGE_HOURS",
    "VECTOR_STATE_MISSING",
    "PlannedDeletion",
    "ReconcileReport",
    "iter_kb_records",
    "lambda_handler",
    "mark_vector_state_missing",
    "max_deletions_per_run",
    "max_knowledge_bases_per_run",
    "orphan_age_hours",
    "orphan_is_deletable",
    "orphan_min_age_hours",
    "parse_aws_timestamp",
    "reconcile",
    "reconciler_armed",
    "refresh_stored_bytes",
    "stored_bytes_from_s3",
]
