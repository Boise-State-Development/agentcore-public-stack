"""
Migration dispatcher and worker: bounded, leased, and safe to interrupt.

Requirements 15, 16, 19.6. The tests here concentrate on the things that are
invisible when they break:

* The dispatcher **no-ops when the flag is off**, and "off" includes present but
  empty. This is the reconciler-arming defect's shape, and it is worth re-testing
  per component because each one reads its own flag.
* The worker dispatches on the **record's** state, never the event's. An event
  field that could select `promote` would let a hand-crafted invocation cut a
  knowledge base over without it ever verifying.
* A document deleted mid-migration is **not resurrected** — asserted by deleting it
  between the snapshot and the ingest, which is the only window where the bug
  exists.
* Catch-up **converges on quiet**, not after a fixed number of passes.
* Concurrent promotion yields **one winner**, which is a property of the
  conditional write rather than of any locking here.

Feature: managed-kb-migration
Requirements: 15.4, 15.5, 15.6, 15.7, 15.8, 15.10, 15.13, 15.14, 16.2, 16.3,
16.4, 16.5, 17.1, 17.4, 19.6, 24.5
"""

from decimal import Decimal
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from apis.app_api.kb_migration import dispatcher, worker
from apis.shared.kb_backend import records as r
from apis.shared.kb_backend.protocol import Chunk

ASSISTANT_ID = "ast-migrate-001"
TABLE = "test-assistants"
BUCKET = "test-documents"

BASE_ENV = {
    "DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE,
    "S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME": BUCKET,
    "AWS_REGION": "us-west-2",
}


def _doc(document_id: str, status: str = "complete", size: int = 1024) -> Dict[str, Any]:
    return {
        "PK": f"AST#{ASSISTANT_ID}",
        "SK": f"DOC#{document_id}",
        "status": status,
        "filename": f"{document_id}.pdf",
        "s3Key": f"assistants/{ASSISTANT_ID}/documents/{document_id}/{document_id}.pdf",
        "contentHash": f"hash-{document_id}",
        "sizeBytes": Decimal(size),
    }


def _kb_record(state: str = r.SHADOW, **overrides) -> Dict[str, Any]:
    record = {
        "PK": f"AST#{ASSISTANT_ID}",
        "SK": f"KB#{ASSISTANT_ID}",
        "appKbId": ASSISTANT_ID,
        "ownerUserId": "user-migrate",
        "migrationState": state,
        "migrationGeneration": Decimal(1),
        "totalBytes": Decimal(4096),
        "awsKbId": "KB123",
        "awsDataSourceId": "DS123",
    }
    record.update(overrides)
    return record


async def _async_noop(*args, **kwargs):
    """An awaitable that does nothing.

    Used as ``side_effect`` rather than assigning a coroutine to ``return_value``:
    a coroutine object assigned that way is created once, so a mock called twice
    raises and a mock called never emits "coroutine was never awaited" — noise that
    makes a real leak invisible.
    """
    return None


class StubBackend:
    """Records what it was asked to ingest, delete and search."""

    def __init__(self, chunks: List[Chunk] = None):
        self.ingested: List[str] = []
        self.searched: List[str] = []
        self._chunks = chunks if chunks is not None else [
            Chunk(text="hit", relevance=1.0, document_id="d1", metadata={"document_id": "d1"})
        ]

    async def ingest_documents(self, kb_ref, sources, *, batch_size=10):
        self.ingested.extend(source.document_id for source in sources)

    async def search(self, kb_ref, query, top_k=5):
        self.searched.append(query)
        return list(self._chunks)

    async def delete_documents(self, kb_ref, document_ids, *, batch_size=10):  # pragma: no cover
        raise NotImplementedError


