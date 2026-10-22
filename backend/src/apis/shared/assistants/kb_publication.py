"""What publication means for a knowledge base's lifecycle.

Requirements 25.8–25.11. Three positions, and one question deliberately left open.

**An engine migration is not a corpus change (25.8).** Parity is the entire
contract of this migration: the same documents, the same ``top_k``, the same
context cap, the same answer model. So swapping which engine serves a published
agent's knowledge base does not change what that agent retrieves and therefore
needs no re-review. :func:`migration_requires_review` says so in one place, with a
test, rather than leaving it as an assumption spread across the worker.

**A listed agent's knowledge base is exempt from reclaim (25.9).** Nothing reclaims
in this phase — ``reclaim`` is reserved in the state enum and never entered — so
this is a guard placed before the mechanism that will need it. Written now because
the follow-up spec's eviction pass will be the first thing to delete a corpus, and
"is this on the store shelf right now?" is not a question it should be answering
for the first time under a deadline.

**A takedown must be walked, not fallen into (25.10).** Reclaim eligibility is
computed from the listing state as it is, and ``taken_down`` is reached only by the
listing state machine's explicit edge. Nothing here infers a takedown from a
missing listing, an expired timestamp, or an error.

**Corpus-revision pinning stays open (25.11).** A marketplace listing freezes a
knowledge base *reference*, not its contents, so a published agent's answers can
change after review without any re-review. That is a real review bypass and this
module does not pretend to close it: exemption from cleanup is not revision
pinning. The question belongs to the marketplace spec.

Why ``is_on_shelf`` and not ``is_listed``
----------------------------------------
The listing module documents the trap and it applies exactly here: an admin
requesting changes on a *live* listing leaves it serving but moves its state to
``changes_requested``, which is not in ``LISTED_STATES``. Asked by state name
alone, such an agent reads as unlisted — so a reclaim pass would delete the corpus
behind an agent users can still see in the store. ``is_on_shelf`` asks the fact
(is a version of this queryable in the store right now) rather than the state name,
and that is the only correct question for a destructive pass.

Feature: managed-kb-migration
Requirements: 25.8, 25.9, 25.10, 25.11
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional

from apis.shared.assistants.listing import is_on_shelf

logger = logging.getLogger(__name__)


def migration_requires_review(from_engine: Optional[str], to_engine: Optional[str]) -> bool:
    """Whether changing engines needs a listed agent to be reviewed again.

    Always ``False``, and a function rather than a comment so the claim is
    something a test can hold. An engine swap moves the same documents to a
    different index; the reviewed artefact — instructions, bindings, the corpus
    itself — is untouched. If a future change makes an engine swap alter retrieval
    results, this must stop returning ``False``, and the test that pins it is where
    that argument has to be had.
    """
    return False


def is_reclaim_exempt(
    kb_record: Optional[Mapping[str, Any]],
    listing_state: Optional[str] = None,
    published_version: Optional[int] = None,
) -> bool:
    """Whether this knowledge base must be left alone by a lifecycle reclaim pass.

    Exempt when any of these holds:

    * the record carries ``exemptFromReclaim`` — an operator's explicit hold;
    * the record is ``pinned``;
    * the agent is on the store shelf right now.

    A **missing** record — ``None``, which is what ``get_kb_record`` returns when
    it cannot find one — is exempt. Reclaim acts on knowledge bases it can
    describe, and "I could not read the record" is not a description; the same
    fail-closed reasoning the access check uses, applied to deletion, where it
    matters more. An *empty* mapping is a different thing: a record that was read
    and carries no holds. Treating the two alike would exempt every unheld
    knowledge base and make the whole predicate vacuous.
    """
    if kb_record is None:
        return True

    if kb_record.get("exemptFromReclaim") or kb_record.get("pinned"):
        return True

    return is_on_shelf(listing_state, published_version)


def reclaim_exemption_reason(
    kb_record: Optional[Mapping[str, Any]],
    listing_state: Optional[str] = None,
    published_version: Optional[int] = None,
) -> Optional[str]:
    """Why this knowledge base is exempt, or ``None`` if it is not.

    For the report-only output of a reclaim pass. A pass that logs "skipped 400
    knowledge bases" tells an operator nothing they can act on; one that says which
    are on the shelf and which an operator pinned by hand does.
    """
    if kb_record is None:
        return "no KB_Record could be read"
    if kb_record.get("exemptFromReclaim"):
        return "exemptFromReclaim is set on the record"
    if kb_record.get("pinned"):
        return "the knowledge base is pinned"
    if is_on_shelf(listing_state, published_version):
        return f"the agent is on the store shelf (listing state {listing_state!r})"
    return None


__all__ = [
    "is_reclaim_exempt",
    "migration_requires_review",
    "reclaim_exemption_reason",
]
