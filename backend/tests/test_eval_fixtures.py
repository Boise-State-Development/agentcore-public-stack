"""The generated eval fixtures hold the facts their cases ask about, as each reader sees them.

``scripts/eval_fixtures`` builds every file a case names that is not
committed (eval-harness spec §15.4). These tests read each one back the way
the deployment does, so a fixture that stops carrying its planted answer, or
a case that drifts from its fixture, fails here rather than as a mystery
regression on dev.
"""

from __future__ import annotations

import io
import os
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from eval_fixtures import build as b  # noqa: E402

from apis.shared.evals.cases import load_cases  # noqa: E402
from apis.shared.evals.fixtures import mime_type_for  # noqa: E402
from apis.shared.files.models import (  # noqa: E402
    ALLOWED_EXTENSIONS,
    INLINE_ATTACHMENTS_MAX_TOTAL_BYTES,
    INLINE_DOCUMENT_MAX_BYTES,
    is_presentation_file,
    is_tabular_file,
)

BACKEND = Path(__file__).resolve().parents[1]
CASES_DIR = BACKEND / "evals" / "cases"
FIXTURES_DIR = BACKEND / "evals" / "fixtures"


def test_every_fixture_a_case_names_is_committed_or_generated():
    cases = load_cases(CASES_DIR, fixtures_dir=FIXTURES_DIR, generated=b.GENERATED)
    named = {name for case in cases for name in case.files}
    assert set(b.GENERATED) <= named, f"generated but never used: {sorted(set(b.GENERATED) - named)}"


@pytest.mark.parametrize("name", sorted(b.GENERATED))
def test_every_generated_fixture_is_an_uploadable_type_with_a_pinned_mime(name):
    suffix = Path(name).suffix
    assert suffix in ALLOWED_EXTENSIONS
    assert mime_type_for(name) == ALLOWED_EXTENSIONS[suffix]
    assert b.build(name)


@pytest.mark.parametrize("name", sorted(b.GENERATED))
def test_generated_bytes_are_deterministic(name):
    assert b.GENERATED[name]() == b.GENERATED[name]()


