"""Skill version pins: where a pin is stored, and how it rides through a turn.

A pin is stored on the binding, in its kind-specific ``config``:
``{"kind": "skill", "ref": "pdf_workflows", "config": {"version": 3}}``. Nothing else
about a binding changes, so a pin survives every path a binding already travels
(Agent versions, the project settings routes, the marketplace snapshot).

At run time a pinned skill travels as one string, ``pdf_workflows@3``, through the
same list of skill ids every unpinned skill uses: the agent cache key, the paused-turn
snapshot and resume, and ``build_skills_runtime``. That is the scoped-tool-id pattern
(``toolId::mcpToolName``), and it is why no signature on the turn path had to grow a
second "and these are pinned" argument that some path would forget to pass. ``@`` can
never appear in a skill id (``SKILL_ID_PATTERN``), so the split is unambiguous.

Anything that compares a run-time id with a catalog id (access checks, slash-command
matching, the freshness lookup) must compare ``base_skill_id(ref)``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

# The binding ``config`` key that holds the pinned version number.
PIN_CONFIG_KEY = "version"

_SEPARATOR = "@"


def pinned_ref(skill_id: str, version: Optional[int]) -> str:
    """The run-time id for ``skill_id`` at ``version``, or the bare id when unpinned."""
    return f"{skill_id}{_SEPARATOR}{version}" if version else skill_id


def split_pinned_ref(ref: str) -> Tuple[str, Optional[int]]:
    """``"pdf_workflows@3"`` → ``("pdf_workflows", 3)``; a bare id → ``(id, None)``.

    A suffix that is not a positive integer is not a pin. The id is returned whole,
    so it simply fails to match any catalog skill rather than running some version.
    """
    base, sep, suffix = (ref or "").partition(_SEPARATOR)
    if not sep:
        return ref, None
    if not suffix.isdigit() or int(suffix) < 1:
        return ref, None
    return base, int(suffix)


def base_skill_id(ref: str) -> str:
    """The catalog id a run-time id refers to."""
    return split_pinned_ref(ref)[0]


def binding_pin(config: Optional[Mapping[str, Any]]) -> Optional[int]:
    """The version a skill binding's ``config`` pins, or ``None`` for a live binding.

    Tolerates the number arriving as a DynamoDB ``Decimal``. Anything that is not a
    positive whole number reads as unpinned; ``validate_agent_write`` is where a
    malformed pin is refused, so this never has to guess.
    """
    value = (config or {}).get(PIN_CONFIG_KEY)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 1 and number == value else None
