"""A turn's attachments are held to one message's budget — never to
``SessionException`` (docs/specs/document-context-offload.md §4E, PR-6).

The stored unit is the MESSAGE, not the file. A turn's inline attachments are
persisted as one AgentCore Memory event; anything over the SDK's ~72 KB
conversational limit is written as a base64 ``blob`` (4/3 inflation) bounded by
the 10 MB event quota, so ~7.5 MB of raw attachment bytes per turn is the break
point. Past it, ``create_message`` re-raises ``SessionException`` — a hole in
history. The per-file 4 MB gate and the SPA's 5-file cap do not protect: prod
measurement (validation doc, Claim 7) found ~1.3–1.4% of attachment turns over
quota, several with only 3–4 files.

Which file survives is the planner's decision (``apis.shared.files.attachment_plan``)
and lives in its table test (``tests/apis/shared/files/test_attachment_plan.py``).
This file keeps the route's side: the guidance and marker text built from the
plan, the over-quota metric, the kill switch reader, the budget's derivation,
and that ``_resolve_turn_attachments`` applies the count cap before the fetch.
"""

import base64
import math

import pytest

from apis.inference_api.chat import routes
from apis.inference_api.chat.models import InvocationRequest
from apis.inference_api.chat.routes import (
    _attachment_marker_names,
    _build_attachment_guidance,
    _emit_attachment_over_quota_metric,
    _estimate_decoded_size,
)
from apis.shared.feature_flags import attachment_turn_guard_enabled
from apis.shared.files import models as file_models

EVENT_QUOTA_BYTES = 10_000_000  # AgentCore Memory event payload quota


def _b64_of_size(n: int) -> str:
    """Base64 text whose decoded size is exactly ``n`` bytes."""
    return base64.b64encode(b"x" * n).decode()


class _Attachment:
    """Minimal stand-in for FileContent — the helpers read these three."""

    def __init__(self, filename: str, content_type: str = "application/pdf", size: int = 0):
        self.filename = filename
        self.content_type = content_type
        self.bytes = _b64_of_size(size)


MB = 1024 * 1024


class TestTheRouteAppliesTheCountCapBeforeTheFetch:
    """Wiring: the planner's count decision reaches the resolver and the plan."""

    @pytest.fixture
    def phase(self, monkeypatch):
        calls: list = []

        class _Resolver:
            async def resolve_files(self, *, user_id, upload_ids, max_files=None):
                calls.append({"upload_ids": list(upload_ids), "max_files": max_files})
                return []

        async def _no_pending(*args, **kwargs):
            return []

        import apis.shared.sessions.metadata as metadata

        monkeypatch.setattr(metadata, "pop_pending_attachments", _no_pending)
        monkeypatch.setattr(routes, "get_file_resolver", lambda: _Resolver())

        async def run(upload_ids):
            req = InvocationRequest(session_id="sess-1", message="hi", file_upload_ids=upload_ids)
            return await routes._resolve_turn_attachments(req, "user-1", None, is_resume=False, is_continuation=False)

        return run, calls

    @pytest.mark.asyncio
    async def test_a_sixth_upload_is_counted_and_never_fetched(self, phase):
        run, calls = phase
        out = await run([f"u{i}" for i in range(6)])

        assert calls == [{"upload_ids": [f"u{i}" for i in range(5)], "max_files": None}]
        assert out.dropped_over_count_total == 1
        assert out.max_files_per_message == 5
        assert out.plan.to_row() == {"profile": "legacy", "files": [], "droppedOverCount": 1}

    @pytest.mark.asyncio
    async def test_with_the_guard_off_the_resolver_keeps_its_backstop(self, phase, monkeypatch):
        monkeypatch.setenv("ATTACHMENT_TURN_GUARD_ENABLED", "false")
        run, calls = phase
        out = await run([f"u{i}" for i in range(6)])

        assert calls[0]["max_files"] == 5 and len(calls[0]["upload_ids"]) == 6
        assert out.dropped_over_count_total == 0


