"""Role-based system instructions on a plain chat turn (issue #13).

An admin writes a free-text ``system_instruction`` on an AppRole. On a turn with
no agent attached, every matched role's instruction is added to the system
prompt under ``## Role Instructions``, highest priority first. The list is
resolved with the user's permissions (``AppRoleService._merge_permissions``) and
rides the per-user permission cache, so reading it costs no DynamoDB call.

Plain-turn order, built by :func:`compose_plain_turn_prompt`::

    base prompt + date → ## Role Instructions → ## Active Mode → ## Personal Instructions

Personal instructions win a conflict with role instructions, and say so when
both are present. A user with no role instruction gets exactly the prompt they
got before this module existed.

Two caps protect the personal section:

* The role section is capped at :data:`MAX_ROLE_INSTRUCTIONS_CHARS`. Roles are
  taken whole, highest priority first; the first role that would cross the cap
  is dropped together with every role below it, so it is always the lowest
  priority roles that go. Nothing is cut mid-text.
* If the whole composed prompt would still exceed what
  ``SystemPromptBuilder.from_user_prompt`` keeps, role sections are dropped,
  lowest priority first, before anything else. ``from_user_prompt`` truncates
  from the end, which would take the personal instructions first. With today's
  caps this never runs; it is a guard.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

from apis.inference_api.chat.system_prompt_resolver import append_active_prompt
from apis.shared.auth.models import User
from apis.shared.rbac.models import RoleInstruction
from apis.shared.security.log_sanitize import scrub_log

logger = logging.getLogger(__name__)

#: Combined cap on the text of every role instruction one turn carries.
MAX_ROLE_INSTRUCTIONS_CHARS = 16_000

ROLE_INSTRUCTIONS_HEADING = "Role Instructions"


def cap_role_instructions(items: Sequence[RoleInstruction], limit: int = MAX_ROLE_INSTRUCTIONS_CHARS) -> List[RoleInstruction]:
    """The leading roles whose combined text fits ``limit``.

    ``items`` is in priority order. The first role that would cross the cap and
    every role after it are dropped, and each drop is logged with the role id.
    """
    kept: List[RoleInstruction] = []
    total = 0
    for index, item in enumerate(items):
        if total + len(item.text) > limit:
            dropped = [i.role_id for i in items[index:]]
            logger.warning(
                "Role instructions exceed %d chars; dropped roles %s",
                limit,
                ", ".join(scrub_log(r) for r in dropped),
            )
            break
        kept.append(item)
        total += len(item.text)
    return kept


def compose_role_instructions(system_prompt: str, items: Sequence[RoleInstruction]) -> str:
    """Append the ``## Role Instructions`` section; unchanged when ``items`` is empty."""
    if not items:
        return system_prompt
    body = "\n\n".join(f"### {i.display_name}\n\n{i.text}" for i in items)
    return f"{system_prompt}\n\n## {ROLE_INSTRUCTIONS_HEADING}\n\n{body}"


def _compose(
    base: str,
    roles: Sequence[RoleInstruction],
    mode: Optional[Tuple[str, str]],
    personal: Optional[str],
) -> str:
    # Imported here: routes.py imports this module at load time.
    from apis.inference_api.chat.routes import compose_personal_instructions

    prompt = compose_role_instructions(base, roles)
    if mode:
        prompt = append_active_prompt(prompt, mode[0], mode[1])
    if personal:
        prompt = compose_personal_instructions(
            prompt,
            personal,
            over=None,
            overrides=ROLE_INSTRUCTIONS_HEADING if roles else None,
        )
    return prompt


def compose_plain_turn_prompt(
    system_prompt: Optional[str],
    *,
    role_instructions: Sequence[RoleInstruction] = (),
    mode: Optional[Tuple[str, str]] = None,
    personal: Optional[str] = None,
) -> Optional[str]:
    """The system prompt of a turn with no agent attached.

    ``system_prompt`` is the base the route already has (``None`` on an ordinary
    plain turn). With nothing to add the base is returned as is, so a user with
    no role instruction, mode or personal instructions keeps ``None`` and
    ``BaseAgent`` builds the default prompt exactly as before.

    The base is built **without** a client surface: ``BaseAgent`` appends the
    surface block after ``</user_instructions>``, so building it in here as well
    would send it twice.
    """
    roles = list(role_instructions)
    if not (roles or mode or personal):
        return system_prompt

    from agents.main_agent.core.system_prompt_builder import (
        MAX_USER_PROMPT_LENGTH,
        SystemPromptBuilder,
    )

    base = system_prompt or SystemPromptBuilder().build(include_date=True)
    prompt = _compose(base, roles, mode, personal)
    while roles and len(prompt) > MAX_USER_PROMPT_LENGTH:
        dropped = roles.pop()
        logger.warning(
            "Composed system prompt exceeds %d chars; dropped role instruction %s",
            MAX_USER_PROMPT_LENGTH,
            scrub_log(dropped.role_id),
        )
        prompt = _compose(base, roles, mode, personal)
    return prompt


async def resolve_role_instructions(user: Optional[User]) -> List[RoleInstruction]:
    """The capped role instructions for ``user``'s plain turn; ``[]`` on any failure.

    Reads the permission object the turn has already resolved (``can_access_model``
    warms the cache before this runs), so this is a cache hit on the chat path.
    Best effort: a resolution error must not fail a turn whose model access was
    already granted, so it falls back to no role instructions and logs.
    """
    if user is None:
        return []
    try:
        from apis.shared.rbac.service import get_app_role_service

        permissions = await get_app_role_service().resolve_user_permissions(user)
    except Exception:
        logger.warning("Role instructions unavailable; continuing without them", exc_info=True)
        return []
    items = getattr(permissions, "role_instructions", None) or []
    return cap_role_instructions([i for i in items if isinstance(i, RoleInstruction) and i.text])
