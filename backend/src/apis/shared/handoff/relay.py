"""Relayed prompts: a paused target's prompts, read for its source thread (handoffs spec §4.3, PR-0.3c).

A handoff runs in the background so the user can stay in the source. When the target's turn pauses
on the user, the source shows that prompt under the handoff row and lets the user answer it there.
The prompt is the target's: this module only reads it, from the target's ``PendingInterrupt``
breadcrumbs, and the answer goes to the target's own resume.

What it never does:

- **Run on the turn path.** It is called from ``GET /messages`` (the ``handoffs`` replay) and the
  single-row ``GET .../handoffs/{id}`` the SPA reads when a target's turn ends, and nowhere else.
- **Reach a model.** The relay is UI. Nothing here is written to the source, so the source's
  history, system prompt and tool list (and the prefix the cache holds for them) never move.
- **Cross users.** The target's ``META`` row is read for ``(target, user)``; a target that is
  another user's has no row for this user and relays nothing, without an error.

Only a ``needs_input`` handoff is read, and only a pause that can still be answered counts: the
breadcrumbs AND an unexpired ``pausedTurn``, the same rule as the sidenav's awaiting-input mark
(``derive_awaiting_input``). A pause a fresh turn superseded has neither, so its relay goes too.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from pydantic import ValidationError

from apis.shared.handoff.models import HandoffView, RelayedPrompt
from apis.shared.sessions.metadata import SessionMetaSnapshot, load_session_meta
from apis.shared.sessions.models import PendingInterrupt, derive_awaiting_input

logger = logging.getLogger(__name__)

MetaLoader = Callable[[str, str], Awaitable[SessionMetaSnapshot]]

#: Breadcrumb kinds a source can relay. ``client_tool`` belongs to the terminal client that
#: declared it, never to a web thread.
_RELAYED_KINDS = frozenset({"oauth", "tool_approval", "user_question", "browser_login"})


def relayed_prompts_from_row(row: Optional[Dict[str, Any]]) -> List[RelayedPrompt]:
    """The prompts a target's ``META`` row is waiting on, as the source relays them.

    OAuth consents are deduplicated by ``providerId`` (the SPA's own rule for ``oauth_required``),
    since one connection answers every interrupt waiting on that provider. A browser sign-in
    carries only its interrupt id: the live view is the target's screen, so the relay links there.
    """
    if not row:
        return []
    raw = row.get("pendingInterrupts") or []
    paused = row.get("pausedTurn") or {}
    interrupts: List[PendingInterrupt] = []
    for item in raw:
        try:
            interrupts.append(PendingInterrupt.model_validate(item))
        except ValidationError:
            logger.warning("Skipping an unreadable pending interrupt on a handoff target")
    expires_at = paused.get("expiresAt") if isinstance(paused, dict) else None
    if derive_awaiting_input((i.kind for i in interrupts), expires_at) is None:
        return []

    prompts: List[RelayedPrompt] = []
    providers: set[str] = set()
    for interrupt in interrupts:
        if interrupt.kind not in _RELAYED_KINDS:
            continue
        if interrupt.kind == "oauth":
            if not interrupt.provider_id or interrupt.provider_id in providers:
                continue
            providers.add(interrupt.provider_id)
        prompts.append(RelayedPrompt.from_interrupt(interrupt))
    return prompts


async def load_relayed_prompts(
    views: Iterable[HandoffView],
    user_id: str,
    meta_loader: Optional[MetaLoader] = None,
) -> None:
    """Fill ``relayed_prompts`` on each ``needs_input`` view, in place. Never raises.

    One ``META`` read per waiting handoff, all at once. Every other status reads nothing.
    """
    waiting = [v for v in views if v.status == "needs_input" and v.target_session_id]
    if not waiting:
        return
    loader = meta_loader or load_session_meta

    async def one(view: HandoffView) -> None:
        try:
            meta = await loader(view.target_session_id or "", user_id)
        except Exception:  # noqa: BLE001 - a relay that fails leaves the row's amber mark and link
            logger.warning("Relayed prompt read failed", exc_info=True)
            return
        view.relayed_prompts = relayed_prompts_from_row(meta.row)

    await asyncio.gather(*(one(v) for v in waiting))


__all__ = ["load_relayed_prompts", "relayed_prompts_from_row"]
