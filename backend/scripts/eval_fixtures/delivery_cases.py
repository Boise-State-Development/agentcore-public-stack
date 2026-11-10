"""Write ``evals/cases/attachment_delivery.json`` from the fixtures' planted facts.

The family's questions are the facts ``eval_fixtures.delivery`` plants, so the
case file is generated from that module rather than typed: a fixture whose
generator changes regenerates its questions with it, and a test
(``tests/test_eval_fixtures.py``) fails when the committed file and the
generated one differ.

    cd backend && uv run python scripts/eval_fixtures/delivery_cases.py --write

Three groups (unified-attachment-delivery §8.4):

- the ``auto`` cases: today's turn path, deciding for itself, across type,
  size, count and question;
- the forced arms (``arm_*``): one planted fact asked under each arm that is
  legal for the file, the gating cells: ``native`` against ``text`` for pdf,
  docx and html (the risk that text loses layout), and both against
  ``on_demand``. ``search`` is reserved (``cases.RESERVED_ARMS``);
- the ceiling arms (``ceiling_*``): one PDF of ~30k to ~150k estimated tokens
  (about 1.35x that measured), held ``native`` or read ``on_demand``, over six
  turns: three lookups, two turns that do not touch the document, then a
  question it answers that was not asked before, after the document's turn
  has left the three-turn protected tail. Two paces: every turn inside the
  prompt-cache TTL (``warm``), and a pause past it before turn 5 (``cold``).
  ``on_demand`` holds no document, so it runs at the default ceiling only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval_fixtures import delivery as d  # noqa: E402

FAMILY = "attachment_delivery"
CASES_PATH = Path(__file__).resolve().parents[2] / "evals" / "cases" / f"{FAMILY}.json"

_NO_TOOLS: Dict[str, Any] = {"enabled_tools": [], "enabled_skills": []}
_SPREADSHEET_TOOLS: Dict[str, Any] = {"enabled_tools": ["list_spreadsheets", "analyze_spreadsheet"], "enabled_skills": []}
_DECK_TOOLS: Dict[str, Any] = {"enabled_tools": ["create_powerpoint_presentation"], "enabled_skills": []}


def _case(
    name: str,
    prompts: Sequence[str],
    files: Sequence[str],
    expect: Dict[str, Any],
    notes: str,
    run: Optional[Dict[str, Any]] = None,
    *,
    arm: str = "auto",
    ceiling: str = "default",
    pauses: Optional[Dict[int, int]] = None,
) -> Dict[str, Any]:
    pauses = pauses or {}
    return {
        "id": f"{FAMILY}.{name}",
        "family": FAMILY,
        "turns": [{"prompt": p, **({"pause_seconds": pauses[i]} if i in pauses else {})}
                  for i, p in enumerate(prompts)],
        "run": {**(run or _NO_TOOLS), "arm": arm, "ceiling": ceiling},
        "expect": expect,
        "files": list(files),
        "notes": notes,
    }


#: How far into the answer the asked-for value must appear.
LEAD_CHARS = 160


def leads(value: str, lookalikes: Sequence[str]) -> str:
    """A pattern: ``value`` within the answer's first ``LEAD_CHARS`` characters, no lookalike before it.

    Not ``forbidden``: a model that answers correctly and then says why it
    rejected the backup permit ("WP-51759; the document also lists WP-51795
    as the backup") read the document right, and a check that fails it
    measures phrasing, not delivery. What a misreading produces is the
    lookalike given as the answer, which puts it first.
    """
    guard = "".join(f"(?!{re.escape(v)})" for v in lookalikes)
    return rf"\A(?:{guard}[\s\S]){{0,{LEAD_CHARS}}}?{re.escape(value)}"


def _lookup(fact: d.Fact) -> Dict[str, Any]:
    return {"contains_any": [fact.value], "regex": {"search": [leads(fact.value, [fact.lookalike])]}}


def cases() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    # Size: one type (Markdown prose), the §8.4 ladder, the fact at 90% depth.
    for size in d.SIZES:
        name = f"delivery_prose_{size}.md"
        fact = d.prose(name).facts["late"]
        out.append(_case(
            f"lookup_md_{size}", [fact.question()], [name], _lookup(fact),
            f"Size ladder, ~{size} tokens of Markdown. The fact is at 90% depth; the backup-style lookalike "
            "is in another section.",
        ))

    # Type: the other formats at ~30k, PDF also up the ladder (its page count
    # crosses Bedrock's documented 100-page rule at ~150k).
    for name, label in (
        ("delivery_log_30k.txt", "txt_30k"), ("delivery_report_30k.pdf", "pdf_30k"),
        ("delivery_report_80k.pdf", "pdf_80k"), ("delivery_report_150k.pdf", "pdf_150k"),
        ("delivery_memo_30k.docx", "docx_30k"), ("delivery_bulletin_30k.html", "html_30k"),
    ):
        fact = d.prose(name).facts["late"]
        out.append(_case(f"lookup_{label}", [fact.question()], [name], _lookup(fact),
                         f"Type coverage: {label.replace('_', ', ~')} tokens. Fact at 90% depth."))
    deck = d.prose("delivery_briefing_30k.pptx").facts["late"]
    out.append(_case(
        "lookup_pptx_30k", [deck.question("the attached deck")], ["delivery_briefing_30k.pptx"], _lookup(deck),
        "A ~30k-token deck, one slide per paragraph. Decks go to the deck tools, never inline.", _DECK_TOOLS,
    ))
    for name, label in (("delivery_ledger_30k.csv", "csv_30k"), ("delivery_ledger_30k.xlsx", "xlsx_30k")):
        cell = d.table_fact(name)
        out.append(_case(
            f"lookup_{label}", [cell.question()], [name],
            {"contains_any": [cell.value], "regex": {"search": [leads(cell.value, [cell.lookalike])]}},
            f"A ~30k-token table ({label.split('_')[0]}). The lookalike ticket is the same site one week later.",
            _SPREADSHEET_TOOLS,
        ))
    out.append(_case(
        "lookup_png", [
            f"According to the attached image, what is the operating permit number for {d.PERMIT_CARD_SUBJECT}? "
            "Reply with just the number."
        ], ["delivery_permit_card.png"],
        {"contains_any": [d.PERMIT_CARD_VALUE],
         "regex": {"search": [leads(d.PERMIT_CARD_VALUE, [d.PERMIT_CARD_LOOKALIKE])]}},
        "A fact that exists only in pixels, beside its backup-permit lookalike.",
    ))

    # Cross-document: one fact from each of two documents of different types.
    for size in ("30k", "80k"):
        first = d.prose(f"delivery_prose_{size}.md").facts["early"]
        second = d.prose(f"delivery_report_{size}.pdf").facts["middle"]
        out.append(_case(
            f"cross_document_{size}", [
                f"Two documents are attached. What is the {first.attribute} for {first.subject}, and what is "
                f"the {second.attribute} for {second.subject}? Reply as: <permit>; <certificate>"
            ], [f"delivery_prose_{size}.md", f"delivery_report_{size}.pdf"],
            {"contains_all": [first.value, second.value],
             "regex": {"search": [leads(first.value, [first.lookalike]), leads(second.value, [second.lookalike])]}},
            f"Each answer is in a different ~{size}-token document (Markdown and PDF).",
        ))

    # Absent fact: the facility is named, only its lookalike value is present.
    for size in ("30k", "150k"):
        doc = d.prose(f"delivery_prose_{size}.md")
        out.append(_case(
            f"absent_fact_{size}", [doc.absent.question()], [f"delivery_prose_{size}.md"],
            {"regex": {"search": [leads("NOT IN DOCUMENT", [doc.absent.lookalike])]}},
            f"Hallucination guard at ~{size}: the backup permit is there and must not be offered as the "
            "operating one. The refusal must lead; naming the backup after it is fine.",
        ))

    # Follow-up: the document arrives on turn 1; turn 2 asks a fact turn 1 did not.
    for size in ("30k", "150k"):
        doc = d.prose(f"delivery_prose_{size}.md")
        early, late = doc.facts["early"], doc.facts["late"]
        out.append(_case(
            f"follow_up_{size}", [early.question(), late.question("the same document")],
            [f"delivery_prose_{size}.md"], _lookup(late),
            f"Turn 2 must still read the ~{size}-token document from the restored history (inside the cache "
            "TTL; the paced arm past it waits for the ceiling wave).",
        ))

    # Count: five documents, the answer in the fourth.
    notes = [f"delivery_note_{letter}_5k.md" for letter in "abcd"]
    target = d.prose(notes[2]).facts["middle"]
    others = [d.prose(n).facts["middle"].value for n in ("delivery_prose_5k.md", notes[0], notes[1], notes[3])]
    out.append(_case(
        "count_five_documents", [
            f"Five site notes are attached. What is the {target.attribute} for {target.subject}? "
            "Reply with just the number."
        ], ["delivery_prose_5k.md", *notes],
        {"contains_any": [target.value], "regex": {"search": [leads(target.value, [target.lookalike, *others])]}},
        "Five ~5k-token documents, today's per-message cap. Each holds a certificate; only one is the subject's.",
    ))
    out.extend(forced_arm_cases())
    out.extend(ceiling_cases())
    return out


#: The arms legal for each gating file, and the file. ``auto`` already covers
#: each file once (``lookup_*``); these name the mode instead of inheriting it.
_GATING: Dict[str, str] = {
    "pdf_30k": "delivery_report_30k.pdf",
    "docx_30k": "delivery_memo_30k.docx",
    "html_30k": "delivery_bulletin_30k.html",
}
_GATING_ARMS = ("native", "text", "on_demand")
#: Larger files where holding competes with reading on demand.
_ON_DEMAND_SIZES: Dict[str, str] = {"pdf_80k": "delivery_report_80k.pdf", "md_150k": "delivery_prose_150k.md"}


def forced_arm_cases() -> List[Dict[str, Any]]:
    """The gating cells: one late-depth lookup per file under each legal arm."""
    out: List[Dict[str, Any]] = []
    for label, name in _GATING.items():
        fact = d.prose(name).facts["late"]
        for arm in _GATING_ARMS:
            out.append(_case(
                f"arm_{label}_{arm}", [fact.question()], [name], _lookup(fact),
                f"Gating cell: {label.replace('_', ', ~')} tokens forced {arm}. Fact at 90% depth.", arm=arm,
            ))
    for label, name in _ON_DEMAND_SIZES.items():
        fact = d.prose(name).facts["late"]
        for arm in ("native", "on_demand"):
            out.append(_case(
                f"arm_{label}_{arm}", [fact.question()], [name], _lookup(fact),
                f"Holding against reading on demand at ~{label.split('_')[1]}: forced {arm}. Fact at 90% depth.",
                arm=arm,
            ))
    return out


#: The ceiling arms' document, by its size label.
CEILING_DOCUMENTS: Dict[str, str] = {
    "30k": "delivery_report_30k.pdf",
    "80k": "delivery_report_80k.pdf",
    "150k": "delivery_report_150k.pdf",
}
#: Past Bedrock's 5-minute prompt-cache TTL, with a margin for clock skew.
COLD_PAUSE_SECONDS = 330
#: Turns that do not touch the document, so its turn leaves the protected tail.
_ASIDES = (
    "Separate question, nothing to do with the document: write a two-sentence reminder asking field crews to "
    "log every inspection on the day it happens.",
    "Thanks. Now give me three short bullet points on why consistent inspection logs matter for a water "
    "utility. Keep it general.",
)


def ceiling_prompts(name: str) -> List[str]:
    """Six turns: three lookups, two asides, then the late fact nobody asked about yet."""
    doc = d.prose(name)
    early, middle, late = doc.facts["early"], doc.facts["middle"], doc.facts["late"]
    backup = (
        f"According to the same document, what is the {early.lookalike_attribute} for {early.subject}? "
        "Reply with just the number."
    )
    return [early.question(), middle.question("the same document"), backup, *_ASIDES,
            late.question("the document I attached at the start")]


def ceiling_cases() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    cells = [("native", c) for c in ("default", "200k", "100k_documents_excluded")] + [("on_demand", "default")]
    for size, name in CEILING_DOCUMENTS.items():
        late = d.prose(name).facts["late"]
        for arm, ceiling in cells:
            for pace in ("warm", "cold"):
                out.append(_case(
                    f"ceiling_pdf_{size}_{arm}_{ceiling}_{pace}", ceiling_prompts(name), [name], _lookup(late),
                    f"Ceiling arm: a ~{size}-token PDF held {arm} at the {ceiling} ceiling, {pace} pace. Turn 6 "
                    "asks the 90%-depth fact after the document's turn has left the protected tail"
                    + ("; turn 5 waits past the cache TTL." if pace == "cold" else "."),
                    arm=arm, ceiling=ceiling, pauses={4: COLD_PAUSE_SECONDS} if pace == "cold" else None,
                ))
    return out


def render() -> str:
    return json.dumps(cases(), indent=1, ensure_ascii=False) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help=f"write {CASES_PATH.name}; without it, print it")
    args = parser.parse_args(argv)
    if args.write:
        CASES_PATH.write_text(render(), encoding="utf-8")
        print(f"wrote {CASES_PATH} ({len(cases())} cases)")
    else:
        sys.stdout.write(render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
