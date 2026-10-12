"""Context-bound tools that author the invoking user's OWN skills (skill-creator spec).

Seven tools over the user tier of the skill catalog: list, read, read one file,
create, update, write one file, delete one file. There is deliberately no
whole-skill delete: it is a hard delete with an S3 purge and no undo, which stays
a UI action behind human intent (spec, Tools).

Permission model, mirroring ``memory_write`` (D4): each tool executes as the
invoking user, captured by closure (the runtime does not populate
``ToolContext``), and ``UserSkillService`` re-checks ownership on every call, so a
skill that is not the caller's reads exactly like one that does not exist. Every
write goes through that service, never raw boto3: it re-projects ``SKILL.md``,
garbage-collects orphaned objects and invalidates the freshness cache (D5).

The four writes pause for the user's approval before they run (D10). That gate is
not in this module: ``MCPExternalApprovalHook`` holds them on
``BeforeToolCallEvent`` (see :mod:`.approval` for what the card shows). Nothing
here takes a ``confirm`` flag, because a model-supplied confirmation is the one
control injected content can forge.

Every failure comes back as an error tool result the model can read and recover
from (a cap, a wrong id, a binary file type), never as a raised exception.

The tools are synchronous on purpose. ``UserSkillService`` is async in signature
but makes blocking boto3 calls, and Strands runs a synchronous tool in a worker
thread, so the event loop (and the live status drain on it) never waits on S3.
Same threading as ``read_skill_file``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
from typing import Any, Coroutine, Dict, List, Optional, TypeVar

from strands import tool

from apis.shared.auth.models import User
from apis.shared.skills.bundle_files import (
    MAX_RESOURCE_BYTES,
    MAX_RESOURCES_PER_SKILL,
    VALID_RESOURCE_KINDS,
)
from apis.shared.skills.models import SKILL_DESCRIPTION_MAX_LENGTH, SkillStatus
from apis.shared.skills.resource_types import is_text_content_type
from apis.shared.skills.user_service import (
    MAX_SKILLS_PER_USER,
    UserSkillError,
    UserSkillLimitError,
    UserSkillNotFoundError,
    get_user_skill_service,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# The user route's limit on a display name (``CreateMySkillRequest``). The
# service does not enforce it, so the tools do, as the route's DTO would.
MAX_DISPLAY_NAME_CHARS = 200

# Where the user turns a skill on. Skills are opt-in per turn and a new skill
# starts off, so this is the step between "created" and "usable" (spec D6).
CUSTOMIZE_SKILLS_PATH = "/customize/skills"

_STATUSES = tuple(s.value for s in SkillStatus)


def _run(coro: Coroutine[Any, Any, T]) -> T:
    """Run one service coroutine to completion from this synchronous tool.

    Strands calls a sync tool from a worker thread with no event loop, which is
    the normal case here. A caller already on a loop (a test, say) gets a
    one-off thread rather than a nested ``asyncio.run`` error.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _user(user_id: str, user_email: Optional[str]) -> User:
    # The service reads only ``user_id`` (ownership, audit fields) and the email
    # (logs). Roles do not matter: ownership is the whole grant for this tier.
    return User(email=user_email or "", user_id=user_id, name="", roles=[])


def _ok(text: str) -> Dict[str, Any]:
    return {"content": [{"text": text}], "status": "success"}


def _ok_json(payload: Any) -> Dict[str, Any]:
    return {"content": [{"json": payload}], "status": "success"}


def _error(text: str) -> Dict[str, Any]:
    return {"content": [{"text": f"❌ {text}"}], "status": "error"}


def _not_found(skill_id: str) -> Dict[str, Any]:
    return _error(
        f"You have no skill with id '{skill_id}'. Call my_skill_list for the ids of "
        "the skills you own. Only your own skills can be read or edited here."
    )


def _failure(action: str, exc: Exception, skill_id: Optional[str] = None) -> Dict[str, Any]:
    """Map a service failure to a result the model can act on."""
    if isinstance(exc, UserSkillNotFoundError) and skill_id is not None:
        return _not_found(skill_id)
    if isinstance(exc, (UserSkillError, ValueError)):
        # The service's own messages are written for a person: the caps, the
        # type allowlist and blank fields all say what to change.
        return _error(f"Could not {action}: {exc}")
    logger.error("my_skill tool failed to %s", action, exc_info=True)
    return _error(
        f"Could not {action} because of an unexpected error ({type(exc).__name__}). "
        "Nothing was changed by this call; you can try again."
    )


