"""
Authorization, isolation and publication: the app is the authority.

Requirement 25. Managed KB ships two features whose names overstate what they
provide — metadata filters are "filter-level (logical) isolation, *not*
IAM-enforced", and ACL-aware retrieval "is not authorization" by AWS's own
statement. This feature therefore keeps authorization in the application, and
these tests are what stop that from eroding.

Four things are asserted, in the order they can fail silently:

1. **The gate runs before the backend does.** Not "an unauthorized caller gets
   an empty list" — that is also what an authorized caller with an empty corpus
   gets. What matters is that no backend was contacted at all, so a stand-in
   backend records its calls and the assertion is on that record.
2. **The gate fails closed**, including when the permission lookup raises.
3. **Filters are not the tenant boundary**, and ACL-aware retrieval is not
   adopted anywhere in the seam.
4. **Publication semantics**: an engine swap is not a corpus change, and an agent
   on the store shelf is exempt from reclaim — asked as ``is_on_shelf`` rather
   than ``is_listed``, because a live listing sitting in ``changes_requested``
   reads as unlisted by state name alone.

Feature: managed-kb-migration
Requirements: 25.1, 25.2, 25.3, 25.4, 25.5, 25.6, 25.7, 25.8, 25.9, 25.10,
25.11, 24.6, 24.12, 24.14, 11.5
"""

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from apis.shared.assistants.kb_access import (
    KB_READ_PERMISSIONS,
    KB_WRITE_PERMISSIONS,
    granted,
    is_shared_beyond_owner,
    resolve_kb_access,
)
from apis.shared.assistants.kb_publication import (
    is_reclaim_exempt,
    migration_requires_review,
    reclaim_exemption_reason,
)
from apis.shared.assistants.rag_service import search_assistant_knowledgebase_with_formatting
from apis.shared.kb_backend.protocol import DEFAULT_TOP_K, Chunk
from apis.shared.kb_backend.resource_policy import (
    POLICY_KB_ID_ATTR,
    POLICY_REVISION_ATTR,
    RETRIEVE_ACTIONS,
    ResourcePolicyError,
    ensure_retrieve_policy,
    knowledge_base_arn,
    policy_is_stale,
    retrieval_principals,
    retrieve_policy_document,
)

ASSISTANT_ID = "ast-authz-001"
USER_ID = "user-authz-001"
TABLE_NAME = "test-table"


class RecordingBackend:
    """A backend that remembers whether it was asked anything.

    The whole point of Requirement 25.1 is *ordering*: a check that runs after
    the corpus has been read is an audit trail, not an access control. An empty
    return value cannot distinguish the two, so this records calls instead.
    """

    def __init__(self, chunks: List[Chunk] = None):
        self._chunks = chunks or []
        self.calls: List[Dict[str, Any]] = []

    async def search(self, kb_ref: str, query: str, top_k: int = DEFAULT_TOP_K) -> List[Chunk]:
        self.calls.append({"kb_ref": kb_ref, "query": query, "top_k": top_k})
        return list(self._chunks)

    async def ingest(self, kb_ref: str, source) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    async def delete_document(self, kb_ref: str, document_id: str) -> None:  # pragma: no cover
        raise NotImplementedError


def _chunk(document_id: str = "doc-1") -> Chunk:
    return Chunk(
        text=f"passage from {document_id}",
        relevance=1.0,
        document_id=document_id,
        metadata={"document_id": document_id},
        key=f"{document_id}#0",
    )


def _complete_document_table() -> MagicMock:
    """A DynamoDB stand-in whose documents are all ``complete``.

    So that a test about authorization cannot pass because the *status* filter
    dropped everything — a false green that would survive removing the gate.
    """
    table = MagicMock()
    table.get_item.return_value = {"Item": {"status": "complete"}}
    resource = MagicMock()
    resource.Table.return_value = table
    return resource


async def _search(access, backend: RecordingBackend, top_k: int = 5):
    with patch.dict("os.environ", {"DYNAMODB_ASSISTANTS_TABLE_NAME": TABLE_NAME}, clear=False), patch(
        "apis.shared.assistants.rag_service.resolve_backend", return_value=backend
    ), patch(
        "apis.shared.assistants.rag_service.boto3.resource",
        return_value=_complete_document_table(),
    ), patch(
        "apis.shared.assistants.rag_service.emit_count"
    ) as emit:
        results = await search_assistant_knowledgebase_with_formatting(
            ASSISTANT_ID, "q", top_k, access=access
        )
    return results, emit