# ── Dispatcher ───────────────────────────────────────────────────────────────
class TestDispatcherFlag:
    @pytest.mark.parametrize("value", [None, "", "  ", "false", "0", "off", "no", "disabled"])
    def test_anything_but_a_truthy_spelling_is_off(self, value):
        """An allow-list, not a truthiness test. The failure being designed around
        is a value that is *present but empty*: ``bool("")`` is correct by luck,
        ``bool("false")`` is not."""
        env = {} if value is None else {dispatcher.FLAG_MIGRATION_ENABLED: value}
        with patch.dict("os.environ", env, clear=True):
            assert dispatcher.migration_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "enabled"])
    def test_affirmative_spellings_are_on(self, value):
        with patch.dict(
            "os.environ", {dispatcher.FLAG_MIGRATION_ENABLED: value}, clear=True
        ):
            assert dispatcher.migration_enabled() is True

    @pytest.mark.asyncio
    async def test_a_tick_with_the_flag_off_invokes_nothing(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(
            dispatcher, "_invoke_worker"
        ) as invoke, patch.object(dispatcher, "_due_records") as due:
            counts = await dispatcher.dispatch_once()

        invoke.assert_not_called()
        due.assert_not_called()
        assert counts == {"Due": 0, "Dispatched": 0, "Failed": 0}


class TestDispatcherLimit:
    def test_the_default_matches_the_sync_dispatcher(self):
        with patch.dict("os.environ", {}, clear=True):
            assert dispatcher.dispatch_limit() == 20

    def test_an_override_is_honoured(self):
        with patch.dict("os.environ", {"KB_MIGRATION_DISPATCH_LIMIT": "5"}, clear=True):
            assert dispatcher.dispatch_limit() == 5

    def test_an_override_above_the_ceiling_is_clamped(self):
        """A larger sweep should require repeated observed ticks, not a variable
        edit — and `StartIngestionJob` is 0.1 RPS account-wide and not
        adjustable, so the only way to stay under it is to not ask."""
        with patch.dict("os.environ", {"KB_MIGRATION_DISPATCH_LIMIT": "5000"}, clear=True):
            assert dispatcher.dispatch_limit() == dispatcher.DISPATCH_LIMIT_CEILING

    def test_a_nonsense_override_falls_back_to_the_default(self):
        with patch.dict("os.environ", {"KB_MIGRATION_DISPATCH_LIMIT": "lots"}, clear=True):
            assert dispatcher.dispatch_limit() == 20

    @pytest.mark.asyncio
    async def test_the_limit_bounds_the_tick_across_all_states_not_per_state(self):
        """Three states each honouring the limit would quietly be a 3x limit.

        Asserted on what each **query asked for**, not on the tick's total: the
        total is trimmed at the end, so a per-state sweep that read three times the
        budget from DynamoDB would still *return* the right number while paying for
        three times the reads.

        The first state deliberately returns fewer rows than the limit. That is the
        only shape where the bug is observable — if the first query fills the
        budget the loop exits either way, which is why the obvious version of this
        test passes with the arithmetic removed.
        """
        asked: List[int] = []
        rows = [_kb_record(r.SHADOW, appKbId=f"kb-{i}") for i in range(10)]

        def _query(state, now_iso, limit):
            asked.append(limit)
            # promote yields 2 of the 4 allowed; the rest could fill the tick.
            available = 2 if state == r.PROMOTE else 10
            return rows[: min(limit, available)]

        with patch.dict(
            "os.environ",
            {**BASE_ENV, dispatcher.FLAG_MIGRATION_ENABLED: "true", "KB_MIGRATION_DISPATCH_LIMIT": "4"},
            clear=True,
        ), patch("apis.shared.kb_backend.records.query_due_work", side_effect=_query), patch.object(
            dispatcher, "_invoke_worker"
        ) as invoke, patch.object(dispatcher, "_emit_metrics"):
            counts = await dispatcher.dispatch_once()

        assert counts["Due"] == 4
        assert invoke.call_count == 4
        assert asked[0] == 4
        assert asked[1] == 2, (
            f"the second state was asked for {asked[1]} records when only "
            f"{4 - 2} of the budget remained; each state is being given the whole "
            f"limit ({asked})"
        )


class TestDispatcherSweep:
    def test_every_work_eligible_state_is_swept(self):
        """Derived from ``WORK_ELIGIBLE_STATES``, so a state added there cannot be
        silently left unswept — it would stall forever with its work keys written
        and nothing reading them."""
        assert set(dispatcher._work_states()) == set(r.WORK_ELIGIBLE_STATES)

    def test_a_state_added_to_the_records_module_is_still_swept(self):
        """The assertion above passes today whether or not the derivation exists,
        because the priority list happens to name every state. So add one the
        dispatcher has never heard of and require it to be swept anyway — which is
        the whole point of deriving rather than restating.
        """
        extended = frozenset(set(r.WORK_ELIGIBLE_STATES) | {"reindex"})
        with patch.object(r, "WORK_ELIGIBLE_STATES", extended):
            states = dispatcher._work_states()

        assert "reindex" in states, (
            "a new work-eligible state is not swept; its records would keep their "
            "GSI7 work keys and never be handed to a worker"
        )
        # Appended, not promoted ahead of the known order.
        assert states[-1] == "reindex"

    def test_promote_is_swept_first(self):
        """A record in ``promote`` is one conditional write from finished, so
        draining beats starting new shadow work."""
        assert dispatcher._work_states()[0] == r.PROMOTE

    def test_no_terminal_state_is_swept(self):
        assert not set(dispatcher._work_states()) & set(r.TERMINAL_STATES)

    @pytest.mark.asyncio
    async def test_an_unaddressable_row_does_not_starve_the_sweep(self):
        good = _kb_record(r.SHADOW, appKbId="kb-good")
        bad = {"SK": "KB#kb-bad", "migrationState": r.SHADOW}  # no PK

        calls = {"n": 0}

        def _query(state, now_iso, limit):
            calls["n"] += 1
            return [bad, good] if calls["n"] == 1 else []

        with patch.dict(
            "os.environ",
            {**BASE_ENV, dispatcher.FLAG_MIGRATION_ENABLED: "true"},
            clear=True,
        ), patch("apis.shared.kb_backend.records.query_due_work", side_effect=_query), patch.object(
            dispatcher, "_invoke_worker"
        ) as invoke, patch.object(dispatcher, "_emit_metrics"):
            counts = await dispatcher.dispatch_once()

        assert counts["Failed"] == 1
        assert counts["Dispatched"] == 1
        assert invoke.call_args.args[0]["appKbId"] == "kb-good"

    @pytest.mark.asyncio
    async def test_a_failing_index_query_does_not_fail_the_tick(self):
        with patch.dict(
            "os.environ",
            {**BASE_ENV, dispatcher.FLAG_MIGRATION_ENABLED: "true"},
            clear=True,
        ), patch(
            "apis.shared.kb_backend.records.query_due_work",
            side_effect=RuntimeError("dynamodb down"),
        ), patch.object(dispatcher, "_emit_metrics"):
            counts = await dispatcher.dispatch_once()

        assert counts == {"Due": 0, "Dispatched": 0, "Failed": 0}

    def test_the_handler_reads_nothing_from_the_event(self):
        """The reconciler's arming bypass came from forwarding an event field.
        Nothing here may select a state, a limit or a knowledge base."""
        seen = {}

        async def _tick():
            seen["called"] = True
            return {"Due": 0, "Dispatched": 0, "Failed": 0}

        with patch.object(dispatcher, "dispatch_once", side_effect=_tick) as tick:
            dispatcher.lambda_handler({"migrationState": "promote", "armed": True}, None)

        assert seen.get("called") is True
        tick.assert_called_once_with()


# ── Worker: state selection ──────────────────────────────────────────────────
class TestTheRecordDecidesTheStep:
    @pytest.mark.asyncio
    async def test_an_event_cannot_select_promote(self):
        """A hand-crafted invocation must not be able to cut over a knowledge base
        that never verified."""
        record = _kb_record(r.SHADOW)

        with patch.dict("os.environ", BASE_ENV, clear=True), patch(
            "apis.shared.kb_backend.records.get_kb_record", return_value=record
        ), patch.object(worker, "take_lease", return_value="later"), patch.object(
            worker, "run_shadow"
        ) as shadow, patch.object(worker, "run_promote") as promote:
            shadow.return_value = worker.StepResult(ASSISTANT_ID, ASSISTANT_ID, r.SHADOW, r.VERIFY)
            await worker.run_step(ASSISTANT_ID, ASSISTANT_ID)

        shadow.assert_called_once()
        promote.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_terminal_record_is_a_no_op(self):
        """The index is eventually consistent, so a record that finished a moment
        ago can still be handed over once. That is not an error."""
        record = _kb_record(r.RETAIN)

        with patch.dict("os.environ", BASE_ENV, clear=True), patch(
            "apis.shared.kb_backend.records.get_kb_record", return_value=record
        ), patch.object(worker, "take_lease") as lease:
            result = await worker.run_step(ASSISTANT_ID)

        lease.assert_not_called()
        assert result.to_state == r.RETAIN
        assert "not work-eligible" in result.detail

    @pytest.mark.asyncio
    async def test_a_lost_lease_propagates_rather_than_failing_the_migration(self):
        """Requirement 15.13. Two overlapping ticks is ordinary; marking the
        migration `failed` because of it would strand a healthy knowledge base."""
        record = _kb_record(r.SHADOW)

        with patch.dict("os.environ", BASE_ENV, clear=True), patch(
            "apis.shared.kb_backend.records.get_kb_record", return_value=record
        ), patch(
            "apis.shared.kb_backend.records.acquire_lease",
            side_effect=RuntimeError("conditional check failed"),
        ), patch(
            "apis.shared.kb_backend.metrics.emit_count"
        ), patch.object(worker, "_fail") as fail:
            with pytest.raises(worker.LeaseLost):
                await worker.run_step(ASSISTANT_ID)

        fail.assert_not_called()


# ── Worker: shadow and catch-up ──────────────────────────────────────────────
class TestShadowAndCatchUp:
    @pytest.mark.asyncio
    async def test_documents_are_ingested_from_their_existing_s3_keys(self):
        """Requirement 15.4: a re-ingest, never a re-upload."""
        docs = [_doc("d1"), _doc("d2")]
        backend = StubBackend()
        captured = {}

        async def _capture(kb_ref, sources, *, batch_size=10):
            captured["sources"] = list(sources)
            backend.ingested.extend(s.document_id for s in sources)

        backend.ingest_documents = _capture

        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", return_value=docs
        ), patch.object(
            worker, "get_document_item", side_effect=lambda a, d: _doc(d)
        ), patch(
            "apis.shared.kb_backend.byte_cap.reserve_snapshot"
        ), patch(
            "apis.shared.kb_backend.provisioning.provision_managed_kb"
        ) as provision, patch(
            "apis.shared.kb_backend.metrics.emit_count"
        ), patch.object(
            worker, "_record_progress"
        ), patch(
            "apis.shared.kb_backend.records.set_migration_state"
        ):
            provision.side_effect = _async_noop
            result = await worker.run_shadow(
                ASSISTANT_ID, ASSISTANT_ID, _kb_record(r.SHADOW), backend
            )

        assert result.to_state == r.VERIFY
        assert sorted(backend.ingested) == ["d1", "d2"]
        keys = {s.s3_key for s in captured["sources"]}
        assert keys == {
            f"assistants/{ASSISTANT_ID}/documents/d1/d1.pdf",
            f"assistants/{ASSISTANT_ID}/documents/d2/d2.pdf",
        }

    @pytest.mark.asyncio
    async def test_only_complete_documents_are_migrated(self):
        """Requirement 15.5. A non-complete document is not retrievable on legacy
        either, so migrating it would create a difference where the point is
        parity."""
        docs = [_doc("d1"), _doc("d2", status="failed"), _doc("d3", status="uploading")]
        backend = StubBackend()

        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", return_value=docs
        ), patch.object(
            worker, "get_document_item", side_effect=lambda a, d: next(
                (x for x in docs if worker.document_id_of(x) == d), None
            )
        ), patch(
            "apis.shared.kb_backend.byte_cap.reserve_snapshot"
        ), patch(
            "apis.shared.kb_backend.provisioning.provision_managed_kb"
        ) as provision, patch(
            "apis.shared.kb_backend.metrics.emit_count"
        ), patch.object(
            worker, "_record_progress"
        ), patch(
            "apis.shared.kb_backend.records.set_migration_state"
        ):
            provision.side_effect = _async_noop
            await worker.run_shadow(ASSISTANT_ID, ASSISTANT_ID, _kb_record(), backend)

        assert backend.ingested == ["d1"]

    @pytest.mark.asyncio
    async def test_a_document_deleted_mid_migration_is_not_resurrected(self):
        """Requirements 16.4, 16.5, and the reason the re-read is per document
        rather than per batch: a PDF batch takes minutes, and the deletion this
        guards against is most likely to land inside exactly that window.

        ``d2`` is in the snapshot but gone by the time its turn comes.
        """
        docs = [_doc("d1"), _doc("d2")]
        deleted = {"d2"}
        backend = StubBackend()

        def _get(assistant_id, document_id):
            return None if document_id in deleted else _doc(document_id)

        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", return_value=docs
        ), patch.object(worker, "get_document_item", side_effect=_get), patch(
            "apis.shared.kb_backend.byte_cap.reserve_snapshot"
        ), patch(
            "apis.shared.kb_backend.provisioning.provision_managed_kb"
        ) as provision, patch(
            "apis.shared.kb_backend.metrics.emit_count"
        ), patch.object(
            worker, "_record_progress"
        ), patch(
            "apis.shared.kb_backend.records.set_migration_state"
        ):
            provision.side_effect = _async_noop
            result = await worker.run_shadow(ASSISTANT_ID, ASSISTANT_ID, _kb_record(), backend)

        assert backend.ingested == ["d1"], "a deleted document was resurrected"
        assert result.documents_skipped >= 1

    @pytest.mark.asyncio
    async def test_a_document_that_stopped_being_complete_is_skipped(self):
        docs = [_doc("d1")]
        backend = StubBackend()

        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", return_value=docs
        ), patch.object(
            worker, "get_document_item", return_value=_doc("d1", status="deleting")
        ), patch(
            "apis.shared.kb_backend.byte_cap.reserve_snapshot"
        ), patch(
            "apis.shared.kb_backend.provisioning.provision_managed_kb"
        ) as provision, patch(
            "apis.shared.kb_backend.metrics.emit_count"
        ), patch.object(
            worker, "_record_progress"
        ), patch(
            "apis.shared.kb_backend.records.set_migration_state"
        ):
            provision.side_effect = _async_noop
            await worker.run_shadow(ASSISTANT_ID, ASSISTANT_ID, _kb_record(), backend)

        assert backend.ingested == []

    @pytest.mark.asyncio
    async def test_the_whole_snapshot_is_reserved_before_anything_is_provisioned(self):
        """Requirement 12.9. Reserving per document would let a migration run for
        an hour and stop halfway, leaving a half-populated corpus and an owner over
        their cap with no way back."""
        order: List[str] = []
        docs = [_doc("d1", size=2048), _doc("d2", size=4096)]

        def _reserve(assistant_id, app_kb_id, total, cap):
            order.append(f"reserve:{total}")

        async def _provision(*args, **kwargs):
            order.append("provision")

        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", return_value=docs
        ), patch.object(
            worker, "get_document_item", side_effect=lambda a, d: _doc(d)
        ), patch(
            "apis.shared.kb_backend.byte_cap.reserve_snapshot", side_effect=_reserve
        ), patch(
            "apis.shared.kb_backend.provisioning.provision_managed_kb", side_effect=_provision
        ), patch(
            "apis.shared.kb_backend.metrics.emit_count"
        ), patch.object(
            worker, "_record_progress"
        ), patch(
            "apis.shared.kb_backend.records.set_migration_state"
        ):
            fresh = _kb_record()
            fresh.pop("totalBytes")
            await worker.run_shadow(ASSISTANT_ID, ASSISTANT_ID, fresh, StubBackend())

        assert order == ["reserve:6144", "provision"]

    @pytest.mark.asyncio
    async def test_an_over_cap_corpus_fails_before_provisioning(self):
        from apis.shared.kb_backend.byte_cap import ByteCapExceeded

        async def _provision(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("provisioned despite the byte cap")

        with patch.dict("os.environ", BASE_ENV, clear=True), patch(
            "apis.shared.kb_backend.records.get_kb_record", return_value=_kb_record()
        ), patch.object(worker, "take_lease", return_value="later"), patch.object(
            worker, "list_document_items", return_value=[_doc("d1")]
        ), patch(
            "apis.shared.kb_backend.byte_cap.reserve_snapshot",
            side_effect=ByteCapExceeded(requested=1, cap=0),
        ), patch(
            "apis.shared.kb_backend.provisioning.provision_managed_kb", side_effect=_provision
        ), patch(
            "apis.shared.kb_backend.metrics.emit_count"
        ), patch.object(
            worker, "_fail"
        ) as fail:
            result = await worker.run_step(ASSISTANT_ID)

        assert result.to_state == r.MIGRATION_FAILED
        fail.assert_called_once()

    @pytest.mark.asyncio
    async def test_catch_up_converges_on_quiet_not_on_a_pass_count(self):
        """Requirement 16.3. A new document appears during the first pass; the
        second finds nothing and that is what ends it."""
        backend = StubBackend()
        state = {"pass": 0}

        def _list(assistant_id):
            state["pass"] += 1
            if state["pass"] == 1:
                return [_doc("d1"), _doc("d2")]
            return [_doc("d1"), _doc("d2")]

        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", side_effect=_list
        ), patch.object(worker, "get_document_item", side_effect=lambda a, d: _doc(d)):
            passes, converged, counts = await worker.catch_up(
                ASSISTANT_ID, ASSISTANT_ID, {"d1"}, backend
            )

        assert converged is True
        assert passes == 2
        assert backend.ingested == ["d2"]

    @pytest.mark.asyncio
    async def test_a_corpus_that_never_settles_does_not_converge(self):
        """And staying in ``shadow`` is the correct outcome: the corpus keeps
        serving from legacy while the owner keeps uploading."""
        backend = StubBackend()
        counter = {"n": 0}

        def _list(assistant_id):
            counter["n"] += 1
            return [_doc(f"d{i}") for i in range(counter["n"] + 1)]

        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", side_effect=_list
        ), patch.object(worker, "get_document_item", side_effect=lambda a, d: _doc(d)):
            passes, converged, _ = await worker.catch_up(
                ASSISTANT_ID, ASSISTANT_ID, set(), backend, max_passes=3
            )

        assert converged is False
        assert passes == 3

    @pytest.mark.asyncio
    async def test_an_unconverged_shadow_stays_in_shadow(self):
        docs = [_doc("d1")]
        transitions: List[str] = []

        def _set_state(assistant_id, app_kb_id, new_state, generation, due=None, expected=None, error=None):
            transitions.append(new_state)

        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", return_value=docs
        ), patch.object(
            worker, "get_document_item", side_effect=lambda a, d: _doc(d)
        ), patch(
            "apis.shared.kb_backend.byte_cap.reserve_snapshot"
        ), patch(
            "apis.shared.kb_backend.provisioning.provision_managed_kb"
        ) as provision, patch(
            "apis.shared.kb_backend.metrics.emit_count"
        ), patch.object(
            worker, "_record_progress"
        ), patch(
            "apis.shared.kb_backend.records.set_migration_state", side_effect=_set_state
        ), patch.object(
            worker, "catch_up", return_value=(5, False, {"migrated": 0, "skipped": 0, "done": []})
        ):
            provision.side_effect = _async_noop
            result = await worker.run_shadow(ASSISTANT_ID, ASSISTANT_ID, _kb_record(), StubBackend())

        assert transitions == [r.SHADOW]
        assert result.to_state == r.SHADOW
        assert result.converged is False


