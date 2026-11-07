"""Never end a continuation request on an assistant message for a model that refuses prefill.

"Continue" after a max_tokens cut, or after an interrupted turn whose partial
was kept, re-enters the agent loop with an empty prompt. Strands then appends
nothing, so the request ends on the partial assistant message and the model
is expected to carry on from it (assistant-message prefill).

Claude models from 4.6 onward reject that request. Bedrock answers
"This model does not support assistant message prefill. The conversation must
end with a user message." Retrying Continue fails the same way each time,
because the history still ends on the same partial.

For those models a continuation sends one short, fixed user message instead
(``CONTINUATION_PROMPT``). Strands appends it like any prompt, so:

- **Stored history is only appended to.** Nothing before the partial changes,
  so the retained prefix is byte-identical and is read from the prompt cache.
- **Roles keep alternating.** The history becomes user, partial, the
  continue message, then the continuation. The prefill path instead leaves two
  assistant messages in a row, which only the restore-time repair merges.
- **The text is a constant.** It does not depend on the turn, so two
  continuations of the same partial send identical bytes.

Models that still accept prefill (Claude 4.5 and earlier, every non-Claude
model) keep the empty prompt. Prefill there continues mid-sentence, which a
user message cannot reproduce exactly.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

#: The user turn a continuation sends to a model that refuses prefill.
#: Deterministic and persisted like any prompt; changing it changes what
#: later turns read back.
CONTINUATION_PROMPT = "Continue from exactly where you stopped, without repeating what you already wrote."

# ``claude-<family>-<major>[-<minor>]`` as it appears in a Bedrock id, a
# cross-region profile (``us.`` / ``global.``) or a first-party id. The minor
# version is one digit and is never followed by another digit, which keeps a
# date stamp (``claude-opus-4-20250514``) from being read as a minor version.
# Claude 3 ids put the version first (``claude-3-5-sonnet``) and do not match,
# which is correct: they accept prefill.
_CLAUDE_VERSION = re.compile(r"claude-[a-z]+-(\d+)(?:-(\d)(?!\d))?")

# The first Claude version that rejects a trailing assistant message.
_FIRST_PREFILL_REFUSING = (4, 6)


def model_accepts_assistant_prefill(model_id: Optional[str]) -> bool:
    """Whether a request may end on an assistant message for this model.

    False for Claude 4.6 and later (Opus 4.6+, Sonnet 4.6+, Haiku 5.5, Fable),
    including versions not released yet, since the newer Claude models all
    reject prefill. True for everything else, which keeps the behaviour those
    models had before this check existed.
    """
    if not model_id:
        return True
    match = _CLAUDE_VERSION.search(model_id.lower())
    if match is None:
        return True
    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) is not None else 0
    return (major, minor) < _FIRST_PREFILL_REFUSING


def _ends_on_assistant_text(messages: List[Any]) -> bool:
    """True when the last message is the assistant's and carries no toolUse.

    A trailing toolUse is not a prefill: Strands answers it with error
    toolResults before the model call, so the request ends on a user turn.
    """
    if not messages:
        return False
    tail = messages[-1]
    if not isinstance(tail, dict) or tail.get("role") != "assistant":
        return False
    content = tail.get("content") or []
    return not any(isinstance(block, dict) and "toolUse" in block for block in content)


def continuation_prompt(prompt: Any, messages: List[Any], model_id: Optional[str]) -> Any:
    """The prompt to send, so a continuation never ends on an assistant message.

    Only an empty-list prompt is a continuation; any other prompt is returned
    as is. A continuation is rewritten to ``CONTINUATION_PROMPT`` only when the
    live history ends on assistant text and the model refuses prefill.

    ``messages`` must be the list the model is about to see, after rewind,
    compaction and the stale-interrupt reset have run. It is read, never
    changed.
    """
    if not isinstance(prompt, list) or prompt:
        return prompt
    if not _ends_on_assistant_text(messages):
        return prompt
    if model_accepts_assistant_prefill(model_id):
        return prompt
    return CONTINUATION_PROMPT
