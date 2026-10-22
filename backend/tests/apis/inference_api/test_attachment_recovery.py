"""Recovery of attachments a failed turn consumed without ever being read.

Inline document bytes are one-shot: `TurnBasedSessionManager._strip_document_bytes`
removes them from restored history (Bedrock rejects duplicate document names),
so a turn that dies before the model reads the documents loses them for good.

Prod session `5f34d2b0` (2026-08-31) is the incident this covers — a
ConverseStream carrying two PDFs failed with ServiceUnavailableException, the
PDFs were gone, and the model's only recourse was to ask the user to upload
them again. The invocations route now pops a write-ahead marker at turn start
and re-sends those uploads on the very next turn.
"""

from apis.inference_api.chat.routes import (
    _build_attachment_recovery_note,
    _select_recovered_attachments,
)


class TestSelectRecoveredAttachments:
    def test_re_attaches_when_the_turn_has_none_of_its_own(self):
        assert _select_recovered_attachments(["up-1", "up-2"]) == ["up-1", "up-2"]

    def test_nothing_recovered_is_a_noop(self):
        assert _select_recovered_attachments([]) == []

    def test_user_upload_ids_win(self):
        """Merging could push a deliberately-attached file past the 5-file
        resolver cap, and re-sending the same document twice is a Bedrock
        ValidationException."""
        assert _select_recovered_attachments(["up-1"], request_upload_ids=["fresh"]) == []

    def test_direct_file_content_wins(self):
        assert _select_recovered_attachments(["up-1"], request_files=[object()]) == []

    def test_empty_request_collections_do_not_count_as_attachments(self):
        assert _select_recovered_attachments(
            ["up-1"], request_upload_ids=[], request_files=[]
        ) == ["up-1"]

    def test_returns_a_copy(self):
        original = ["up-1"]
        result = _select_recovered_attachments(original)
        result.append("up-2")
        assert original == ["up-1"]


class TestBuildAttachmentRecoveryNote:
    def test_names_the_files_and_forbids_asking_for_a_re_upload(self):
        """The whole failure mode was the model telling the user to re-upload
        files the server already had."""
        note = _build_attachment_recovery_note(["a.pdf", "b.pdf"])
        assert note.startswith("<attachment_recovery_note>")
        assert note.endswith("</attachment_recovery_note>")
        assert "a.pdf" in note and "b.pdf" in note
        assert "do not ask the user to upload them" in note

    def test_says_the_user_did_not_re_attach_them(self):
        note = _build_attachment_recovery_note(["report.pdf"])
        assert "did not attach them again" in note
        assert "previous turn" in note