# ── Worker: verify ───────────────────────────────────────────────────────────
class TestVerify:
    def test_the_manifest_is_content_identity_not_a_count(self):
        """Requirement 15.6. Count parity is satisfied by a corpus with the right
        *number* of wrong documents — exactly what a migration that raced an upload
        and a delete produces."""
        before = worker.source_manifest([_doc("d1"), _doc("d2")])
        changed = dict(_doc("d2"))
        changed["contentHash"] = "hash-d2-edited"
        after = worker.source_manifest([_doc("d1"), changed])

        assert len(before) == len(after)
        assert before != after, "the manifest is count-equivalent and cannot see an edit"

    def test_a_document_with_no_hash_still_contributes_a_changing_value(self):
        item = {"SK": "DOC#d9", "status": "complete", "updatedAt": "2026-08-01T00:00:00Z"}
        assert worker.manifest_entry(item) == "d9:2026-08-01T00:00:00Z"

    @pytest.mark.asyncio
    async def test_an_unqueryable_corpus_defers_instead_of_failing(self):
        """Requirement 15.7, corrected by measurement.

        "Not queryable yet" is a verification that has not happened, not one that
        failed. The docstring's 0.75-1.03 s was measured on a warm knowledge base;
        a first ingest into a fresh one took ~45 s in dev, and treating that as
        terminal marked a good migration `failed` and showed its owner a retry
        button for a problem that resolves itself.
        """
        backend = StubBackend(chunks=[])
        deferred = []

        def _defer(_assistant, _kb, _generation, due_at):
            deferred.append(due_at)
            return len(deferred)

        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", return_value=[_doc("d1")]
        ), patch.object(r, "defer_verify", _defer):
            result = await worker.run_verify(
                ASSISTANT_ID, ASSISTANT_ID, _kb_record(r.VERIFY), backend
            )

        assert deferred, "did not defer; an early canary would fail the migration"
        assert result.to_state == r.VERIFY, "left verify on a deferral"
        assert "not queryable" in result.detail

    @pytest.mark.asyncio
    async def test_deferring_forever_eventually_fails(self):
        """Bounded, because "not queryable" past some point is not latency.

        Matched on the attempt count rather than "not queryable", so this cannot
        be satisfied by the deferral path it is meant to sit past.
        """
        backend = StubBackend(chunks=[])

        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", return_value=[_doc("d1")]
        ), patch.object(
            r, "defer_verify", lambda *a, **k: worker.MAX_VERIFY_ATTEMPTS + 1
        ):
            with pytest.raises(worker.VerificationFailed, match="attempts over"):
                await worker.run_verify(
                    ASSISTANT_ID, ASSISTANT_ID, _kb_record(r.VERIFY), backend
                )

    @pytest.mark.asyncio
    async def test_verify_rejects_a_canary_that_returns_foreign_documents(self):
        backend = StubBackend(
            chunks=[
                Chunk(
                    text="someone else's",
                    relevance=1.0,
                    document_id="not-ours",
                    metadata={"document_id": "not-ours"},
                )
            ]
        )

        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", return_value=[_doc("d1")]
        ):
            with pytest.raises(worker.VerificationFailed):
                await worker.run_verify(ASSISTANT_ID, ASSISTANT_ID, _kb_record(r.VERIFY), backend)

    @pytest.mark.asyncio
    async def test_an_empty_corpus_cannot_be_verified(self):
        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", return_value=[_doc("d1", status="failed")]
        ):
            with pytest.raises(worker.VerificationFailed, match="nothing"):
                await worker.run_verify(
                    ASSISTANT_ID, ASSISTANT_ID, _kb_record(r.VERIFY), StubBackend()
                )

    @pytest.mark.asyncio
    async def test_a_successful_verify_moves_to_promote(self):
        backend = StubBackend(
            chunks=[
                Chunk(text="hit", relevance=1.0, document_id="d1", metadata={"document_id": "d1"})
            ]
        )
        transitions: List[tuple] = []

        def _set_state(assistant_id, app_kb_id, new_state, generation, due=None, expected=None, error=None):
            transitions.append((new_state, expected))

        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", return_value=[_doc("d1")]
        ), patch("apis.shared.kb_backend.records.set_migration_state", side_effect=_set_state):
            result = await worker.run_verify(
                ASSISTANT_ID, ASSISTANT_ID, _kb_record(r.VERIFY), backend
            )

        assert result.to_state == r.PROMOTE
        assert transitions == [(r.PROMOTE, [r.VERIFY])]

    def test_the_canary_query_is_built_from_the_corpus(self):
        """Not a fixed string: a constant like "test" can legitimately match
        nothing in a real corpus, which would fail healthy knowledge bases and
        train whoever is watching to ignore it."""
        query = worker._canary_query([{"filename": "student_handbook.pdf"}])
        assert "student" in query and "handbook" in query
        assert ".pdf" not in query


