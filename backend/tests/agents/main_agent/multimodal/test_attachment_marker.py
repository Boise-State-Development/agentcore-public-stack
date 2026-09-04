"""The `[Attached files: …]` marker must name every attachment, not just inline ones.

The marker is the ONLY link between an uploaded file and the message it was
attached to once a session is reloaded. The SPA renders attachment cards from
a `fileAttachment` content block that it builds client-side at send time and
that is never persisted; on reload it reconstructs those blocks by parsing
this marker out of the message text and matching the names against
`GET /files?sessionId=…` (``restoreFileAttachments`` in message-map.service.ts).

So a filename missing from the marker is not a cosmetic problem — that file's
card silently disappears from the conversation on refresh, while the file
itself is still perfectly present in the session.

That is what happened when the presentation carve-out shipped: the marker was
derived from the inline set, decks are deliberately not in the inline set, and
a lone .pptx even took ``build_prompt``'s "no files → return the bare message"
early path, so it left no trace at all. Spreadsheets had the same gap from the
tabular carve-out.

Two invariants hold this together, and both are load-bearing:

* the marker names diverted attachments as well as inline ones;
* the marker stays at the very END of the text, because the SPA's
  ``ATTACHED_FILES_PATTERN`` is ``$``-anchored — anything appended after it
  makes the regex miss and the cards vanish just as completely.
"""

import re

from agents.main_agent.multimodal.prompt_builder import PromptBuilder

# Mirrors ATTACHED_FILES_PATTERN in
# frontend/ai.client/src/app/session/services/session/message-map.service.ts
SPA_MARKER_PATTERN = re.compile(r"\n\n\[Attached files: ([^\]]+)\]$")


def _text_of(result):
    """The prompt's text, whether build_prompt returned a str or blocks."""
    return result if isinstance(result, str) else result[0]["text"]


class TestMarkerNamesDivertedAttachments:
    def test_lone_diverted_deck_still_produces_a_marker(self):
        # The regression: nothing inline, so the old code returned the bare
        # message and the deck vanished from restored history.
        result = PromptBuilder().build_prompt(
            "Summarize this", files=None, attachment_names=["deck.pptx"]
        )
        assert _text_of(result) == "Summarize this\n\n[Attached files: deck.pptx]"

    def test_marker_covers_inline_and_diverted_together(self, sample_files):
        inline = sample_files[0]
        result = PromptBuilder().build_prompt(
            "Compare these",
            files=[inline],
            attachment_names=[inline.filename, "deck.pptx", "data.xlsx"],
        )
        names = SPA_MARKER_PATTERN.search(_text_of(result)).group(1)
        assert names == f"{inline.filename}, deck.pptx, data.xlsx"

    def test_diverted_names_do_not_become_content_blocks(self, sample_files):
        # The marker naming a deck must not smuggle it into the blocks — a
        # pptx document block is a Bedrock ValidationException.
        inline = sample_files[0]
        result = PromptBuilder().build_prompt(
            "Compare these",
            files=[inline],
            attachment_names=[inline.filename, "deck.pptx"],
        )
        assert isinstance(result, list)
        # One text block + exactly one block for the single inline file.
        assert len(result) == 2
        assert "deck.pptx" not in str(result[1])


class TestMarkerIsSpaParseable:
    def test_marker_is_last_so_the_anchored_regex_matches(self):
        result = PromptBuilder().build_prompt(
            "Question here", files=None, attachment_names=["a.pptx", "b.xlsx"]
        )
        match = SPA_MARKER_PATTERN.search(_text_of(result))
        assert match is not None
        assert match.group(1).split(", ") == ["a.pptx", "b.xlsx"]

    def test_stripping_the_marker_leaves_the_users_text(self):
        # The SPA removes the marker before display; what's left must be the
        # message, not a fragment of it.
        result = PromptBuilder().build_prompt(
            "What is in here?", files=None, attachment_names=["deck.pptx"]
        )
        assert SPA_MARKER_PATTERN.sub("", _text_of(result)) == "What is in here?"


class TestBackwardCompatibility:
    def test_omitting_attachment_names_still_derives_them_from_files(self, sample_files):
        # Existing callers pass positionally; behaviour must not shift.
        inline = sample_files[0]
        result = PromptBuilder().build_prompt("Describe this", [inline])
        assert SPA_MARKER_PATTERN.search(_text_of(result)).group(1) == inline.filename

    def test_no_files_and_no_names_returns_the_bare_message(self):
        result = PromptBuilder().build_prompt("Just talking")
        assert result == "Just talking"

    def test_empty_names_list_adds_no_marker(self):
        result = PromptBuilder().build_prompt(
            "Just talking", files=None, attachment_names=[]
        )
        assert result == "Just talking"
