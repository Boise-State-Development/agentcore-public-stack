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

Two helpers, composed in the chat route:

- ``_apply_message_file_cap`` — the server side of the SPA's
  ``MAX_FILES_PER_MESSAGE``, applied *before* the S3 fetch so a sixth file is
  reported to the user instead of silently truncated by the resolver.
- ``_apply_inline_byte_budget`` — first-fit in attachment order against
  ``INLINE_ATTACHMENTS_MAX_TOTAL_BYTES``; images count.
"""

import base64
import math

import pytest

from apis.inference_api.chat.routes import (
    _apply_inline_byte_budget,
    _apply_message_file_cap,
    _attachment_marker_names,
    _build_attachment_guidance,
    _emit_attachment_over_quota_metric,
    _estimate_decoded_size,
    _partition_attachments,
)
from apis.shared.feature_flags import attachment_turn_guard_enabled
from apis.shared.files import models as file_models

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
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


class TestInlineByteBudget:
    def test_under_the_budget_keeps_everything_in_order(self):
        files = [_Attachment("a.pdf", size=3 * MB), _Attachment("b.pdf", size=3 * MB)]
        kept, over, requested = _apply_inline_byte_budget(files, 7_500_000)

        assert kept == files
        assert over == []
        assert requested == 6 * MB

    def test_over_the_budget_moves_the_file_that_breaks_it(self):
        files = [_Attachment("a.pdf", size=4 * MB), _Attachment("b.pdf", size=4 * MB)]
        kept, over, requested = _apply_inline_byte_budget(files, 7_500_000)

        assert [f.filename for f in kept] == ["a.pdf"]
        assert [f.filename for f in over] == ["b.pdf"]
        assert requested == 8 * MB

    def test_first_fit_earlier_files_win_and_a_later_small_file_still_rides(self):
        # A(4) fits, B(4) would break the budget, C(1) still fits after A.
        files = [
            _Attachment("a.pdf", size=4 * MB),
            _Attachment("b.pdf", size=4 * MB),
            _Attachment("c.pdf", size=1 * MB),
        ]
        kept, over, _ = _apply_inline_byte_budget(files, 7_500_000)

        assert [f.filename for f in kept] == ["a.pdf", "c.pdf"]
        assert [f.filename for f in over] == ["b.pdf"]

    def test_order_is_deterministic_attachment_order_in_both_lists(self):
        files = [
            _Attachment("z.pdf", size=3 * MB),
            _Attachment("y.pdf", size=3 * MB),
            _Attachment("x.pdf", size=3 * MB),
            _Attachment("w.pdf", size=3 * MB),
        ]
        kept, over, _ = _apply_inline_byte_budget(files, 7_500_000)

        assert [f.filename for f in kept] == ["z.pdf", "y.pdf"]
        assert [f.filename for f in over] == ["x.pdf", "w.pdf"]

    def test_images_count_toward_the_budget(self):
        # The per-file document gate skips images; the message-level budget
        # must not, because the image bytes are in the same persisted event.
        files = [
            _Attachment("photo.png", content_type="image/png", size=4 * MB),
            _Attachment("report.pdf", size=4 * MB),
        ]
        kept, over, _ = _apply_inline_byte_budget(files, 7_500_000)

        assert [f.filename for f in kept] == ["photo.png"]
        assert [f.filename for f in over] == ["report.pdf"]

    def test_a_single_file_over_the_budget_is_moved_not_raised(self):
        files = [_Attachment("huge.png", content_type="image/png", size=8 * MB)]
        kept, over, _ = _apply_inline_byte_budget(files, 7_500_000)

        assert kept == []
        assert over == files

    def test_exactly_at_the_budget_is_allowed(self):
        files = [_Attachment("a.pdf", size=7_500_000)]
        kept, over, _ = _apply_inline_byte_budget(files, 7_500_000)
        assert kept == files and over == []

    def test_non_positive_budget_disables_the_cap(self):
        files = [_Attachment("a.pdf", size=4 * MB), _Attachment("b.pdf", size=4 * MB)]
        kept, over, requested = _apply_inline_byte_budget(files, 0)

        assert kept == files
        assert over == []
        assert requested == 8 * MB

    def test_empty_input(self):
        assert _apply_inline_byte_budget([], 7_500_000) == ([], [], 0)


class TestNothingReachesTheSessionExceptionPath:
    """The property the whole PR exists for: whatever survives the budget is
    writable as one AgentCore Memory event.

    The SDK serialises the message as base64 inside a ``blob`` payload. If the
    kept set's encoded size is under the 10 MB event quota, ``create_message``
    cannot fail on size, so the turn degrades to the guidance note rather than
    to ``SessionException``.
    """

    @pytest.mark.parametrize(
        "sizes",
        [
            [4 * MB, 4 * MB],                       # the classic two-file case
            [4 * MB, 4 * MB, 4 * MB, 4 * MB, 4 * MB],  # SPA-legal five files
            [3 * MB, 3 * MB, 3 * MB],               # 3 files, each fine
            [2_600_000, 2_600_000, 2_600_000],      # the "3–4 files" prod shape
            [7_500_000, 1],
            [1] * 5 + [4 * MB] * 5,
        ],
    )
    def test_kept_set_encodes_under_the_event_quota(self, sizes):
        files = [_Attachment(f"f{i}.pdf", size=s) for i, s in enumerate(sizes)]
        default_cap = file_models.INLINE_ATTACHMENTS_MAX_TOTAL_BYTES
        kept, over, _ = _apply_inline_byte_budget(files, default_cap)

        encoded = sum(len(f.bytes) for f in kept)
        assert encoded <= math.ceil(default_cap * 4 / 3) + 4 * len(kept)  # padding slack
        assert encoded <= EVENT_QUOTA_BYTES
        # Nothing is lost silently: every input is in exactly one bucket.
        assert sorted(f.filename for f in kept + over) == sorted(f.filename for f in files)

    def test_partition_then_budget_chain_on_files_that_each_pass_the_per_file_gate(self):
        # Each file is under INLINE_DOCUMENT_MAX_BYTES (4 MiB) so the
        # per-file gate keeps all three; only the aggregate budget catches
        # the turn. This is exactly the hole the per-file gate left open.
        per_file = file_models.INLINE_DOCUMENT_MAX_BYTES - 1024
        files = [_Attachment(f"f{i}.pdf", size=per_file) for i in range(3)]
        sheet = _Attachment("data.xlsx", content_type=XLSX_MIME, size=6 * MB)

        inline, tabular, _, oversized = _partition_attachments(files + [sheet])
        assert oversized == [] and inline == files and tabular == [sheet]

        kept, over, requested = _apply_inline_byte_budget(
            inline, file_models.INLINE_ATTACHMENTS_MAX_TOTAL_BYTES
        )
        assert [f.filename for f in kept] == ["f0.pdf"]
        assert [f.filename for f in over] == ["f1.pdf", "f2.pdf"]
        assert requested == 3 * per_file
        # The diverted spreadsheet never counted: it is not in the message.
        assert sum(len(f.bytes) for f in kept) <= EVENT_QUOTA_BYTES

    def test_default_cap_is_the_documented_derivation(self):
        # 10 MB event quota × 3/4 (base64) = 7.5 MB raw. Encoding exactly the
        # cap lands on the quota, not over it.
        assert file_models.INLINE_ATTACHMENTS_MAX_TOTAL_BYTES == 7_500_000
        assert len(_b64_of_size(7_500_000)) == EVENT_QUOTA_BYTES


class TestMessageFileCap:
    def test_under_the_cap_is_untouched(self):
        direct = [_Attachment("a.pdf")]
        ids = ["u1", "u2"]
        assert _apply_message_file_cap(direct, ids, 5) == (direct, ids, [], 0)

    def test_upload_ids_beyond_the_cap_are_counted_not_fetched(self):
        # The old resolver truncated at 5 silently; now the route knows.
        ids = [f"u{i}" for i in range(7)]
        kept_direct, kept_ids, names, total = _apply_message_file_cap([], ids, 5)

        assert kept_direct == []
        assert kept_ids == ids[:5]
        assert names == []
        assert total == 2

    def test_direct_files_beyond_the_cap_are_named(self):
        direct = [_Attachment(f"d{i}.pdf") for i in range(6)]
        kept_direct, kept_ids, names, total = _apply_message_file_cap(direct, [], 5)

        assert kept_direct == direct[:5]
        assert kept_ids == []
        assert names == ["d5.pdf"]
        assert total == 1

    def test_direct_files_come_first_and_ids_fill_the_remainder(self):
        direct = [_Attachment("d0.pdf"), _Attachment("d1.pdf")]
        ids = ["u0", "u1", "u2", "u3", "u4"]
        kept_direct, kept_ids, names, total = _apply_message_file_cap(direct, ids, 5)

        assert kept_direct == direct
        assert kept_ids == ["u0", "u1", "u2"]
        assert names == []
        assert total == 2

    def test_direct_overflow_leaves_no_budget_for_ids(self):
        direct = [_Attachment(f"d{i}.pdf") for i in range(6)]
        kept_direct, kept_ids, names, total = _apply_message_file_cap(direct, ["u0"], 5)

        assert len(kept_direct) == 5
        assert kept_ids == []
        assert names == ["d5.pdf"]
        assert total == 2

    def test_non_positive_cap_disables(self):
        direct = [_Attachment(f"d{i}.pdf") for i in range(9)]
        ids = [f"u{i}" for i in range(9)]
        assert _apply_message_file_cap(direct, ids, 0) == (direct, ids, [], 0)

    def test_default_cap_matches_the_spa(self):
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