def test_a_workbook_is_the_same_bytes_a_second_later(monkeypatch):
    # openpyxl writes the current time into core.xml on save; stable_xlsx pins it.
    import datetime as real_datetime

    first = b.grant_expenses()

    class _Later(real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime.datetime(2031, 6, 1, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr("openpyxl.writer.excel.datetime.datetime", _Later)
    assert b.grant_expenses() == first


class TestWord:
    def test_the_blank_cell_keeps_quantity_in_its_column(self):
        from apis.shared.files.docx_markdown import docx_to_markdown

        text = docx_to_markdown(b.build("lab_equipment_inventory.docx"))
        assert re.search(rf"\| Microscope \| +\| {b.MICROSCOPE_QUANTITY} \| {b.MICROSCOPE_SPARE_PARTS} \|", text), text

    def test_tracked_changes_reach_the_model_as_deleted_and_inserted(self):
        from apis.shared.files.docx_markdown import docx_to_markdown

        text = docx_to_markdown(b.build("travel_policy_redline.docx"))
        assert f"[deleted: {b.DINNER_ALLOWANCE_STRUCK}][inserted: {b.DINNER_ALLOWANCE_NEW}]" in text


class TestPdf:
    @staticmethod
    def _text(name: str) -> str:
        import pypdfium2

        document = pypdfium2.PdfDocument(b.build(name))
        return " ".join(page.get_textpage().get_text_bounded() for page in document)

    def test_the_timetable_carries_both_departures(self):
        text = self._text("shuttle_schedule.pdf")
        assert b.LAST_WEEKDAY_SHUTTLE in text and b.LAST_WEEKEND_SHUTTLE in text

    def test_only_part_c_holds_the_retention_code(self):
        assert b.ARCHIVE_RETENTION_CODE in self._text("overflow_part_c.pdf")
        assert b.ARCHIVE_RETENTION_CODE not in self._text("overflow_part_a.pdf") + self._text("overflow_part_b.pdf")

    def test_two_parts_fit_the_inline_budget_and_the_third_does_not(self):
        sizes = [len(b.build(f"overflow_part_{x}.pdf")) for x in "abc"]
        assert all(size <= INLINE_DOCUMENT_MAX_BYTES for size in sizes), "each part must stay an inline-class file"
        assert sizes[0] + sizes[1] <= INLINE_ATTACHMENTS_MAX_TOTAL_BYTES < sum(sizes)


class TestSpreadsheets:
    def _rows(self, sheet: str):
        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(b.build("grant_expenses.xlsx")))
        return list(book[sheet].iter_rows(min_row=2, values_only=True))

    def test_is_diverted_to_the_spreadsheet_tools(self):
        assert is_tabular_file("grant_expenses.xlsx", mime_type_for("grant_expenses.xlsx"))
        assert is_tabular_file("sensor_readings.csv", mime_type_for("sensor_readings.csv"))

    def test_travel_total_and_its_lookalike(self):
        total = sum(amount for _, category, amount in self._rows("Expenses") if category == "Travel")
        assert f"{total:,}" == b.TRAVEL_TOTAL != b.TRAVEL_LOOKALIKE

    def test_equipment_lines_over_1000(self):
        count = sum(1 for _, category, amount in self._rows("Expenses") if category == "Equipment" and amount > 1000)
        assert str(count) == b.EQUIPMENT_OVER_1000

    def test_the_approver_is_only_on_the_second_sheet(self):
        assert (b.APPROVER_LINE, b.APPROVER_CODE) in self._rows("Approvals")
        assert all(b.APPROVER_CODE not in map(str, row) for row in self._rows("Expenses"))

    def test_sensor_s3_peak(self):
        rows = [line.split(",") for line in b.build("sensor_readings.csv").decode().splitlines()[1:]]
        assert str(max(float(r[2]) for r in rows if r[0] == "S-3")) == b.SENSOR_S3_MAX


class TestDeck:
    """python-pptx reads the deck inside Code Interpreter; it is not a backend
    dependency, so the package's structure is pinned here instead: every
    relationship resolves, every part has a content type, and the facts sit
    on the slide and in the notes the cases name."""

    @staticmethod
    def _parts():
        archive = zipfile.ZipFile(io.BytesIO(b.build("quarterly_review.pptx")))
        return {name: archive.read(name).decode() for name in archive.namelist()}

    def test_is_diverted_to_the_powerpoint_tools(self):
        assert is_presentation_file("quarterly_review.pptx", mime_type_for("quarterly_review.pptx"))

    def test_every_relationship_target_exists(self):
        parts = self._parts()
        for name, text in parts.items():
            if not name.endswith(".rels"):
                continue
            base = Path(name).parent.parent
            for rel in ElementTree.fromstring(text):
                target = os.path.normpath(base / rel.get("Target")).lstrip("./")
                assert target in parts, f"{name} points at missing {target}"

    def test_every_xml_part_has_a_content_type(self):
        parts = self._parts()
        types = ElementTree.fromstring(parts["[Content_Types].xml"])
        overrides = {o.get("PartName").lstrip("/") for o in types if o.get("PartName")}
        for name in parts:
            if name.endswith(".xml") and name != "[Content_Types].xml":
                assert name in overrides, name

    def test_every_part_is_well_formed(self):
        for name, text in self._parts().items():
            ElementTree.fromstring(text)

    def test_the_slide_and_the_notes_hold_their_facts(self):
        parts = self._parts()
        assert f"Pilot sites this quarter: {b.PILOT_SITES}" in parts["ppt/slides/slide3.xml"]
        assert b.CONTINGENCY_PERCENT in parts["ppt/notesSlides/notesSlide2.xml"]
        assert all(b.CONTINGENCY_PERCENT not in parts[f"ppt/slides/slide{n}.xml"] for n in (1, 2, 3))


def test_the_images_are_pngs():
    from PIL import Image

    for name in ("enrollment_chart.png", "sync_error.png", "harvest_week1.png", "harvest_week2.png"):
        with Image.open(io.BytesIO(b.build(name))) as image:
            assert image.format == "PNG" and image.size[0] >= 800


class TestEntities:
    def test_every_case_names_a_defined_fixture(self):
        from eval_fixtures import entities

        for case in load_cases(CASES_DIR):
            if case.run.project:
                assert case.run.project in entities.PROJECTS, case.id
            if case.run.agent:
                assert case.run.agent in entities.AGENTS, case.id

    def test_digests_are_stable_and_distinct(self):
        from eval_fixtures import entities

        fixtures = [*entities.PROJECTS.values(), *entities.AGENTS.values()]
        digests = [entities.digest(f) for f in fixtures]
        assert digests == [entities.digest(f) for f in fixtures]
        assert len(set(digests)) == len(digests)

    def test_the_full_context_fixtures_sit_either_side_of_the_deployed_budget(self):
        from eval_fixtures import entities

        from apis.shared.files.document_tokens import CHARS_PER_TOKEN

        def tokens(fx):
            return sum(len(f.build()) for f in fx.files) // CHARS_PER_TOKEN

        # Dev and prod set the budget to 75,000 (#1915); the code default is 50,000.
        assert tokens(entities.PROJECTS["kb_small"]) < 50_000
        assert tokens(entities.PROJECTS["kb_large"]) > 75_000
        assert tokens(entities.AGENTS["agent_loader"]) > 75_000

    def test_the_large_handbooks_carry_their_planted_facts(self):
        from eval_fixtures import entities as e

        text = {f.name: f.build().decode() for f in e.PROJECTS["kb_large"].files}
        assert f"at least {e.INTL_TRAVEL_DAYS} days" in text["travel_handbook.md"]
        assert f"every {e.ACCESS_CODE_ROTATION_V2} days" in text["facilities_handbook.md"]
        assert f"every {e.ACCESS_CODE_ROTATION_V1} days" not in text["facilities_handbook.md"]
        assert f"## Section 7: {e.HR_SECTION_7_TITLE}" in text["hr_handbook.md"]
        old = e.PROJECTS["kb_large"].superseded[0].build().decode()
        assert f"every {e.ACCESS_CODE_ROTATION_V1} days" in old

    def test_long_instructions_are_over_8_kib(self):
        from eval_fixtures import entities

        assert len(entities.AGENTS["agent_long_instructions"].instructions.encode()) > 8 * 1024