# ── Worker: promote and rollback ─────────────────────────────────────────────
class TestPromote:
    @pytest.mark.asyncio
    async def test_promotion_is_refused_without_a_byte_cap_accumulator(self):
        """Requirement 12.9: no traffic is promoted to an unmetered corpus."""
        record = _kb_record(r.PROMOTE)
        record.pop("totalBytes")

        with patch.dict("os.environ", BASE_ENV, clear=True), patch(
            "apis.shared.kb_backend.records.promote_engine"
        ) as promote:
            with pytest.raises(worker.MigrationError, match="totalBytes"):
                await worker.run_promote(ASSISTANT_ID, ASSISTANT_ID, record)

        promote.assert_not_called()

    @pytest.mark.asyncio
    async def test_promotion_writes_once_and_then_retains(self):
        calls: List[str] = []

        with patch.dict("os.environ", BASE_ENV, clear=True), patch(
            "apis.shared.kb_backend.records.promote_engine",
            side_effect=lambda *a: calls.append("promote"),
        ), patch(
            "apis.shared.kb_backend.metrics.emit_count"
        ), patch.object(
            worker, "_set_retain_until", side_effect=lambda *a: calls.append("retain_until")
        ), patch(
            "apis.shared.kb_backend.records.set_migration_state",
            side_effect=lambda *a, **k: calls.append("state"),
        ):
            result = await worker.run_promote(ASSISTANT_ID, ASSISTANT_ID, _kb_record(r.PROMOTE))

        assert calls == ["promote", "retain_until", "state"]
        assert result.to_state == r.RETAIN

    @pytest.mark.asyncio
    async def test_concurrent_promotion_yields_one_winner(self):
        """Requirement 15.10. The property belongs to the conditional write, so the
        test is that the loser's exception is not swallowed into a second success."""
        from botocore.exceptions import ClientError

        winners = {"n": 0}

        def _promote(assistant_id, app_kb_id, generation, now_iso):
            winners["n"] += 1
            if winners["n"] > 1:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )

        # The loser re-reads before deciding, because a refused write means either
        # "somebody else promoted" (success) or "a guard genuinely failed" (not).
        # Here the record is still unpromoted, so the refusal must propagate.
        with patch.dict("os.environ", BASE_ENV, clear=True), patch(
            "apis.shared.kb_backend.records.promote_engine", side_effect=_promote
        ), patch(
            "apis.shared.kb_backend.records.get_kb_record", return_value=_kb_record(r.PROMOTE)
        ), patch("apis.shared.kb_backend.metrics.emit_count"), patch.object(
            worker, "_set_retain_until"
        ), patch("apis.shared.kb_backend.records.set_migration_state"):
            first = await worker.run_promote(ASSISTANT_ID, ASSISTANT_ID, _kb_record(r.PROMOTE))
            with pytest.raises(ClientError):
                await worker.run_promote(ASSISTANT_ID, ASSISTANT_ID, _kb_record(r.PROMOTE))

        assert first.to_state == r.RETAIN
        assert winners["n"] == 2

    def test_the_retain_window_cannot_be_shortened_below_thirty_days(self):
        """Requirement 15.11 says *at least* 30 days. Shortening the rollback
        window is not a tuning knob."""
        with patch.dict("os.environ", {"KB_MIGRATION_RETAIN_DAYS": "3"}, clear=True):
            assert worker._retain_days() == 30
        with patch.dict("os.environ", {"KB_MIGRATION_RETAIN_DAYS": "90"}, clear=True):
            assert worker._retain_days() == 90


