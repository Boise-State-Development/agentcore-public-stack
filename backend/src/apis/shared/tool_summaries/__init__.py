"""Model-generated one-line summaries for finished tool batches.

The write side runs from the agents stream coordinator (where the batch is
born); the read side runs on the app-api messages endpoint. Both reach this
shared package and neither imports the other — import-boundary safe
(``tests/architecture/test_import_boundaries.py``).
"""

from apis.shared.tool_summaries.store import (
    ToolSummaryStore,
    get_tool_summary_store,
)
from apis.shared.tool_summaries.summarizer import summarize_tool_batch

__all__ = [
    "ToolSummaryStore",
    "get_tool_summary_store",
    "summarize_tool_batch",
]