class TestDefaults:
    def test_default_cap_is_the_documented_derivation(self):
        # 10 MB event quota × 3/4 (base64) = 7.5 MB raw is the *break point*,
        # not a safe cap: encoding exactly that lands ON the quota, and the
        # event's JSON envelope is then added on top. The default sits below it.
        assert len(_b64_of_size(7_500_000)) == EVENT_QUOTA_BYTES, "the break point is real"
        assert file_models.INLINE_ATTACHMENTS_MAX_TOTAL_BYTES == 7_000_000
        assert len(_b64_of_size(file_models.INLINE_ATTACHMENTS_MAX_TOTAL_BYTES)) < EVENT_QUOTA_BYTES

    def test_default_file_cap_matches_the_spa(self):
        assert file_models.MAX_FILES_PER_MESSAGE == 5


class TestGuidanceText:
    def test_aggregate_case_names_the_files_and_says_follow_up(self):
        over = [_Attachment("b.pdf"), _Attachment("c.pdf")]
        text = _build_attachment_guidance([], [], [], None, over_budget=over)

        assert "`b.pdf`, `c.pdf`" in text
        assert "together exceed the combined size limit" in text
        assert "follow-up message" in text

    def test_aggregate_and_per_file_notes_are_different_sentences(self):
        # The remedy differs: per-file → smaller file; aggregate → next message.
        huge = [_Attachment("huge.pdf")]
        over = [_Attachment("b.pdf")]
        text = _build_attachment_guidance([], [], huge, None, over_budget=over)

        per_file, aggregate = text.split("\n\n")
        assert "`huge.pdf`" in per_file and "Try a smaller file" in per_file
        assert "`b.pdf`" in aggregate and "follow-up message" in aggregate
        assert "`b.pdf`" not in per_file

    def test_count_cap_note_with_names(self):
        text = _build_attachment_guidance(
            [], [], [], None,
            dropped_over_count_names=["f.pdf"], dropped_over_count_total=1, max_files=5,
        )
        assert "Only the first 5 files per message are attached" in text
        assert "`f.pdf` were not" in text

    def test_count_cap_note_with_names_and_unnamed_ids(self):
        text = _build_attachment_guidance(
            [], [], [], None,
            dropped_over_count_names=["f.pdf"], dropped_over_count_total=3, max_files=5,
        )
        assert "`f.pdf` and 2 more were not" in text

    def test_count_cap_note_count_only_when_ids_were_dropped(self):
        text = _build_attachment_guidance(
            [], [], [], None, dropped_over_count_total=2, max_files=5,
        )
        assert "2 more files were not" in text
        assert "follow-up message" in text

    def test_count_cap_note_singular(self):
        text = _build_attachment_guidance(
            [], [], [], None, dropped_over_count_total=1, max_files=5,
        )
        assert "1 more file was not" in text

    def test_silent_when_nothing_was_trimmed(self):
        assert _build_attachment_guidance([], [], [], None) == ""
        assert _build_attachment_guidance(
            [], [], [], None, over_budget=[], dropped_over_count_names=[], dropped_over_count_total=0
        ) == ""

    def test_existing_notes_are_unchanged_by_the_new_defaults(self):
        huge = [_Attachment("huge.pdf")]
        assert _build_attachment_guidance([], [], huge, None) == _build_attachment_guidance(
            [], [], huge, None, over_budget=None, dropped_over_count_total=0
        )


class TestMarkerNames:
    def test_over_budget_files_are_excluded_like_oversized_ones(self):
        # Both were dropped from the turn; the SPA must not rebuild a card.
        a, b, c = _Attachment("a.pdf"), _Attachment("b.pdf"), _Attachment("c.pdf")
        assert _attachment_marker_names([a, b, c], [c] + [b]) == ["a.pdf"]


