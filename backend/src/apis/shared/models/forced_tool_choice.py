"""Which Claude models refuse a forced ``toolChoice`` on Bedrock Converse.

``toolChoice: {"any": {}}`` and ``{"tool": {"name": ...}}`` force the model to
call a tool. Claude Fable 5.1 and later, Mythos 5.1 and later, Opus 5.5 and
Sonnet 5.5 reject both with a 400 before inference:

    tool_choice: type "tool" and "any" are not supported for this model.

Measured on dev us-west-2, 2026-11-14, one Converse call per choice:

=================================  ======  =====  =====
model                              auto    any    tool
=================================  ======  =====  =====
``us.anthropic.claude-opus-5-5``   OK      400    400
``us.anthropic.claude-sonnet-5-5`` OK      400    400
``us.anthropic.claude-haiku-5-5``  OK      OK     OK
``us.anthropic.claude-haiku-4-5``  OK      OK     OK
=================================  ======  =====  =====

Fable 5.2 could not be called from dev (its account must opt in to the
``aws_review`` data-retention mode first), so its row comes from Anthropic's
published contract, which lists Fable 5.1 / 5.2 and Mythos 5.1 / 5.2 beside
Opus 5.5 and Sonnet 5.5. Haiku 5.5 is not on that list, and the probe agrees.

Two of our side channels force a tool, and they fare differently:

- **Memory maintenance** (``apis/shared/memory/maintenance/planner.py``) calls
  ``converse`` itself with ``{"any": {}}``. On a refusing model every file
  fails, so ``resolve_maintenance_model`` refuses the model up front.
- **Structured extraction** (``apis/shared/extraction/extractor.py``) goes
  through Strands' ``BedrockModel.structured_output``, which matches this exact
  400 and retries with native structured output (``outputConfig``). Measured
  on dev 2026-11-14 for Opus 5.5 and Sonnet 5.5: both runs came back
  ``schema_enforced=True``. It needs no guard.

Unknown models, non-Claude models and versions older than the ones above
accept a forced choice. A newer Opus, Sonnet, Fable or Mythos is assumed to
refuse, since every release in those lines since 5.1 / 5.5 has.
"""

from __future__ import annotations

import re
from typing import Optional

# ``claude-<family>-<major>[-<minor>]``; a date stamp is not read as a minor.
_CLAUDE_ID = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?!\d)")

#: The first version in each family that refuses a forced ``toolChoice``.
#: Haiku is absent: Haiku 5.5 accepts it (probed 2026-11-14).
_FIRST_REFUSING = {
    "fable": (5, 1),
    "mythos": (5, 1),
    "opus": (5, 5),
    "sonnet": (5, 5),
}


def model_accepts_forced_tool_choice(model_id: Optional[str]) -> bool:
    """Whether a Converse request to ``model_id`` may force a tool call.

    Reads any Bedrock spelling of the id: bare, ``us.`` / ``global.`` profile,
    ``-v1:0`` suffix. True for anything it does not recognize as a refusing
    Claude model.
    """
    match = _CLAUDE_ID.search((model_id or "").lower())
    if match is None:
        return True
    family, major, minor = match.group(1), int(match.group(2)), int(match.group(3) or 0)
    first = _FIRST_REFUSING.get(family)
    return first is None or (major, minor) < first
