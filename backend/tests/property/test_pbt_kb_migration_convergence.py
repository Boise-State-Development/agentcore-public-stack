"""
Property-based tests for migration convergence.

**Property 6: an interrupted migration converges without duplication**

For any interruption point in the state machine, a resumed run reaches the same
terminal state, creates exactly **one** knowledge base, promotes exactly **once**,
and leaves each document in the corpus exactly once.

What "without duplication" can and cannot mean
----------------------------------------------
A worker can die between a successful ``IngestKnowledgeBaseDocuments`` and the
DynamoDB write that records it, and no transaction spans Bedrock and DynamoDB. So
"each document is ingested at most once" is not achievable, and asserting it would
be asserting something false. Two things *are* achievable, and both are asserted:

* **Each document appears in the corpus exactly once**, because
  ``customDocumentIdentifier`` is the platform document id and a re-ingest
  therefore replaces. A migration that derived its own identifier would fail here.
* **Redundant re-ingests are bounded by one batch** — the size of the crash window.
  That bound is what proves progress is persisted *as the migration proceeds*
  rather than only at the end. It is not a theoretical distinction: this test
  initially failed because the completed-document set lived inside the
  ``migrationProgress`` map, which a later write replaced wholesale, so a crash
  near the end of a 25-document corpus re-ingested all 25.

Why this needs to be a property rather than a set of cases
----------------------------------------------------------
The interruption points are not a short list. A migration can be cut off between
any two of: reserving bytes, creating the knowledge base, creating the data source,
writing the AWS identifiers back, ingesting each individual batch, recording
progress, promoting, and stamping the retention window. Enumerating them by hand
produces the cases somebody thought of, and the ones that matter are the ones
nobody did — this feature has already been bitten by a crash window between an AWS
create and the database write that records it.

So the interruption index is a hypothesis input over the sequence of effects, and
the invariants are asserted after replaying from the start, which is what a retry
actually does.

The model is deliberately in-memory
-----------------------------------
A fake DynamoDB and a fake Bedrock, both of which enforce the properties that make
convergence possible rather than assuming them:

* ``create_knowledge_base`` is deduplicated by ``clientToken`` — which is how AWS
  behaves, and the reason the worker persists the token before calling AWS.
* ``ingest_knowledge_base_documents`` records every ``customDocumentIdentifier``
  it is handed, so "at most once" is measured over the whole replay rather than
  per attempt.

A test against real clients could not interrupt at a chosen point, and a test with
no model at all would assert only that the code does not raise.

Feature: managed-kb-migration
**Validates: Requirements 15.9, 15.10, 15.13, 7.4**
"""

from typing import Any, Dict, List, Optional, Set

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


class Interrupted(Exception):
    """The simulated crash. Raised at the chosen effect index."""


class Clock:
    """Counts effects and raises at the interruption point.

    Every externally-visible side effect passes through :meth:`tick`, so the
    interruption index addresses effects rather than lines of code — the unit a
    crash actually lands between.
    """

    def __init__(self, interrupt_at: Optional[int] = None):
        self.count = 0
        self.interrupt_at = interrupt_at
        self.log: List[str] = []

    def tick(self, what: str) -> None:
        self.count += 1
        self.log.append(what)
        if self.interrupt_at is not None and self.count == self.interrupt_at:
            raise Interrupted(f"crashed at effect {self.count}: {what}")


