"""Seeded synthetic transcripts with planted, exactly-scorable facts.

A transcript is a long document-editing session (the shape of the sessions that
reach the compaction ceiling in prod): short user requests, long assistant
drafts, and a periodic ``search_sources`` tool round trip with a large result.
Into it we plant facts at known turns, in four families:

- ``constraint`` — a standing instruction the user gives once ("always call the
  funding agency X"). The summarizer prompt promises to keep these verbatim.
- ``decision``   — a value the conversation settled on.
- ``reference``  — an exact identifier, stated in a tool result or a draft.
- ``superseded`` — a value stated, then changed later. Only the later value is
  correct, and repeating the earlier one fails the task.

The plants are *authored*, not generated, so the ground truth is known exactly.
That is what lets slice 1 score by exact match with no model acting as judge.

Everything is a pure function of ``(base_seed, variant, CorpusConfig)``.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

CHARS_PER_TOKEN = 4
BASE_SEED = 20260925

TOOL_NAME = "search_sources"
TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "toolSpec": {
            "name": TOOL_NAME,
            "description": "Search the project's source library and return matching excerpts.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                }
            },
        }
    }
]

FAMILIES: Tuple[str, ...] = ("constraint", "decision", "reference", "superseded")

_WORDS: Tuple[str, ...] = (
    "analysis", "approach", "argument", "assessment", "baseline", "capacity", "cohort",
    "community", "context", "criteria", "curriculum", "data", "design", "evidence",
    "evaluation", "framework", "funding", "goals", "impact", "implementation", "indicators",
    "instruction", "learning", "literature", "measures", "method", "milestones", "model",
    "narrative", "outcomes", "outreach", "participants", "partnership", "pilot", "plan",
    "practice", "priorities", "process", "program", "proposal", "protocol", "quality",
    "rationale", "recruitment", "research", "resources", "results", "retention", "review",
    "rigor", "sampling", "schedule", "scope", "sites", "stakeholders", "strategy", "students",
    "support", "survey", "sustainability", "teachers", "timeline", "training", "validity",
    "workload", "clearly", "carefully", "directly", "consistently", "broadly", "closely",
    "strengthens", "supports", "extends", "clarifies", "addresses", "reflects", "informs",
    "improves", "connects", "documents", "measures", "tracks", "aligns", "builds", "the",
    "our", "this", "each", "every", "a", "an", "with", "across", "within", "through", "for",
    "and", "while", "because", "so", "that", "which", "into", "over", "under", "between",
    "regional", "early", "sustained", "measurable", "practical", "formative", "summative",
    "qualitative", "quantitative", "iterative", "targeted", "shared", "local", "annual",
)

_REQUESTS: Tuple[str, ...] = (
    "Please revise section {n} so the argument about {a} and {b} is tighter.",
    "Can you rework section {n}? The part on {a} reads as repetitive, and {b} needs more weight.",
    "Draft a new version of section {n} that leads with {a} and folds {b} into the second half.",
    "Section {n} needs to be shorter. Keep the point about {a}, cut most of the {b} material.",
    "Rewrite section {n} for a reviewer who cares mostly about {a}; mention {b} only briefly.",
)

_CONSTRAINTS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (
        'Standing instruction for the rest of this project: always refer to the funding agency as "{v}", never any other name.',
        "What exact name did I tell you to always use for the funding agency? Reply with just the name.",
        ("the Kestrel Foundation", "the Aldergrove Trust", "the Halvorsen Fund", "the Marrow Creek Endowment"),
    ),
    (
        'Standing instruction: our lead investigator must always be written as "{v}" in every draft.',
        "How did I say the lead investigator must always be written? Reply with just the name.",
        ("Dr. Imogen Varga", "Dr. Tobias Renquist", "Dr. Priya Castellane", "Dr. Oren Blackwood"),
    ),
    (
        "Standing instruction: format every citation in {v}, no exceptions.",
        "Which citation style did I tell you to use for every citation? Reply with just the style.",
        ("Chicago author-date", "APA 7th edition", "IEEE numeric", "MLA 9th edition"),
    ),
    (
        'Standing instruction: the program is always called "{v}" in anything we write.',
        "What did I say the program must always be called? Reply with just the name.",
        ("Project Lumen", "the Tidewater Initiative", "Project Northstar", "the Basalt Pathways Program"),
    ),
    (
        "Standing instruction: never mention {v} anywhere in the proposal.",
        "Which organization did I tell you never to mention in the proposal? Reply with just the name.",
        ("Crestline Analytics", "Vantor Learning Group", "Pellham Consulting", "Iridia EdTech"),
    ),
)

_DECISIONS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (
        "Decision: we're settling the travel budget cap at {v}. Keep that fixed from here on.",
        "What did we settle the travel budget cap at? Reply with just the amount.",
        ("$4,750", "$6,120", "$3,985", "$5,340"),
    ),
    (
        "Decision: the participant stipend is {v} per person. That's final.",
        "What participant stipend per person did we decide on? Reply with just the amount.",
        ("$145", "$210", "$175", "$260"),
    ),
    (
        "Decision: the target sample is {v} participants across all sites.",
        "How many participants did we decide the target sample is? Reply with just the number.",
        ("312", "486", "257", "641"),
    ),
    (
        "Decision: the pilot launches in {v}. Build the timeline around that.",
        "Which month did we decide the pilot launches in? Reply with just the month and year.",
        ("February 2027", "August 2027", "January 2028", "September 2027"),
    ),
)

_REFERENCES: Tuple[Tuple[str, str], ...] = (
    ("grant portal submission ID", "What is the grant portal submission ID? Reply with just the ID."),
    ("IRB protocol number", "What is the IRB protocol number? Reply with just the number."),
    ("shared drive folder code", "What is the shared drive folder code? Reply with just the code."),
    ("budget workbook version tag", "What is the budget workbook version tag? Reply with just the tag."),
)

_SUPERSEDED: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (
        "the internal review deadline",
        "What is the internal review deadline now? Reply with just the date.",
        ("March 14", "April 2", "May 19", "June 6", "October 9"),
    ),
    (
        "the site-visit meeting room",
        "Which room is the site-visit meeting in now? Reply with just the room.",
        ("Room 214B", "Room 311", "the Hemingway Annex", "Room 108C"),
    ),
    (
        "the narrative page limit",
        "What is the narrative page limit now? Reply with just the limit.",
        ("12 pages", "15 pages", "10 pages", "18 pages"),
    ),
)


@dataclass(frozen=True)
class Plant:
    plant_id: str
    family: str
    # Turn whose messages state the value the task is scored against.
    turn: int
    # ``user_first`` | ``user_mid`` | ``assistant`` | ``tool_result``
    where: str
    statement: str
    question: str
    expected: Tuple[str, ...]
    forbidden: Tuple[str, ...] = ()
    # ``superseded`` only: the turn that stated the old value.
    first_turn: Optional[int] = None
    first_statement: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Plant":
        data = dict(data)
        data["expected"] = tuple(data.get("expected") or ())
        data["forbidden"] = tuple(data.get("forbidden") or ())
        return cls(**data)


@dataclass(frozen=True)
class CorpusConfig:
    turns: int = 48
    per_family: Tuple[Tuple[str, int], ...] = (
        ("constraint", 3), ("decision", 2), ("reference", 2), ("superseded", 2),
    )
    # Sized so a 48-turn session crosses a 100k ceiling more than once and the
    # whole history still fits a 200k window (the full-history control arm).
    assistant_tokens: Tuple[int, int] = (1_200, 2_800)
    tool_result_tokens: Tuple[int, int] = (2_500, 6_000)
    tool_turn_every: int = 6

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Transcript:
    variant: int
    seed: int
    turns: List[List[Dict[str, Any]]]
    plants: List[Plant] = field(default_factory=list)

    @property
    def messages(self) -> List[Dict[str, Any]]:
        return [m for turn in self.turns for m in turn]

    @property
    def turn_starts(self) -> List[int]:
        """Absolute index of each turn's first message in ``messages``."""
        starts, index = [], 0
        for turn in self.turns:
            starts.append(index)
            index += len(turn)
        return starts


