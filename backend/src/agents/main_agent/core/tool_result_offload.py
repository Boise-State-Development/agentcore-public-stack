"""
Tool-result offload at intake — the compaction escalation for oversized tool
results (docs/specs/compaction-model-relative-thresholds.md §3.6, PR-4).

The compaction cut keeps the last ``protected_turns`` turns whole. When one of
those turns carries a 90k-token tool result, the protected tail alone exceeds
the floor and no cut can get the session back under the ceiling
(``compaction_floor_unreachable``). Cutting inside the tail would drop the
turn the user is working on; the right move is to keep the *reference* and
drop the *bytes*.

Strands 1.55's vended ``ContextOffloader`` does exactly that at the cheapest
possible moment — ``AfterToolCallEvent``, before the result is appended to the
conversation. The persisted message already carries the bounded form, so a
restore reproduces it byte-for-byte and nothing in the prefix is ever
mutated after the fact (the byte-stability contract in CLAUDE.md). The model
keeps a preview and can pull any span back with ``retrieve_offloaded_content``
(pattern / line range / full).

What this module owns on top of the plugin:

- **Storage**: S3 in the user-files bucket under
  ``compaction-offload/{user_id}/{session_id}/`` — one namespaced storage per
  agent, so references are scoped to the session by construction (a session
  cannot retrieve another session's content) and a second agent instance for
  the same session (an ``@``-mention) resolves the same references.
- **No eviction from the model path.** ``evict_after_cycles=None``: the
  plugin's cycle-based eviction runs on ``BeforeModelCallEvent`` and would
  turn a retrieval into a miss mid-conversation. Objects expire by S3
  lifecycle instead (see the file-upload construct).
- **A cheap pre-filter.** The plugin sizes every result with
  ``model.count_tokens`` — a Bedrock CountTokens round trip per tool call.
  ``BoundedToolResultOffloader`` estimates with chars/4 first and only lets
  results near or over the gate reach the API.
- **A content-free record per offload** (``AgentCoreStack/Compaction``:
  ``ToolResultOffloaded`` + token count), so the data point "how much tool
  payload were we about to put in the prefix" exists.

Documents attached by the user are NOT handled here: the digest + page-range
read tool in ``document-context-offload.md`` is the right shape for those, and
this module deliberately does not strip them.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from agents.main_agent.config.constants import Defaults, EnvVars
from agents.main_agent.session.compaction_policy import estimate_message_tokens

logger = logging.getLogger(__name__)

# Below this fraction of the gate the chars/4 estimate is trusted outright
# and CountTokens is skipped. The heuristic is ~±25% on English text and JSON;
# half the gate leaves that margin twice over.
PREFILTER_RATIO = 0.5

# Tools whose results are never offloaded. ``document_read`` returns the page
# slice the model just asked for as a native document block; offloading it
# to S3 and handing back a text preview would undo the read and cost a second
# round trip (docs/specs/document-context-offload.md §4B). Its own
# ``max_pages`` cap is the bound.
OFFLOAD_EXEMPT_TOOLS = frozenset({"document_read"})


def tool_result_offload_enabled() -> bool:
    """Default ON with a kill switch (house style): only the literal "false" disables."""
    return os.environ.get(EnvVars.TOOL_RESULT_OFFLOAD_ENABLED, "").strip().lower() != "false"


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _import_plugin():
    from strands.vended_plugins.context_offloader import ContextOffloader

    return ContextOffloader


def _exempt_tool(event: Any) -> bool:
    """True when the event's tool is in ``OFFLOAD_EXEMPT_TOOLS``."""
    try:
        name = (getattr(event, "tool_use", None) or {}).get("name")
    except Exception:  # noqa: BLE001
        return False
    return name in OFFLOAD_EXEMPT_TOOLS


def _should_offload(tool_name: str, _token_count: int) -> bool:
    """The plugin's own ``should_offload`` hook — defense in depth behind the
    mixin's early return, for the path where the base class is reached."""
    return tool_name not in OFFLOAD_EXEMPT_TOOLS


