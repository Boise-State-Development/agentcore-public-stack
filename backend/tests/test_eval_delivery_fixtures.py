"""The attachment_delivery fixtures hold their planted facts at their sizes, as each reader sees them.

Unified-attachment-delivery spec §8.4: the same questions over documents that
differ in type and in size. A fixture that drifts from its stated size, loses
a fact, or plants its value twice would turn a delivery result into a fixture
result, so each is read back here the way the deployment reads it.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from eval_fixtures import build as b  # noqa: E402
from eval_fixtures import delivery as d  # noqa: E402
from eval_fixtures import delivery_cases  # noqa: E402

from apis.shared.evals.cases import load_cases  # noqa: E402
from apis.shared.files.models import INLINE_DOCUMENT_MAX_BYTES, is_presentation_file, is_tabular_file  # noqa: E402

CASES_DIR = Path(__file__).resolve().parents[1] / "evals" / "cases"
PROSE = sorted(d._PROSE)


def _read(name: str) -> str:
    data = b.build(name)
    suffix = Path(name).suffix
    if suffix in (".md", ".txt", ".csv"):
        return data.decode("utf-8")
    if suffix == ".html":
        from apis.shared.files.html_markdown import html_to_markdown

        return html_to_markdown(data.decode("utf-8"))
    if suffix == ".docx":
        from apis.shared.files.docx_markdown import docx_to_markdown

        return docx_to_markdown(data)
    if suffix == ".pdf":
        import pypdfium2

        document = pypdfium2.PdfDocument(data)
        return " ".join(page.get_textpage().get_text_bounded() for page in document)
    if suffix == ".pptx":
        archive = zipfile.ZipFile(io.BytesIO(data))
        return " ".join(archive.read(n).decode("utf-8") for n in archive.namelist() if n.startswith("ppt/slides/slide"))
    if suffix == ".xlsx":
        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(data))
        return "\n".join(",".join(str(c) for c in row) for row in book["Readings"].iter_rows(values_only=True))
    raise AssertionError(name)


def test_the_committed_case_file_is_the_generated_one():
    committed = (CASES_DIR / "attachment_delivery.json").read_text(encoding="utf-8")
    assert committed == delivery_cases.render(), "run scripts/eval_fixtures/delivery_cases.py --write"


def test_every_case_parses_and_names_only_delivery_fixtures():
    cases = load_cases(CASES_DIR, families=["attachment_delivery"])
    assert len(cases) >= 20
    assert {c.run.arm for c in cases} == {"auto", "native", "text", "on_demand", "search"}
    assert {c.run.ceiling for c in cases} == {"default", "200k"}
    assert {f for c in cases for f in c.all_files} == set(d.GENERATED)


def test_an_overflow_cell_grows_its_set_two_a_turn_and_asks_an_unnamed_earlier_document():
    cases = {c.id.split(".", 1)[1]: c for c in load_cases(CASES_DIR, families=["attachment_delivery"])}
    for size in delivery_cases.OVERFLOW_SIZES:
        pair = [cases[f"overflow_{size}_documents_{arm}"] for arm in delivery_cases.OVERFLOW_ARMS]
        assert [c.run.arm for c in pair] == ["on_demand", "search"]
        assert pair[0].turns == pair[1].turns and pair[0].expect == pair[1].expect
        case = pair[0]
        assert case.files == () and len(case.all_files) == size == len(set(case.all_files))
        assert all(len(case.meta_for(i).files) == 2 for i in range(size // 2))
        assert case.meta_for(len(case.turns) - 1).files == ()
        # The answer is in exactly one dossier, which the last prompt never names
        # and which was not attached on the last attaching turn (unless there is only one).
        _, _, target = delivery_cases.overflow_prompts(size)
        holders = [n for n in case.all_files if target.value in d.GENERATED[n]().decode("utf-8")]
        assert len(holders) == 1
        assert holders[0] not in case.turns[-1]
        if size > 2:
            assert holders[0] not in case.meta_for(size // 2 - 1).files
        # Every dossier alone is over Haiku 5.5's 25k document budget.
        for name in case.all_files:
            assert len(d.prose(name).text) // 4 * 1.40 > 25_000


def test_each_gating_file_runs_every_legal_arm():
    cases = {c.id.split(".", 1)[1]: c for c in load_cases(CASES_DIR, families=["attachment_delivery"])}
    for label in ("pdf_30k", "docx_30k", "html_30k"):
        assert {cases[f"arm_{label}_{arm}"].run.arm for arm in ("native", "text", "on_demand")} == {
            "native", "text", "on_demand"}


def test_a_ceiling_case_asks_the_late_fact_on_turn_six_after_the_tail_moves_on():
    cases = load_cases(CASES_DIR, families=["attachment_delivery"])
    ceiling = [c for c in cases if c.id.split(".", 1)[1].startswith("ceiling_")]
    # Three PDFs: native at two ceilings and on_demand at one, each at two paces.
    assert len(ceiling) == 3 * 3 * 2
    for case in ceiling:
        assert len(case.turns) == 6 and len(case.files) == 1 and case.files[0].endswith(".pdf")
        late = d.prose(case.files[0]).facts["late"]
        # Not asked before turn 6, and turn 1 is out of the 3-turn protected tail by then.
        assert late.subject in case.turns[-1] and not any(late.subject in t for t in case.turns[:-1])
        pauses = [case.meta_for(i).pause_seconds for i in range(6)]
        assert pauses == ([0, 0, 0, 0, delivery_cases.COLD_PAUSE_SECONDS, 0] if case.id.endswith("_cold") else [0] * 6)
        assert delivery_cases.COLD_PAUSE_SECONDS > 300
        if case.run.arm == "on_demand":
            assert case.run.ceiling == "default"


@pytest.mark.parametrize("name", PROSE)
def test_prose_is_its_stated_size_in_estimated_tokens(name):
    tokens = d.estimated_tokens(name)
    target = d._PROSE[name][1]
    assert 0.95 * target <= tokens <= 1.1 * target


@pytest.mark.parametrize("name", PROSE)
def test_each_fact_and_its_lookalike_appear_exactly_once_as_the_reader_sees_them(name):
    text = re.sub(r"\s+", " ", _read(name))
    doc = d.prose(name)
    for fact in doc.facts.values():
        assert text.count(fact.value) == 1, (name, fact.depth)
        assert text.count(fact.lookalike) == 1, (name, fact.depth)
    assert text.count(doc.absent.lookalike) == 1
    assert f"{doc.absent.attribute} for {doc.absent.subject}" not in text


@pytest.mark.parametrize("name", PROSE)
def test_facts_sit_at_their_depths(name):
    text = d.prose(name).text
    for fact in d.prose(name).facts.values():
        position = text.index(fact.sentence) / len(text)
        assert abs(position - d.DEPTHS[fact.depth]) < 0.06, (fact.depth, position)


def test_no_two_documents_share_a_subject():
    subjects = [f.subject for name in PROSE for f in d.prose(name).facts.values()]
    subjects += [d.prose(name).absent.subject for name in PROSE]
    assert len(subjects) == len(set(subjects))


@pytest.mark.parametrize("name", sorted(d._TABLES))
def test_the_planted_ticket_and_its_lookalike_are_in_the_table(name):
    cell = d.table_fact(name)
    rows = _read(name).splitlines()
    assert sum(cell.value in r for r in rows) == 1 and sum(cell.lookalike in r for r in rows) == 1
    (row,) = [r for r in rows if cell.value in r]
    assert row.startswith(f"{cell.site},{cell.week},")
    assert is_tabular_file(name, "")


def test_the_deck_goes_to_the_deck_tools_and_the_pdf_ladder_crosses_100_pages():
    import pypdfium2

    assert is_presentation_file("delivery_briefing_30k.pptx", "")
    pages = {n: len(pypdfium2.PdfDocument(b.build(n))) for n in d._PROSE if n.endswith(".pdf")}
    assert pages["delivery_report_80k.pdf"] < 100 < pages["delivery_report_150k.pdf"], pages


def test_every_fixture_stays_an_inline_class_file():
    # The auto arm measures today's routing by type and tokens, not the 4 MB reference rule.
    assert all(len(b.build(n)) <= INLINE_DOCUMENT_MAX_BYTES for n in d.GENERATED)


def test_case_answers_are_the_planted_values():
    by_id = {c["id"]: c for c in json.loads((CASES_DIR / "attachment_delivery.json").read_text())}
    fact = d.prose("delivery_prose_300k.md").facts["late"]
    assert by_id["attachment_delivery.lookup_md_300k"]["expect"] == {
        "contains_any": [fact.value], "regex": {"search": [delivery_cases.leads(fact.value, [fact.lookalike])]}}


@pytest.mark.parametrize("answer,passes", [
    ("WP-51759", True),
    ("**WP-51759**\n\nThe document also lists WP-51795 as the backup permit, kept for audit only.", True),
    ("WP-51795", False),
    ("The backup permit is WP-51795; the operating permit is WP-51759.", False),
    ("x" * 200 + " WP-51759", False),
])
def test_the_right_value_must_lead_the_answer_and_a_lookalike_after_it_is_fine(answer, passes):
    pattern = delivery_cases.leads("WP-51759", ["WP-51795"])
    assert bool(re.search(pattern, answer, re.MULTILINE)) is passes


# ---------------------------------------------------------------------------
# Whole-document questions (§11 Q3: where holding a document might win)
# ---------------------------------------------------------------------------


def _whole_cases():
    return {c.id.split(".", 1)[1]: c for c in load_cases(CASES_DIR, families=["attachment_delivery"])
            if c.id.split(".", 1)[1].startswith("whole_")}


def test_every_whole_document_question_runs_held_and_on_demand_at_three_sizes():
    cases = _whole_cases()
    assert len(cases) == 3 * 3 * 2
    for kind in ("list", "compare", "summarise"):
        for size in ("30k", "80k", "150k"):
            for arm in ("native", "on_demand"):
                case = cases[f"whole_{kind}_pdf_{size}_{arm}"]
                assert case.run.arm == arm and case.run.ceiling == "default" and len(case.turns) == 1


@pytest.mark.parametrize("name", sorted(d._WHOLE))
def test_whole_document_facts_are_planted_once_and_spread(name):
    doc, facts = d.whole(name)
    text = doc.text
    assert text.count("boil-water notice was issued") == 6 and text.count("boil-water notice was considered") == 1
    for site in facts.notices + (facts.decoy, facts.gauge) + tuple(s for s, _ in facts.incidents):
        assert text.count(site) == 1 or site == facts.gauge and text.count(site) == 2, site
    assert text.index("spring survey, the") < 0.2 * len(text) < 0.8 * len(text) < text.index("autumn survey, the")
    assert round(float(facts.spring_level) - float(facts.autumn_level), 1) == float(facts.change)
    assert 0.95 * d._WHOLE[name][1] <= d.estimated_tokens(name) <= 1.1 * d._WHOLE[name][1]
    assert d.GENERATED[name]().startswith(b"%PDF")


def test_whole_document_scoring_passes_a_right_answer_and_fails_the_decoy_and_the_misread():
    from apis.shared.evals.score import score_sample

    cases = _whole_cases()
    _, facts = d.whole("delivery_yearbook_30k.pdf")

    def passed(kind, text):
        return score_sample(cases[f"whole_{kind}_pdf_30k_native"].expect, final_message=text,
                            tool_calls=[], status="completed").passed

    sites = list(facts.notices)
    assert passed("list", "Notices went to " + ", ".join(sites) + ". Six in total.")
    assert not passed("list", "Notices went to " + ", ".join(sites + [facts.decoy]) + ". 7 in total.")
    assert not passed("list", "Notices went to " + ", ".join(sites[:-1]) + ". 5 in total.")
    assert passed("compare", f"It fell by {facts.change} metres, from {facts.spring_level} to {facts.autumn_level}.")
    assert not passed("compare", f"It rose by {facts.change} metres.")
    assert not passed("compare", f"It fell by {float(facts.change) + 1:.1f} metres.")
    names = [s for s, _ in facts.incidents]
    assert passed("summarise", "\n".join(f"- {s}: something happened" for s in names))
    assert not passed("summarise", "\n".join(f"- {s}: something happened" for s in names[:3]))
