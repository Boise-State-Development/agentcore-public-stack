"""Which backend serves this knowledge base.

One question, answered in one place: read the KB_Record's ``retrievalEngine`` and
hand back the matching implementation. Callers get an object satisfying
:class:`~apis.shared.kb_backend.protocol.KnowledgeBaseBackend` and are given no
way to ask which one it is.

Absence means legacy
--------------------
The decision itself is delegated to
:func:`apis.shared.kb_backend.records.resolve_engine` rather than re-derived
here. That function is the one place that knows a missing ``retrievalEngine``
attribute means the legacy backend, and it is covered by its own property test
(task 3.3). Two implementations of the same default would be two chances to
disagree about the invariant that lets 1,692 existing knowledge bases keep
working with zero backfill writes.

Resolution never fails a turn
-----------------------------
The KB_Record lookup is a DynamoDB read that today's retrieval path does not
perform, so it is a new way for retrieval to break. It is therefore wrapped: any
failure — unreachable table, unset ``DYNAMODB_ASSISTANTS_TABLE_NAME``, malformed
item — resolves to legacy, which is what every knowledge base in existence
already uses. The failure is logged at warning level. Choosing legacy on an
unreadable record is not a guess; it is the same answer the absent attribute
gives, and the whole migration is built so that answer is always safe.

Why both backends are registered at import
------------------------------------------
Registration is not a startup step. Both adapters are installed in
:data:`_BACKENDS` when this module is imported, so there is no sequence anybody
has to remember and no service that can come up half-configured. That matters
because forgetting would not be loud in a useful way: a promoted knowledge base
would raise :class:`BackendUnavailable` on every turn — correct as a fail-safe,
useless as a signal, and only ever seen by the one user whose knowledge base was
migrated.

It costs nothing. Both adapter modules import stdlib and the protocol only, with
``boto3`` and their clients created lazily inside methods, which
``tests/architecture/test_kb_backend_boundary.py`` asserts in a fresh
interpreter. Registering an object whose constructor does no work is not the same
as connecting to anything.

Registering the managed backend does **not** make the feature live. Nothing can
resolve to it until a record says ``retrievalEngine == "managed"``, and nothing
writes that value except a promotion, which needs the migration flag on and an
explicit opt-in. Registration only settles what happens *once* a record says so.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional

from apis.shared.kb_backend.managed_backend import ManagedKbBackend
from apis.shared.kb_backend.protocol import KnowledgeBaseBackend
from apis.shared.kb_backend.records import ENGINE_LEGACY, ENGINE_MANAGED, resolve_engine
from apis.shared.kb_backend.s3vectors_backend import S3VectorsBackend

logger = logging.getLogger(__name__)


class BackendUnavailable(RuntimeError):
    """A record names an engine this build has no implementation for.

    Raised rather than quietly falling back to legacy. A record only ever names
    ``managed`` after a successful promotion, and serving legacy for a promoted
    knowledge base would read an index that migration has stopped maintaining —
    fewer results, silently, with no error to notice.

    Reachable only if a backend is explicitly unregistered (which tests do) or if
    a future engine name is written by a newer deployment than the one reading it.
    """


# Engine → backend, populated at import. See the module docstring for why this is
# not a startup step. Both constructors are inert: clients are created lazily.
_BACKENDS: Dict[str, KnowledgeBaseBackend] = {
    ENGINE_LEGACY: S3VectorsBackend(),
    ENGINE_MANAGED: ManagedKbBackend(),
}


def register_backend(engine: str, backend: KnowledgeBaseBackend) -> None:
    """Install the implementation for ``engine``, replacing any previous one."""
    _BACKENDS[engine] = backend


def unregister_backend(engine: str) -> None:
    """Remove ``engine``'s implementation. Absent engines are ignored."""
    _BACKENDS.pop(engine, None)


def registered_engines() -> frozenset:
    """Engines this build can serve. Introspection for tests and diagnostics."""
    return frozenset(_BACKENDS)


def load_record(
    assistant_id: str,
    app_kb_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The KB_Record, or an empty mapping if there is none to be had.

    For callers that need more from the record than which backend serves it — the
    dual-read pilot flag, the byte cap, the migration state — and would otherwise
    read it a second time.

    Returns ``{}`` rather than ``None`` for both "no such record" and "the read
    failed", because those two cases have the same answer everywhere in this
    feature: an absent opinion is the legacy opinion. Collapsing them here means
    no caller has to remember to handle ``None`` and every caller can pass the
    result straight to :func:`resolve_backend` as ``record=``, which is what makes
    one read enough.
    """
    from apis.shared.kb_backend.records import get_kb_record

    try:
        return dict(get_kb_record(assistant_id, app_kb_id or assistant_id) or {})
    except Exception as exc:
        logger.warning(
            f"KB_Record lookup failed for assistant {assistant_id}; treating it as "
            f"absent, which resolves to {ENGINE_LEGACY}: {exc}"
        )
        return {}


def backend_for_engine(engine: str) -> Optional[KnowledgeBaseBackend]:
    """The implementation for ``engine``, or ``None`` if this build has none.

    Unlike :func:`resolve_backend` this does not raise, because its callers are
    asking a different question. The dual-read pilot wants "is there a managed
    backend I could compare against?", and ``None`` is an ordinary answer for it —
    a test that unregistered one, or a deployment older than the engine name it was
    handed — not the fail-safe emergency that an unservable *promoted* record is.
    """
    return _BACKENDS.get(engine)


def resolve_engine_for(
    assistant_id: str,
    app_kb_id: Optional[str] = None,
    record: Optional[Mapping[str, Any]] = None,
) -> str:
    """Return the engine name for a knowledge base.

    Pass ``record`` when the caller already holds the KB_Record to skip the
    read. ``app_kb_id`` defaults to ``assistant_id``, which is the 1:1 binding
    this phase deliberately preserves.
    """
    if record is not None:
        return resolve_engine(record)

    from apis.shared.kb_backend.records import get_kb_record

    try:
        item = get_kb_record(assistant_id, app_kb_id or assistant_id)
    except Exception as exc:
        # Unreadable record ⇒ legacy, the same answer absence gives.
        logger.warning(
            f"KB_Record lookup failed for assistant {assistant_id}, "
            f"resolving to {ENGINE_LEGACY}: {exc}"
        )
        return ENGINE_LEGACY

    return resolve_engine(item)


def resolve_backend(
    assistant_id: str,
    app_kb_id: Optional[str] = None,
    record: Optional[Mapping[str, Any]] = None,
) -> KnowledgeBaseBackend:
    """Return the backend instance that should serve this knowledge base."""
    engine = resolve_engine_for(assistant_id, app_kb_id, record)
    try:
        return _BACKENDS[engine]
    except KeyError:
        raise BackendUnavailable(
            f"knowledge base {app_kb_id or assistant_id} names engine {engine!r}, "
            f"which this build cannot serve (have: {sorted(_BACKENDS)}). "
            f"Refusing to substitute {ENGINE_LEGACY}: a promoted knowledge base's "
            f"legacy index is no longer maintained."
        ) from None


__all__ = [
    "BackendUnavailable",
    "ENGINE_LEGACY",
    "ENGINE_MANAGED",
    "backend_for_engine",
    "load_record",
    "register_backend",
    "registered_engines",
    "resolve_backend",
    "resolve_engine_for",
    "unregister_backend",
]