class _OffloaderMixin:
    """Behavior layered on the vended plugin; kept separate so it can be tested
    against a stub base class without importing the real one."""

    async def _handle_tool_result(self, event: Any) -> None:  # type: ignore[override]
        try:
            result = getattr(event, "result", None)
            if not isinstance(result, dict):
                return
            content = result.get("content")
            if not isinstance(content, list):
                return
            if _exempt_tool(event):
                return
            # Cheap pre-filter: skip the CountTokens round trip for results
            # that cannot be anywhere near the gate.
            estimate = estimate_message_tokens({"role": "user", "content": [{"toolResult": result}]})
            if estimate < self._max_result_tokens * PREFILTER_RATIO:  # type: ignore[attr-defined]
                return
            before = event.result
            await super()._handle_tool_result(event)  # type: ignore[misc]
            if event.result is not before:
                self._record_offload(event, estimate)
        except Exception:  # noqa: BLE001 - offload is never worth a failed tool call
            logger.warning("tool-result offload skipped, keeping the original result", exc_info=True)

    @staticmethod
    def _record_offload(event: Any, estimate: int) -> None:
        tool_name = None
        try:
            tool_name = (getattr(event, "tool_use", None) or {}).get("name")
        except Exception:  # noqa: BLE001
            pass
        logger.info("tool_result_offloaded: tool=%s est_tokens=%d", tool_name, estimate)
        try:
            from apis.shared.observability.prompt_cache import prompt_cache_observability_enabled
            from apis.shared.observability.emf import emit_emf_metrics

            if prompt_cache_observability_enabled():
                emit_emf_metrics(
                    "AgentCoreStack/Compaction",
                    metrics={"ToolResultOffloaded": 1, "ToolResultOffloadedTokens": int(estimate)},
                    properties={"toolName": tool_name},
                    units={"ToolResultOffloadedTokens": "Count"},
                )
        except Exception as e:  # noqa: BLE001
            logger.debug("offload EMF skipped: %s", e)


def _offloader_class():
    """``BoundedToolResultOffloader`` built lazily so importing this module never
    imports the plugin (and boto3) on paths that do not use it."""
    ContextOffloader = _import_plugin()

    class BoundedToolResultOffloader(_OffloaderMixin, ContextOffloader):  # type: ignore[misc, valid-type]
        pass

    BoundedToolResultOffloader.__name__ = "BoundedToolResultOffloader"
    return BoundedToolResultOffloader


def offload_prefix(user_id: str, session_id: str) -> str:
    return f"{Defaults.TOOL_RESULT_OFFLOAD_S3_PREFIX}/{user_id}/{session_id}"


def build_tool_result_offloader(
    *,
    session_id: str,
    user_id: str,
    region: Optional[str] = None,
    storage: Any = None,
) -> Optional[Any]:
    """The plugin for one agent, or ``None`` when off or unconfigured (fail-open).

    ``storage`` overrides the S3 backend (tests). Returns ``None`` — never
    raises — when the flag is off, the user-files bucket is not configured,
    or the plugin cannot be constructed.
    """
    if not tool_result_offload_enabled():
        return None
    try:
        max_tokens = max(1, _int_env(EnvVars.TOOL_RESULT_OFFLOAD_MAX_TOKENS, Defaults.TOOL_RESULT_OFFLOAD_MAX_TOKENS))
        preview = _int_env(EnvVars.TOOL_RESULT_OFFLOAD_PREVIEW_TOKENS, Defaults.TOOL_RESULT_OFFLOAD_PREVIEW_TOKENS)
        preview = max(0, min(preview, max_tokens - 1))

        if storage is None:
            bucket = os.environ.get("S3_USER_FILES_BUCKET_NAME", "").strip()
            if not bucket:
                logger.info("tool-result offload disabled: S3_USER_FILES_BUCKET_NAME is not set")
                return None
            from strands.storage import S3Storage

            storage = S3Storage(
                bucket,
                prefix=offload_prefix(user_id, session_id),
                region_name=region or os.environ.get("AWS_REGION", "us-west-2"),
            )

        cls = _offloader_class()
        return cls(
            storage=storage,
            max_result_tokens=max_tokens,
            preview_tokens=preview,
            include_retrieval_tool=True,
            should_offload=_should_offload,
            evict_after_cycles=None,
        )
    except Exception:  # noqa: BLE001
        logger.warning("tool-result offload disabled: plugin construction failed", exc_info=True)
        return None
