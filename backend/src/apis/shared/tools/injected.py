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

# Document retrieval (bound to session/user). Listed for the record, and
# deliberately NOT folded into ``INJECTED_TOOL_IDS`` below: ``document_read``
# is gated on the session having a readable attachment, not on
# ``enabled_tools`` — there is no catalog entry, no RBAC grant and no picker
# toggle (docs/specs/document-context-offload.md §4B; the ``workspace_files``
# key it would otherwise hang off is granted to no prod role). Like the
# Memory-Space tools it never reaches ``ToolFilter``.
DOCUMENT_TOOL_IDS = frozenset({"document_read"})

# Every id owned by a per-request factory. Memory-Space and document tools are
# deliberately absent: they are gated on an Agent's memory binding (or a
# project harness) / the session's attachments rather than on
# ``enabled_tools``, so they never reach the filter.
INJECTED_TOOL_IDS = frozenset(
    SPREADSHEET_TOOL_IDS
    | ARTIFACT_TOOL_IDS
    | WORD_DOCUMENT_TOOL_IDS
    | EXCEL_SPREADSHEET_TOOL_IDS
    | POWERPOINT_PRESENTATION_TOOL_IDS
    | WORKSPACE_TOOL_IDS
)


# ============================================================
# Agent-cache eligibility
# ============================================================

# Ids whose factories capture *nothing the agent cache key does not already
# describe*. `_create_cache_key` carries `session_id`, `user_id` and a hash of
# `enabled_tools`, so a builder that closes over only those produces tools that
# are provably equivalent to freshly-built ones for any turn that hits the same
# slot — which is what makes reusing the cached agent safe.
#
# Why this set exists at all: `get_agent` bypassed the cache for *any*
# `extra_tools`, which reads as "this agent captured something the key doesn't
# describe". That is correct for two families and far too broad for the rest —
# it reaches 76% of sessions and 95% of spend, and makes every turn pay a full
# `initialize()` + AgentCore Memory restore (see
# docs/specs/agent-cache-extra-tools-bypass.md §1–§2).
#
# The set started at ARTIFACT only, as the single-variable arm of that spec's
# §6 experiment. The arm read clean (§8, 2026-08-05: hit/hit/hit after turn 1
# once #841 pinned sessions to a microVM, ~60% per-turn latency win, no
# prompt-cache regression), so the families that capture exactly what
# artifacts capture are promoted on the same reasoning. Each builder in
# `inference_api/chat/routes.py` closes over `(session_id, user_id)` and
# nothing else — both are key elements:
#
#   - WORD (`_build_word_document_tools`), EXCEL
#     (`_build_excel_spreadsheet_tools`), POWERPOINT
#     (`_build_powerpoint_presentation_tools`), WORKSPACE
#     (`_build_workspace_tools`).
#
#   - SPREADSHEET (`_build_spreadsheet_tools`) closes over `assistant_id` as
#     well. That was NOT a key element until `_create_cache_key` gained it,
#     alongside `PausedTurnSnapshot.assistant_id` and the resume path replaying
#     it — all three have to agree or a paused agent is orphaned. With the key
#     carrying it, the closure is described and the family is eligible. This
#     is the dominant cohort: the 2026-08-03 prod read put
#     `analyze_spreadsheet` on ~2,669 of 3,565 sessions.
#
# Memory-Space tools are not in any set here (they are not gated on
# `enabled_tools`), but they are described too: they close over the resolved
# binding (space id, name, access) plus `(user_id, user_email)`, and
# `_create_cache_key` carries a digest of the binding (Shared Projects 2.1).
# The caller must pass that binding to `get_agent` for the key to carry it;
# the tools themselves read the space live on every call. A project harness's
# scope-addressed family (Shared Projects 2.4b) is described the same way: it
# closes over the project and its two space ids, which reach the key through
# the same `memory_binding` element in a different shape.
KEY_DESCRIBED_INJECTED_TOOL_IDS = frozenset(
    ARTIFACT_TOOL_IDS
    | WORD_DOCUMENT_TOOL_IDS
    | EXCEL_SPREADSHEET_TOOL_IDS
    | POWERPOINT_PRESENTATION_TOOL_IDS
    | WORKSPACE_TOOL_IDS
    | SPREADSHEET_TOOL_IDS
)


def injected_tools_are_key_described(
    enabled_tools: list | frozenset | set | None,
) -> bool:
    """Whether this turn's injected tools are fully described by the cache key.

    True means every per-request tool the turn built closes over only values the
    agent cache key already carries, so a cached agent from an earlier turn in
    the same slot holds equivalent closures and can be reused.

    Args:
        enabled_tools: The turn's *effective* enabled tool ids (an Agent's tool
            binding replaces the request's list — pass whatever reaches
            ``get_agent``, or the key and this predicate disagree).

    Memory-Space tools need no argument here: the binding they close over is a
    cache-key element (``memory_binding`` on ``get_agent``), not something this
    predicate has to veto.

    Conservative in both directions that matter. A feature-flagged-off family
    whose id is still in ``enabled_tools`` counts as not-described even though
    it built nothing — that costs a cache bypass, never a wrong reuse.
    """
    built = INJECTED_TOOL_IDS.intersection(enabled_tools or ())
    return built.issubset(KEY_DESCRIBED_INJECTED_TOOL_IDS)
