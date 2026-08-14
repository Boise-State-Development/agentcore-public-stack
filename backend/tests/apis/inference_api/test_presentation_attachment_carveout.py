"""A .pptx attachment must never reach Bedrock as an inline document block.

This is not the same kind of rule as the tabular carve-out it sits next to.
Spreadsheets are diverted as an *optimization* — an xlsx would technically be
accepted inline, it just inflates past the 4.5MB internal limit and analyzes
worse than pandas would. A pptx is diverted because Bedrock's Converse
``DocumentFormat`` enum has no ``pptx`` member at all:

    pdf | csv | doc | docx | xls | xlsx | html | txt | md

So an inline deck is an unconditional ValidationException that kills the turn,
at any size, forever — not a threshold we tune. That is why
``is_presentation_file`` is checked BEFORE the size gate: routing a small deck
to the "oversized" bucket would produce a note that misdescribes why it was
skipped, and routing it to `inline` at all is simply broken.

The upload path and this carve-out are one feature. `.pptx` is in the backend
and frontend upload allowlists only because these tools can receive it; if a
future change re-narrows either allowlist, the create-deck tool's own error
text ("Upload a .pptx template first") becomes a lie again.
"""

import pytest

from apis.inference_api.chat.routes import (
    _attachment_marker_names,
    _build_attachment_guidance,
    _partition_attachments,
)
from apis.shared.files.models import ALLOWED_MIME_TYPES, is_presentation_file

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class _Attachment:
    """Minimal stand-in for FileContent — the partition only reads these three."""

    def __init__(self, filename: str, content_type: str, bytes_: str = ""):
        self.filename = filename
        self.content_type = content_type
        self.bytes = bytes_


class TestIsPresentationFile:
    def test_detects_by_mime_type(self):
        assert is_presentation_file("anything", PPTX_MIME) is True

    def test_detects_by_extension_when_mime_is_missing(self):
        # Browsers and some clients send "" or application/octet-stream for
        # pptx; the extension is the fallback, same as the tabular helper.
        assert is_presentation_file("deck.pptx", "") is True
        assert is_presentation_file("deck.PPTX", "application/octet-stream") is True

    def test_does_not_claim_other_documents(self):
        assert is_presentation_file("report.pdf", "application/pdf") is False
        assert is_presentation_file("data.xlsx", XLSX_MIME) is False

    def test_legacy_ppt_is_not_claimed(self):
        # .ppt (binary, pre-2007) is not in the upload allowlist and
        # python-pptx cannot open it — don't silently divert it.
        assert is_presentation_file("old.ppt", "application/vnd.ms-powerpoint") is False

    def test_pptx_is_uploadable(self):
        # The carve-out is unreachable if the upload gate rejects the file.
        assert ALLOWED_MIME_TYPES.get(PPTX_MIME) == "pptx"


class TestPartitionAttachments:
    def test_pptx_is_diverted_not_inlined(self):
        deck = _Attachment("deck.pptx", PPTX_MIME)
        inline, tabular, presentations, oversized = _partition_attachments([deck])

        assert presentations == [deck]
        assert inline == []
        assert tabular == []
        assert oversized == []

    def test_tiny_pptx_still_diverted_never_oversized(self):
        # The size gate must not get a vote: a 12-byte deck is still a deck,
        # and "too big" would be the wrong explanation for skipping it.
        deck = _Attachment("small.pptx", PPTX_MIME, bytes_="AAAA")
        inline, _, presentations, oversized = _partition_attachments([deck])

        assert presentations == [deck]
        assert oversized == []
        assert inline == []

    def test_ordinary_documents_still_inline(self):
        pdf = _Attachment("report.pdf", "application/pdf", bytes_="AAAA")
        _, _, presentations, _ = _partition_attachments([pdf])
        assert presentations == []

    def test_mixed_batch_lands_in_the_right_buckets(self):
        pdf = _Attachment("report.pdf", "application/pdf", bytes_="AAAA")
        sheet = _Attachment("data.xlsx", XLSX_MIME)
        deck = _Attachment("deck.pptx", PPTX_MIME)

        inline, tabular, presentations, oversized = _partition_attachments(
            [pdf, sheet, deck]
        )

        assert inline == [pdf]
        assert tabular == [sheet]
        assert presentations == [deck]
        assert oversized == []