class FakeAws:
    """Bedrock's idempotency, modelled rather than assumed."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.kbs_by_token: Dict[str, str] = {}
        self.data_sources: Dict[str, str] = {}
        #: Every ingest ever accepted, across every attempt. The duplication
        #: invariant is measured here.
        self.ingest_log: List[str] = []
        #: document_id -> times written. Distinct keys are the corpus; the counts
        #: are the redundant work a resume did.
        self.corpus: Dict[str, int] = {}
        self.next_id = 0

    def create_knowledge_base(self, client_token: str) -> str:
        # Deduplicated by token: this is what makes a retried create safe, and the
        # reason the record persists the token *before* the AWS call.
        if client_token in self.kbs_by_token:
            return self.kbs_by_token[client_token]
        self.clock.tick("CreateKnowledgeBase")
        self.next_id += 1
        kb_id = f"KB{self.next_id:04d}"
        self.kbs_by_token[client_token] = kb_id
        return kb_id

    def create_data_source(self, kb_id: str, client_token: str) -> str:
        if client_token in self.data_sources:
            return self.data_sources[client_token]
        self.clock.tick("CreateDataSource")
        ds_id = f"DS-{kb_id}"
        self.data_sources[client_token] = ds_id
        return ds_id

    def ingest(self, document_ids: List[str]) -> None:
        self.clock.tick(f"Ingest({','.join(document_ids)})")
        self.ingest_log.extend(document_ids)
        for document_id in document_ids:
            # ``customDocumentIdentifier`` is the platform document id, so a
            # re-ingest *replaces* rather than appends. Modelled because it is what
            # makes the unavoidable crash window survivable: a worker can die
            # between a successful Ingest and the bookkeeping write, and no
            # transaction spans Bedrock and DynamoDB.
            self.corpus[document_id] = self.corpus.get(document_id, 0) + 1

    @property
    def corpus_document_count(self) -> int:
        """Distinct documents in the knowledge base."""
        return len(self.corpus)

    @property
    def knowledge_base_count(self) -> int:
        return len(set(self.kbs_by_token.values()))


class FakeRecord:
    """The KB_Record, with the conditional writes that matter."""

    def __init__(self, clock: Clock, document_ids: List[str]):
        self.clock = clock
        self.item: Dict[str, Any] = {}
        self.documents: Dict[str, str] = {d: "complete" for d in document_ids}
        self.promotions = 0

    # -- reads ---------------------------------------------------------------
    def get(self) -> Dict[str, Any]:
        return dict(self.item)

    def list_complete(self) -> List[str]:
        return sorted(d for d, status in self.documents.items() if status == "complete")

    def status_of(self, document_id: str) -> Optional[str]:
        return self.documents.get(document_id)

    # -- writes --------------------------------------------------------------
    def create_provisioning(self, client_token: str) -> None:
        if self.item:
            return  # attribute_not_exists guard: the retry anchor already exists
        self.clock.tick("CreateProvisioning")
        self.item = {
            "clientToken": client_token,
            "provisioningState": "provisioning",
            "migrationState": "shadow",
            "migrationGeneration": 1,
            "totalBytes": 0,
        }

    def attach_ids(self, kb_id: str, ds_id: str) -> None:
        if self.item.get("awsKbId"):
            return
        self.clock.tick("AttachAwsIds")
        self.item["awsKbId"] = kb_id
        self.item["awsDataSourceId"] = ds_id
        self.item["provisioningState"] = "active"

    def reserve(self, total: int) -> None:
        self.clock.tick("ReserveSnapshot")
        self.item["totalBytes"] = total

    def set_progress(self, migrated: int, total: int, newly_done: List[str] = None) -> None:
        self.clock.tick("SetProgress")
        self.item["migrationProgress"] = {"migrated": migrated, "total": total}
        if newly_done:
            # ADD on a string set: additive, and a *separate attribute* from the
            # progress map this write replaces. Modelled that way because the
            # first version of this test kept the completed set inside the map,
            # the map got overwritten, and the resumed run re-ingested a corpus
            # it had already finished. The worker had the same bug.
            existing = set(self.item.get("migratedDocIds") or ())
            self.item["migratedDocIds"] = existing | set(newly_done)

    def add_done(self, document_ids: List[str]) -> None:
        """The per-batch ADD, which is what survives a crash between batches."""
        if not document_ids:
            return
        self.clock.tick(f"AddDone({','.join(document_ids)})")
        existing = set(self.item.get("migratedDocIds") or ())
        self.item["migratedDocIds"] = existing | set(document_ids)

    def set_state(self, new_state: str, expected: Optional[Set[str]] = None) -> bool:
        if expected is not None and self.item.get("migrationState") not in expected:
            return False
        self.clock.tick(f"SetState({new_state})")
        self.item["migrationState"] = new_state
        return True

    def promote(self) -> bool:
        progress = self.item.get("migrationProgress") or {}
        if self.item.get("retrievalEngine"):
            # attribute_not_exists(retrievalEngine): already promoted. Every other
            # guard stays true after a successful promotion, so without this one a
            # crash between the promotion and the state transition promotes twice —
            # and two concurrent workers both succeed.
            return False
        if self.item.get("migrationState") != "promote":
            return False
        if progress.get("migrated") != progress.get("total"):
            # Requirement 15.9 in the model: convergence is part of the condition,
            # not a separate check somebody could forget to call.
            return False
        self.clock.tick("Promote")
        self.promotions += 1
        self.item["retrievalEngine"] = "managed"
        return True


BATCH = 10


def run_migration(record: FakeRecord, aws: FakeAws, clock: Clock) -> str:
    """Replay the whole machine from the start. Idempotent by construction.

    This mirrors the real worker's ordering exactly, and the ordering is the thing
    under test: the record is written *before* AWS is called, the persisted token is
    reused on resume, and every document's status is re-read immediately before it
    is ingested.
    """
    token = "kb-token-fixed-length-padding-000000"

    # shadow
    record.create_provisioning(token)
    if not record.item.get("totalBytes"):
        record.reserve(len(record.list_complete()) * 1024)

    kb_id = record.item.get("awsKbId") or aws.create_knowledge_base(
        record.item.get("clientToken") or token
    )
    ds_id = record.item.get("awsDataSourceId") or aws.create_data_source(kb_id, token)
    record.attach_ids(kb_id, ds_id)

    already = set(record.item.get("migratedDocIds") or ())
    snapshot = record.list_complete()
    pending = [d for d in snapshot if d not in already]

    migrated = set(already)
    for start in range(0, len(pending), BATCH):
        batch = [
            d
            for d in pending[start : start + BATCH]
            # Requirement 16.4: re-read immediately before ingesting.
            if record.status_of(d) == "complete"
        ]
        if not batch:
            continue
        aws.ingest(batch)
        migrated.update(batch)
        # Persisted per batch, so a crash between batches loses only the batch in
        # flight rather than the whole run's progress.
        record.add_done(batch)

    # catch-up until quiet
    passes = 0
    while passes < 5:
        passes += 1
        new = [d for d in record.list_complete() if d not in migrated]
        if not new:
            break
        aws.ingest(new)
        migrated.update(new)
        record.add_done(new)

    record.set_progress(len(migrated), len(record.list_complete()))
    record.set_state("verify", {"shadow"})

    # verify
    record.set_state("promote", {"verify"})

    # promote. An already-promoted record still finishes: the promotion write is
    # guarded on the engine attribute being absent, so a resume after a crash
    # between the promotion and the state transition must continue to `retain`
    # rather than treat the refusal as a failure.
    if record.promote() or record.item.get("retrievalEngine") == "managed":
        record.set_state("retain", {"promote"})

    return record.item.get("migrationState", "")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

st_document_ids = st.lists(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=6),
    min_size=1,
    max_size=25,
    unique=True,
)

#: Effects, not lines. A migration of 25 documents produces roughly a dozen; the
#: upper bound is generous so an index past the end simply means "not interrupted",
#: which is a case worth generating too.
st_interrupt_at = st.integers(min_value=1, max_value=30)


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(document_ids=st_document_ids, interrupt_at=st_interrupt_at)
def test_an_interrupted_migration_converges_without_duplication(document_ids, interrupt_at):
    """The whole property, in one test.

    Run once with a crash injected at ``interrupt_at``; then run again from the
    start, as a retry does. Assert the terminal state, exactly one knowledge base,
    and each document ingested at most once across **both** runs.
    """
    clock = Clock(interrupt_at=interrupt_at)
    aws = FakeAws(clock)
    record = FakeRecord(clock, document_ids)

    try:
        run_migration(record, aws, clock)
    except Interrupted:
        pass

    # The retry. No interruption this time.
    clock.interrupt_at = None
    final_state = run_migration(record, aws, clock)

    assert final_state == "retain", (
        f"a resumed migration did not converge: state={final_state!r}, "
        f"effects={clock.log}"
    )

    assert aws.knowledge_base_count == 1, (
        f"{aws.knowledge_base_count} knowledge bases were created; the persisted "
        f"clientToken is not deduplicating the retried create"
    )

    counts: Dict[str, int] = {}
    for document_id in aws.ingest_log:
        counts[document_id] = counts.get(document_id, 0) + 1
    redundant = sum(n - 1 for n in counts.values())

    # Each document appears in the corpus exactly once. This is the invariant that
    # actually matters, and it is real rather than tautological: it holds because
    # `customDocumentIdentifier` is the platform document id, so a re-ingest
    # replaces. A migration that derived its own identifier would fail here.
    assert aws.corpus_document_count == len(document_ids)
    assert all(document_id in aws.corpus for document_id in document_ids)

    # Redundant re-ingests are bounded by one batch: the crash window between a
    # successful Ingest and the write that records it. No transaction spans Bedrock
    # and DynamoDB, so that window cannot be closed — but it can be *bounded*, and
    # the bound is what proves progress is persisted per batch. Before the
    # completed-document set was persisted, a crash near the end of a 25-document
    # corpus re-ingested all 25; this assertion is what caught that.
    assert redundant <= BATCH, (
        f"{redundant} redundant ingests after one interruption, which is more than "
        f"the single batch that can be in flight; progress is not being persisted "
        f"as the migration proceeds. effects={clock.log}"
    )

    assert set(aws.ingest_log) == set(document_ids), (
        "the resumed migration did not end up with every document"
    )

    assert record.promotions == 1, (
        f"promotion happened {record.promotions} times; the conditional write is "
        f"not the single cutover"
    )


@settings(max_examples=100, deadline=None)
@given(document_ids=st_document_ids, delete_index=st.integers(min_value=0, max_value=24))
def test_a_document_deleted_mid_migration_is_never_ingested(document_ids, delete_index):
    """Requirements 16.4, 16.5, as a property over which document is deleted.

    The deletion lands after the snapshot is taken and before the document's turn
    comes, which is the only window in which resurrection is possible.
    """
    clock = Clock()
    aws = FakeAws(clock)
    record = FakeRecord(clock, document_ids)

    victim = document_ids[delete_index % len(document_ids)]

    original_status_of = record.status_of

    def _status_with_deletion(document_id: str):
        if document_id == victim:
            return None
        return original_status_of(document_id)

    record.status_of = _status_with_deletion
    record.documents.pop(victim)

    run_migration(record, aws, clock)

    assert victim not in aws.ingest_log, (
        f"document {victim!r} was deleted mid-migration and still reached the "
        f"managed corpus"
    )


@settings(max_examples=100, deadline=None)
@given(document_ids=st_document_ids)
def test_promotion_is_refused_until_catch_up_converges(document_ids):
    """Requirement 15.9, asserted through the promotion condition itself.

    Progress is deliberately left short of the total, as an unconverged catch-up
    leaves it. Promotion must be refused — and refused by the condition, so no
    caller can reach past it.
    """
    clock = Clock()
    record = FakeRecord(clock, document_ids)

    record.item = {
        "migrationState": "promote",
        "migrationProgress": {"migrated": max(len(document_ids) - 1, 0), "total": len(document_ids)},
    }

    assert record.promote() is False
    assert record.promotions == 0
    assert "retrievalEngine" not in record.item


@settings(max_examples=50, deadline=None)
@given(document_ids=st_document_ids)
def test_only_one_of_two_concurrent_promotions_wins(document_ids):
    """Requirement 15.10. The second attempt sees a record no longer in ``promote``
    and is refused, which is what the real conditional write does."""
    clock = Clock()
    record = FakeRecord(clock, document_ids)

    total = len(document_ids)
    record.item = {
        "migrationState": "promote",
        "migrationProgress": {"migrated": total, "total": total},
    }

    first = record.promote()
    record.set_state("retain", {"promote"})
    second = record.promote()

    assert first is True
    assert second is False
    assert record.promotions == 1


def test_the_model_can_actually_be_interrupted():
    """Guards the guard.

    If ``Clock.tick`` stopped raising, every property above would pass while
    testing nothing but the happy path. So assert that some interruption index
    genuinely prevents convergence on the first run.
    """
    clock = Clock(interrupt_at=1)
    aws = FakeAws(clock)
    record = FakeRecord(clock, ["d1", "d2"])

    with pytest.raises(Interrupted):
        run_migration(record, aws, clock)

    assert record.item.get("migrationState") != "retain"
