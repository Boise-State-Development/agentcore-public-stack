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
# Deliberately starting at ARTIFACT only. That spec's §6 asks for a
# single-builder experiment rather than more observational data, because the
# bypass→cache-write correlation is confounded by workload; artifacts are the
# clean arm (no `assistant_id`, no memory binding, ~957 sessions). The rest are
# excluded for now, each for a stated reason:
#
#   - SPREADSHEET: `make_*_tool(assistant_id, …)` closes over `assistant_id`,
#     which is NOT a key element. Needs the key (and `PausedTurnSnapshot`)
#     extended first, per that spec's §6.
#   - WORD / EXCEL / POWERPOINT / WORKSPACE: capture only session+user, so they
#     are *eligible on the same reasoning as artifacts* — held back only so the
#     experiment measures one variable. Promote them once the artifact arm reads
#     clean.
#   - Memory-Space tools: capture the resolved binding (space id + access) and
#     are not gated on `enabled_tools` at all, so they are not in any set here.
#     `get_agent`'s caller must treat a live memory binding as an independent
#     veto — see `injected_tools_are_key_described`.
KEY_DESCRIBED_INJECTED_TOOL_IDS = frozenset(ARTIFACT_TOOL_IDS)


def injected_tools_are_key_described(
    enabled_tools: list | frozenset | set | None,
    has_memory_binding: bool,
) -> bool:
    """Whether this turn's injected tools are fully described by the cache key.

    True means every per-request tool the turn built closes over only values the
    agent cache key already carries, so a cached agent from an earlier turn in
    the same slot holds equivalent closures and can be reused.

    Args:
        enabled_tools: The turn's *effective* enabled tool ids (an Agent's tool
            binding replaces the request's list — pass whatever reaches
            ``get_agent``, or the key and this predicate disagree).
        has_memory_binding: Whether a resolved Memory-Space binding produced
            tools this turn. An independent veto: those tools close over the
            binding, which is not in the key, and they are not gated on
            ``enabled_tools`` so no id here can represent them.

    Conservative in both directions that matter. A feature-flagged-off family
    whose id is still in ``enabled_tools`` counts as not-described even though
    it built nothing — that costs a cache bypass, never a wrong reuse.
    """
    if has_memory_binding:
        return False
    built = INJECTED_TOOL_IDS.intersection(enabled_tools or ())
    return built.issubset(KEY_DESCRIBED_INJECTED_TOOL_IDS)
