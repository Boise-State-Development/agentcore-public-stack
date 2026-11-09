"""The seeded eval fixtures: projects and agents the harness runs cases against (eval-harness spec D13).

``seed_eval_fixtures.py`` creates each one as the eval user, named
``[eval] <name>``, and records :func:`digest` of its definition and files in its
description. A case names a fixture by its short name (``run.project`` /
``run.agent``), and the harness resolves the name to an id by listing the eval
user's own projects and agents. A definition that changes changes its digest,
and the next seed replaces that fixture; the families using it re-promote their
baselines, because a fixture change is a case change.

Everything is synthetic (spec D6). The large handbooks are generated prose with
the planted facts in named sections, so retrieval has to find a needle in
about 120k tokens of plausible text, not in an empty file.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from eval_fixtures import build, ooxml

#: Prefix of every fixture's name, so the seed can tell its fixtures from anything else the eval user owns.
NAME_PREFIX = "[eval] "
#: Prefix of the digest in a fixture's description.
DIGEST_PREFIX = "eval-fixture sha256:"

# ---------------------------------------------------------------------------
# Planted facts
# ---------------------------------------------------------------------------

#: kb_small
SAFETY_TRAINING_DAYS = "10"
VISITOR_ORIENTATION_DAYS = "2"
TRIPOD_LOAN_DAYS = "7"
TRIPOD_LATE_FEE = "$3"
SPRING_TRIP_DATE = "April 22"
FALL_TRIP_DATE = "October 9"
GLASSWARE_BUDGET = "1,940"
KB_SMALL_SIGN_OFF = "Source: project files"

#: kb_large
INTL_TRAVEL_DAYS = "45"
DOMESTIC_TRAVEL_DAYS = "21"
QUOTES_ABOVE_THRESHOLD = "three"
GRANTS_COMMITTEE_DAY = "Tuesday"
IT_BOARD_DAY = "Thursday"
ACCESS_CODE_ROTATION_V2 = "30"
ACCESS_CODE_ROTATION_V1 = "60"
HR_SECTION_7_TITLE = "Leave Requests"
KB_LARGE_FILES = (
    "grants_handbook.md", "travel_handbook.md", "procurement_handbook.md",
    "hr_handbook.md", "facilities_handbook.md", "it_handbook.md",
)

#: agents
FORMAT_KEY = "answer"
LONG_INSTRUCTIONS_TOKEN = "ZQ-END"
LOADER_PREFIX = "Per process:"
AGENT_KNOWLEDGE_HOURS = "07:30"
AGENT_KNOWLEDGE_DISTRACTOR = "09:30"

# ---------------------------------------------------------------------------
# Generated prose
# ---------------------------------------------------------------------------

_SUBJECTS = (
    "The office", "Each department", "A program lead", "The review team", "Staff", "A coordinator",
    "The administrator", "Every unit", "The finance partner", "The approver",
)
_VERBS = (
    "records", "reviews", "confirms", "documents", "checks", "tracks", "updates", "files", "verifies", "notes",
)
_OBJECTS = (
    "the request history", "routine changes", "the quarterly summary", "supporting records", "the shared calendar",
    "the standard checklist", "open items", "the approval trail", "recurring tasks", "the reference copy",
)
_TAILS = (
    "before the end of each month.", "as part of the regular cycle.", "when the work is complete.",
    "so that the next reviewer can follow it.", "in the shared folder.", "without changing earlier entries.",
    "once the owner has signed off.", "using the current template.", "at the next scheduled check.",
    "and keeps a copy for audit.",
)


def _filler(rng: random.Random, sentences: int) -> str:
    return " ".join(
        f"{rng.choice(_SUBJECTS)} {rng.choice(_VERBS)} {rng.choice(_OBJECTS)} {rng.choice(_TAILS)}"
        for _ in range(sentences)
    )


def handbook(title: str, topic: str, planted: Dict[int, str], *, seed: int, sections: int = 12,
             paragraphs: int = 16, named: Optional[Dict[int, str]] = None) -> bytes:
    """A markdown handbook of numbered sections of generated prose, with ``planted`` sentences.

    ``planted`` maps a section number to a sentence placed in the middle of that
    section; ``named`` gives some sections a fixed title (the rest are
    "<topic> procedures N"). At the defaults a handbook is about 20k tokens.
    """
    rng = random.Random(seed)
    named = named or {}
    out = [f"# {title}\n"]
    for number in range(1, sections + 1):
        out.append(f"\n## Section {number}: {named.get(number, f'{topic} procedures {number}')}\n")
        for p in range(paragraphs):
            text = _filler(rng, 7)
            if number in planted and p == paragraphs // 2:
                text = f"{text} {planted[number]}"
            out.append(f"\n{text}\n")
    return "".join(out).encode("utf-8")


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _onboarding_policy() -> bytes:
    return (
        "# Field station onboarding\n\n"
        f"New lab members complete the safety training within {SAFETY_TRAINING_DAYS} working days of their start date.\n\n"
        f"Day visitors complete a short orientation within {VISITOR_ORIENTATION_DAYS} working days of booking.\n\n"
        "Keys are issued after the training record is filed.\n"
    ).encode("utf-8")


def _equipment_loans() -> bytes:
    return ooxml.docx([
        "Equipment loans. Deposit is left blank where no deposit is taken.",
        ooxml.Table([
            ("Item", "Deposit", "Loan days", "Late fee"),
            ("Camera", "$50", "3", "$10"),
            ("Tripod", "", TRIPOD_LOAN_DAYS, TRIPOD_LATE_FEE),
            ("GPS unit", "$25", "5", "$5"),
        ]),
    ])


def _field_trips() -> bytes:
    return build._text_pdf("Field trips", [
        f"The spring field trip departs on {SPRING_TRIP_DATE} from the north lot at 07:00.",
        f"The fall field trip departs on {FALL_TRIP_DATE} from the same lot.",
    ])


def _supply_budget() -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "Budget"
    sheet.append(["Category", "Item", "Amount"])
    for row in (("Glassware", "Beakers", 640), ("Glassware", "Flasks", 1300), ("Chemicals", "Buffers", 820),
                ("Safety", "Goggles", 210)):
        sheet.append(list(row))
    return build.stable_xlsx(book)


def _kb_large_file(name: str, v1: bool = False) -> bytes:
    topic = name.split("_")[0]
    planted: Dict[int, str] = {}
    named: Dict[int, str] = {}
    if name == "travel_handbook.md":
        planted = {4: f"International travel requests must be filed at least {INTL_TRAVEL_DAYS} days before departure.",
                   9: f"Domestic travel requests must be filed at least {DOMESTIC_TRAVEL_DAYS} days before departure."}
    elif name == "procurement_handbook.md":
        planted = {6: "Any purchase above $7,500 requires three written quotes from different vendors.",
                   3: "Purchases between $2,500 and $7,500 require two written quotes."}
    elif name == "grants_handbook.md":
        planted = {5: f"The grants review committee meets every {GRANTS_COMMITTEE_DAY} afternoon."}
    elif name == "it_handbook.md":
        planted = {5: f"The IT change advisory board meets every {IT_BOARD_DAY} morning."}
    elif name == "hr_handbook.md":
        named = {7: HR_SECTION_7_TITLE}
    elif name == "facilities_handbook.md":
        days = ACCESS_CODE_ROTATION_V1 if v1 else ACCESS_CODE_ROTATION_V2
        planted = {8: f"The after-hours building access code rotates every {days} days."}
    title = f"{topic.capitalize()} handbook" + (" (superseded draft)" if v1 else "")
    return handbook(title, topic.capitalize(), planted, named=named, seed=sum(map(ord, name)))


def _agent_knowledge() -> bytes:
    return (
        "# Front desk hours\n\n"
        f"The front desk opens at {AGENT_KNOWLEDGE_HOURS} on weekdays.\n\n"
        f"On Saturdays it opens at {AGENT_KNOWLEDGE_DISTRACTOR}.\n"
    ).encode("utf-8")


def _loader_process() -> bytes:
    return (
        "# Process\n\n"
        f"Begin every answer with the words \"{LOADER_PREFIX}\" and then answer in one sentence.\n"
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixtureFile:
    name: str
    build: Callable[[], bytes]


@dataclass(frozen=True)
class ProjectFixture:
    name: str
    instructions: str
    files: Sequence[FixtureFile]
    #: Uploaded, waited for, then deleted before ``files`` holds its replacement:
    #: the knowledge base's replace path, with the old value as a distractor.
    superseded: Sequence[FixtureFile] = ()


@dataclass(frozen=True)
class AgentFixture:
    name: str
    instructions: str
    tools: Sequence[str] = ()
    skills: Sequence[str] = ()
    files: Sequence[FixtureFile] = ()


def _file(name: str, fn: Callable[[], bytes]) -> FixtureFile:
    return FixtureFile(name, fn)


PROJECTS: Dict[str, ProjectFixture] = {
    "kb_small": ProjectFixture(
        name="kb_small",
        instructions=f"End every answer with the line \"{KB_SMALL_SIGN_OFF}\".",
        files=(
            _file("onboarding_policy.md", _onboarding_policy),
            _file("equipment_loans.docx", _equipment_loans),
            _file("field_trips.pdf", _field_trips),
            _file("supply_budget.xlsx", _supply_budget),
        ),
    ),
    "kb_large": ProjectFixture(
        name="kb_large",
        instructions="Answer from the project files.",
        files=tuple(_file(n, (lambda n=n: _kb_large_file(n))) for n in KB_LARGE_FILES),
        superseded=(_file("facilities_handbook.md", lambda: _kb_large_file("facilities_handbook.md", v1=True)),),
    ),
}

_LONG_RULES = "\n".join(
    f"{i}. When a question touches {topic}, keep the answer to what the question asks and do not add a summary."
    for i, topic in enumerate(
        [f"topic area {n}" for n in range(1, 120)], start=1
    )
)

AGENTS: Dict[str, AgentFixture] = {
    "agent_format": AgentFixture(
        name="agent_format",
        instructions=(
            f"Reply with a single JSON object and nothing else: {{\"{FORMAT_KEY}\": \"<your answer>\"}}. "
            "No code fences, no text before or after it."
        ),
    ),
    "agent_bound_tools": AgentFixture(
        name="agent_bound_tools",
        instructions="Use your tools when a question needs them.",
        tools=("calculator",),
    ),
    "agent_knowledge": AgentFixture(
        name="agent_knowledge",
        instructions="Answer questions about the front desk from your knowledge. If it does not say, reply exactly: NOT IN KNOWLEDGE",
        files=(_file("front_desk_hours.md", _agent_knowledge),),
    ),
    "agent_loader": AgentFixture(
        name="agent_loader",
        instructions="Before you answer anything, read process.md in your Knowledge in full and follow it exactly.",
        files=(_file("process.md", _loader_process),)
        + tuple(_file(n, (lambda n=n: _kb_large_file(n))) for n in KB_LARGE_FILES[:5]),
    ),
    "agent_long_instructions": AgentFixture(
        name="agent_long_instructions",
        instructions=(
            "You are a careful assistant. Follow every numbered rule below.\n\n" + _LONG_RULES
            + f"\n\nFinal rule, which overrides nothing above: end every answer with the token {LONG_INSTRUCTIONS_TOKEN}."
        ),
    ),
    "agent_skill": AgentFixture(
        name="agent_skill",
        instructions="Help with research questions about web pages.",
        tools=("fetch_url_content",),
        skills=("web_research",),
    ),
}


def digest(fixture) -> str:
    """A short hash of everything the seed would create for ``fixture``."""
    h = hashlib.sha256()
    h.update(repr((type(fixture).__name__, fixture.name, fixture.instructions,
                   tuple(getattr(fixture, "tools", ())), tuple(getattr(fixture, "skills", ())))).encode())
    for group in (fixture.files, getattr(fixture, "superseded", ())):
        for f in group:
            h.update(f.name.encode())
            h.update(hashlib.sha256(f.build()).digest())
    return h.hexdigest()[:16]


def description(fixture) -> str:
    return f"{DIGEST_PREFIX}{digest(fixture)}"


def fixture_names() -> Tuple[List[str], List[str]]:
    return sorted(PROJECTS), sorted(AGENTS)
