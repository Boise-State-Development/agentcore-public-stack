"""The generated eval fixtures: every file a case names that is not committed under ``evals/fixtures/``.

Each entry of :data:`GENERATED` is a filename and a function that builds its
bytes. Everything is synthetic (eval-harness spec D6): invented buildings,
codes and numbers, and no real person, organisation or identifier. The facts a
case asks about are module constants, so a case file and its fixture cannot
drift without a test noticing (``tests/test_eval_fixtures.py``).

Each fixture plants its answer beside a lookalike wrong value, the way the
committed fixtures do, so a case can forbid the value a misreading produces:
the bench count in the column left of a blank cell, the struck-out per-diem,
the weekend timetable, last year's rate sheet.
"""

from __future__ import annotations

import io
import random
from typing import Callable, Dict, List

from large_document_quality import pdf

from eval_fixtures import images, ooxml

# ---------------------------------------------------------------------------
# Planted facts
# ---------------------------------------------------------------------------

#: lab_equipment_inventory.docx: microscopes' Quantity sits right of a blank
#: "Benches" cell. A renderer that drops blank cells shifts every value one
#: column left, and the Quantity column then reads the spare-parts count.
MICROSCOPE_QUANTITY = "6"
MICROSCOPE_SPARE_PARTS = "14"

#: travel_policy_redline.docx: the dinner allowance after tracked changes.
DINNER_ALLOWANCE_NEW = "58"
DINNER_ALLOWANCE_STRUCK = "52"

#: shuttle_schedule.pdf: the last weekday and weekend departures.
LAST_WEEKDAY_SHUTTLE = "21:40"
LAST_WEEKEND_SHUTTLE = "19:15"

#: parking_notice.html: the semester permit fee and the annual one beside it.
SEMESTER_PERMIT_FEE = "$118"
ANNUAL_PERMIT_FEE = "$181"

#: room_rates_2025.html / room_rates_2026.html: the Lakeview room's day rate in each.
LAKEVIEW_RATE_2026 = "$240"
LAKEVIEW_RATE_2025 = "$210"

#: overflow_part_c.pdf: the one fact in the part that goes by reference.
ARCHIVE_RETENTION_CODE = "RX-4417"

#: enrollment_chart.png: the three bars.
ENROLLMENT = (("2023", 1240), ("2024", 1315), ("2025", 1402))

#: sync_error.png and it_runbook.md: the code on the dialog, and the runbook step for it.
SYNC_ERROR_CODE = "0x80070AB3"
SYNC_ERROR_STEP = "STEP-K9"

#: harvest_week1.png / harvest_week2.png: the one crop whose count rose.
HARVEST_WEEK1 = (("Apples", 12), ("Pears", 30), ("Plums", 9))
HARVEST_WEEK2 = (("Apples", 12), ("Pears", 22), ("Plums", 17))
HARVEST_RISER = "Plums"

#: grant_expenses.xlsx, sheet "Expenses": the Travel total, a lookalike, the
#: count of Equipment lines over $1,000, and on sheet "Approvals" one line's approver.
TRAVEL_TOTAL = "4,815"
TRAVEL_LOOKALIKE = "4,185"
EQUIPMENT_OVER_1000 = "3"
APPROVER_LINE = "EXP-0042"
APPROVER_CODE = "AP-77"

#: sensor_readings.csv: sensor S-3's highest reading, and its digits swapped.
SENSOR_S3_MAX = "41.7"
SENSOR_S3_SWAPPED = "47.1"

#: quarterly_review.pptx: a fact on slide 3 and one only in slide 2's speaker notes.
PILOT_SITES = "14"
CONTINGENCY_PERCENT = "7.5"

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def lab_equipment_inventory() -> bytes:
    return ooxml.docx([
        "Harrowfield Field Station: lab equipment inventory, spring term.",
        "Quantity is the number of working units. Benches is left blank where the item is not bench-mounted.",
        ooxml.Table([
            ("Item", "Benches", "Quantity", "Spare parts"),
            ("Centrifuge", "2", "3", "5"),
            ("Microscope", "", MICROSCOPE_QUANTITY, MICROSCOPE_SPARE_PARTS),
            ("Fume hood", "4", "4", "2"),
            ("Incubator", "", "2", "9"),
        ]),
    ])


