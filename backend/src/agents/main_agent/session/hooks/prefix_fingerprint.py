"""Hook that fingerprints the cacheable request prefix before each model call.

Bedrock prompt caching is exact-prefix-match: toolConfig, then system prompt,
then message history must be byte-identical to the previous call for cached
tokens to be read. When a session shows ``cacheStatus=miss_avoidable`` on its
metadata rows, these three hashes — persisted per call alongside tokenUsage —
show *which* component diverged between consecutive calls, replacing hours of
raw-row forensics with a column diff.

On every ``BeforeModelCallEvent`` the hook computes:

- ``toolConfigHash``: canonical JSON of the tool specs the model will see, in
  registration order (order-sensitive on purpose — order flips are a real
  cache-buster).
- ``systemPromptHash``: the effective system prompt. Fires after plugin
  injection (e.g. AgentSkills' ``<available_skills>`` block lands on
  ``BeforeInvocationEvent``), so nondeterministic skill ordering is visible
  here.
- ``historyHash``: ``agent.messages`` excluding the newest message — the
  prior-history prefix that must match the previous call's full history for
  a cache hit.

Fingerprints accumulate in a per-turn list on the agent (one entry per model
call; a tool-use turn makes several). The stream coordinator resets the list
at turn start and attaches entry N to the Nth assistant message's metadata
row. Best-effort: any failure is swallowed so fingerprinting can never break
a model call.
"""

import logging
from typing import Any, Dict, List, Optional

from strands.hooks import BeforeModelCallEvent, HookProvider, HookRegistry

from apis.shared.observability import fingerprint_canonical_json, fingerprint_text

logger = logging.getLogger(__name__)

# Per-turn list of fingerprint dicts, stashed on the Strands agent instance.
_FINGERPRINTS_ATTR = "_prefix_fingerprints"


def reset_prefix_fingerprints(agent: Any) -> None:
    """Clear the per-turn fingerprint list. Called at turn start."""
    setattr(agent, _FINGERPRINTS_ATTR, [])


def get_prefix_fingerprint(agent: Any, index: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Return the fingerprint for the ``index``-th model call of this turn.

    ``index=None`` returns the latest entry (used by single-call persistence
    paths like the interrupted-turn writer). Returns None when the hook never
    fired (non-Bedrock paths, hook disabled, or index out of range).
    """
    fingerprints: List[Dict[str, Any]] = getattr(agent, _FINGERPRINTS_ATTR, None) or []
    if not fingerprints:
        return None
    if index is None:
        return fingerprints[-1]
    if 0 <= index < len(fingerprints):
        return fingerprints[index]
    # More assistant messages than observed model calls (shouldn't happen) —
    # better to attach nothing than a wrong fingerprint.
    return None


class PrefixFingerprintHook(HookProvider):
    """Compute toolConfig / system-prompt / history hashes per model call."""

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self._on_before_model_call)

    def _on_before_model_call(self, event: BeforeModelCallEvent) -> None:
        try:
            agent = event.agent

            tool_specs = agent.tool_registry.get_all_tool_specs()
            system_prompt = getattr(agent, "system_prompt", None)
            messages = list(getattr(agent, "messages", None) or [])

            # System prompt may be a plain string or structured content blocks
            # (SystemContentBlock list, used to place cache points).
            if isinstance(system_prompt, str) or system_prompt is None:
                system_hash = fingerprint_text(system_prompt)
            else:
                system_hash = fingerprint_canonical_json(system_prompt)

            fingerprint = {
                "toolConfigHash": fingerprint_canonical_json(tool_specs),
                "systemPromptHash": system_hash,
                # History *prefix*: everything except the newest message. For
                # a cache hit this must equal the previous call's full history.
                "historyHash": fingerprint_canonical_json(messages[:-1]),
                "messageCount": len(messages),
            }

            fingerprints = getattr(agent, _FINGERPRINTS_ATTR, None)
            if fingerprints is None:
                fingerprints = []
                setattr(agent, _FINGERPRINTS_ATTR, fingerprints)
            fingerprints.append(fingerprint)
        except Exception as e:  # noqa: BLE001 - observability must never break a turn
            logger.debug("Prefix fingerprinting skipped: %s", e)
