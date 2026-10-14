"""The large-document quality harness (scripts/large_document_quality_harness.py).

Hermetic: no AWS, no model. Pins what the comparison depends on:

- the fixtures are deterministic, and every one is reference-class (over 4 MB);
- each class has the property it exists to test, checked with the PRODUCTION
  text extraction: the text and docx facts are in the text layer, the scanned
  document has no text layer at all, and the layout document's tables come out
  with every label separated from its value;
- every question's distractors are really planted, and never overlap its answer;
- scoring: majority of k, paired per class, exact McNemar, no pooled score;
- the ask step resumes, classifies tool calls by mode, and sends the user
  message the chat route would build.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from large_document_quality import fixtures as fx  # noqa: E402
from large_document_quality.ask import Job, _mode, final_answer, jobs  # noqa: E402
from large_document_quality.score import compare, operations, outcomes  # noqa: E402

from apis.shared.files.document_read import _open_pdf, _pdf_page_text, docx_paragraphs  # noqa: E402
from apis.shared.files.models import FileMetadata, is_reference_document  # noqa: E402


@pytest.fixture(scope="module")
def built():
    return {f.name: f for f in fx.build_all(7)}


def _texts(fixture):
    if fixture.fmt == "docx":
        return docx_paragraphs(fixture.data)
    pdf = _open_pdf(fixture.data)
    try:
        return [_pdf_page_text(pdf, i) for i in range(len(pdf))]
    finally:
        pdf.close()


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).replace(",", "").lower()


class TestFixtures:
    def test_deterministic_per_seed(self):
        a, b = fx.build_brochure(7, pages=6), fx.build_brochure(7, pages=6)
        assert a.data == b.data
        assert fx.build_brochure(8, pages=6).data != a.data

    def test_every_document_is_attached_by_reference(self, built):
        for f in built.values():
            meta = FileMetadata(upload_id="u", user_id="x", session_id="s", filename=f.filename,
                                mime_type=f.mime_type, size_bytes=len(f.data), s3_key="k", s3_bucket="b")
            assert is_reference_document(meta), f.filename
        assert {f.doc_class for f in built.values()} == {"text", "scanned", "layout", "docx"}

    def test_text_and_docx_facts_are_in_the_text_layer_where_planted(self, built):
        for name in ("manual", "handbook"):
            texts = _texts(built[name])
            for q in built[name].questions:
                where = [i + 1 for i, t in enumerate(texts) if any(_flat(e) in _flat(t) for e in q.expected)]
                assert where, q.qid
                if q.page:
                    assert q.page in where, q.qid

    def test_the_scanned_document_has_no_text_layer(self, built):
        assert all(not t.strip() for t in _texts(built["scanned"]))

    def test_layout_tables_separate_each_label_from_its_value(self, built):
        texts = _texts(built["brochure"])
        pairs = {"layout-silver": ("Silver", "$61"), "layout-pool": ("Saturday", "7:45 pm"),
                 "layout-studio": ("Studio B", "$27")}
        for q in built["brochure"].questions:
            assert any(_flat(e) in _flat(texts[q.page - 1]) for e in q.expected), q.qid
            if q.qid in pairs:
                label, value = pairs[q.qid]
                lines = texts[q.page - 1].splitlines()
                assert not any(label in line and value in line for line in lines), q.qid

    def test_distractors_are_planted_and_never_the_answer(self, built):
        for f in built.values():
            if f.doc_class == "scanned":
                continue  # image-only; the scanned distractor is checked by construction below
            whole = _flat(" ".join(_texts(f)))
            for q in f.questions:
                for bad in q.forbidden:
                    assert _flat(bad) in whole, (q.qid, bad)
                    assert not any(_flat(bad) == _flat(e) for e in q.expected)
        assert any("96,300" in fact for fact in fx._SCANNED_FACTS.values())

    def test_a_literal_question_reuses_the_phrase_a_pattern_search_would_find(self, built):
        for f in built.values():
            for q in f.questions:
                if q.wording == "literal":
                    assert re.search(q.locator, q.question, re.I), q.qid


class TestAsk:
    def test_jobs_skip_what_is_done(self):
        q = fx._LAYOUT_QUESTIONS[:2]
        pending = jobs(q, ["tier1", "tier2"], 2, done={"tier1|layout-silver|0"})
        assert len(pending) == 7 and Job("tier1", q[0], 0) not in pending

    @pytest.mark.parametrize("tool_input,mode", [
        ({}, "list"),
        ({"query": "notice", "upload_id": "u"}, "query"),
        ({"query": "notice"}, "query"),
        ({"upload_id": "u", "page_range": "4-6", "pattern": "x"}, "page_range"),
        ({"upload_id": "u", "pattern": "notice"}, "pattern"),
        ({"upload_id": "u"}, "index"),
    ])
    def test_tool_calls_are_classified_by_mode(self, tool_input, mode):
        assert _mode(tool_input) == mode

    @pytest.mark.parametrize("reply,line", [
        ("Casual staff get 3 days, but you can carry over 11.\n\nAnswer: 11 days", "11 days"),
        ("Found it.\n**Answer:** $61 per month", "$61 per month"),
        ("Answer: first\nmore text\nAnswer: last", "last"),
        ("The document says 17 items.", None),
    ])
    def test_only_the_final_answer_line_is_scored(self, reply, line):
        assert final_answer(reply) == line

    def test_the_user_message_is_the_one_the_chat_route_builds(self, monkeypatch):
        from large_document_quality import ask
        from apis.shared.files import large_document
        from apis.shared.files.models import FileStatus

        meta = FileMetadata(
            upload_id="up-1", user_id="u", session_id="s", filename="staff-policy-manual.pdf",
            mime_type="application/pdf", size_bytes=7 * 1024 * 1024, s3_key="k", s3_bucket="b",
            status=FileStatus.READY, preparation=large_document.preparation_record(status="pending"),
        )

        class Repo:
            async def get_file(self, user_id, upload_id):
                return meta

        monkeypatch.setattr("apis.shared.files.repository.get_file_upload_repository", lambda: Repo())
        message = asyncio.run(ask.user_message(fx._MANUAL_QUESTIONS[0],
                                               {"userId": "u", "uploadId": "up-1"}))
        question, guidance, reference = message.split("\n\n")
        assert question == fx._MANUAL_QUESTIONS[0].question
        assert "call `document_read` with `upload_id` `up-1`" in guidance
        assert reference.startswith('<document-reference name="staff-policy-manual.pdf" upload_id="up-1"')


def _row(arm, qid, doc, correct, sample=0, wording="paraphrase", modes=("pattern",)):
    return {"arm": arm, "qid": qid, "doc": doc, "wording": wording, "sample": sample, "correct": correct,
            "answerLine": "x",
            "modes": list(modes), "usage": {"inputTokens": 1000}, "costUsd": 0.001, "seconds": 2.0, "error": None}


class TestScore:
    def test_majority_of_samples_and_ties_are_wrong(self):
        rows = [_row("tier1", "q", "scanned", c, s) for s, c in enumerate([True, True, False])]
        rows += [_row("tier2", "q", "scanned", c, s) for s, c in enumerate([True, False])]
        assert outcomes(rows) == {("tier1", "q"): True, ("tier2", "q"): False}

    def test_paired_per_class_with_n_and_no_pooled_row(self):
        rows = []
        for i in range(6):
            rows += [_row("tier1", f"s{i}", "scanned", False), _row("tier2", f"s{i}", "scanned", True)]
        rows += [_row("tier1", "t0", "manual", True, wording="literal"), _row("tier2", "t0", "manual", True,
                                                                             wording="literal")]
        table = {(r["class"], r["wording"]): r for r in compare(rows)}
        scanned = table[("scanned", "all")]
        assert scanned["n"] == 6 and scanned["tier1"] == 0 and scanned["tier2"] == 6
        assert scanned["onlyTreatment"] == 6 and scanned["p"] == pytest.approx(2 / 64)
        assert table[("text", "all")]["p"] == 1.0
        assert {r["class"] for r in compare(rows)} == {"scanned", "text"}

    def test_a_question_missing_from_one_arm_is_not_compared(self):
        rows = [_row("tier1", "q1", "brochure", True), _row("tier2", "q1", "brochure", False),
                _row("tier1", "q2", "brochure", True)]
        assert compare(rows)[0]["n"] == 1

    def test_operations_count_query_use(self):
        rows = [_row("tier2", "q", "scanned", True, modes=("query", "page_range")),
                _row("tier2", "q", "scanned", True, sample=1, modes=("pattern",))]
        (op,) = operations(rows)
        assert op["queryTurns"] == 1 and op["pageReadTurns"] == 1 and op["turns"] == 2


def test_fixtures_command_writes_documents_and_questions(tmp_path):
    import large_document_quality_harness as harness

    assert harness.main(["fixtures", "--out", str(tmp_path), "--docs", "brochure"]) == 0
    manifest = json.loads((tmp_path / "fixtures.json").read_text())
    assert [d["name"] for d in manifest["documents"]] == ["brochure"]
    assert (tmp_path / "documents" / "leisure-centre-guide.pdf").stat().st_size == manifest["documents"][0]["sizeBytes"]
    questions = [json.loads(line) for line in (tmp_path / "questions.jsonl").read_text().splitlines()]
    assert {q["doc"] for q in questions} == {"brochure"} and len(questions) == 5