def _prose(rng: random.Random, tokens: int) -> str:
    target = tokens * CHARS_PER_TOKEN
    paragraphs: List[str] = []
    used = 0
    while used < target:
        sentences = []
        for _ in range(rng.randint(4, 7)):
            words = [rng.choice(_WORDS) for _ in range(rng.randint(12, 24))]
            sentences.append(" ".join(words).capitalize() + ".")
        paragraph = " ".join(sentences)
        paragraphs.append(paragraph)
        used += len(paragraph) + 2
    return "\n\n".join(paragraphs)


def _insert_mid(text: str, statement: str) -> str:
    """Insert ``statement`` as its own paragraph near the middle of ``text``."""
    paragraphs = text.split("\n\n")
    at = max(1, len(paragraphs) // 2)
    return "\n\n".join(paragraphs[:at] + [statement] + paragraphs[at:])


def _reference_value(rng: random.Random, label: str) -> str:
    if label.startswith("IRB"):
        return f"IRB-2026-{rng.randint(1000, 9999)}"
    letters = "".join(rng.choice("BCDFGHJKLMNPQRSTVWXZ") for _ in range(2))
    return f"{letters}-{rng.randint(10000, 99999)}"


def _money_variants(value: str) -> Tuple[str, ...]:
    """``$4,750`` also accepts ``4,750`` — the model often drops the sign."""
    if value.startswith("$"):
        return (value, value[1:])
    return (value,)


def _plan_plants(rng: random.Random, config: CorpusConfig) -> List[Plant]:
    """Choose plant values and statement turns (no two statements share a turn)."""
    last = config.turns - 1
    # Turn 0 is the opening request; the probe is asked after the last turn.
    free = list(range(1, last + 1))
    rng.shuffle(free)

    def take(lo: int = 1, hi: int = last) -> int:
        for i, t in enumerate(free):
            if lo <= t <= hi:
                return free.pop(i)
        raise ValueError("not enough free turns for the requested plants")

    plants: List[Plant] = []
    counts = dict(config.per_family)

    for i, (tpl, question, values) in enumerate(rng.sample(_CONSTRAINTS, counts.get("constraint", 0))):
        value = rng.choice(values)
        plants.append(Plant(
            plant_id=f"constraint-{i}", family="constraint", turn=take(),
            where=rng.choice(("user_first", "user_mid")), statement=tpl.format(v=value),
            question=question, expected=(value,),
        ))

    for i, (tpl, question, values) in enumerate(rng.sample(_DECISIONS, counts.get("decision", 0))):
        value = rng.choice(values)
        plants.append(Plant(
            plant_id=f"decision-{i}", family="decision", turn=take(),
            where=rng.choice(("user_first", "user_mid", "assistant")), statement=tpl.format(v=value),
            question=question, expected=_money_variants(value),
        ))

    for i, (label, question) in enumerate(rng.sample(_REFERENCES, counts.get("reference", 0))):
        value = _reference_value(rng, label)
        turn = take()
        is_tool_turn = turn % config.tool_turn_every == config.tool_turn_every - 1
        where = "tool_result" if is_tool_turn else "assistant"
        statement = f"Record note: the {label} is {value}." if is_tool_turn else f"(For reference, the {label} is {value}.)"
        plants.append(Plant(
            plant_id=f"reference-{i}", family="reference", turn=turn, where=where,
            statement=statement, question=question, expected=(value,),
        ))

    for i, (topic, question, values) in enumerate(rng.sample(_SUPERSEDED, counts.get("superseded", 0))):
        old, new = rng.sample(values, 2)
        first = take(1, max(1, last // 2))
        second = take(first + 4, last)
        plants.append(Plant(
            plant_id=f"superseded-{i}", family="superseded", turn=second,
            where=rng.choice(("user_first", "user_mid")),
            statement=f"Update: {topic} has changed. It is now {new}, not {old}.",
            question=question, expected=(new,), forbidden=(old,),
            first_turn=first, first_statement=f"Note for the plan: {topic} is {old}.",
        ))
    return plants


def build_transcript(variant: int, config: CorpusConfig = CorpusConfig(), base_seed: int = BASE_SEED) -> Transcript:
    seed = base_seed * 1_000 + variant
    rng = random.Random(seed)
    plants = _plan_plants(rng, config)

    # (turn, where) -> statements to place there.
    placements: Dict[int, List[Tuple[str, str]]] = {}
    for plant in plants:
        placements.setdefault(plant.turn, []).append((plant.where, plant.statement))
        if plant.first_turn is not None and plant.first_statement:
            placements.setdefault(plant.first_turn, []).append(("user_mid", plant.first_statement))

    turns: List[List[Dict[str, Any]]] = []
    for t in range(config.turns):
        here = placements.get(t, [])
        a, b = rng.sample(_WORDS[:66], 2)
        request = rng.choice(_REQUESTS).format(n=t + 1, a=a, b=b)
        request = request + " " + _prose(rng, rng.randint(30, 90)).replace("\n\n", " ")
        for where, statement in here:
            if where == "user_first":
                request = statement + "\n\n" + request
            elif where == "user_mid":
                request = _insert_mid(request.replace(". ", ".\n\n", 2), statement)

        draft = f"Here is the revised section {t + 1}.\n\n" + _prose(rng, rng.randint(*config.assistant_tokens))
        for where, statement in here:
            if where == "assistant":
                draft = _insert_mid(draft, statement)

        messages: List[Dict[str, Any]] = [{"role": "user", "content": [{"text": request}]}]
        is_tool_turn = t % config.tool_turn_every == config.tool_turn_every - 1
        if is_tool_turn:
            tool_use_id = f"tooluse_v{variant}_t{t}"
            excerpts = "Source excerpts:\n\n" + _prose(rng, rng.randint(*config.tool_result_tokens))
            for where, statement in here:
                if where == "tool_result":
                    excerpts = _insert_mid(excerpts, statement)
            messages.append({"role": "assistant", "content": [
                {"text": "Let me check the source library first."},
                {"toolUse": {"toolUseId": tool_use_id, "name": TOOL_NAME, "input": {"query": f"{a} {b}"}}},
            ]})
            messages.append({"role": "user", "content": [
                {"toolResult": {"toolUseId": tool_use_id, "content": [{"text": excerpts}], "status": "success"}},
            ]})
        messages.append({"role": "assistant", "content": [{"text": draft}]})
        turns.append(messages)

    return Transcript(variant=variant, seed=seed, turns=turns, plants=plants)


def build_corpus(variants: int, config: CorpusConfig = CorpusConfig(), base_seed: int = BASE_SEED) -> List[Transcript]:
    return [build_transcript(v, config, base_seed) for v in range(variants)]


def message_text(message: Dict[str, Any]) -> str:
    """Every string the model would read in one message (text, tool input, tool result)."""
    import json

    parts: List[str] = []

    def walk(blocks: Sequence[Any]) -> None:
        for block in blocks or ():
            if not isinstance(block, dict):
                continue
            if "text" in block and isinstance(block["text"], str):
                parts.append(block["text"])
            elif "toolUse" in block:
                parts.append(json.dumps(block["toolUse"].get("input"), ensure_ascii=False))
            elif "toolResult" in block:
                walk(block["toolResult"].get("content") or [])
            elif "json" in block:
                parts.append(json.dumps(block["json"], ensure_ascii=False))

    content = message.get("content")
    if isinstance(content, str):
        return content
    walk(content if isinstance(content, list) else [])
    return "\n".join(parts)
