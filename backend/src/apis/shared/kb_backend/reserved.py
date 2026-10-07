"""App KB ids the platform owns, which no assistant may take.

A Managed Knowledge Base's KB_Record lives at ``AST#{assistant_id} / KB#{app_kb_id}``
in the assistants table. The conversation-search index (docs/specs/conversation-search.md
§4) is one shared knowledge base per environment, and it keeps its record at
``AST#conversations / KB#conversations`` so the kb-migration reconciler, which
treats an AWS knowledge base with no record as an orphan, sees it as owned.

That puts a platform record in the assistants' key space. Assistant ids are
generated (``ast-{hex}``), so no assistant can be *created* with a reserved id,
but a route that takes an assistant id from a URL must not resolve one either:
reading, sharing, tearing down or deleting ``conversations`` as if it were an
agent would surface or destroy every user's search index. :func:`is_reserved_kb_id`
is the single check those choke points call.

Stdlib only, like the rest of this package
(``tests/architecture/test_kb_backend_boundary.py``).
"""

from __future__ import annotations

from typing import Any

#: The conversation-search knowledge base: ``assistant_id`` and ``app_kb_id`` both.
CONVERSATIONS_KB_ID = "conversations"

#: Every reserved id. Compared exactly, never by prefix: ``conversations-1`` is
#: an ordinary (if unlikely) id.
RESERVED_KB_IDS = frozenset({CONVERSATIONS_KB_ID})


def is_reserved_kb_id(value: Any) -> bool:
    """Whether ``value`` names a platform-owned knowledge base."""
    return isinstance(value, str) and value in RESERVED_KB_IDS


__all__ = ["CONVERSATIONS_KB_ID", "RESERVED_KB_IDS", "is_reserved_kb_id"]
