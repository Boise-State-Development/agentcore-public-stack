"""Counting how often tasks read memory files (Shared Projects 2.6b, ``STATS#{slug}``).

A project harness's ``memory_read`` calls :func:`record_read` after it has the
file. The count is never on the tool's return path: the call only hands one
``UpdateItem`` to a single background thread and returns, so the model's next
call waits on nothing (measured in the 2.6b spec entry). A turn counts each
file once, however often it reads it: the tool passes the turn's ``seen`` set,
which lives in Strands' per-invocation ``invocation_state``.

Best-effort: a count that fails is logged and lost, and never fails a read.
The thread owns its repository, because boto3 resources aren't thread-safe,
and one thread keeps the writes from ever competing with the turn for a pool.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-stats")
_local = threading.local()


def _repository():
    repo = getattr(_local, "repository", None)
    if repo is None:
        from .repository import MemorySpaceRepository

        repo = MemorySpaceRepository()
        _local.repository = repo
    return repo


def _record(space_id: str, slug: str, at: str) -> None:
    try:
        _repository().record_retrieval(space_id, slug, at)
    except Exception:  # noqa: BLE001 - a lost count is the whole cost of a failure
        logger.warning("memory-stats: could not count a read of %s in %s", slug, space_id, exc_info=True)


def record_read(
    space_id: str,
    slug: str,
    *,
    seen: Optional[Set[Tuple[str, str]]] = None,
    submit: Optional[Callable[..., object]] = None,
) -> bool:
    """Count one read of ``slug``, once per ``seen`` (a turn). Never blocks or raises.

    Returns whether a count was scheduled. ``submit`` replaces the background
    thread in tests.
    """
    key = (space_id, slug)
    if seen is not None:
        if key in seen:
            return False
        seen.add(key)
    at = datetime.now(timezone.utc).isoformat()
    try:
        (submit or _executor.submit)(_record, space_id, slug, at)
    except RuntimeError:  # the interpreter is shutting down
        return False
    return True