def _blank(value: Optional[str]) -> bool:
    return value is not None and not value.strip()


def _field_problems(display_name: Optional[str], description: Optional[str]) -> List[str]:
    problems = []
    if _blank(display_name):
        problems.append("the name cannot be blank")
    elif display_name is not None and len(display_name) > MAX_DISPLAY_NAME_CHARS:
        problems.append(f"the name is over {MAX_DISPLAY_NAME_CHARS} characters")
    if _blank(description):
        problems.append(
            "the description cannot be blank (it is the line the agent reads when "
            "deciding whether to use the skill)"
        )
    elif description is not None and len(description) > SKILL_DESCRIPTION_MAX_LENGTH:
        problems.append(
            f"the description is {len(description)} characters; the limit is "
            f"{SKILL_DESCRIPTION_MAX_LENGTH}"
        )
    return problems


def _log_applied(tool_name: str, skill_id: str, session_id: str) -> None:
    """One line per write that went through, for the pilot readout.

    The approval hook logs every pause, so pauses minus these lines is the number
    of writes the user declined (or that failed after approval). Ids only: the
    content stays out of logs. ``scripts/skill_authoring_pilot_readout.py``
    parses this exact shape.
    """
    logger.info(
        "%s applied %s",
        tool_name,
        skill_id,
        extra={"event": "skill_authoring_write", "tool": tool_name, "session_id": session_id},
    )


def _stale_note() -> str:
    return (
        "If this skill was activated earlier in this conversation, what that earlier "
        "activation loaded is now out of date: activate the skill again before relying "
        "on it."
    )


# ---------------------------------------------------------------------------
# Reads (never pause for approval)
# ---------------------------------------------------------------------------


def make_my_skill_list_tool(session_id: str, user_id: str, user_email: Optional[str]):
    @tool
    def my_skill_list() -> Dict[str, Any]:
        """List the skills you (the user) authored: id, name, description, status.

        Only the user's own skills, never catalog skills. Use the ids with the other
        my_skill_* tools.
        """
        try:
            skills = _run(get_user_skill_service().list_my_skills(_user(user_id, user_email)))
        except Exception as exc:  # noqa: BLE001 - surfaced as a tool result
            return _failure("list your skills", exc)
        summary = [
            {
                "skill_id": s.skill_id,
                "display_name": s.display_name,
                "description": s.description,
                "status": getattr(s.status, "value", s.status),
            }
            for s in sorted(skills, key=lambda s: s.skill_id)
        ]
        return _ok_json({"skills": summary, "limit": MAX_SKILLS_PER_USER})

    return my_skill_list


def make_my_skill_read_tool(session_id: str, user_id: str, user_email: Optional[str]):
    @tool
    def my_skill_read(skill_id: str) -> Dict[str, Any]:
        """Read one of the user's own skills: its fields, instructions and file list.

        Returns the manifest of bundle files (name, kind, size) but not their
        contents; use my_skill_read_file for one file's text.

        Args:
            skill_id: The skill's id, from my_skill_list.
        """
        try:
            skill = _run(get_user_skill_service().get_my_skill(skill_id, _user(user_id, user_email)))
        except Exception as exc:  # noqa: BLE001 - surfaced as a tool result
            return _failure(f"read skill '{skill_id}'", exc, skill_id)
        return _ok_json(
            {
                "skill_id": skill.skill_id,
                "display_name": skill.display_name,
                "description": skill.description,
                "category": skill.category,
                "status": getattr(skill.status, "value", skill.status),
                "skill_metadata": dict(skill.skill_metadata or {}),
                "instructions": skill.instructions,
                "files": [
                    {
                        "filename": r.filename,
                        "kind": r.kind,
                        "size": r.size,
                        "content_type": r.content_type,
                    }
                    for r in skill.resources
                ],
                "file_limit": MAX_RESOURCES_PER_SKILL,
            }
        )

    return my_skill_read


