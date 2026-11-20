"""Skill activations a compaction cut removes, and the one summary line naming them.

A skill reaches the model as the result of a ``skills`` tool call: the model
calls ``skills(skill_name=...)`` and the plugin returns the skill's
instructions. Nothing else holds them. When a compaction cut retires the turn
that made that call, the instructions leave the context, but the model may
still believe it is following them. The agentskills.io client guidance says to
protect skill content from compaction; this stack's answer is one
deterministic line in the summary that names the skills and tells the model to
activate them again. There are no protected blocks: the activation result is
cut like any other tool result, and the line is how the model finds it again.

Everything here is a pure function of the messages being cut, called once, at
the checkpoint advance in ``TurnBasedSessionManager.update_after_turn``. The
line is part of the summary text persisted with the compaction state, so a
restore prepends the same bytes every time and nothing here runs on restore or
before the first token.
"""

from __future__ import annotations

import re
from typing import Any, Collection, Iterable, List, Optional, Sequence

#: The activation tool the Strands ``AgentSkills`` plugin registers.
SKILLS_TOOL_NAME = "skills"

#: At most this many names in the line; any more are counted, not named.
MAX_LISTED_SKILLS = 20
#: A name longer than this is cut to it. Catalog slugs are capped at 64
#: (``slugify_skill_name``), so only a name the model invented is ever cut.
MAX_SKILL_NAME_CHARS = 64

_LINE_PREFIX = "Skills that were active before this point: "
_LINE_SUFFIX = ". Activate one again with the skills tool before relying on its instructions."
# The line as an earlier cut wrote it into a summary that is itself being cut.
_PREVIOUS_LINE = re.compile(re.escape(_LINE_PREFIX) + r"(.+?)" + re.escape(_LINE_SUFFIX))
_MORE = re.compile(r"^and \d+ more$")


def activated_skills_in(messages: Sequence[Any]) -> List[str]:
    """The ``skill_name`` of every ``skills`` call in ``messages``, first-seen order, deduplicated.

    A ``skills`` call with no ``skill_name``, or one that is not a non-empty
    string, is ignored, as is anything that is not a ``toolUse`` block.
    """
    names: List[str] = []
    seen = set()
    for message in messages or ():
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            tool_use = block.get("toolUse") if isinstance(block, dict) else None
            if not isinstance(tool_use, dict) or tool_use.get("name") != SKILLS_TOOL_NAME:
                continue
            tool_input = tool_use.get("input")
            name = tool_input.get("skill_name") if isinstance(tool_input, dict) else None
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _previously_listed(messages: Sequence[Any]) -> List[str]:
    """Names an earlier cut's line listed, found in summary text that is now being cut.

    The first retained message carries the previous summary, so a second cut
    retires that line along with it. Reading it back keeps a skill the model
    never re-activated from dropping out of the next summary.
    """
    names: List[str] = []
    for message in messages or ():
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            text = block.get("text") if isinstance(block, dict) else None
            if not isinstance(text, str) or _LINE_PREFIX not in text:
                continue
            for match in _PREVIOUS_LINE.finditer(text):
                for part in match.group(1).split(", "):
                    part = part.strip()
                    if part and not _MORE.match(part):
                        names.append(part)
    return names


def _dedupe(names: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def skills_reminder_line(names: Sequence[str]) -> Optional[str]:
    """The summary line for ``names``, bounded; ``None`` when there are none."""
    if not names:
        return None
    listed = [name[:MAX_SKILL_NAME_CHARS] for name in names[:MAX_LISTED_SKILLS]]
    extra = len(names) - len(listed)
    if extra > 0:
        listed.append(f"and {extra} more")
    return _LINE_PREFIX + ", ".join(listed) + _LINE_SUFFIX


def cut_skills_reminder(
    cut_messages: Sequence[Any],
    available: Optional[Collection[str]] = None,
) -> Optional[str]:
    """The line for the skills a cut over ``cut_messages`` removes, or ``None``.

    ``available`` is the turn's skill set at the moment of compaction (the
    names the ``skills`` tool can activate). When given, only names still in
    it are listed, so the line never sends the model after a skill it can no
    longer load. ``None`` means the caller cannot say, and every name found is
    listed.
    """
    names = _dedupe(_previously_listed(cut_messages) + activated_skills_in(cut_messages))
    if available is not None:
        allowed = set(available)
        names = [name for name in names if name in allowed]
    return skills_reminder_line(names)


def with_skills_reminder(summary: Optional[str], reminder: Optional[str]) -> Optional[str]:
    """``summary`` with ``reminder`` appended as its last line; unchanged when there is none."""
    if not reminder:
        return summary
    if not summary:
        return reminder
    return f"{summary}\n\n{reminder}"
