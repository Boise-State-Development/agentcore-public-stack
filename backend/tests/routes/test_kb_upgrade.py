"""The owner-facing upgrade surface: enrolment, status, and who may see it.

Requirements 21 and 23. The assertions here concentrate on the failures that
would ship looking correct:

* **The offer is gated on the migration flag.** Offering an upgrade the worker
  cannot perform parks a record in ``shadow`` behind a spinner that never moves.
  "Off" includes present-but-empty, which is the shape of the reconciler-arming
  defect and is therefore re-tested per component.
* **Enrolment writes the work keys.** A record created with
  ``migrationState="shadow"`` but no ``GSI7_PK``/``GSI7_SK`` is invisible to the
  dispatcher's sparse-index sweep *forever*, while every surface reports an
  upgrade in progress. ``KbRecord.to_item`` does not write those keys, so this is
  a live trap rather than a hypothetical one.
* **Enrolment never writes ``retrievalEngine``.** Promotion belongs to the worker
  and only after verification. An HTTP request that could set it would cut a
  knowledge base over to an empty corpus.
* **A viewer cannot enrol, and is not offered it.** Requirement 23.7.
* **A retry bumps the generation**, which is what fences a straggler worker from
  the abandoned attempt.
* **``deleting`` documents are surfaced, not hidden.** They are 101 of the 200
  affected production records, and the ordinary document list filters them out.
* **No user-facing string says "vector"** (Requirement 23.6), asserted over every
  string the module can emit rather than spot-checked.

Feature: managed-kb-migration
Requirements: 21.1, 21.2, 21.3, 21.4, 23.1, 23.2, 23.3, 23.4, 23.5, 23.6, 23.7,
23.8
"""

from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from apis.app_api.kb_upgrade import models as m
from apis.app_api.kb_upgrade import service as s
from apis.shared.kb_backend import records as r

ASSISTANT_ID = "ast-upgrade-001"
OWNER_ID = "user-owner"
TABLE = "test-assistants"

ENV_ON = {
    "DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE,
    "MANAGED_KB_MIGRATION_ENABLED": "true",
    "AWS_REGION": "us-west-2",
}


def _doc(
    document_id: str,
    status: str = "complete",
    filename: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "PK": f"AST#{ASSISTANT_ID}",
        "SK": f"DOC#{document_id}",
        "documentId": document_id,
        "status": status,
        "filename": filename or f"{document_id}.pdf",
    }


def _kb_record(**overrides) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "PK": f"AST#{ASSISTANT_ID}",
        "SK": f"KB#{ASSISTANT_ID}",
        "appKbId": ASSISTANT_ID,
        "ownerUserId": OWNER_ID,
        "migrationGeneration": Decimal(0),
    }
    record.update(overrides)
    return record


class _FakeTable:
    """Enough DynamoDB for these paths, recording every write for assertion."""

    def __init__(self, item: Optional[Dict[str, Any]] = None, docs=None):
        self.item = item
        self.docs = list(docs or [])
        self.updates: List[Dict[str, Any]] = []
        self.puts: List[Dict[str, Any]] = []

    def get_item(self, Key, **kwargs):
        return {"Item": self.item} if self.item else {}

    def query(self, **kwargs):
        return {"Items": self.docs}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        return {}

    def put_item(self, **kwargs):
        self.puts.append(kwargs)
        return {}


@pytest.fixture
def table(monkeypatch):
    """Point both ``records`` and the service's document query at one fake."""
    fake = _FakeTable()

    def _resource(*args, **kwargs):
        resource = MagicMock()
        resource.Table.return_value = fake
        return resource

    monkeypatch.setattr("boto3.resource", _resource)
    for key, value in ENV_ON.items():
        monkeypatch.setenv(key, value)
    return fake


