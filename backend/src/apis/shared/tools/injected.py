"""Catalog ids for *context-bound* tools built per request, not registered.

Most tools live in the ``ToolRegistry`` and are resolved by id. A handful
cannot: they need request scope (session, user, assistant) baked in at
construction time, so ``inference_api`` builds them per invocation with
factory functions and hands them to the agent as ``extra_tools``. See
``agents/builtin_tools/__init__.py`` — only static tools go in ``__all__``.

That makes them a fourth tool class, alongside registry / gateway /
external-MCP. ``ToolFilter`` has to know about it: without this set every
enabled id below falls through to the "unknown tool" branch and logs a
warning claiming the tool was skipped, while a separate code path injects
it and it works fine. In prod that was ~2.5k false warnings a day, drowning
the one signal that branch exists to give — a genuinely stale tool id
pinned in a saved session's ``enabledTools``.

**These are catalog/gate ids, not tool names.** One id can provision several
tools (``workspace_files`` → list + read + write), so the set cannot be
derived from the names of the objects the factories return.

Some families are additionally feature-flagged, so an id being listed here
means "a builder owns this id", not "a tool was necessarily produced". That
is the right granularity for the filter: a flagged-off family is a
deliberate gate, not an unknown tool.

Both sides import from here — ``inference_api`` to decide what to build and
``ToolFilter`` to classify — so there is a single definition to keep in sync.
Adding a ``_build_*_tools`` factory means adding its id(s) here.
"""

# Spreadsheet analysis via Code Interpreter (bound to assistant/session/user).
SPREADSHEET_TOOL_IDS = frozenset({"list_spreadsheets", "analyze_spreadsheet"})

# Artifact authoring (bound to session/user).
ARTIFACT_TOOL_IDS = frozenset({"create_artifact"})

# Generated-document authoring (bound to session/user).
WORD_DOCUMENT_TOOL_IDS = frozenset({"create_word_document"})
EXCEL_SPREADSHEET_TOOL_IDS = frozenset({"create_excel_spreadsheet"})
POWERPOINT_PRESENTATION_TOOL_IDS = frozenset({"create_powerpoint_presentation"})

# Session workspace files. A single toggle that provisions list/read/write.
WORKSPACE_TOOL_IDS = frozenset({"workspace_files"})

# Every id owned by a per-request factory. Memory-Space tools are deliberately
# absent: they are gated on an Agent's memory binding rather than on
# ``enabled_tools``, so they never reach the filter.
INJECTED_TOOL_IDS = frozenset(
    SPREADSHEET_TOOL_IDS
    | ARTIFACT_TOOL_IDS
    | WORD_DOCUMENT_TOOL_IDS
    | EXCEL_SPREADSHEET_TOOL_IDS
    | POWERPOINT_PRESENTATION_TOOL_IDS
    | WORKSPACE_TOOL_IDS
)
