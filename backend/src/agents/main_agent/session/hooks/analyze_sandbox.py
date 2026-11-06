"""Scopes the ``analyze_spreadsheet`` Code Interpreter sandbox to one agent turn.

``BeforeInvocationEvent`` registers a turn token for the conversation, which is
what lets ``analyze_spreadsheet`` keep its sandbox (and the files already
staged in it) alive between calls. ``AfterInvocationEvent`` stops the sandbox.
Strands fires it from a ``finally`` around each invocation, so it runs on
normal completion, on an exception, when the stream is closed, and on an
interrupt (OAuth consent, tool approval, ``ask_user_question``). An interrupt
therefore ends the sandbox and the resume stages the file again. That costs one
re-stage and keeps no session open while a person decides.

The stream coordinator also calls :meth:`AnalyzeSandboxHook.release` from its
own ``finally``, because a dropped client connection does not always let the
agent's ``finally`` run promptly. Releasing is idempotent and a stale token is
a no-op. The sandbox's short remote timeout is the last line of defense.

See ``agents/builtin_tools/spreadsheet_analysis/sandbox_pool.py`` and #259.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from strands.hooks import (
    AfterInvocationEvent,
    BeforeInvocationEvent,
    HookProvider,
    HookRegistry,
)

from agents.builtin_tools.spreadsheet_analysis import sandbox_pool

logger = logging.getLogger(__name__)


class AnalyzeSandboxHook(HookProvider):
    """Registers and releases the per-turn sandbox token for one conversation."""

    def __init__(self, session_id: str, user_id: str) -> None:
        self._session_id = session_id
        self._user_id = user_id
        self._token: Optional[str] = None

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_turn_start)
        registry.add_callback(AfterInvocationEvent, self._on_turn_end)

    async def _on_turn_start(self, event: BeforeInvocationEvent) -> None:
        # A turn that never reached its end (killed task) leaves a token and
        # maybe a sandbox behind; begin_turn replaces and stops both.
        try:
            self._token = await sandbox_pool.begin_turn(self._session_id, self._user_id)
        except Exception:  # noqa: BLE001 - never block a turn on sandbox bookkeeping
            self._token = None
            logger.warning("analyze_sandbox begin_turn failed", exc_info=True)

    async def _on_turn_end(self, event: AfterInvocationEvent) -> None:
        await self.release()

    async def release(self) -> None:
        """Stop this turn's sandbox. Safe to call more than once."""
        token, self._token = self._token, None
        if not token:
            return
        try:
            await sandbox_pool.end_turn(self._session_id, self._user_id, token)
        except Exception:  # noqa: BLE001 - the remote timeout is the backstop
            logger.warning("analyze_sandbox end_turn failed", exc_info=True)
