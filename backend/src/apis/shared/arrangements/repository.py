"""DynamoDB storage for list arrangements.

One item per user per surface on the **user-settings** table, beside
``PINNED_AGENTS``:

    PK = USER#{user_id}, SK = "ARRANGEMENT#agents" | "ARRANGEMENT#projects"
    { sort, order: [id, ...], hidden: [id, ...], updatedAt }

Not a field on the ``SETTINGS`` item: the turn path reads that item on every
turn for personal instructions and memory mode, and an arrangement can be
hundreds of ids (spec §5). This item is read only by the two list pages.

A ``put`` replaces the item; two tabs saving at once is last write wins, the
trade-off ``PINNED_AGENTS`` makes. The boto3 calls run in a worker thread so a
slow read never stalls the event loop.
"""

import asyncio
import logging
import os
from typing import Optional

import boto3

from apis.shared.timestamps import utc_now_iso

from .models import Arrangement, ArrangementSurface, ArrangementUpdate

logger = logging.getLogger(__name__)

_SK_PREFIX = "ARRANGEMENT#"


class ArrangementStorageUnavailable(RuntimeError):
    """The user-settings table is not configured on this server."""


class ArrangementRepository:
    """Reads and writes one user's arrangement of one surface."""

    def __init__(self, table_name: Optional[str] = None):
        if table_name is None:
            table_name = os.getenv("DYNAMODB_USER_SETTINGS_TABLE_NAME", "")
        self._table = boto3.resource("dynamodb").Table(table_name) if table_name else None

    @property
    def enabled(self) -> bool:
        return self._table is not None

    @staticmethod
    def _key(user_id: str, surface: ArrangementSurface) -> dict:
        return {"PK": f"USER#{user_id}", "SK": f"{_SK_PREFIX}{surface}"}

    async def get(self, user_id: str, surface: ArrangementSurface) -> Arrangement:
        """The stored arrangement, or the defaults when there is none."""
        if self._table is None:
            return Arrangement()
        response = await asyncio.to_thread(self._table.get_item, Key=self._key(user_id, surface))
        item = response.get("Item")
        if not item:
            return Arrangement()
        return Arrangement.model_validate(
            {
                "sort": item.get("sort", "recent"),
                "order": item.get("order", []),
                "hidden": item.get("hidden", []),
                "updatedAt": item.get("updatedAt"),
            }
        )

    async def put(
        self, user_id: str, surface: ArrangementSurface, update: ArrangementUpdate
    ) -> Arrangement:
        """Replace the stored arrangement with ``update``."""
        if self._table is None:
            raise ArrangementStorageUnavailable("DYNAMODB_USER_SETTINGS_TABLE_NAME is not configured")
        stored = Arrangement(
            sort=update.sort,
            order=list(update.order),
            hidden=list(update.hidden),
            updated_at=utc_now_iso(),
        )
        await asyncio.to_thread(
            self._table.put_item,
            Item={
                **self._key(user_id, surface),
                "sort": stored.sort,
                "order": stored.order,
                "hidden": stored.hidden,
                "updatedAt": stored.updated_at,
            },
        )
        return stored