class TestRollback:
    @pytest.mark.asyncio
    async def test_rollback_moves_no_data(self):
        """Requirement 17.2. The legacy index was never mutated — that is what
        building the managed corpus alongside it bought."""
        touched: List[str] = []

        with patch.dict("os.environ", BASE_ENV, clear=True), patch(
            "apis.shared.kb_backend.records.rollback_engine",
            side_effect=lambda *a: touched.append("engine"),
        ), patch("apis.shared.kb_backend.metrics.emit_count"):
            result = await worker.rollback(ASSISTANT_ID, ASSISTANT_ID)

        assert touched == ["engine"]
        assert "no data moved" in result.detail

    @pytest.mark.asyncio
    async def test_rollback_does_not_delete_the_managed_knowledge_base(self):
        """Deleting it here would turn a reversible decision into an irreversible
        one at the moment somebody is least sure."""
        with patch.dict("os.environ", BASE_ENV, clear=True), patch(
            "apis.shared.kb_backend.records.rollback_engine"
        ), patch("apis.shared.kb_backend.metrics.emit_count"), patch(
            "apis.shared.kb_backend.tombstones.delete_knowledge_base", create=True
        ) as delete_kb:
            await worker.rollback(ASSISTANT_ID, ASSISTANT_ID)

        delete_kb.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_pre_promotion_failure_leaves_the_record_on_legacy(self):
        """Requirement 17.4. `failed` is terminal and removes the work keys; the
        engine attribute was never written, so the knowledge base is still legacy
        and still usable."""
        recorded: List[tuple] = []

        def _set_state(assistant_id, app_kb_id, new_state, generation, due=None, expected=None, error=None):
            recorded.append((new_state, error))

        with patch.dict("os.environ", BASE_ENV, clear=True), patch(
            "apis.shared.kb_backend.records.get_kb_record", return_value=_kb_record(r.VERIFY)
        ), patch.object(worker, "take_lease", return_value="later"), patch.object(
            worker, "run_verify", side_effect=worker.VerificationFailed("canary empty")
        ), patch(
            "apis.shared.kb_backend.metrics.emit_count"
        ), patch(
            "apis.shared.kb_backend.records.set_migration_state", side_effect=_set_state
        ), patch(
            "apis.shared.kb_backend.records.promote_engine"
        ) as promote:
            result = await worker.run_step(ASSISTANT_ID)

        assert result.to_state == r.MIGRATION_FAILED
        assert recorded and recorded[0][0] == r.MIGRATION_FAILED
        promote.assert_not_called()