def make_my_skill_read_file_tool(session_id: str, user_id: str, user_email: Optional[str]):
    @tool
    def my_skill_read_file(skill_id: str, filename: str) -> Dict[str, Any]:
        """Read the text of one file in one of the user's own skills.

        Use it to start an edit from the file's current content. Binary files
        (images, PDFs) cannot be read as text.

        Args:
            skill_id: The skill's id, from my_skill_list.
            filename: The file's name as listed by my_skill_read, e.g. "forms.md".
        """
        try:
            ref, content = _run(
                get_user_skill_service().read_resource(
                    skill_id, filename, _user(user_id, user_email)
                )
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a tool result
            return _failure(f"read '{filename}'", exc, skill_id)
        if not is_text_content_type(ref.content_type):
            return _error(
                f"'{filename}' is a {ref.content_type} file, which cannot be read as text."
            )
        try:
            return _ok(content.decode("utf-8"))
        except UnicodeDecodeError:
            return _error(f"'{filename}' is not valid UTF-8 text, so it cannot be shown.")

    return my_skill_read_file


# ---------------------------------------------------------------------------
# Writes (each one pauses for the user's approval first, see .approval)
# ---------------------------------------------------------------------------


def make_my_skill_create_tool(session_id: str, user_id: str, user_email: Optional[str]):
    @tool
    def my_skill_create(
        display_name: str,
        description: str,
        instructions: str,
        category: Optional[str] = None,
        skill_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a new skill owned by the user. The user approves it before it is saved.

        A skill is instructions that future conversations follow when the user turns
        it on. The id is assigned from the name. A new skill starts turned off and
        cannot be used in this conversation.

        Args:
            display_name: Short human name, e.g. "Grant proposal review".
            description: One or two sentences saying what the skill does and when to
                use it. This is what decides whether it gets used, so be specific.
            instructions: The skill's full instructions, in markdown.
            category: Optional grouping label.
            skill_metadata: Optional extra frontmatter fields, e.g. {"version": "1.0"}.
        """
        problems = _field_problems(display_name or "", description or "")
        if problems:
            return _error("Could not create the skill: " + "; ".join(problems) + ".")
        try:
            skill = _run(
                get_user_skill_service().create_my_skill(
                    _user(user_id, user_email),
                    display_name=display_name,
                    description=description,
                    instructions=instructions or "",
                    category=category or None,
                    skill_metadata=skill_metadata or None,
                )
            )
        except UserSkillLimitError as exc:
            return _error(
                f"Could not create the skill: {exc} The user can delete a skill under "
                f"Customize → Skills ({CUSTOMIZE_SKILLS_PATH}); you cannot."
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a tool result
            return _failure("create the skill", exc)
        _log_applied("my_skill_create", skill.skill_id, session_id)
        return _ok(
            f'Created the skill "{skill.display_name}" (id: {skill.skill_id}). It is saved '
            "but turned off, and it cannot be used in this conversation. To try it, the "
            f"user turns it on under Customize → Skills ({CUSTOMIZE_SKILLS_PATH}) and then "
            "starts a new conversation. Tell the user this."
        )

    return my_skill_create


def make_my_skill_update_tool(session_id: str, user_id: str, user_email: Optional[str]):
    @tool
    def my_skill_update(
        skill_id: str,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        instructions: Optional[str] = None,
        category: Optional[str] = None,
        status: Optional[str] = None,
        skill_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Change fields of one of the user's own skills. The user approves it first.

        Pass only the fields to change; instructions are replaced whole, so read the
        skill first. The change reaches every agent or project that uses the skill
        unpinned, on its next turn.

        Args:
            skill_id: The skill's id, from my_skill_list.
            display_name: New name.
            description: New description.
            instructions: New full instructions (replaces the old ones).
            category: New category.
            status: "active", "draft" or "disabled".
            skill_metadata: New frontmatter fields (replaces the old ones).
        """
        updates: Dict[str, Any] = {
            k: v
            for k, v in {
                "display_name": display_name,
                "description": description,
                "instructions": instructions,
                "category": category,
                "skill_metadata": skill_metadata,
            }.items()
            if v is not None
        }
        if status is not None:
            if status not in _STATUSES:
                return _error(
                    f"Could not update the skill: status must be one of {', '.join(_STATUSES)}."
                )
            updates["status"] = SkillStatus(status)
        problems = _field_problems(display_name, description)
        if problems:
            return _error("Could not update the skill: " + "; ".join(problems) + ".")
        if not updates:
            return _error("Nothing to update: pass at least one field to change.")
        try:
            skill = _run(
                get_user_skill_service().update_my_skill(
                    skill_id, updates, _user(user_id, user_email)
                )
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a tool result
            return _failure(f"update skill '{skill_id}'", exc, skill_id)
        _log_applied("my_skill_update", skill_id, session_id)
        return _ok(
            f'Updated "{skill.display_name}" ({", ".join(sorted(updates))}). {_stale_note()}'
        )

    return my_skill_update


def make_my_skill_write_file_tool(session_id: str, user_id: str, user_email: Optional[str]):
    @tool
    def my_skill_write_file(
        skill_id: str,
        filename: str,
        content: str,
        kind: str = "reference",
    ) -> Dict[str, Any]:
        """Create or replace one text file in one of the user's own skills. The user
        approves it first.

        Writing an existing filename replaces it. Text formats only (.md, .txt,
        .json, .yaml, source code). Script files are stored but never run.

        Args:
            skill_id: The skill's id, from my_skill_list.
            filename: A flat name with an extension, e.g. "forms.md" (no folders).
            content: The file's full text.
            kind: "reference" (default), "script" or "asset".
        """
        if kind not in VALID_RESOURCE_KINDS:
            return _error(
                f"Could not write '{filename}': kind must be one of "
                f"{', '.join(VALID_RESOURCE_KINDS)}."
            )
        size = len((content or "").encode("utf-8"))
        if size > MAX_RESOURCE_BYTES:
            return _error(
                f"Could not write '{filename}': it is {size:,} bytes and the limit is "
                f"{MAX_RESOURCE_BYTES:,}. Split it into smaller files."
            )
        try:
            refs = _run(
                get_user_skill_service().write_text_file(
                    skill_id, kind, filename, content or "", _user(user_id, user_email)
                )
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a tool result
            return _failure(f"write '{filename}'", exc, skill_id)
        _log_applied("my_skill_write_file", skill_id, session_id)
        return _ok(
            f"Saved '{filename}' ({kind}, {size:,} bytes) to skill '{skill_id}', which now "
            f"has {len(refs)} of {MAX_RESOURCES_PER_SKILL} files. If this file was read "
            "earlier in this conversation, that copy is out of date."
        )

    return my_skill_write_file


def make_my_skill_delete_file_tool(session_id: str, user_id: str, user_email: Optional[str]):
    @tool
    def my_skill_delete_file(skill_id: str, filename: str) -> Dict[str, Any]:
        """Delete one file from one of the user's own skills. The user approves it first.

        Removes only that file; the skill itself stays.

        Args:
            skill_id: The skill's id, from my_skill_list.
            filename: The file's name as listed by my_skill_read.
        """
        try:
            refs = _run(
                get_user_skill_service().delete_resource(
                    skill_id, filename, _user(user_id, user_email)
                )
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a tool result
            return _failure(f"delete '{filename}'", exc, skill_id)
        _log_applied("my_skill_delete_file", skill_id, session_id)
        return _ok(
            f"Deleted '{filename}' from skill '{skill_id}', which now has {len(refs)} files."
        )

    return my_skill_delete_file


def build_skill_authoring_tools(
    session_id: str, user_id: str, user_email: Optional[str]
) -> list:
    """All seven tools, in a fixed order (it is part of the cached ``toolConfig``).

    The builder runs on every turn that has the tools, before the first token,
    and decorating seven ``@tool`` functions costs about 8 ms. The tools are pure
    closures over these three values and nothing mutates a registered tool, so
    the set built for a ``(session, user, email)`` is reused: a session pays the
    cost on its first turn and a dictionary lookup after that.
    """
    return list(_build_for(session_id, user_id, user_email))


@functools.lru_cache(maxsize=256)
def _build_for(session_id: str, user_id: str, user_email: Optional[str]) -> tuple:
    return (
        make_my_skill_list_tool(session_id, user_id, user_email),
        make_my_skill_read_tool(session_id, user_id, user_email),
        make_my_skill_read_file_tool(session_id, user_id, user_email),
        make_my_skill_create_tool(session_id, user_id, user_email),
        make_my_skill_update_tool(session_id, user_id, user_email),
        make_my_skill_write_file_tool(session_id, user_id, user_email),
        make_my_skill_delete_file_tool(session_id, user_id, user_email),
    )