# ── 1. The gate runs, and it runs first ──────────────────────────────────────
class TestAccessIsResolvedBeforeRetrieval:
    @pytest.mark.asyncio
    async def test_an_owner_reaches_the_backend(self):
        """The permissive case, first — so every denial below means something."""
        backend = RecordingBackend([_chunk()])
        results, _ = await _search(granted(ASSISTANT_ID, USER_ID, "owner"), backend)

        assert len(results) == 1
        assert backend.calls, "an owner must reach the backend"

    @pytest.mark.asyncio
    async def test_a_viewer_reads_through_the_agent(self):
        """Sharing an agent is *for* reading, so a viewer retrieves (Req 25.2)."""
        backend = RecordingBackend([_chunk()])
        results, _ = await _search(granted(ASSISTANT_ID, USER_ID, "viewer"), backend)

        assert len(results) == 1
        assert backend.calls

    @pytest.mark.asyncio
    async def test_a_viewer_may_not_upgrade(self):
        """Reading is not upgrading: an engine migration spends money."""
        access = granted(ASSISTANT_ID, USER_ID, "viewer")
        assert access.may_read is True
        assert access.may_upgrade is False

        for permission in ("owner", "editor"):
            assert granted(ASSISTANT_ID, USER_ID, permission).may_upgrade is True

    @pytest.mark.asyncio
    async def test_no_grant_never_reaches_the_backend(self):
        """Requirement 25.1: resolved *before* retrieval is attempted.

        Asserting on ``backend.calls`` rather than on the return value, because
        an empty list is also what an authorized user with no matches gets.
        """
        backend = RecordingBackend([_chunk()])
        results, emit = await _search(None, backend)

        assert results == []
        assert backend.calls == [], (
            "the backend was queried despite there being no grant; the access "
            "check is running after retrieval, which makes it an audit log"
        )
        assert any(call.args and call.args[0] == "KbAccessDenied" for call in emit.call_args_list)

    @pytest.mark.asyncio
    async def test_a_grant_for_another_assistant_is_refused(self):
        """The copy-paste shape: permission resolved for one id, retrieval on another."""
        backend = RecordingBackend([_chunk()])
        other = granted("ast-somebody-else", USER_ID, "owner")
        results, _ = await _search(other, backend)

        assert results == []
        assert backend.calls == []

    def test_the_access_argument_is_required(self):
        """Forgetting it must be a call-site failure, not a silent empty result.

        A default of ``None`` would fail closed too, but it would fail closed
        *quietly* in a caller that never intended to deny anyone.
        """
        import asyncio

        with pytest.raises(TypeError, match="access"):
            asyncio.run(search_assistant_knowledgebase_with_formatting(ASSISTANT_ID, "q"))


# ── 2. Fail closed ───────────────────────────────────────────────────────────
class TestAccessChecksFailClosed:
    """Requirement 24.6. Contrast with the resolver, which treats an unreadable
    KB_Record as legacy: there both answers serve the user's own documents, so
    one of them is always safe. Here the answers are "yours" and "someone
    else's"."""

    @pytest.mark.parametrize("permission", [None, "", "unknown", "auditor", "OWNER"])
    def test_anything_but_a_known_read_permission_denies(self, permission):
        """Including a case-variant, which is how a refactor introduces this bug."""
        assert granted(ASSISTANT_ID, USER_ID, permission) is None

    @pytest.mark.asyncio
    async def test_a_failing_permission_lookup_denies(self):
        async def _boom(**kwargs):
            raise RuntimeError("dynamodb unavailable")

        with patch(
            "apis.shared.assistants.service.resolve_assistant_permission", side_effect=_boom
        ):
            assert await resolve_kb_access(ASSISTANT_ID, USER_ID, "a@b.test") is None

    @pytest.mark.asyncio
    async def test_a_resolved_permission_becomes_a_grant(self):
        async def _resolve(**kwargs):
            return object(), "editor"

        with patch(
            "apis.shared.assistants.service.resolve_assistant_permission", side_effect=_resolve
        ):
            access = await resolve_kb_access(ASSISTANT_ID, USER_ID, "a@b.test")

        assert access is not None
        assert access.permission == "editor"
        # 1:1 this phase: the knowledge base id is the assistant id.
        assert access.app_kb_id == ASSISTANT_ID

    @pytest.mark.asyncio
    async def test_no_permission_resolves_to_no_grant(self):
        async def _resolve(**kwargs):
            return object(), None

        with patch(
            "apis.shared.assistants.service.resolve_assistant_permission", side_effect=_resolve
        ):
            assert await resolve_kb_access(ASSISTANT_ID, USER_ID) is None

    def test_the_permission_sets_do_not_overlap_wrongly(self):
        """Write implies read; read does not imply write."""
        assert KB_WRITE_PERMISSIONS < KB_READ_PERMISSIONS
        assert "viewer" in KB_READ_PERMISSIONS
        assert "viewer" not in KB_WRITE_PERMISSIONS

    def test_a_grant_cannot_be_mutated_after_it_is_resolved(self):
        access = granted(ASSISTANT_ID, USER_ID, "viewer")
        with pytest.raises(Exception):
            access.permission = "owner"