class TestResumingWithoutRedoingWork:
    """The two behaviours the convergence property test forced into existence."""

    def test_the_completed_set_is_read_off_the_record(self):
        assert worker.already_migrated({}) == set()
        assert worker.already_migrated({"migratedDocIds": {"d1", "d2"}}) == {"d1", "d2"}

    def test_a_non_iterable_completed_set_degrades_to_empty(self):
        """Re-ingesting is slow, not wrong — ``customDocumentIdentifier`` makes it a
        replace — so a malformed attribute must not stop the migration."""
        assert worker.already_migrated({"migratedDocIds": 7}) == set()

    @pytest.mark.asyncio
    async def test_a_resumed_shadow_skips_documents_it_already_ingested(self):
        """Before this, a crash near the end of a PDF corpus re-parsed all of it —
        37-264 s per document, so an hour of work redone for nothing."""
        docs = [_doc("d1"), _doc("d2"), _doc("d3")]
        backend = StubBackend()
        record = _kb_record(r.SHADOW, migratedDocIds={"d1", "d2"})

        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", return_value=docs
        ), patch.object(
            worker, "get_document_item", side_effect=lambda a, d: _doc(d)
        ), patch(
            "apis.shared.kb_backend.byte_cap.reserve_snapshot"
        ) as reserve, patch(
            "apis.shared.kb_backend.provisioning.provision_managed_kb", side_effect=_async_noop
        ), patch(
            "apis.shared.kb_backend.metrics.emit_count"
        ), patch.object(
            worker, "_record_progress"
        ), patch(
            "apis.shared.kb_backend.records.set_migration_state"
        ):
            await worker.run_shadow(ASSISTANT_ID, ASSISTANT_ID, record, backend)

        assert backend.ingested == ["d3"]
        # And the corpus is not reserved a second time: the accumulator is on the
        # record, so re-reserving would double-count the owner's own corpus against
        # their cap until the migration refused itself.
        reserve.assert_not_called()

    @pytest.mark.asyncio
    async def test_progress_persists_the_ids_not_just_a_count(self):
        docs = [_doc("d1"), _doc("d2")]
        captured = {}

        async def _progress(assistant_id, app_kb_id, *, migrated, total, skipped, newly_done=None):
            captured["newly_done"] = list(newly_done or [])
            captured["migrated"] = migrated

        with patch.dict("os.environ", BASE_ENV, clear=True), patch.object(
            worker, "list_document_items", return_value=docs
        ), patch.object(
            worker, "get_document_item", side_effect=lambda a, d: _doc(d)
        ), patch(
            "apis.shared.kb_backend.byte_cap.reserve_snapshot"
        ), patch(
            "apis.shared.kb_backend.provisioning.provision_managed_kb", side_effect=_async_noop
        ), patch(
            "apis.shared.kb_backend.metrics.emit_count"
        ), patch.object(
            worker, "_record_progress", side_effect=_progress
        ), patch(
            "apis.shared.kb_backend.records.set_migration_state"
        ):
            await worker.run_shadow(ASSISTANT_ID, ASSISTANT_ID, _kb_record(), StubBackend())

        assert sorted(captured["newly_done"]) == ["d1", "d2"], (
            "a count alone cannot tell a resume *which* documents to skip"
        )

    @pytest.mark.asyncio
    async def test_a_record_already_promoted_finishes_instead_of_failing(self):
        """The crash window between the promotion write and the state transition.

        The promotion write is guarded on ``attribute_not_exists(retrievalEngine)``,
        so retrying it is refused — and treating that refusal as a failure would
        mark a migration that actually succeeded as ``failed``, leaving a promoted
        knowledge base with no retention window.
        """
        record = _kb_record(r.PROMOTE, retrievalEngine="managed")
        calls: List[str] = []

        with patch.dict("os.environ", BASE_ENV, clear=True), patch(
            "apis.shared.kb_backend.records.promote_engine",
            side_effect=lambda *a: calls.append("promote"),
        ), patch("apis.shared.kb_backend.metrics.emit_count"), patch.object(
            worker, "_set_retain_until", side_effect=lambda *a: calls.append("retain_until")
        ), patch(
            "apis.shared.kb_backend.records.set_migration_state",
            side_effect=lambda *a, **k: calls.append("state"),
        ):
            result = await worker.run_promote(ASSISTANT_ID, ASSISTANT_ID, record)

        assert "promote" not in calls, "promoted a second time"
        assert calls == ["retain_until", "state"]
        assert result.to_state == r.RETAIN
        assert "already promoted" in result.detail

    @pytest.mark.asyncio
    async def test_a_refused_promotion_on_an_unpromoted_record_still_raises(self):
        """So "already promoted" cannot become a blanket swallow of the guard."""
        with patch.dict("os.environ", BASE_ENV, clear=True), patch(
            "apis.shared.kb_backend.records.promote_engine",
            side_effect=RuntimeError("conditional check failed"),
        ), patch(
            "apis.shared.kb_backend.records.get_kb_record", return_value=_kb_record(r.PROMOTE)
        ), patch("apis.shared.kb_backend.metrics.emit_count"):
            with pytest.raises(RuntimeError):
                await worker.run_promote(ASSISTANT_ID, ASSISTANT_ID, _kb_record(r.PROMOTE))


