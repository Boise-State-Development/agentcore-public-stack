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

**Who added a binding** lives in the same ``config`` slot (``addedBy``, a user id),
written by the project settings route and never accepted from a client. It is what lets
a skill an *editor* authored run for every member of the project (shared-projects §9.6,
clause 4 of ``resolve_invocable_skill_ids``): the harness's ``ownerId`` is its creator,
so the owner clause alone covers only the creator's skills. A user id, so it is stripped
from every project response the way ``createdBy`` is.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

# The binding ``config`` key that holds the pinned version number.
PIN_CONFIG_KEY = "version"

# The binding ``config`` key that holds the user id of whoever added the binding.
ADDED_BY_CONFIG_KEY = "addedBy"

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


def binding_adder(config: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The user id that added a skill binding, or ``None`` when it was never recorded.

    Bindings from before the key existed have none, and so get no clause-4 grant: the
    rule never guesses who added a skill.
    """
    value = (config or {}).get(ADDED_BY_CONFIG_KEY)
    return value if isinstance(value, str) and value else None


def public_skill_config(config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """A skill binding's config as an API returns it: without the adder's user id."""
    return {k: v for k, v in (config or {}).items() if k != ADDED_BY_CONFIG_KEY}


def project_skill_adders(assistant: Any) -> Dict[str, str]:
    """``{skill ref: adder user id}`` for a **project harness**'s skill bindings.

    Empty for every other Agent, so the adder clause can only ever widen a project's
    skills: an ordinary Agent's bindings are the owner's, judged by the owner clause,
    and its config is client-writable through ``PUT /agents/{id}``. Keyed by the base
    skill id, so a ref that carries ``@n`` is judged like a pin in ``config``.
    """
    if getattr(assistant, "kind", None) != "project":
        return {}
    return skill_binding_adders(getattr(assistant, "bindings", None) or [])


def skill_binding_adders(bindings: Iterable[Any]) -> Dict[str, str]:
    """``{skill ref: adder user id}`` over skill bindings that recorded one."""
    adders: Dict[str, str] = {}
    for binding in bindings:
        if getattr(binding, "kind", None) != "skill":
            continue
        adder = binding_adder(getattr(binding, "config", None))
        if adder:
            adders.setdefault(base_skill_id(binding.ref), adder)
    return adders
