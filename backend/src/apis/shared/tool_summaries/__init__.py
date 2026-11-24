"""Model-generated one-line summaries for finished tool batches.

The write side runs from the agents stream coordinator (where the batch is
born); the read side runs on the app-api messages endpoint. Both reach this
shared package and neither imports the other — import-boundary safe
(``tests/architecture/test_import_boundaries.py``).

The summarizer is imported from ``apis.shared.tool_summaries.summarizer`` and is
deliberately not re-exported here: it resolves the admin's task model through the
model catalog, and the readers of this package (the scheduled-runs worker image among
them) need only the store.
"""

from apis.shared.tool_summaries.store import (
    ToolSummaryStore,
    get_tool_summary_store,
)

__all__ = [
    "ToolSummaryStore",
    "get_tool_summary_store",
]
