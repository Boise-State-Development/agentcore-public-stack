"""What kinds of file a handoff or delivery carries in, for the turn's tool gates (handoffs spec §4.9).

Three gates decide tools from the conversation's files: ``document_read`` (a readable document),
the Spreadsheet Analysis tools (a CSV or workbook) and the PowerPoint tools (a deck). Each one
queries the conversation's READY uploads. A shared file's reference row is written during the
stream, after those queries ran, so on the first turn none of them could see it. The gate then
answered no on turn 1 and yes on turn 2, which rewrote the whole cached prefix once and left the
model without the tool on the very turn the file arrived.

The send's files are read before the lease (``HandoffTurn.prefetch`` / ``DeliveryTurn.prefetch``),
so they are classified there, once, into the kinds below. Each gate then awaits its own kind's hint
beside the query it runs anyway, and only when its memo has not already answered yes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, FrozenSet, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

KIND_DOCUMENT = "document"
KIND_TABULAR = "tabular"
KIND_PRESENTATION = "presentation"


def file_kinds(files: Iterable[Tuple[str, str]]) -> FrozenSet[str]:
    """The gate kinds present among ``(mime_type, filename)`` pairs.

    The same predicates the three session queries apply to READY uploads, so a hint never says
    yes where the next turn's query would say no.
    """
    from apis.shared.files.document_read import is_document_class
    from apis.shared.files.models import is_presentation_file, is_tabular_file

    kinds = set()
    for mime_type, filename in files:
        if is_document_class(mime_type, filename):
            kinds.add(KIND_DOCUMENT)
        if is_tabular_file(filename, mime_type):
            kinds.add(KIND_TABULAR)
        if is_presentation_file(filename, mime_type):
            kinds.add(KIND_PRESENTATION)
    return frozenset(kinds)


class FileKindHints:
    """One hint per kind, all answered by one classification task."""

    def __init__(self, kinds: "asyncio.Task[FrozenSet[str]]") -> None:
        self._kinds = kinds
        self._hints: Dict[str, "asyncio.Task[bool]"] = {}

    def hint(self, kind: str) -> "asyncio.Task[bool]":
        """Whether the shared files include ``kind``. Never raises: a failed read is "no"."""
        task = self._hints.get(kind)
        if task is None:
            task = asyncio.ensure_future(self._has(kind))
            self._hints[kind] = task
        return task

    async def _has(self, kind: str) -> bool:
        try:
            return kind in await self._kinds
        except Exception:  # noqa: BLE001 - a failed hint is "no", as before the feature
            logger.warning("shared file classification failed; gates answer from their queries", exc_info=True)
            return False


def any_hint(*hints: Optional["asyncio.Task[bool]"]) -> Optional["asyncio.Task[bool]"]:
    """One hint that is yes when any given hint is; ``None`` when none is given."""
    present = [h for h in hints if h is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]

    async def _any() -> bool:
        answers = await asyncio.gather(*present, return_exceptions=True)
        return any(a is True for a in answers)

    return asyncio.ensure_future(_any())
