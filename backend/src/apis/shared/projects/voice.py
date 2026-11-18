"""Whether a project's conversations can run in voice mode (#2021).

Voice is on for every project unless its owner switches it off
(``ProjectSettings.voice_enabled``). It is also off, whatever the switch says,
while either of these holds, because voice would run the project worse than text:

* **A bound tool is not available in voice.** Nova 2 Sonic goes quiet for the
  rest of a session when a tool takes longer than about a second, so voice loads
  only tools an admin marked ``voiceAvailable`` (#625). A project whose voice
  session silently lost one of its tools would answer without it, so the project
  is kept out of voice instead.
* **The project's files are too large for voice.** Voice has no retrieval step:
  the files reach it whole, under :func:`kb_full_context.voice_max_tokens`, or
  not at all.

Nothing is stored for the automatic part. The status is worked out when it is
asked for, so removing the tool or the file turns voice back on by itself, and
the owner's own choice is never overwritten.

Both the app-api settings route and the voice route call :func:`project_voice_status`,
so the warning the owner sees and the refusal a member gets cannot disagree.
Access is the caller's job: neither function checks who is asking.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

from apis.shared.projects.models import Project

logger = logging.getLogger(__name__)

#: ``kb_full_context`` reasons that mean the files are too large to reach voice.
_TOO_LARGE_REASONS = ("over_budget", "document_over_budget", "raw_bytes")

#: The deployment keeps knowledge bases out of voice (``VOICE_KB_MAX_TOKENS=0``,
#: or full context switched off) and the project has files.
FILES_OFF = "voice_files_off"


@dataclass(frozen=True)
class FilesTooLarge:
    """Why the project's files cannot reach voice."""

    #: A ``kb_full_context`` ``REASON_*`` value, or :data:`FILES_OFF`.
    reason: str
    estimated_tokens: int = 0
    budget_tokens: int = 0


@dataclass(frozen=True)
class ProjectVoiceStatus:
    #: The owner's switch.
    enabled: bool
    #: Bound tools (base ids) that voice cannot run, in binding order.
    blocked_tool_ids: Tuple[str, ...] = field(default_factory=tuple)
    files: Optional[FilesTooLarge] = None

    @property
    def blocked(self) -> bool:
        """Voice is off for a reason the owner's switch cannot override."""
        return bool(self.blocked_tool_ids) or self.files is not None

    @property
    def available(self) -> bool:
        return self.enabled and not self.blocked


def bound_tool_ids(harness) -> List[str]:
    """The tool ids a project's harness binds, in binding order."""
    return [b.ref for b in (getattr(harness, "bindings", None) or []) if b.kind == "tool"]


async def tools_voice_cannot_run(tool_ids: Iterable[str]) -> List[str]:
    """The base ids among ``tool_ids`` that would be left out of a voice session.

    A disabled tool is not counted: it never loads, in text or in voice. Fails
    closed with the voice filter (``keep_voice_tools``): if the catalog can't be
    read, every bound tool counts, because voice would run without any of them.
    """
    from apis.shared.tools.freshness import get_disabled_tool_ids, get_voice_tool_ids
    from apis.shared.tools.scoped_ids import base_tool_id

    ids = [base_tool_id(t) for t in tool_ids]
    if not ids:
        return []
    voice, disabled = await asyncio.gather(get_voice_tool_ids(), get_disabled_tool_ids())
    out: List[str] = []
    for tid in ids:
        if tid in voice or tid in disabled or tid in out:
            continue
        out.append(tid)
    return out


def files_too_large_for_voice(harness_agent_id: str) -> Optional[FilesTooLarge]:
    """Why the project's files can't reach voice, or None when they can. Blocking.

    Uses :func:`kb_full_context.assess_full_context`, the function the voice
    session loads the files with, under the same budget. Only size counts: a
    project whose files have no text voice can read (a scan, only spreadsheets)
    still runs, because ``project_files`` lists them and the knowledge base
    names the skipped ones.
    """
    from apis.shared.assistants import kb_full_context as fc
    from apis.shared.feature_flags import assistant_kb_full_context_enabled

    budget = fc.voice_max_tokens()
    if not assistant_kb_full_context_enabled() or budget <= 0:
        from apis.shared.assistants.kb_documents import list_kb_documents

        has_files = any(d.get("status") == "complete" for d in list_kb_documents(harness_agent_id))
        return FilesTooLarge(reason=FILES_OFF, budget_tokens=max(budget, 0)) if has_files else None
    found = fc.assess_full_context(harness_agent_id, budget)
    if found.reason not in _TOO_LARGE_REASONS:
        return None
    return FilesTooLarge(reason=found.reason, estimated_tokens=found.estimated_tokens, budget_tokens=budget)


async def project_voice_status(project: Project, harness, *, check_files: bool = True) -> ProjectVoiceStatus:
    """The project's voice status: the owner's switch and anything that keeps voice off.

    ``check_files=False`` skips the files check, for a caller that already knows
    the files reached voice (the voice route, after loading them).
    """
    enabled = project.settings.voice_enabled
    tools_task = tools_voice_cannot_run(bound_tool_ids(harness))
    if check_files:
        blocked_tools, files = await asyncio.gather(
            tools_task, asyncio.to_thread(files_too_large_for_voice, project.harness_agent_id)
        )
    else:
        blocked_tools, files = await tools_task, None
    return ProjectVoiceStatus(enabled=enabled, blocked_tool_ids=tuple(blocked_tools), files=files)
