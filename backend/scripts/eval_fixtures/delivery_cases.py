"""Write ``evals/cases/attachment_delivery.json`` from the fixtures' planted facts.

The family's questions are the facts ``eval_fixtures.delivery`` plants, so the
case file is generated from that module rather than typed: a fixture whose
generator changes regenerates its questions with it, and a test
(``tests/test_eval_fixtures.py``) fails when the committed file and the
generated one differ.

    cd backend && uv run python scripts/eval_fixtures/delivery_cases.py --write

Every case runs the ``auto`` arm (unified-attachment-delivery §8.4): today's
turn path, deciding for itself. The forced arms and the ceiling arms come
once the planner's fixture-only ``delivery`` value exists; the cases already
carry ``run.arm`` and ``run.ceiling`` so that wave adds arms, not a schema.
"""

from __future__ import annotations

import argparse
import json
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
) -> Dict[str, Any]:
    return {
        "id": f"{FAMILY}.{name}",
        "family": FAMILY,
        "turns": [{"prompt": p} for p in prompts],
        "run": {**(run or _NO_TOOLS), "arm": "auto", "ceiling": "default"},
        "expect": expect,
        "files": list(files),
        "notes": notes,
    }


def _lookup(fact: d.Fact) -> Dict[str, Any]:
    return {"contains_any": [fact.value], "forbidden": [fact.lookalike]}


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
            {"contains_any": [cell.value], "forbidden": [cell.lookalike]},
            f"A ~30k-token table ({label.split('_')[0]}). The lookalike ticket is the same site one week later.",
            _SPREADSHEET_TOOLS,
        ))
    out.append(_case(
        "lookup_png", [
            f"According to the attached image, what is the operating permit number for {d.PERMIT_CARD_SUBJECT}? "
            "Reply with just the number."
        ], ["delivery_permit_card.png"],
        {"contains_any": [d.PERMIT_CARD_VALUE], "forbidden": [d.PERMIT_CARD_LOOKALIKE]},
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
            {"contains_all": [first.value, second.value], "forbidden": [first.lookalike, second.lookalike]},
            f"Each answer is in a different ~{size}-token document (Markdown and PDF).",
        ))

    # Absent fact: the facility is named, only its lookalike value is present.
    for size in ("30k", "150k"):
        doc = d.prose(f"delivery_prose_{size}.md")
        out.append(_case(
            f"absent_fact_{size}", [doc.absent.question()], [f"delivery_prose_{size}.md"],
            {"regex": {"search": ["NOT IN DOCUMENT"]}, "forbidden": [doc.absent.lookalike]},
            f"Hallucination guard at ~{size}: the backup permit is there and must not be offered as the "
            "operating one.",
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
        {"contains_any": [target.value], "forbidden": [target.lookalike, *others]},
        "Five ~5k-token documents, today's per-message cap. Each holds a certificate; only one is the subject's.",
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
