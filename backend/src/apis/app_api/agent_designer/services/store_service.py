"""Agent Marketplace Phase 2 — the browse read (D4, D10).

The user-facing half of the marketplace. Everything here reads the sparse GSI5 index and
projects into ``AgentListingResponse``, which carries icon, name, tagline, publisher and
category — and nothing else.

Two properties are worth stating because they are easy to erode later:

* **The query cannot return an unpublished agent.** Not because it filters, but because
  an unpublished agent has no GSI5 key. Any future change that starts filtering
  ``listing.state`` in here is a sign the index has stopped being sparse, which is a
  bug in the write path, not something to compensate for on read.
* **The store read never carries behavior.** No ``instructions``, no binding refs, no
  owner id. ``AgentResponse`` exists for the surfaces that legitimately need those; the
  shelf is seen by every browsing user and gets the narrowest projection that renders.
"""

import logging
from typing import List, Optional, Tuple

from apis.shared.assistants.categories import ensure_seeded, list_categories
from apis.shared.assistants.icons import icon_url
from apis.shared.assistants.listing import is_published
from apis.shared.assistants.listing_repository import batch_get_agents, query_store
from apis.shared.assistants.models import (
    AgentCategory,
    AgentListingResponse,
    Assistant,
    ListingPublisher,
    PublisherProfile,
)
from apis.shared.assistants.publishers import list_publishers
from apis.shared.assistants.storefront import get_featured_ids

logger = logging.getLogger(__name__)

# How many shelves the no-category browse will sweep. Guards against a future category
# set large enough to turn one page load into dozens of queries.
_MAX_FANOUT_CATEGORIES = 12


def to_listing_response(
    assistant: Assistant, publishers: dict
) -> Optional[AgentListingResponse]:
    """Project a stored agent into the shelf shape, or ``None`` if it cannot render."""
    listing = assistant.listing
    if not listing:
        # Only reachable if a GSI5 key outlived its listing block, which the single-write
        # invariant in listing_repository is designed to prevent. Skip rather than crash
        # the whole shelf for one bad row.
        logger.warning(f"Indexed agent {assistant.assistant_id} has no listing block; skipping")
        return None

    profile: Optional[PublisherProfile] = publishers.get(listing.publisher_id)
    return AgentListingResponse(
        agent_id=assistant.assistant_id,
        name=assistant.name,
        tagline=assistant.tagline,
        emoji=assistant.emoji,
        # Phase 4: the uploaded icon when there is one, the generated gradient when not —
        # and the emoji above is what that gradient carries, so both always ship.
        icon_url=icon_url(assistant.assistant_id, assistant.icon_key),
        publisher=(
            ListingPublisher(label=profile.label, kind=profile.kind, verified=profile.verified)
            if profile
            else None
        ),
        category=listing.category,
    )


def _project(items: list, publishers: dict) -> List[AgentListingResponse]:
    responses = []
    for item in items:
        try:
            assistant = Assistant.model_validate(item)
        except Exception:
            logger.warning("Skipping unparseable store row", exc_info=True)
            continue
        projected = to_listing_response(assistant, publishers)
        if projected:
            responses.append(projected)
    return responses


async def browse_category(
    category: str, *, limit: int = 50, cursor: Optional[str] = None
) -> Tuple[List[AgentListingResponse], Optional[str]]:
    """One category's shelf, newest-first, with a real cursor (single GSI5 partition)."""
    publishers = {p.id: p for p in await list_publishers()}
    items, next_cursor = await query_store(category, limit=limit, cursor=cursor)
    return _project(items, publishers), next_cursor


async def browse_all(*, limit: int = 50) -> List[AgentListingResponse]:
    """The whole store, newest-first across every enabled category.

    Fans out one query per category and merge-sorts. **No cursor** — paging a merge
    across N partitions means carrying N cursors, and the honest options were a
    misleading half-cursor or none at all. Callers that need to page ask for a category,
    which is a single partition and pages properly.

    The Discover page renders per-category sections and does not use this path; it exists
    for search and for "everything" views.
    """
    categories = [c for c in await list_categories(enabled_only=True)]
    if len(categories) > _MAX_FANOUT_CATEGORIES:
        logger.warning(
            f"Store fan-out capped at {_MAX_FANOUT_CATEGORIES} of {len(categories)} categories; "
            "browse-all is no longer showing the whole store"
        )
        categories = categories[:_MAX_FANOUT_CATEGORIES]

    publishers = {p.id: p for p in await list_publishers()}

    merged: List[Tuple[str, AgentListingResponse]] = []
    for category in categories:
        items, _ = await query_store(category.id, limit=limit)
        for item, response in zip(items, _project(items, publishers)):
            merged.append((str(item.get("createdAt", "")), response))

    # Newest-first across the merged set, matching the per-category ordering.
    merged.sort(key=lambda pair: pair[0], reverse=True)
    return [response for _created, response in merged[:limit]]


async def resolve_featured(
    agent_ids: List[str],
) -> Tuple[List[AgentListingResponse], List[str]]:
    """Project the configured store-front ids into shelf rows, in the admin's order.

    Returns ``(rows, unavailable_ids)``. An id is *unavailable* when the Agent was deleted
    or its listing is no longer ``published`` — a takedown must drop it off the shelf, and
    the sparse-index physics that guarantee this for browse do not apply to a hand-curated
    id list, so the state check has to be explicit here.

    The unavailable ids are returned rather than swallowed so the admin console can show
    an admin why their row is short. Nothing on this path *writes*: a takedown that is
    later reversed restores the Agent to its slot (see ``storefront``).
    """
    if not agent_ids:
        return [], []

    records = await batch_get_agents(agent_ids)
    publishers = {p.id: p for p in await list_publishers()}

    rows: List[AgentListingResponse] = []
    unavailable: List[str] = []
    for agent_id in agent_ids:
        item = records.get(agent_id)
        if not item:
            unavailable.append(agent_id)
            continue
        try:
            assistant = Assistant.model_validate(item)
        except Exception:
            logger.warning(f"Skipping unparseable featured agent {agent_id}", exc_info=True)
            unavailable.append(agent_id)
            continue
        listing = assistant.listing
        if not listing or not is_published(listing.state):
            unavailable.append(agent_id)
            continue
        projected = to_listing_response(assistant, publishers)
        if projected:
            rows.append(projected)
        else:
            unavailable.append(agent_id)

    return rows, unavailable


async def store_front() -> Tuple[List[AgentListingResponse], List[AgentCategory]]:
    """The browse header: the featured row and the categories to render (D10).

    The featured row is the store's **only ranking lever** — everything below it is
    newest-first — so it is a hand-curated ordered list rather than anything derived.
    Entries that are no longer published are dropped silently here; the admin console is
    where that gap is named.
    """
    categories = await ensure_seeded()
    featured, _unavailable = await resolve_featured(await get_featured_ids())
    return featured, [c for c in categories if c.enabled]
