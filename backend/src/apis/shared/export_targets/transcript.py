"""Page a whole conversation into a single chronological message list.

Shared by app-api's export endpoint and the agent-side `save_conversation`
tool so both render an identical, complete transcript. Pure paging over
`get_messages` — no rendering, no provider I/O.
"""

import logging
from typing import List, Optional

from apis.shared.sessions.messages import get_messages
from apis.shared.sessions.models import MessageResponse

logger = logging.getLogger(__name__)

# Page the transcript out in chunks rather than one unbounded read. The cap is
# a runaway guard, not a product limit; if a conversation is somehow longer we
# log and export what we have rather than silently truncating without a trace.
_EXPORT_PAGE_SIZE = 200
_MAX_EXPORT_PAGES = 100


async def collect_transcript(session_id: str, user_id: str) -> List[MessageResponse]:
    """Page the whole conversation into a single chronological list.

    Pages are sequence-ordered and returned oldest-first, so concatenating
    them preserves chronology. Stops at the runaway-guard page cap.
    """
    messages: List[MessageResponse] = []
    next_token: Optional[str] = None
    for _ in range(_MAX_EXPORT_PAGES):
        page = await get_messages(
            session_id=session_id,
            user_id=user_id,
            limit=_EXPORT_PAGE_SIZE,
            next_token=next_token,
        )
        messages.extend(page.messages)
        next_token = page.next_token
        if not next_token:
            return messages
    logger.warning(
        "Export for session %s hit the %d-page cap; transcript may be truncated",
        session_id,
        _MAX_EXPORT_PAGES,
    )
    return messages
