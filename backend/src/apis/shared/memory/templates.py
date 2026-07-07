"""Space templates — presets that seed a new Memory Space (PR-1).

A template supplies the starter ``MEMORY.md`` index text, the entry types the
space uses, and the ``always_load`` rule (which entries hydrate at an agent's
wake-up). Templates keep the Oliver-class ergonomics without hardcoding any
one use case: "Oliver" is the ``chief-of-staff`` template + a bound agent.

PR-1 ships the registry and starter index text. Richer template seeding
(pre-created example entries, per-type frontmatter scaffolds) can be layered
in PR-2 when the read path lands; the shape here is deliberately minimal.
"""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, Field

from .models import EntryType


class SpaceTemplate(BaseModel):
    """A preset for creating a Memory Space."""

    template_id: str
    name: str
    description: str
    entry_types: List[EntryType] = Field(default_factory=list)
    # Entries hydrated at wake-up. ``MEMORY.md`` is the index; ``latest:{type}``
    # resolves to the most-recent entry of that type (the Oliver rule).
    always_load: List[str] = Field(default_factory=lambda: ["MEMORY.md"])
    starter_index: str = "# Memory\n"


_BLANK = SpaceTemplate(
    template_id="blank",
    name="Blank Wiki",
    description="An empty space. Start from scratch.",
    entry_types=["entity", "episodic", "fact"],
    always_load=["MEMORY.md"],
    starter_index=(
        "# Memory\n\n"
        "This is your memory index. One-line pointers to the things worth "
        "remembering go here; the details live in linked entries.\n"
    ),
)

_CHIEF_OF_STAFF = SpaceTemplate(
    template_id="chief-of-staff",
    name="Chief of Staff",
    description=(
        "An institutional-memory space for a chief-of-staff-style assistant: "
        "people, projects, and commitments, with a daily log and periodic "
        "briefs."
    ),
    entry_types=["entity", "episodic", "fact"],
    always_load=["MEMORY.md", "latest:episodic/daily", "latest:episodic/brief"],
    starter_index=(
        "# Memory\n\n"
        "## Strategic priorities\n"
        "_What matters most right now. Everything maps back to these._\n\n"
        "## Key people\n"
        "_Pointers to `people/` entries — role, what they care about, what's "
        "owed in both directions._\n\n"
        "## Active projects\n"
        "_Pointers to `projects/` entries — status and stakeholders._\n\n"
        "## Open commitments\n"
        "_Who owes what, and by when. The commitments sections in each person "
        "entry are the CRM._\n"
    ),
)

_RESEARCH_NOTEBOOK = SpaceTemplate(
    template_id="research-notebook",
    name="Research Notebook",
    description=(
        "A space for research work: papers, threads of inquiry, and open "
        "questions, with a running log."
    ),
    entry_types=["entity", "episodic", "fact"],
    always_load=["MEMORY.md", "latest:episodic/log"],
    starter_index=(
        "# Memory\n\n"
        "## Threads of inquiry\n"
        "_Active lines of research — pointers to their entries._\n\n"
        "## Papers & sources\n"
        "_Key references worth remembering._\n\n"
        "## Open questions\n"
        "_What we don't yet know and want to resolve._\n"
    ),
)


TEMPLATES: Dict[str, SpaceTemplate] = {
    t.template_id: t
    for t in (_BLANK, _CHIEF_OF_STAFF, _RESEARCH_NOTEBOOK)
}

DEFAULT_TEMPLATE_ID = "blank"


def get_template(template_id: str) -> SpaceTemplate:
    """Return a template by id, or raise ``KeyError`` if unknown."""
    return TEMPLATES[template_id]


def is_valid_template(template_id: str) -> bool:
    """True when ``template_id`` names a known template."""
    return template_id in TEMPLATES