# ── 3. Filters are not the tenant boundary ───────────────────────────────────
_KB_BACKEND_DIR = (
    Path(__file__).resolve().parent.parent.parent / "src" / "apis" / "shared" / "kb_backend"
)


class TestFiltersAreNotTheTenantBoundary:
    def test_the_seam_does_not_adopt_acl_aware_retrieval(self):
        """Requirement 25.5, asserted against the source.

        ACL-aware retrieval's identity is email only, with no alias resolution,
        and a mismatch fails silently. On a platform that authenticates via OIDC
        with claim mappings, that is a worse primitive than an explicit check —
        so the configuration keys must not appear at all. A test on behaviour
        could not see a config key that was set but never exercised in tests.
        """
        forbidden = ("aclConfiguration", "userGroupFilter", "implicitFilterConfiguration")
        offenders = []
        for pyfile in sorted(_KB_BACKEND_DIR.glob("*.py")):
            body = pyfile.read_text(encoding="utf-8")
            for key in forbidden:
                if key in body:
                    offenders.append(f"{pyfile.name} mentions {key}")
        assert offenders == [], (
            "ACL-aware retrieval is deliberately not adopted in this phase "
            "(Requirement 25.5): " + "; ".join(offenders)
        )

    def test_retrieval_is_scoped_by_knowledge_base_not_by_filter(self):
        """Requirement 25.4. The default retrieval configuration carries no filter.

        The boundary is one knowledge base per assistant, which holds because
        this phase keeps ``App_KB_Id == assistant_id``. A filter is a query
        argument, and anything that can issue a query can omit it — so if the
        default config carried a tenancy filter, omitting it would be a
        cross-tenant read.
        """
        from apis.shared.kb_backend.managed_backend import retrieval_configuration

        config = retrieval_configuration(top_k=5)
        managed = config["managedSearchConfiguration"]
        assert "filter" not in managed, (
            "the default retrieval configuration carries a filter; if isolation "
            "depended on it, omitting it would cross a tenant boundary"
        )
        assert "vectorSearchConfiguration" not in config

    def test_an_isolation_critical_filter_must_be_exact_match(self):
        """Requirement 11.5. Prefix and substring operators over-match silently:
        a filter isolating ``ast-1`` also admits ``ast-10``."""
        from apis.shared.kb_backend.managed_backend import (
            UnsafeFilterOperator,
            validate_isolation_filter,
        )

        validate_isolation_filter({"equals": {"key": "document_id", "value": "doc-1"}})
        with pytest.raises(UnsafeFilterOperator):
            validate_isolation_filter({"startsWith": {"key": "document_id", "value": "doc-"}})
        with pytest.raises(UnsafeFilterOperator):
            validate_isolation_filter(
                {"andAll": [{"equals": {}}, {"stringContains": {}}]}
            )


# ── 4. Resource policies ─────────────────────────────────────────────────────
AWS_KB_ID = "KB1234567890"
NEW_AWS_KB_ID = "KB0987654321"
PRINCIPAL = "arn:aws:iam::123456789012:role/test-project-agentcore-runtime-role"
POLICY_ENV = {
    "AWS_ACCOUNT_ID": "123456789012",
    "AWS_REGION": "us-west-2",
    "MANAGED_KB_RETRIEVAL_PRINCIPAL_ARNS": PRINCIPAL,
}