class TestRehydrationReappliesTheResourcePolicy:
    """Task 13.7 / Requirement 24.12, asserted at the level a rehydration works at.

    A resource policy attaches to the AWS knowledge base ARN. Provisioning that
    produces a *new* ``awsKbId`` — a rehydration, or a replacement after a failed
    delete — therefore leaves the old policy on a resource nobody reads, and sharing
    silently stops. The repair is a state comparison rather than an event, so it
    cannot be bypassed by a code path that forgets to fire anything.
    """

    @pytest.mark.asyncio
    async def test_a_new_aws_kb_id_makes_the_recorded_policy_stale(self):
        from apis.shared.kb_backend.resource_policy import POLICY_KB_ID_ATTR, policy_is_stale

        rehydrated = _kb_record(r.RETAIN, awsKbId="KB-NEW", **{POLICY_KB_ID_ATTR: "KB123"})
        assert policy_is_stale(rehydrated) is True

    @pytest.mark.asyncio
    async def test_the_policy_is_reapplied_to_the_new_arn(self):
        from apis.shared.kb_backend.resource_policy import (
            POLICY_KB_ID_ATTR,
            ensure_retrieve_policy,
        )

        client = MagicMock()
        client.put_resource_policy.return_value = {"revisionId": "rev-after-rehydration"}
        rehydrated = _kb_record(r.RETAIN, awsKbId="KB-NEW", **{POLICY_KB_ID_ATTR: "KB123"})

        with patch.dict(
            "os.environ",
            {
                **BASE_ENV,
                "AWS_ACCOUNT_ID": "123456789012",
                "MANAGED_KB_RETRIEVAL_PRINCIPAL_ARNS": "arn:aws:iam::123456789012:role/runtime",
            },
            clear=True,
        ), patch("apis.shared.kb_backend.records.set_resource_policy_state") as setter:
            revision = await ensure_retrieve_policy(
                ASSISTANT_ID, ASSISTANT_ID, shared=True, record=rehydrated, client=client
            )

        assert revision == "rev-after-rehydration"
        assert client.put_resource_policy.call_args.kwargs["resourceArn"].endswith(
            "knowledge-base/KB-NEW"
        )
        setter.assert_called_once_with(
            ASSISTANT_ID, ASSISTANT_ID, "KB-NEW", "rev-after-rehydration"
        )