def travel_policy_redline() -> bytes:
    return ooxml.docx([
        "Travel policy, draft 3, with the reviewer's tracked changes.",
        ["Breakfast allowance: $18 per day."],
        ["Dinner allowance: $", ooxml.Tracked(deleted=DINNER_ALLOWANCE_STRUCK, inserted=DINNER_ALLOWANCE_NEW), " per day."],
        ["Mileage is reimbursed at the published rate for the travel date."],
    ])


def _text_pdf(heading: str, paragraphs: List[str], *, padding_bytes: int = 0, seed: int = 0) -> bytes:
    page = pdf.Page()
    page.text(72, 720, heading, size=16, bold=True)
    y = 690.0
    for paragraph in paragraphs:
        y = page.paragraph(72, y, paragraph, width=468) - 10
    if padding_bytes:
        # An image of noise drawn in a corner: it adds bytes, never words, so a
        # set of these crosses the 7 MB inline budget without filling the context.
        side = int((padding_bytes / 3) ** 0.5) + 1
        noise = random.Random(seed).randbytes(side * side * 3)
        page.image(pdf.Image(side, side, noise), 560, 20, 24, 24)
    return pdf.build([page])


def shuttle_schedule() -> bytes:
    return _text_pdf("Campus shuttle timetable", [
        "Weekday service runs from 06:30. The last weekday departure from the Library stop is "
        f"{LAST_WEEKDAY_SHUTTLE}.",
        f"Weekend service runs from 09:00. The last weekend departure from the Library stop is {LAST_WEEKEND_SHUTTLE}.",
        "Service does not run on university holidays.",
    ])


#: Each overflow part is about 2.45 MB: two fit the 7 MB inline budget, the third does not.
OVERFLOW_PART_BYTES = 2_450_000


def overflow_part(letter: str) -> Callable[[], bytes]:
    facts = {
        "a": "Part A covers the archive's opening hours, which are 08:00 to 17:00 on weekdays.",
        "b": "Part B covers request forms. Requests are answered within five working days.",
        "c": f"Part C covers retention. Records under this policy carry the retention code {ARCHIVE_RETENTION_CODE}.",
    }

    def build() -> bytes:
        return _text_pdf(f"Records archive handbook, part {letter.upper()}", [facts[letter]],
                         padding_bytes=OVERFLOW_PART_BYTES, seed=ord(letter))

    return build


def parking_notice() -> bytes:
    return (
        "<!doctype html><html><head><title>Parking permits</title></head><body>"
        "<h1>Parking permits, 2026</h1>"
        f"<p>A semester permit costs <strong>{SEMESTER_PERMIT_FEE}</strong>. An annual permit costs "
        f"<strong>{ANNUAL_PERMIT_FEE}</strong> and covers the summer session.</p>"
        "<p>Permits are valid in the North and East lots only.</p></body></html>"
    ).encode("utf-8")


def room_rates(year: str, lakeview: str, cedar: str) -> Callable[[], bytes]:
    def build() -> bytes:
        return (
            f"<!doctype html><html><head><title>Room rates {year}</title></head><body>"
            f"<h1>Conference room day rates, {year}</h1><table>"
            "<tr><th>Room</th><th>Day rate</th></tr>"
            f"<tr><td>Lakeview</td><td>{lakeview}</td></tr><tr><td>Cedar</td><td>{cedar}</td></tr>"
            "</table></body></html>"
        ).encode("utf-8")

    return build


