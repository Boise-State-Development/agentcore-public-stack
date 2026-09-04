"""Who may read a knowledge base — resolved before retrieval is attempted.

Requirement 25.1–25.3. The application is the authorization authority for
knowledge base reads. Neither of the two mechanisms Bedrock offers is trusted to
be that authority:

* **Metadata filters** give what AWS's own multi-tenant guidance calls
  "filter-level (logical) isolation, *not* IAM-enforced (infrastructure)
  isolation". A filter is a query argument; anything that can issue a query can
  omit it.
* **ACL-aware retrieval** fails closed, which is better than the document-status
  filter used to be, but AWS states plainly that it "is not authorization" and
  does not authenticate users. Its identity is **email only, with no alias
  resolution, and a mismatch fails silently**. On a platform that authenticates
  via OIDC with claim mappings, a silently-failing email comparison is a worse
  primitive than an explicit check against the permission model that already
  governs the agent.

So the check happens here, in the application, on the way in.

Why this lives in ``apis.shared.assistants`` and not in ``kb_backend``
---------------------------------------------------------------------
It reuses :func:`apis.shared.assistants.service.resolve_assistant_permission`
rather than introducing a parallel permission model (Requirement 25.2), and
``kb_backend`` may not import the assistants package — that boundary is what keeps
the migration Lambda images small, and it is enforced by
``tests/architecture/test_kb_backend_boundary.py``. Authorization is also
*above* the seam by nature: it is the same answer whichever engine serves the
query, so implementing it once above both adapters is the only way it cannot
differ between them.

Why the facade takes a resolved grant rather than a user
-------------------------------------------------------
Both production callers resolve the invoking user's permission a few lines before
they retrieve — ``inference_api/chat/routes.py`` via
``get_assistant_with_access_check``, ``app_api/assistants/routes.py`` via
``resolve_assistant_permission``. Re-resolving inside the facade would add a
second DynamoDB read per turn to answer a question the caller has already
answered.

Passing the answer instead is not weaker, because the parameter is **required and
keyword-only**: a caller that forgets it raises ``TypeError`` at the call site,
which no test suite can miss, while a caller that genuinely has no grant passes
``None`` and gets nothing back. Trusting a caller-supplied *string* would be
weaker; :class:`KbAccess` cannot be constructed with a permission outside
:data:`KB_READ_PERMISSIONS`, so "I have a grant object" is not something a caller
can assert without having gone through :func:`granted` or
:func:`resolve_kb_access`.

The 1:1 binding is what makes this simple
-----------------------------------------
This phase holds ``App_KB_Id == assistant_id``, so an agent's knowledge base is
exactly that agent's own and "may this user invoke this agent" already answers
"may this turn retrieve". There is deliberately no handling for the 0..N case —
whether one inaccessible knowledge base among several should fail the whole turn
is F4's question, and answering it here would bake in a guess.

Feature: managed-kb-migration
Requirements: 25.1, 25.2, 25.3
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

#: Permissions that may read a knowledge base through its agent. A viewer reads
#: (that is what sharing an agent is *for*) but never sees the upgrade control,
#: which is why the two sets below are separate rather than one ranked scale.
KB_READ_PERMISSIONS = frozenset({"owner", "editor", "viewer"})

#: Permissions that may change a knowledge base — upload, delete, or trigger a
#: migration. Deliberately a strict subset: an engine upgrade spends money and
#: mutates the corpus, so it is an owner/editor act.
KB_WRITE_PERMISSIONS = frozenset({"owner", "editor"})


class KbAccessDenied(PermissionError):
    """The invoking user may not read this knowledge base.

    Raised only by callers that want an error; :func:`granted` and
    :func:`resolve_kb_access` return ``None`` instead, because the retrieval path
    turns a denial into "no context" rather than into a failed turn.
    """


@dataclass(frozen=True)
class KbAccess:
    """A resolved grant to read one knowledge base.

    Frozen, and only ever produced by :func:`granted` or
    :func:`resolve_kb_access`, so its existence *is* the statement that the
    permission model was consulted. Holding a permission string proves nothing;
    holding one of these does.
    """

    assistant_id: str
    app_kb_id: str
    user_id: str
    permission: str

    @property
    def may_read(self) -> bool:
        """True for every instance. Kept as a named predicate rather than an
        implicit invariant so a call site reads as a check, and so the day a
        write-only grant is added there is somewhere for it to go."""
        return self.permission in KB_READ_PERMISSIONS

    @property
    def may_upgrade(self) -> bool:
        """Whether this grant may trigger a migration for the knowledge base."""
        return self.permission in KB_WRITE_PERMISSIONS


def granted(
    assistant_id: str,
    user_id: str,
    permission: Optional[str],
    app_kb_id: Optional[str] = None,
) -> Optional[KbAccess]:
    """Wrap an already-resolved permission as a grant, or ``None`` if it grants nothing.

    For the callers that resolved the permission themselves a moment earlier.
    ``None``, an empty string, and any unrecognized value all return ``None``:
    an unknown permission written by newer code is not evidence of access, and
    guessing in the permissive direction is how a viewer-shaped bug becomes a
    disclosure.

    ``app_kb_id`` defaults to ``assistant_id`` — the 1:1 binding this phase keeps.
    """
    if not permission or permission not in KB_READ_PERMISSIONS:
        logger.warning(
            f"knowledge base access denied for user {user_id} on assistant "
            f"{assistant_id}: permission {permission!r} does not grant read"
        )
        return None

    return KbAccess(
        assistant_id=assistant_id,
        app_kb_id=app_kb_id or assistant_id,
        user_id=user_id,
        permission=permission,
    )


async def resolve_kb_access(
    assistant_id: str,
    user_id: str,
    user_email: Optional[str] = None,
    app_kb_id: Optional[str] = None,
) -> Optional[KbAccess]:
    """Resolve a grant from the assistant permission model, failing closed.

    For callers that do not already hold a permission. Delegates to
    ``resolve_assistant_permission`` — the same function the document routes and
    the listing service gate on — so owner/editor/viewer semantics are whatever
    that function says they are and cannot drift here (Requirement 25.2).

    **Any** failure denies: a missing table, an unreachable table, a malformed
    record. This is the opposite of the resolver's choice to treat an unreadable
    KB_Record as legacy, and deliberately so. There, both answers serve the user's
    own documents and one of them is always safe. Here the two answers are "your
    documents" and "someone else's", and an error tells us which is which is
    exactly what we do not know (Requirement 24.6).
    """
    # Function-local: importing ``.service`` at module scope would run inside the
    # package ``__init__``'s own import of ``rag_service``, and the ordering that
    # makes that work today is not a property worth depending on.
    from apis.shared.assistants.service import resolve_assistant_permission

    try:
        _assistant, permission = await resolve_assistant_permission(
            assistant_id=assistant_id, user_id=user_id, user_email=user_email
        )
    except Exception as exc:
        logger.error(
            f"knowledge base access check failed for user {user_id} on assistant "
            f"{assistant_id}; denying because access cannot be confirmed: {exc}",
            exc_info=True,
        )
        return None

    return granted(assistant_id, user_id, permission, app_kb_id)


async def is_shared_beyond_owner(assistant_id: str, owner_id: str, visibility: Optional[str] = None) -> bool:
    """Whether anyone other than the owner can reach this assistant's documents.

    The input to Requirement 25.6's "where a knowledge base is shared beyond its
    owner". Lives here rather than in ``kb_backend`` for the same reason the access
    check does: it is an application fact, and the seam may not read it.

    Two independent sources, because either alone under-detects:

    * **Visibility.** ``PUBLIC`` shares with everyone and ``SHARED`` announces the
      intent, so neither is owner-only.
    * **Share records.** A ``PRIVATE`` assistant can still carry explicit shares —
      ``resolve_assistant_permission`` resolves an editor share on a private
      assistant to ``editor``. Judging by visibility alone would leave those
      knowledge bases unprotected, which is the case most likely to exist and least
      likely to be noticed.

    Fails **shared** on error. The two answers are "apply a narrowing policy that
    was not strictly needed" and "leave a multi-user corpus reachable by anything
    in the account with a wildcard grant"; the first costs one control-plane call.
    """
    if visibility in ("PUBLIC", "SHARED"):
        return True

    from apis.shared.assistants.service import list_assistant_shares

    try:
        shares = await list_assistant_shares(assistant_id, owner_id)
    except Exception as exc:
        logger.error(
            f"could not determine whether assistant {assistant_id} is shared; "
            f"assuming it is, so that a resource policy is applied rather than "
            f"skipped: {exc}",
            exc_info=True,
        )
        return True

    return bool(shares)


__all__ = [
    "KB_READ_PERMISSIONS",
    "KB_WRITE_PERMISSIONS",
    "KbAccess",
    "KbAccessDenied",
    "granted",
    "is_shared_beyond_owner",
    "resolve_kb_access",
]
