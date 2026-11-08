"""Unplaced changes as ACTION NEEDED comments (docs/specs/word-tracked-changes.md §3b).

``if_unplaced="comment"`` keeps every guarantee of ``"fail"`` for the changes
it places: **a placed change is written exactly as a fail-mode run of the
placed changes alone writes it**, ids included, and Reject All still gives back
the original wording. Each change the document defeats becomes one comment, on
the paragraph it most likely meant, that begins "ACTION NEEDED —", says why in
one sentence and carries everything the change asked for. A problem with the
request itself still fails the call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple
from xml.etree import ElementTree

import pytest

from agents.builtin_tools.office.docx_redline import (
    ACTION_NEEDED,
    Change,
    NewParagraph,
    RedlineError,
    _unit_spans,
    redline,
)
from tests.shared.docx_fixtures import W_NS, build_docx, paragraph, part, run

W = f"{{{W_NS}}}"
NOW = datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc)
FIXTURE = (Path(__file__).parent / "fixtures" / "contract-numbered.docx").read_bytes()

HYPERLINK = paragraph(run("See "), '<w:hyperlink><w:r><w:t>our terms</w:t></w:r></w:hyperlink>', run(" online."))
CONTRACT = build_docx(
    paragraph(run("SERVICES AGREEMENT", bold=True)),
    paragraph(run("1. Payment. Invoices are payable within thirty (30) days of receipt.")),
    paragraph(run("2. Governing law. The laws of the State of Idaho govern this Agreement.")),
    paragraph(run("3. Term. The term is one year, renewable by the parties.")),
    paragraph(run("4. Notice. Notice of a breach is due within thirty (30) days of it.")),
    HYPERLINK,
)


def _run(docx: bytes, *changes: Change, mode: str = "comment"):
    return redline(docx, list(changes), "Pat Reviewer", now=NOW, if_unplaced=mode)


def _paragraph_text(p: ElementTree.Element) -> str:
    return "".join(t.text or "" for t in p.iter() if t.tag in (W + "t", W + "delText"))


def _comments(docx: bytes) -> Dict[str, str]:
    """Comment id → its text, one line per comment paragraph."""
    root = ElementTree.fromstring(part(docx, "word/comments.xml"))
    return {
        c.get(W + "id"): "\n".join(_paragraph_text(p) for p in c.iter(W + "p"))
        for c in root.iter(W + "comment")
    }


def _action_needed(docx: bytes) -> List[Tuple[str, str]]:
    """``(paragraph text, comment text)`` for each ACTION NEEDED comment, in document order."""
    texts = _comments(docx)
    out = []
    for p in ElementTree.fromstring(part(docx)).iter(W + "p"):
        for start in p.findall(W + "commentRangeStart"):
            text = texts[start.get(W + "id")]
            if text.startswith(ACTION_NEEDED):
                out.append((_paragraph_text(p), text))
    return out


def _only(result) -> Tuple[str, str]:
    docx = getattr(result, "docx", result)
    found = _action_needed(docx)
    assert len(found) == 1, found
    return found[0]


# ── Each kind of miss, on the right paragraph ────────────────────────────────


class TestWhereTheCommentGoes:
    def test_wording_found_nowhere_goes_on_the_paragraph_holding_most_of_it(self):
        where, text = _only(_run(CONTRACT, Change("payable within sixty (60) days of receipt", replace="payable on receipt")))
        assert where.startswith("1. Payment.")
        assert text.splitlines()[0] == f"{ACTION_NEEDED} The wording to change was not found in the document."

    def test_wording_with_nothing_in_common_goes_on_the_first_paragraph_with_words(self):
        where, _ = _only(_run(CONTRACT, Change("force majeure", comment="Add one?")))
        assert where == "SERVICES AGREEMENT"

    def test_different_capitalization_goes_on_that_paragraph(self):
        where, text = _only(_run(CONTRACT, Change("the laws of the state of idaho", replace="the laws of Utah")))
        assert where.startswith("2. Governing law.")
        assert "only with different capitalization" in text

    def test_a_find_spanning_paragraphs_goes_on_its_first_line(self):
        where, text = _only(_run(CONTRACT, Change("govern this Agreement.\n3. Term.", replace="govern it.")))
        assert where.startswith("2. Governing law.")
        assert "spans more than one paragraph" in text

    def test_an_ambiguous_find_goes_on_the_first_candidate(self):
        where, text = _only(_run(CONTRACT, Change("within thirty (30) days", replace="within ten (10) days")))
        assert where.startswith("1. Payment.")
        assert "appears 2 times" in text and "this comment is on the first" in text

    def test_an_occurrence_past_the_last_goes_on_the_last_candidate(self):
        where, text = _only(_run(CONTRACT, Change("within thirty (30) days", replace="within ten (10) days", occurrence=3)))
        assert where.startswith("4. Notice.")
        assert "asked for occurrence 3" in text and "on the last" in text

    def test_a_run_into_a_hyperlink_goes_on_its_paragraph(self):
        where, text = _only(_run(CONTRACT, Change("See our terms", replace="See their terms")))
        assert where == "See our terms online."
        assert "runs into a hyperlink, which this tool cannot change" in text

    def test_the_later_of_two_overlapping_changes_is_the_comment(self):
        result = _run(
            CONTRACT,
            Change("thirty (30) days of receipt", replace="forty (40) days of receipt"),
            Change("payable within thirty (30) days", replace="payable within sixty (60) days"),
        )
        where, text = _only(result.docx)
        assert where.startswith("1. Payment.")
        assert "overlaps another change to the same wording (change 1)" in text
        assert result.applied == ['1. deleted "thirty (30)", inserted "forty (40)"']
        assert [line.split(":")[0] for line in result.commented] == ['2. "payable within thirty (30) days"']

    def test_overlaps_resolve_by_request_order_not_position(self):
        # Change 1 sits later in the paragraph than change 2; change 2 still loses.
        result = _run(
            CONTRACT,
            Change("(30) days of receipt", replace="(45) days of receipt"),
            Change("within thirty (30)", replace="within forty-five (45)"),
            Change("(30) days of receipt", comment="Third, also overlapping."),
        )
        assert [line[:2] for line in result.applied] == ["1."]
        assert [line[:2] for line in result.commented] == ["2.", "3."]

    def test_an_insertion_whose_anchor_is_missing_goes_on_its_likely_paragraph(self):
        change = Change("Neither party is liable for any damages", insert="after", paragraphs=(NewParagraph("Survival"),))
        where, text = _only(_run(FIXTURE, change))
        assert where.startswith("Neither party shall be liable")
        assert "was not found" in text

    def test_a_like_found_nowhere_goes_on_the_anchor(self):
        change = Change(
            "consequential damages", insert="after", paragraphs=(NewParagraph("Survival", like="Heading that is not there"),)
        )
        where, text = _only(_run(FIXTURE, change))
        assert where.startswith("Neither party shall be liable")
        assert "whose formatting new paragraph 1 should copy was not found" in text

    def test_an_ambiguous_like_goes_on_the_anchor(self):
        change = Change("consequential damages", insert="after", paragraphs=(NewParagraph("Survival", like="the parties"),))
        where, text = _only(_run(FIXTURE, change))
        assert where.startswith("Neither party shall be liable")
        assert "appears in 2 paragraphs" in text

    def test_a_cell_end_anchor_with_another_format_goes_on_the_anchor(self):
        change = Change("Kickoff", insert="after", paragraphs=(NewParagraph("On signature.", like="Limitation of Liability"),))
        where, text = _only(_run(FIXTURE, change))
        assert where == "Kickoff"
        assert "is the last paragraph of its table cell" in text

    def test_an_anchor_whose_mark_is_revised_goes_on_the_anchor(self):
        marked = (
            '<w:p><w:pPr><w:rPr><w:ins w:id="5" w:author="Sponsor Counsel" w:date="2026-10-01T09:00:00Z"/>'
            '</w:rPr></w:pPr><w:r><w:t>Closing.</w:t></w:r></w:p>'
        )
        docx = build_docx(paragraph(run("Opening.")), marked)
        where, text = _only(_run(docx, Change("Closing", insert="after", paragraphs=(NewParagraph("More."),))))
        assert where == "Closing."
        assert "already a tracked change or a section break" in text

    def test_of_two_insertions_after_a_cell_end_only_the_offending_one_is_dropped(self):
        result = _run(
            FIXTURE,
            Change("Kickoff", insert="after", paragraphs=(NewParagraph("Second line."),)),
            Change("Kickoff", insert="after", paragraphs=(NewParagraph("Bad.", like="Limitation of Liability"),)),
        )
        assert [line[:2] for line in result.applied] == ["1."]
        assert [line[:2] for line in result.commented] == ["2."]


# ── What the comment says ────────────────────────────────────────────────────


class TestTheComment:
    def test_it_carries_the_reason_the_full_find_the_full_replace_and_the_note(self):
        find = "Invoices are payable within sixty (60) days of receipt of a correct invoice"
        replace = "Invoices are payable within forty-five (45) days of receipt of a correct and undisputed invoice"
        _, text = _only(_run(CONTRACT, Change(find, replace=replace, comment="Align with policy 4.2.")))
        assert text.split("\n") == [
            f"{ACTION_NEEDED} The wording to change was not found in the document.",
            f'Find: "{find}"',
            f'Replace with: "{replace}"',
            "Comment: Align with policy 4.2.",
        ]

    def test_a_deletion_says_so(self):
        _, text = _only(_run(CONTRACT, Change("renewable by both parties", replace="")))
        assert "Delete it." in text.split("\n")

    def test_a_comment_only_change_gives_its_note(self):
        _, text = _only(_run(CONTRACT, Change("indemnity", comment="Missing an indemnity clause.")))
        assert text.split("\n")[1:] == ['Find: "indemnity"', "Comment: Missing an indemnity clause."]

    def test_an_insertion_gives_each_new_paragraph(self):
        change = Change(
            "No such anchor wording",
            insert="before",
            paragraphs=(NewParagraph("Sections 3 and 4 survive.", lead="Survival."), NewParagraph("Second.")),
        )
        _, text = _only(_run(FIXTURE, change))
        assert text.split("\n")[1:] == [
            'Find: "No such anchor wording"',
            "Insert before that paragraph:",
            '"Survival. Sections 3 and 4 survive."',
            '"Second."',
        ]

    def test_it_is_written_by_the_reviewer_with_the_call_date(self):
        out = _run(CONTRACT, Change("force majeure", comment="Add one?")).docx
        comment = next(ElementTree.fromstring(part(out, "word/comments.xml")).iter(W + "comment"))
        assert comment.get(W + "author") == "Pat Reviewer"
        assert comment.get(W + "date") == "2026-11-08T09:00:00Z"

    def test_every_miss_comes_back_with_its_reason(self):
        result = _run(CONTRACT, Change("force majeure", comment="x"), Change("See our terms", replace="See their terms"))
        assert result.applied == []
        assert result.commented == [
            '1. "force majeure": The wording to change was not found in the document.',
            '2. "See our terms": The wording to change runs into a hyperlink, which this tool cannot change.',
        ]


# ── The caller's own problems still fail ─────────────────────────────────────


class TestCallerProblemsStillFail:
    @pytest.mark.parametrize(
        "change,problem",
        [
            (Change("  ", replace="x"), "Change 2: `find` is empty."),
            (Change("one year"), "Change 2: give `replace`"),
            (Change("one year", replace="one  year"), "Change 2: `replace` is the same wording as `find`."),
            (Change("Payment", insert="beside", paragraphs=(NewParagraph("x"),)), 'Change 2: `insert` must be "after"'),
            (Change("Payment", insert="after"), "Change 2: `insert` needs `paragraphs`"),
            (Change("Payment", insert="after", paragraphs=(NewParagraph(" "),)), "Change 2: new paragraph 1 has no `text`."),
        ],
    )
    def test_nothing_is_saved_and_only_the_callers_problems_are_listed(self, change, problem):
        with pytest.raises(RedlineError) as err:
            _run(CONTRACT, Change("force majeure", comment="a document miss"), change)
        assert len(err.value.problems) == 1
        assert err.value.problems[0].startswith(problem)

    def test_an_unknown_mode_is_refused(self):
        with pytest.raises(RedlineError) as err:
            _run(CONTRACT, Change("one year", replace="two years"), mode="skip")
        assert err.value.problems == ['`if_unplaced` must be "fail" or "comment".']

    def test_fail_mode_still_fails_on_a_document_miss(self):
        with pytest.raises(RedlineError) as err:
            _run(CONTRACT, Change("force majeure", comment="x"), mode="fail")
        assert err.value.problems[0].startswith('Change 1: "force majeure" was not found.')


# ── Placed changes are exactly what fail mode writes ─────────────────────────


def _strip_comments(xml: str, ids: List[str]) -> str:
    for cid in ids:
        xml = xml.replace(f'<w:commentRangeStart w:id="{cid}"/>', "")
        xml = xml.replace(f'<w:commentRangeEnd w:id="{cid}"/>', "")
        xml = xml.replace(f'<w:r><w:commentReference w:id="{cid}"/></w:r>', "")
    return xml


def _units(xml: str) -> List[str]:
    return [xml[a:b] for a, b in _unit_spans(xml, "w")]


GOOD = [
    Change("within thirty (30) days of receipt", replace="within forty-five (45) days of receipt", comment="Per policy."),
    Change("renewable by the parties", replace="renewable by mutual written agreement"),
    Change("SERVICES AGREEMENT", comment="Title checked."),
]
BAD = [
    Change("force majeure", comment="Add a force majeure clause."),  # → "SERVICES AGREEMENT", also edited
    Change("See our terms", replace="See their terms"),  # → the hyperlink paragraph, otherwise untouched
    Change("renewable by the parties", replace="renewable by all the parties"),  # meets a GOOD change
    Change("the laws of the state of idaho", replace="the laws of the State of Utah"),  # → paragraph 2
]


class TestPlacedChangesAreUnchanged:
    def _mixed(self):
        # Interleaved, so a placed change's position in the request moves.
        order = [BAD[0], GOOD[0], BAD[1], GOOD[1], BAD[2], GOOD[2], BAD[3]]
        return _run(CONTRACT, *order), _run(CONTRACT, *GOOD, mode="fail")

    def test_the_body_is_the_fail_mode_body_once_the_action_comments_are_taken_out(self):
        mixed, alone = self._mixed()
        action_ids = [cid for cid, text in _comments(mixed.docx).items() if text.startswith(ACTION_NEEDED)]
        assert len(action_ids) == 4
        assert _strip_comments(part(mixed.docx), action_ids) == part(alone.docx)

    def test_the_hyperlink_paragraph_differs_only_by_its_comment(self):
        mixed, alone = self._mixed()
        changed = [(a, b) for a, b in zip(_units(part(mixed.docx)), _units(part(alone.docx))) if a != b]
        # SERVICES AGREEMENT, payment and term carry placed edits in both; the
        # four action comments land on the title, payment, governing law and
        # the hyperlink paragraph, so only those can differ.
        assert len(changed) == 4
        assert all("ACTION" not in b for _, b in changed)

    def test_placed_comments_keep_their_ids_and_the_action_comments_follow(self):
        mixed, alone = self._mixed()
        placed, everything = _comments(alone.docx), _comments(mixed.docx)
        assert {cid: everything[cid] for cid in placed} == placed
        assert min(int(c) for c, t in everything.items() if t.startswith(ACTION_NEEDED)) > max(int(c) for c in placed)

    def test_reject_all_still_gives_back_every_paragraph(self):
        mixed, _ = self._mixed()
        before = [_paragraph_text(p) for p in ElementTree.fromstring(part(CONTRACT)).iter(W + "p")]
        after = []
        for p in ElementTree.fromstring(part(mixed.docx)).iter(W + "p"):
            after.append("".join(
                t.text or "" for t in p.iter()
                if t.tag in (W + "t", W + "delText") and not _inside_insertion(p, t)
            ))
        assert after == before

    def test_with_nothing_unplaced_comment_mode_is_fail_mode(self):
        assert _run(CONTRACT, *GOOD).docx == _run(CONTRACT, *GOOD, mode="fail").docx

    def test_insertions_place_identically_too(self):
        good = Change(
            "consequential damages",
            insert="after",
            paragraphs=(NewParagraph("Survival", like="Limitation of Liability"), NewParagraph("Sections 3 and 4 survive.")),
        )
        bad = Change("Kickoff", insert="after", paragraphs=(NewParagraph("x", like="Limitation of Liability"),))
        mixed = _run(FIXTURE, bad, good)
        alone = _run(FIXTURE, good, mode="fail")
        action_ids = [cid for cid, text in _comments(mixed.docx).items() if text.startswith(ACTION_NEEDED)]
        assert len(action_ids) == 1
        pairs = list(zip(_units(part(mixed.docx)), _units(part(alone.docx))))
        assert len(pairs) == len(_units(part(alone.docx)))
        # Only the paragraph holding the action comment is written anew (here
        # "Kickoff", which LibreOffice spelled with empty ``<w:rPr></w:rPr>``);
        # every other paragraph, the inserted ones included, is identical.
        hosts = [i for i, (a, _) in enumerate(pairs) if f'commentRangeStart w:id="{action_ids[0]}"' in a]
        assert len(hosts) == 1
        assert [a == b for i, (a, b) in enumerate(pairs) if i not in hosts] == [True] * (len(pairs) - 1)


def _inside_insertion(paragraph_el: ElementTree.Element, target: ElementTree.Element) -> bool:
    for ins in paragraph_el.iter(W + "ins"):
        if any(t is target for t in ins.iter()):
            return True
    return False