class TestKillSwitch:
    @pytest.mark.parametrize("value", [None, "", "true", "TRUE", "yes", "0"])
    def test_default_on(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("ATTACHMENT_TURN_GUARD_ENABLED", raising=False)
        else:
            monkeypatch.setenv("ATTACHMENT_TURN_GUARD_ENABLED", value)
        assert attachment_turn_guard_enabled() is True

    @pytest.mark.parametrize("value", ["false", "FALSE", " False "])
    def test_only_literal_false_disables(self, monkeypatch, value):
        monkeypatch.setenv("ATTACHMENT_TURN_GUARD_ENABLED", value)
        assert attachment_turn_guard_enabled() is False


class TestOverQuotaMetric:
    def test_emits_one_content_free_record_in_bytes(self, monkeypatch):
        from apis.shared.observability import emf, prompt_cache

        calls = []
        monkeypatch.setattr(prompt_cache, "prompt_cache_observability_enabled", lambda: True)
        monkeypatch.setattr(
            emf, "emit_emf_metrics",
            lambda namespace, metrics, properties=None, units=None: calls.append(
                (namespace, metrics, properties, units)
            ),
        )
        _emit_attachment_over_quota_metric(
            requested_bytes=8 * MB, cap_bytes=7_500_000, inline_count=2, dropped_count=1
        )

        assert calls == [(
            "AgentCoreStack/Compaction",
            {"AttachmentTurnOverQuota": 8 * MB},
            {"capBytes": 7_500_000, "inlineFileCount": 2, "droppedFileCount": 1},
            {"AttachmentTurnOverQuota": "Bytes"},
        )]
        # Content-free: no filename, no user, no session in the record.
        assert not any("name" in k.lower() and "file" not in k.lower() for k in calls[0][2])

    def test_silenced_with_the_observability_layer(self, monkeypatch):
        from apis.shared.observability import emf, prompt_cache

        monkeypatch.setattr(prompt_cache, "prompt_cache_observability_enabled", lambda: False)
        monkeypatch.setattr(
            emf, "emit_emf_metrics", lambda *a, **k: pytest.fail("must not emit")
        )
        _emit_attachment_over_quota_metric(1, 1, 1, 1)

    def test_never_raises(self, monkeypatch):
        from apis.shared.observability import emf, prompt_cache

        monkeypatch.setattr(prompt_cache, "prompt_cache_observability_enabled", lambda: True)

        def boom(*a, **k):
            raise RuntimeError("emf down")

        monkeypatch.setattr(emf, "emit_emf_metrics", boom)
        _emit_attachment_over_quota_metric(1, 1, 1, 1)


class TestEstimateDecodedSize:
    def test_matches_real_decoded_length(self):
        for n in (0, 1, 2, 3, 4, 100, 7_500_000):
            f = _Attachment("x", size=n)
            assert _estimate_decoded_size(f) == n


class TestBudgetHeadroom:
    """The guard exists so a turn's inline attachments never exceed AgentCore's
    10 MB *event* quota — past which ``create_message`` raises
    ``SessionException`` and leaves a hole in history.

    Its first default did not achieve that. base64 of N raw bytes is
    ``4*ceil(N/3)``, so 7,500,000 encoded to **exactly 10,000,000** — the quota
    itself, with nothing left for the event's JSON envelope. A guard whose
    default sits precisely on the break point it exists to stay under does not
    prevent the failure it was written for.
    """

    QUOTA_BYTES = 10_000_000

    def _encoded(self, raw_bytes: int) -> int:
        return 4 * math.ceil(raw_bytes / 3)

    def test_the_default_leaves_room_for_the_event_envelope(self):
        from apis.shared.files.models import INLINE_ATTACHMENTS_MAX_TOTAL_BYTES as cap

        encoded = self._encoded(cap)
        headroom = self.QUOTA_BYTES - encoded
        assert headroom > 0, f"budget encodes to {encoded:,} against a {self.QUOTA_BYTES:,} quota"
        # Enough for the role, content keys, prompt text and per-file metadata
        # many times over, without being so conservative it trims real turns
        # (prod p90 attachment cluster is 2.58 MB).
        assert headroom >= 250_000, f"only {headroom:,} bytes of headroom"

    def test_the_encoding_math_matches_base64(self):
        """Pin the 4/3 inflation the budget is derived from, against the real
        encoder rather than the arithmetic in a comment."""
        for raw in (3, 3_000, 7_000_000):
            assert len(base64.b64encode(b"\0" * raw)) == self._encoded(raw)

    def test_a_turn_at_the_budget_still_fits_the_quota(self):
        from apis.shared.files.models import INLINE_ATTACHMENTS_MAX_TOTAL_BYTES as cap

        assert len(base64.b64encode(b"\0" * cap)) < self.QUOTA_BYTES