def it_runbook() -> bytes:
    return (
        "# Desktop sync runbook\n\n"
        "Look up the error code shown in the dialog, then follow its step.\n\n"
        "| Error code | Step |\n|---|---|\n"
        "| 0x80070AB1 | STEP-K2: restart the sync client |\n"
        f"| {SYNC_ERROR_CODE} | {SYNC_ERROR_STEP}: re-link the folder from the sync settings |\n"
        "| 0x80070AC4 | STEP-M3: free disk space and retry |\n"
    ).encode("utf-8")


def grant_expenses() -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    expenses = book.active
    expenses.title = "Expenses"
    expenses.append(["Line", "Category", "Amount"])
    lines = [
        ("EXP-0040", "Travel", 1210), ("EXP-0041", "Equipment", 2400), ("EXP-0042", "Travel", 905),
        ("EXP-0043", "Supplies", 310), ("EXP-0044", "Equipment", 1500), ("EXP-0045", "Travel", 2700),
        ("EXP-0046", "Equipment", 640), ("EXP-0047", "Equipment", 1180), ("EXP-0048", "Supplies", 95),
    ]
    for line in lines:
        expenses.append(list(line))
    approvals = book.create_sheet("Approvals")
    approvals.append(["Line", "Approver code"])
    for line, _, _ in lines:
        approvals.append([line, APPROVER_CODE if line == APPROVER_LINE else f"AP-{10 + int(line[-2:])}"])
    book.properties.creator = "eval-fixtures"
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def sensor_readings() -> bytes:
    rows = ["sensor,hour,reading"]
    readings = {"S-1": (38.2, 39.0, 40.4), "S-2": (44.0, 46.3, 45.1), "S-3": (39.8, float(SENSOR_S3_MAX), 40.9)}
    for sensor, values in readings.items():
        for hour, value in enumerate(values, start=8):
            rows.append(f"{sensor},{hour:02d}:00,{value}")
    return ("\n".join(rows) + "\n").encode("utf-8")


def quarterly_review() -> bytes:
    return ooxml.pptx([
        ooxml.Slide("Quarterly program review", ["Field operations", "Spring term"]),
        ooxml.Slide("Budget", ["Spend is on plan for the quarter."],
                    notes=f"Mention that the budget holds a contingency of {CONTINGENCY_PERCENT} percent."),
        ooxml.Slide("Pilot", [f"Pilot sites this quarter: {PILOT_SITES}", "Two sites joined in March."]),
    ])


GENERATED: Dict[str, Callable[[], bytes]] = {
    "lab_equipment_inventory.docx": lab_equipment_inventory,
    "travel_policy_redline.docx": travel_policy_redline,
    "shuttle_schedule.pdf": shuttle_schedule,
    "overflow_part_a.pdf": overflow_part("a"),
    "overflow_part_b.pdf": overflow_part("b"),
    "overflow_part_c.pdf": overflow_part("c"),
    "parking_notice.html": parking_notice,
    "room_rates_2025.html": room_rates("2025", LAKEVIEW_RATE_2025, "$150"),
    "room_rates_2026.html": room_rates("2026", LAKEVIEW_RATE_2026, "$165"),
    "it_runbook.md": it_runbook,
    "enrollment_chart.png": lambda: images.bar_chart("Fall enrollment by year", ENROLLMENT),
    "sync_error.png": lambda: images.dialog("Desktop Sync", [
        f"Error {SYNC_ERROR_CODE}: sync failed for the folder Lab-Data.",
        "The folder could not be reached.",
    ]),
    "harvest_week1.png": lambda: images.bar_chart("Orchard harvest, week 1 (crates)", HARVEST_WEEK1),
    "harvest_week2.png": lambda: images.bar_chart("Orchard harvest, week 2 (crates)", HARVEST_WEEK2),
    "grant_expenses.xlsx": grant_expenses,
    "sensor_readings.csv": sensor_readings,
    "quarterly_review.pptx": quarterly_review,
}

_CACHE: Dict[str, bytes] = {}


def build(name: str) -> bytes:
    """The bytes of generated fixture ``name``, built once per process."""
    if name not in _CACHE:
        _CACHE[name] = GENERATED[name]()
    return _CACHE[name]
