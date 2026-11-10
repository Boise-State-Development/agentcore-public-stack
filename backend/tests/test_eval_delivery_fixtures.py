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


def test_every_case_parses_runs_the_auto_arm_and_names_only_delivery_fixtures():
    cases = load_cases(CASES_DIR, families=["attachment_delivery"])
    assert len(cases) >= 20
    assert {c.run.arm for c in cases} == {"auto"} and {c.run.ceiling for c in cases} == {"default"}
    assert {f for c in cases for f in c.files} == set(d.GENERATED)


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