class TestAttachmentMarkerNames:
    """Diverting a file must not erase it from the message it was attached to.

    The `[Attached files: …]` marker is the only link the SPA can replay on
    reload — see `_attachment_marker_names`. Deriving it from the inline set
    is what made a diverted deck's card vanish from restored history.
    """

    def test_includes_a_diverted_deck(self):
        pdf = _Attachment("report.pdf", "application/pdf")
        deck = _Attachment("deck.pptx", PPTX_MIME)
        assert _attachment_marker_names([pdf, deck], []) == [
            "report.pdf",
            "deck.pptx",
        ]

    def test_includes_a_diverted_spreadsheet(self):
        sheet = _Attachment("data.xlsx", XLSX_MIME)
        assert _attachment_marker_names([sheet], []) == ["data.xlsx"]

    def test_a_lone_deck_still_yields_a_name(self):
        # Nothing inline at all — the case that previously left no trace.
        deck = _Attachment("deck.pptx", PPTX_MIME)
        assert _attachment_marker_names([deck], []) == ["deck.pptx"]

    def test_excludes_oversized_files(self):
        # Dropped from the turn entirely; the guidance explains their absence,
        # so a card promising otherwise would be misleading.
        pdf = _Attachment("report.pdf", "application/pdf")
        huge = _Attachment("huge.pdf", "application/pdf")
        assert _attachment_marker_names([pdf, huge], [huge]) == ["report.pdf"]

    def test_preserves_attachment_order(self):
        # Order is deterministic because this text reaches the cacheable
        # prefix on later turns.
        files = [
            _Attachment("b.pptx", PPTX_MIME),
            _Attachment("a.pdf", "application/pdf"),
            _Attachment("c.xlsx", XLSX_MIME),
        ]
        assert _attachment_marker_names(files, []) == ["b.pptx", "a.pdf", "c.xlsx"]

    def test_no_attachments_yields_no_names(self):
        assert _attachment_marker_names([], []) == []


class TestAttachmentGuidance:
    def test_names_the_deck_and_the_read_tool_when_enabled(self):
        deck = _Attachment("deck.pptx", PPTX_MIME)
        guidance = _build_attachment_guidance(
            [], [deck], [], ["create_powerpoint_presentation"]
        )

        assert "`deck.pptx`" in guidance
        assert "read_powerpoint_presentation" in guidance

    def test_tells_the_user_which_toggle_to_flip_when_disabled(self):
        # A diverted deck with no tool to read it is a dead end unless the
        # note names the toggle — the file is neither inline nor reachable.
        deck = _Attachment("deck.pptx", PPTX_MIME)
        guidance = _build_attachment_guidance([], [deck], [], ["some_other_tool"])

        assert "PowerPoint Presentations" in guidance
        assert "read_powerpoint_presentation" not in guidance

    @pytest.mark.parametrize("enabled_tools", [None, []])
    def test_no_enabled_tools_is_treated_as_disabled(self, enabled_tools):
        deck = _Attachment("deck.pptx", PPTX_MIME)
        guidance = _build_attachment_guidance([], [deck], [], enabled_tools)
        assert "PowerPoint Presentations" in guidance

    def test_silent_when_nothing_was_diverted(self):
        assert _build_attachment_guidance([], [], [], ["create_powerpoint_presentation"]) == ""

    def test_spreadsheet_and_deck_notes_coexist(self):
        # Both carve-outs can fire on one turn; neither may swallow the other.
        sheet = _Attachment("data.xlsx", XLSX_MIME)
        deck = _Attachment("deck.pptx", PPTX_MIME)
        guidance = _build_attachment_guidance(
            [sheet], [deck], [], ["analyze_spreadsheet", "create_powerpoint_presentation"]
        )

        assert "`data.xlsx`" in guidance
        assert "`deck.pptx`" in guidance
        assert "analyze_spreadsheet" in guidance
        assert "read_powerpoint_presentation" in guidance