class TestResourcePolicyDocument:
    def test_the_policy_names_principals_and_a_single_resource(self):
        arn = knowledge_base_arn(AWS_KB_ID, "us-west-2", "123456789012")
        document = retrieve_policy_document(arn, [PRINCIPAL])
        statement = document["Statement"][0]

        assert statement["Effect"] == "Allow"
        assert statement["Principal"] == {"AWS": [PRINCIPAL]}
        assert statement["Action"] == list(RETRIEVE_ACTIONS)
        assert statement["Resource"] == arn

    def test_there_is_no_branch_that_produces_a_wildcard(self):
        """The CDK grant cannot condition on a policy's contents, so this
        function is the only thing standing between "narrow the blast radius"
        and "widen it". Serialized and searched, because a wildcard could hide
        in a nested structure a key-by-key assertion would miss."""
        arn = knowledge_base_arn(AWS_KB_ID, "us-west-2", "123456789012")
        body = json.dumps(retrieve_policy_document(arn, [PRINCIPAL]))
        assert '"*"' not in body
        assert "arn:aws:iam::*" not in body

    def test_an_empty_principal_list_is_refused(self):
        with pytest.raises(ResourcePolicyError):
            retrieve_policy_document("arn:aws:bedrock:us-west-2:1:knowledge-base/x", [])

    def test_an_unknown_account_is_named_rather_than_guessed(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(ResourcePolicyError, match="AWS_ACCOUNT_ID"):
                knowledge_base_arn(AWS_KB_ID)

    def test_principals_are_deduplicated_and_ordered(self):
        """So the same configuration always produces the same document; otherwise
        every call looks like a change and nothing can be compared."""
        with patch.dict(
            "os.environ",
            {"MANAGED_KB_RETRIEVAL_PRINCIPAL_ARNS": f" {PRINCIPAL} ,{PRINCIPAL},"},
            clear=False,
        ):
            assert retrieval_principals() == (PRINCIPAL,)


class TestResourcePolicyStaleness:
    def test_a_new_aws_kb_id_makes_the_policy_stale(self):
        """Requirement 25.7 / 24.12. A policy attaches to an ARN, so a
        replacement identifier silently drops sharing."""
        assert policy_is_stale({"awsKbId": NEW_AWS_KB_ID, POLICY_KB_ID_ATTR: AWS_KB_ID}) is True

    def test_a_matching_target_is_not_stale(self):
        assert policy_is_stale({"awsKbId": AWS_KB_ID, POLICY_KB_ID_ATTR: AWS_KB_ID}) is False

    def test_never_applied_is_stale(self):
        assert policy_is_stale({"awsKbId": AWS_KB_ID}) is True

    def test_an_unprovisioned_record_is_not_stale(self):
        """Nothing exists yet, so there is nothing to be stale against — lazy
        provisioning makes this the ordinary state, not an error."""
        assert policy_is_stale({}) is False
        assert policy_is_stale(None) is False


class TestEnsureRetrievePolicy:
    @pytest.mark.asyncio
    async def test_a_shared_knowledge_base_gets_a_policy(self):
        client = MagicMock()
        client.put_resource_policy.return_value = {"revisionId": "rev-1"}
        record = {"awsKbId": AWS_KB_ID}

        with patch.dict("os.environ", POLICY_ENV, clear=False), patch(
            "apis.shared.kb_backend.records.set_resource_policy_state"
        ) as setter:
            revision = await ensure_retrieve_policy(
                ASSISTANT_ID, ASSISTANT_ID, shared=True, record=record, client=client
            )

        assert revision == "rev-1"
        arn = client.put_resource_policy.call_args.kwargs["resourceArn"]
        assert arn.endswith(f"knowledge-base/{AWS_KB_ID}")
        setter.assert_called_once_with(ASSISTANT_ID, ASSISTANT_ID, AWS_KB_ID, "rev-1")

    @pytest.mark.asyncio
    async def test_a_private_knowledge_base_gets_none(self):
        """A policy on a single-owner corpus restricts nothing the assistant's
        own access check does not already restrict."""
        client = MagicMock()

        with patch.dict("os.environ", POLICY_ENV, clear=False):
            revision = await ensure_retrieve_policy(
                ASSISTANT_ID,
                ASSISTANT_ID,
                shared=False,
                record={"awsKbId": AWS_KB_ID},
                client=client,
            )

        assert revision is None
        client.put_resource_policy.assert_not_called()
        client.delete_resource_policy.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsharing_removes_the_policy_and_forgets_the_target(self):
        client = MagicMock()
        record = {"awsKbId": AWS_KB_ID, POLICY_KB_ID_ATTR: AWS_KB_ID}

        with patch.dict("os.environ", POLICY_ENV, clear=False), patch(
            "apis.shared.kb_backend.records.set_resource_policy_state"
        ) as setter:
            await ensure_retrieve_policy(
                ASSISTANT_ID, ASSISTANT_ID, shared=False, record=record, client=client
            )

        client.delete_resource_policy.assert_called_once()
        setter.assert_called_once_with(ASSISTANT_ID, ASSISTANT_ID, None, None)

    @pytest.mark.asyncio
    async def test_a_current_policy_makes_no_aws_call(self):
        """The common path. Callers may invoke this freely only if it is cheap."""
        client = MagicMock()
        record = {"awsKbId": AWS_KB_ID, POLICY_KB_ID_ATTR: AWS_KB_ID, POLICY_REVISION_ATTR: "rev-1"}

        with patch.dict("os.environ", POLICY_ENV, clear=False):
            revision = await ensure_retrieve_policy(
                ASSISTANT_ID, ASSISTANT_ID, shared=True, record=record, client=client
            )

        assert revision == "rev-1"
        client.put_resource_policy.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_rehydration_reapplies_to_the_new_identifier(self):
        """Requirement 24.12, the reason this whole mechanism is state-based.

        The record still names the *old* target. Nothing fired an event; the
        mismatch alone is enough to repair it.
        """
        client = MagicMock()
        client.put_resource_policy.return_value = {"revisionId": "rev-2"}
        record = {"awsKbId": NEW_AWS_KB_ID, POLICY_KB_ID_ATTR: AWS_KB_ID}

        with patch.dict("os.environ", POLICY_ENV, clear=False), patch(
            "apis.shared.kb_backend.records.set_resource_policy_state"
        ) as setter:
            revision = await ensure_retrieve_policy(
                ASSISTANT_ID, ASSISTANT_ID, shared=True, record=record, client=client
            )

        assert revision == "rev-2"
        arn = client.put_resource_policy.call_args.kwargs["resourceArn"]
        assert arn.endswith(f"knowledge-base/{NEW_AWS_KB_ID}"), (
            "the policy was re-applied to the old identifier, so sharing is "
            "still attached to a knowledge base nobody reads"
        )
        setter.assert_called_once_with(ASSISTANT_ID, ASSISTANT_ID, NEW_AWS_KB_ID, "rev-2")

    @pytest.mark.asyncio
    async def test_an_unprovisioned_shared_knowledge_base_is_not_an_error(self):
        client = MagicMock()
        with patch.dict("os.environ", POLICY_ENV, clear=False):
            assert (
                await ensure_retrieve_policy(
                    ASSISTANT_ID, ASSISTANT_ID, shared=True, record={}, client=client
                )
                is None
            )
        client.put_resource_policy.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_configured_principals_applies_nothing(self):
        """Rather than inventing one, which would either widen access or lock
        the platform out of its own corpus."""
        client = MagicMock()
        with patch.dict(
            "os.environ",
            {**POLICY_ENV, "MANAGED_KB_RETRIEVAL_PRINCIPAL_ARNS": ""},
            clear=False,
        ):
            revision = await ensure_retrieve_policy(
                ASSISTANT_ID,
                ASSISTANT_ID,
                shared=True,
                record={"awsKbId": AWS_KB_ID},
                client=client,
            )

        assert revision is None
        client.put_resource_policy.assert_not_called()


class TestSharedBeyondOwner:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("visibility", ["PUBLIC", "SHARED"])
    async def test_visibility_alone_can_answer_yes(self, visibility):
        assert await is_shared_beyond_owner(ASSISTANT_ID, USER_ID, visibility) is True

    @pytest.mark.asyncio
    async def test_a_private_assistant_with_a_share_record_is_shared(self):
        """The case visibility alone misses, and the one most likely to exist:
        ``resolve_assistant_permission`` resolves an editor share on a PRIVATE
        assistant to ``editor``."""

        async def _shares(assistant_id, owner_id):
            return [{"email": "someone@else.test", "permission": "editor"}]

        with patch(
            "apis.shared.assistants.service.list_assistant_shares", side_effect=_shares
        ):
            assert await is_shared_beyond_owner(ASSISTANT_ID, USER_ID, "PRIVATE") is True

    @pytest.mark.asyncio
    async def test_a_private_assistant_with_no_shares_is_not_shared(self):
        async def _shares(assistant_id, owner_id):
            return []

        with patch(
            "apis.shared.assistants.service.list_assistant_shares", side_effect=_shares
        ):
            assert await is_shared_beyond_owner(ASSISTANT_ID, USER_ID, "PRIVATE") is False

    @pytest.mark.asyncio
    async def test_an_error_assumes_shared(self):
        """Fails toward the narrowing policy: one extra control-plane call
        against leaving a multi-user corpus account-readable."""

        async def _boom(assistant_id, owner_id):
            raise RuntimeError("dynamodb unavailable")

        with patch("apis.shared.assistants.service.list_assistant_shares", side_effect=_boom):
            assert await is_shared_beyond_owner(ASSISTANT_ID, USER_ID, "PRIVATE") is True


# ── 5. Publication semantics ─────────────────────────────────────────────────
class TestPublishedAgentCorpusBehaviour:
    """Requirement 24.14."""

    def test_an_engine_swap_is_not_a_corpus_change(self):
        """Requirement 25.8. Parity is the contract, so a swap needs no re-review.
        If a future change makes engines return different results, this is where
        the argument has to be had."""
        assert migration_requires_review("s3vectors", "managed") is False
        assert migration_requires_review(None, "managed") is False

    def test_an_engine_swap_does_not_change_what_is_retrieved(self):
        """Asserted where it is observable: the facade applies the same rules to
        whatever comes back across the seam, so two backends returning the same
        chunks produce the same response — the published-agent guarantee."""
        import asyncio

        chunks = [_chunk("doc-a"), _chunk("doc-b")]
        legacy = RecordingBackend(chunks)
        managed = RecordingBackend(chunks)
        access = granted(ASSISTANT_ID, USER_ID, "viewer")

        before, _ = asyncio.run(_search(access, legacy))
        after, _ = asyncio.run(_search(access, managed))

        assert before == after
        assert [r["text"] for r in after] == [c.text for c in chunks]


class TestReclaimExemption:
    """Requirements 25.9, 25.10."""

    def test_an_agent_on_the_shelf_is_exempt(self):
        assert is_reclaim_exempt({}, "published", 3) is True

    def test_a_live_listing_in_changes_requested_is_still_exempt(self):
        """The trap ``is_on_shelf`` exists for: an admin requesting changes on a
        live listing leaves it serving but moves its state out of
        ``LISTED_STATES``. Keyed on ``is_listed``, a reclaim pass would delete
        the corpus behind an agent users can still see."""
        assert is_reclaim_exempt({}, "changes_requested", 3) is True

    def test_a_taken_down_agent_is_not_exempt_by_state_alone(self):
        """Requirement 25.10: reaching reclaim eligibility requires the listing
        machine's explicit ``taken_down`` edge, which clears
        ``published_version`` in the same breath. Nothing infers a takedown."""
        assert is_reclaim_exempt({}, "taken_down", None) is False

    def test_a_private_agent_is_not_exempt(self):
        assert is_reclaim_exempt({}, None, None) is False
        assert is_reclaim_exempt({}, "private", None) is False

    @pytest.mark.parametrize("attribute", ["exemptFromReclaim", "pinned"])
    def test_an_explicit_hold_is_exempt_regardless_of_listing(self, attribute):
        assert is_reclaim_exempt({attribute: True}, None, None) is True

    def test_an_unreadable_record_is_exempt(self):
        """Fail-closed applied to deletion, where it matters more than anywhere
        else: reclaim acts on knowledge bases it can describe."""
        assert is_reclaim_exempt(None, "private", None) is True

    def test_the_reason_is_actionable(self):
        """A report-only pass that says "skipped 400" tells an operator nothing."""
        assert "shelf" in reclaim_exemption_reason({}, "published", 1)
        assert "pinned" in reclaim_exemption_reason({"pinned": True})
        assert reclaim_exemption_reason({}, "private", None) is None