# ── Mixed old/new deployment ─────────────────────────────────────────────────
class TestMixedDeployment:
    def test_a_record_without_an_engine_resolves_to_legacy(self):
        """Requirements 1.6, 24.8. Old and new code serving simultaneously agree,
        because "absence means legacy" is a property of the data rather than of the
        code version reading it."""
        for item in ({}, None, _kb_record(), {"appKbId": "x", "migrationState": r.SHADOW}):
            assert r.resolve_engine(item) == r.ENGINE_LEGACY

    def test_only_an_explicit_managed_value_resolves_to_managed(self):
        assert r.resolve_engine({"retrievalEngine": "managed"}) == r.ENGINE_MANAGED
        for wrong in ("MANAGED", "Managed", "s3vectors", "", None, True):
            assert r.resolve_engine({"retrievalEngine": wrong}) == r.ENGINE_LEGACY

    def test_a_mid_migration_record_still_serves_legacy(self):
        """Requirements 15.3, 16.1. `shadow` and `verify` never touch
        `retrievalEngine`, so a knowledge base being migrated is indistinguishable
        from one that is not, to anything doing retrieval."""
        for state in (r.SHADOW, r.VERIFY, r.PROMOTE):
            assert r.resolve_engine(_kb_record(state)) == r.ENGINE_LEGACY