# ── The flag gate (Requirement 23.1) ─────────────────────────────────────────
class TestFlagGate:
    @pytest.mark.parametrize(
        "raw", ["", "  ", "false", "no", "off", "0", "disabled", "True-ish", None]
    )
    def test_absent_empty_or_negative_reads_as_off(self, monkeypatch, raw):
        """Present-but-empty must read as off, not as truthy-by-accident."""
        if raw is None:
            monkeypatch.delenv(s.FLAG_MIGRATION_ENABLED, raising=False)
        else:
            monkeypatch.setenv(s.FLAG_MIGRATION_ENABLED, raw)
        assert s.migration_enabled() is False

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on", "enabled"])
    def test_affirmative_spellings_read_as_on(self, monkeypatch, raw):
        monkeypatch.setenv(s.FLAG_MIGRATION_ENABLED, raw)
        assert s.migration_enabled() is True

    def test_the_flag_is_read_at_call_time(self, monkeypatch):
        """Bound as a default argument the flag would be unpatchable.

        Asserted by flipping it *between* two calls on the same import.
        """
        monkeypatch.setenv(s.FLAG_MIGRATION_ENABLED, "true")
        assert s.migration_enabled() is True
        monkeypatch.setenv(s.FLAG_MIGRATION_ENABLED, "false")
        assert s.migration_enabled() is False

    @pytest.mark.asyncio
    async def test_no_offer_while_the_flag_is_off(self, table, monkeypatch):
        monkeypatch.setenv(s.FLAG_MIGRATION_ENABLED, "false")
        table.docs = [_doc("d1")]
        status = await s.get_upgrade_status(ASSISTANT_ID, can_edit=True)
        assert status.phase == "none"
        assert status.can_upgrade is False

    @pytest.mark.asyncio
    async def test_enrolment_refuses_while_the_flag_is_off(self, table, monkeypatch):
        monkeypatch.setenv(s.FLAG_MIGRATION_ENABLED, "false")
        with pytest.raises(s.UpgradeUnavailable):
            await s.enroll(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert table.updates == [], "a refused enrolment must not write"
        assert table.puts == [], "a refused enrolment must not create a record"


# ── Status derivation (Requirement 23.1–23.5) ────────────────────────────────
class TestStatus:
    @pytest.mark.asyncio
    async def test_empty_knowledge_base_shows_nothing(self, table):
        """Requirement 23.1: no action required means no badge, banner or prompt."""
        table.docs = []
        status = await s.get_upgrade_status(ASSISTANT_ID, can_edit=True)
        assert status.phase == "none"
        assert status.documents_not_carried == []

    @pytest.mark.asyncio
    async def test_legacy_with_documents_is_available(self, table):
        table.docs = [_doc("d1"), _doc("d2")]
        status = await s.get_upgrade_status(ASSISTANT_ID, can_edit=True)
        assert status.phase == "available"
        assert status.can_upgrade is True
        assert status.progress is not None
        assert status.progress.total == 2

    @pytest.mark.asyncio
    async def test_a_viewer_is_never_offered_the_control(self, table):
        """Requirement 23.7 — and the server, not the client, decides."""
        table.docs = [_doc("d1")]
        status = await s.get_upgrade_status(ASSISTANT_ID, can_edit=False)
        assert status.can_upgrade is False
        assert status.phase == "none"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", [r.SHADOW, r.VERIFY, r.PROMOTE])
    async def test_working_states_collapse_to_in_progress(self, table, state):
        """The client is not told which internal step is running."""
        table.item = _kb_record(
            migrationState=state,
            migrationProgress={
                "migrated": Decimal(12),
                "total": Decimal(40),
                "skipped": Decimal(0),
            },
        )
        status = await s.get_upgrade_status(ASSISTANT_ID, can_edit=True)
        assert status.phase == "in_progress"
        assert status.can_upgrade is False, "no second upgrade while one runs"
        assert status.progress.completed == 12
        assert status.progress.total == 40

    @pytest.mark.asyncio
    async def test_failed_stays_retryable_and_explains_itself(self, table):
        """Requirement 23.5: plain-language reason, retry offered, never a dead end."""
        table.item = _kb_record(
            migrationState=r.MIGRATION_FAILED,
            migrationError="ByteCapExceeded: 900000000 over cap",
        )
        status = await s.get_upgrade_status(ASSISTANT_ID, can_edit=True)
        assert status.phase == "failed"
        assert status.can_upgrade is True
        assert status.reason and "size limit" in status.reason
        assert "ByteCapExceeded" not in status.reason, (
            "the operator's error string must not reach the user verbatim"
        )

    @pytest.mark.asyncio
    async def test_an_unrecognised_failure_does_not_leak_the_operator_string(
        self, table
    ):
        """The fallback must be copy, not a pass-through.

        Found by mutation: mapping a *recognised* error proved nothing about the
        unrecognised path, and ``return stored or _FAILURE_FALLBACK`` survived —
        a stack-trace fragment rendered into the card.
        """
        leak = "ClientError: An error occurred (AccessDeniedException) calling ..."
        table.item = _kb_record(migrationState=r.MIGRATION_FAILED, migrationError=leak)
        status = await s.get_upgrade_status(ASSISTANT_ID, can_edit=True)
        assert status.phase == "failed"
        assert status.reason == s._FAILURE_FALLBACK
        assert "AccessDeniedException" not in status.reason
        assert "ClientError" not in status.reason

    @pytest.mark.asyncio
    async def test_a_viewer_cannot_retry_a_failure(self, table):
        table.item = _kb_record(migrationState=r.MIGRATION_FAILED)
        status = await s.get_upgrade_status(ASSISTANT_ID, can_edit=False)
        assert status.phase == "failed"
        assert status.can_upgrade is False

    @pytest.mark.asyncio
    async def test_promoted_owes_a_one_time_notice(self, table):
        """Requirement 23.4: dismissible notice, never a permanent badge."""
        table.item = _kb_record(
            retrievalEngine=r.ENGINE_MANAGED, migrationState=r.RETAIN
        )
        status = await s.get_upgrade_status(ASSISTANT_ID, can_edit=True)
        assert status.phase == "succeeded"
        assert status.notice_pending is True

    @pytest.mark.asyncio
    async def test_a_dismissed_notice_never_returns(self, table):
        table.item = _kb_record(
            retrievalEngine=r.ENGINE_MANAGED,
            migrationState=r.RETAIN,
            upgradeNoticeDismissedAt="2026-08-01T00:00:00Z",
        )
        status = await s.get_upgrade_status(ASSISTANT_ID, can_edit=True)
        assert status.phase == "succeeded"
        assert status.notice_pending is False

    @pytest.mark.asyncio
    async def test_an_unreadable_document_list_fails_loudly_here(
        self, table, monkeypatch
    ):
        """The service raises; the *route* is what degrades.

        Split deliberately: a misconfigured table name must not make an
        upgradeable knowledge base look clean to anything that calls the service
        directly. Only the HTTP layer is allowed to soften it, and only to
        "nothing to show".
        """
        monkeypatch.setenv(s.FLAG_MIGRATION_ENABLED, "true")
        monkeypatch.delenv("DYNAMODB_ASSISTANTS_TABLE_NAME", raising=False)
        # KeyError from the record read, which happens first; RuntimeError from
        # the document query if it ever gets there. Either is loud, and neither
        # is "phase: none".
        with pytest.raises((RuntimeError, KeyError)):
            await s.get_upgrade_status(ASSISTANT_ID, can_edit=True)


# ── Stranded documents (Requirement 21) ──────────────────────────────────────
class TestStrandedDocuments:
    def test_complete_documents_are_not_flagged(self):
        assert s.classify_document(_doc("d1", "complete")) is None

    def test_an_unsupported_format_is_distinguished_from_a_failure(self):
        """Requirement 21.4 — the two demand different actions from the user."""
        unsupported = s.classify_document(
            _doc("d1", "failed", filename="keynote-deck.pages")
        )
        broken = s.classify_document(_doc("d2", "failed", filename="report.pdf"))
        assert unsupported.kind == "unsupported_format"
        assert unsupported.retryable is False
        assert broken.kind == "processing_failure"
        assert broken.retryable is True
        assert unsupported.message != broken.message

    def test_the_unsupported_set_comes_from_the_ingestion_pipeline(self):
        """A copied extension list is the tag-contract defect's exact shape."""
        from apis.app_api.documents.ingestion.processors.docling_processor import (
            DOCLING_SUPPORTED_EXTENSIONS,
        )

        assert s._supported_extensions() == frozenset(DOCLING_SUPPORTED_EXTENSIONS)
        # A format the pipeline genuinely supports must never be called
        # unsupported, however it failed.
        assert ".pdf" in DOCLING_SUPPORTED_EXTENSIONS
        issue = s.classify_document(_doc("d1", "failed", filename="x.pdf"))
        assert issue.kind == "processing_failure"

    def test_stuck_deleting_documents_are_surfaced(self):
        """101 of the 200 affected production records are in this status.

        The ordinary document list filters ``deleting`` out as soft-deleted,
        which is right there and wrong here: a user never shown them cannot tell
        that they are stuck.
        """
        issue = s.classify_document(_doc("d1", "deleting"))
        assert issue is not None
        assert issue.kind == "being_removed"

    @pytest.mark.parametrize("status", ["uploading", "chunking", "embedding"])
    def test_in_flight_documents_are_surfaced_as_still_processing(self, status):
        issue = s.classify_document(_doc("d1", status))
        assert issue.kind == "still_processing"
        assert issue.retryable is True

    @pytest.mark.asyncio
    async def test_stranded_documents_ride_along_with_the_offer(self, table):
        """Requirement 21.1/21.3: surfaced *before* the user commits, not after."""
        table.docs = [
            _doc("d1", "complete"),
            _doc("d2", "failed"),
            _doc("d3", "deleting"),
        ]
        status = await s.get_upgrade_status(ASSISTANT_ID, can_edit=True)
        assert status.phase == "available"
        assert {d.document_id for d in status.documents_not_carried} == {"d2", "d3"}
        assert status.progress.total == 1, "only the complete document is carried"
        assert status.progress.skipped == 2

    @pytest.mark.asyncio
    async def test_an_all_stranded_corpus_is_not_offered_but_is_reported(self, table):
        table.docs = [_doc("d1", "failed"), _doc("d2", "failed")]
        status = await s.get_upgrade_status(ASSISTANT_ID, can_edit=True)
        assert status.phase == "none", "an upgrade carrying nothing is not an upgrade"
        assert len(status.documents_not_carried) == 2, (
            "but the owner still needs to see them (Requirement 21.3)"
        )

    def test_a_document_id_survives_a_missing_attribute(self):
        """Falls back to the sort key rather than emitting a blank retry target."""
        item = _doc("d9", "failed")
        del item["documentId"]
        assert s.classify_document(item).document_id == "d9"


# ── Copy (Requirement 23.6) ──────────────────────────────────────────────────
class TestCopy:
    def test_no_user_facing_string_says_vector(self):
        """Swept over every string the module can emit, not spot-checked."""
        emitted = list(s._FAILURE_COPY.values()) + [s._FAILURE_FALLBACK]
        for status in ["failed", "deleting", "uploading", "chunking"]:
            for filename in ["a.pdf", "b.pages"]:
                issue = s.classify_document(_doc("d1", status, filename=filename))
                if issue:
                    emitted.append(issue.message)
        for text in emitted:
            assert "vector" not in text.lower(), f"user-facing copy says vector: {text}"

    @pytest.mark.asyncio
    async def test_enrolment_copy_promises_continued_service(self, table):
        """Requirement 23.2's honest claim, and the one users act on."""
        result = await s.enroll(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert "keeps working" in result.message
        assert "vector" not in result.message.lower()


# ── Enrolment (Requirements 23.2, 23.8) ──────────────────────────────────────
def _update_expressions(fake: _FakeTable) -> str:
    return " | ".join(u.get("UpdateExpression", "") for u in fake.updates)


class TestEnrolment:
    @pytest.mark.asyncio
    async def test_enrolment_creates_the_record_then_enters_shadow(self, table):
        result = await s.enroll(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert result.started is True
        assert result.phase == "in_progress"
        assert len(table.puts) == 1, "the record is created exactly once"
        assert len(table.updates) == 1, "then transitioned exactly once"

    @pytest.mark.asyncio
    async def test_enrolment_writes_the_dispatcher_work_keys(self, table):
        """Without these the record is invisible to the sweep, forever.

        ``KbRecord.to_item`` does not write them, so a one-put enrolment would
        look correct and strand the knowledge base behind a permanent spinner.
        """
        await s.enroll(ASSISTANT_ID, owner_user_id=OWNER_ID)
        expression = _update_expressions(table)
        assert "GSI7_PK" in expression
        assert "GSI7_SK" in expression
        values = table.updates[0]["ExpressionAttributeValues"]
        assert values[":wpk"] == r.work_pk(r.SHADOW)
        assert values[":wsk"], "a work-eligible state requires a due time"

    @pytest.mark.asyncio
    async def test_the_created_record_does_not_claim_to_be_migrating(self, table):
        """The put must not carry ``migrationState``.

        If it did, a crash between the two writes would leave a record that says
        it is migrating with no work keys to make it so.
        """
        await s.enroll(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert "migrationState" not in table.puts[0]["Item"]

    @pytest.mark.asyncio
    async def test_enrolment_never_writes_the_retrieval_engine(self, table):
        """Promotion is the worker's, after verification. Requirement 23.8.

        An HTTP path that could set this would cut a knowledge base over to a
        corpus nothing had carried across yet.
        """
        await s.enroll(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert "retrievalEngine" not in table.puts[0]["Item"]
        assert "retrievalEngine" not in _update_expressions(table)

    @pytest.mark.asyncio
    async def test_enrolling_twice_starts_one_migration(self, table):
        """A double-click is not an error, and not two provisioning sagas."""
        await s.enroll(ASSISTANT_ID, owner_user_id=OWNER_ID)
        table.item = _kb_record(migrationState=r.SHADOW)
        again = await s.enroll(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert again.started is False
        assert again.phase == "in_progress"
        assert len(table.puts) == 1, "no second record"

    @pytest.mark.asyncio
    async def test_enrolling_an_already_upgraded_kb_is_a_no_op(self, table):
        table.item = _kb_record(retrievalEngine=r.ENGINE_MANAGED)
        result = await s.enroll(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert result.started is False
        assert result.phase == "succeeded"
        assert table.updates == []

    @pytest.mark.asyncio
    async def test_a_lost_transition_reports_the_running_upgrade(self, table):
        """Losing the race is normal and must not surface as an error."""
        with patch.object(
            r, "set_migration_state", side_effect=r.TransitionLost("raced")
        ):
            result = await s.enroll(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert result.started is False
        assert result.phase == "in_progress"

    @pytest.mark.asyncio
    async def test_a_concurrently_created_record_does_not_fail_enrolment(self, table):
        """``create_provisioning`` losing means someone else made it, not an error."""
        with patch.object(
            r, "create_provisioning", side_effect=r.TransitionLost("raced")
        ):
            result = await s.enroll(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert result.phase == "in_progress"


# ── Retry (Requirement 23.5) ─────────────────────────────────────────────────
class TestRetry:
    @pytest.mark.asyncio
    async def test_retry_bumps_the_generation_and_re_enters_shadow(self, table):
        """The bump is what fences a straggler from the abandoned attempt."""
        table.item = _kb_record(
            migrationState=r.MIGRATION_FAILED, migrationGeneration=Decimal(3)
        )
        result = await s.retry(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert result.started is True
        values = table.updates[0]["ExpressionAttributeValues"]
        assert values[":gen"] == Decimal(3), "guarded on the generation it read"
        assert values[":next"] == Decimal(4)
        assert values[":shadow"] == r.SHADOW

    @pytest.mark.asyncio
    async def test_retry_is_one_atomic_write(self, table):
        """Two writes leave a crash window with a new generation and no work keys."""
        table.item = _kb_record(migrationState=r.MIGRATION_FAILED)
        await s.retry(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert len(table.updates) == 1
        expression = table.updates[0]["UpdateExpression"]
        assert "migrationGeneration" in expression
        assert "migrationState" in expression
        assert "GSI7_PK" in expression

    @pytest.mark.asyncio
    async def test_retry_is_guarded_on_still_being_failed(self, table):
        table.item = _kb_record(migrationState=r.MIGRATION_FAILED)
        await s.retry(ASSISTANT_ID, owner_user_id=OWNER_ID)
        condition = table.updates[0]["ConditionExpression"]
        assert "migrationGeneration = :gen" in condition
        assert "migrationState = :failed" in condition

    @pytest.mark.asyncio
    async def test_retry_clears_the_previous_error(self, table):
        """A stale reason would be read as the next failure's."""
        table.item = _kb_record(
            migrationState=r.MIGRATION_FAILED, migrationError="ByteCapExceeded"
        )
        await s.retry(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert "REMOVE migrationError" in table.updates[0]["UpdateExpression"]

    @pytest.mark.asyncio
    async def test_concurrent_retries_yield_one_attempt(self, table):
        table.item = _kb_record(migrationState=r.MIGRATION_FAILED)
        with patch.object(
            r, "retry_from_failed", side_effect=r.TransitionLost("raced")
        ):
            result = await s.retry(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert result.started is False
        assert result.phase == "in_progress"

    @pytest.mark.asyncio
    async def test_retry_on_a_running_upgrade_does_not_restart_it(self, table):
        table.item = _kb_record(migrationState=r.VERIFY)
        result = await s.retry(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert result.started is False
        assert table.updates == []

    @pytest.mark.asyncio
    async def test_retry_with_no_record_enrols_instead(self, table):
        table.item = None
        result = await s.retry(ASSISTANT_ID, owner_user_id=OWNER_ID)
        assert result.started is True
        assert len(table.puts) == 1


# ── Notice dismissal (Requirement 23.4) ──────────────────────────────────────
class TestNotice:
    @pytest.mark.asyncio
    async def test_dismissal_is_guarded_on_the_record_existing(self, table):
        table.item = _kb_record(retrievalEngine=r.ENGINE_MANAGED)
        await s.dismiss_notice(ASSISTANT_ID)
        assert "upgradeNoticeDismissedAt" in table.updates[0]["UpdateExpression"]
        assert "attribute_exists(PK)" in table.updates[0]["ConditionExpression"]

    @pytest.mark.asyncio
    async def test_dismissing_a_missing_record_is_not_an_error(self, table):
        with patch.object(
            r, "dismiss_upgrade_notice", side_effect=r.TransitionLost("absent")
        ):
            await s.dismiss_notice(ASSISTANT_ID)  # must not raise


# ── Routes (Requirement 23.7) ────────────────────────────────────────────────
@pytest.fixture
def app():
    from apis.app_api.kb_upgrade.routes import router

    application = FastAPI()
    application.include_router(router)
    return application


class _FakeAssistant:
    owner_id = OWNER_ID
    visibility = "PRIVATE"


def _permission(permission: Optional[str], exists: bool = True):
    """Patch the shared permission resolver the routes gate on."""
    from unittest.mock import AsyncMock

    assistant = _FakeAssistant() if exists else None
    return patch(
        "apis.app_api.kb_upgrade.routes.resolve_assistant_permission",
        new=AsyncMock(return_value=(assistant, permission)),
    )


class TestRoutePermissions:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("post", ""),
            ("post", "/retry"),
            ("post", "/notice"),
        ],
    )
    def test_a_viewer_cannot_write(
        self, app, authenticated_client, make_user, method, path
    ):
        """Requirement 23.7 enforced server-side, not by hiding a button."""
        client = authenticated_client(app, make_user(user_id="someone-else"))
        with _permission("viewer"):
            response = getattr(client, method)(
                f"/assistants/{ASSISTANT_ID}/knowledge-base/upgrade{path}"
            )
        assert response.status_code == 403

    def test_no_permission_reads_as_not_found(
        self, app, authenticated_client, make_user
    ):
        """404 rather than 403, so the endpoint does not confirm existence."""
        client = authenticated_client(app, make_user(user_id="stranger"))
        with _permission(None):
            response = client.get(
                f"/assistants/{ASSISTANT_ID}/knowledge-base/upgrade"
            )
        assert response.status_code == 404

    def test_a_viewer_may_read_but_is_told_it_cannot_upgrade(
        self, app, authenticated_client, make_user, table
    ):
        table.docs = [_doc("d1")]
        client = authenticated_client(app, make_user(user_id="viewer-1"))
        with _permission("viewer"):
            response = client.get(
                f"/assistants/{ASSISTANT_ID}/knowledge-base/upgrade"
            )
        assert response.status_code == 200
        assert response.json()["canUpgrade"] is False

    def test_an_owner_can_start_an_upgrade(
        self, app, authenticated_client, make_user, table
    ):
        client = authenticated_client(app, make_user(user_id=OWNER_ID))
        with _permission("owner"):
            response = client.post(
                f"/assistants/{ASSISTANT_ID}/knowledge-base/upgrade"
            )
        assert response.status_code == 202
        assert response.json()["started"] is True

    def test_the_status_read_degrades_rather_than_500s(
        self, app, authenticated_client, make_user, monkeypatch
    ):
        """A card that cannot be described must not take the page down."""
        monkeypatch.delenv("DYNAMODB_ASSISTANTS_TABLE_NAME", raising=False)
        client = authenticated_client(app, make_user(user_id=OWNER_ID))
        with _permission("owner"):
            response = client.get(
                f"/assistants/{ASSISTANT_ID}/knowledge-base/upgrade"
            )
        assert response.status_code == 200
        assert response.json()["phase"] == "none"

    def test_enrolment_conflicts_while_the_flag_is_off(
        self, app, authenticated_client, make_user, table, monkeypatch
    ):
        monkeypatch.setenv(s.FLAG_MIGRATION_ENABLED, "false")
        client = authenticated_client(app, make_user(user_id=OWNER_ID))
        with _permission("owner"):
            response = client.post(
                f"/assistants/{ASSISTANT_ID}/knowledge-base/upgrade"
            )
        assert response.status_code == 409


class TestWireContract:
    """The client reads camelCase; a rename here silently empties the card."""

    def test_status_serialises_camel_case(self):
        payload = m.UpgradeStatusResponse(
            phase="available", canUpgrade=True
        ).model_dump(by_alias=True)
        assert "canUpgrade" in payload
        assert "noticePending" in payload
        assert "documentsNotCarried" in payload

    def test_stranded_document_serialises_camel_case(self):
        payload = m.DocumentNotCarried(
            documentId="d1",
            filename="a.pdf",
            status="failed",
            kind="processing_failure",
            message="x",
            retryable=True,
        ).model_dump(by_alias=True)
        assert payload["documentId"] == "d1"
