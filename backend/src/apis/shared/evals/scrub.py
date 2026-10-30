"""Strip what would unblind a judge, or swamp it, before it reads a session (spec §6.4).

A judge sees the user's prompt, the final answer, and each tool's name and
arguments. It does not see document bodies: a ``<document-digest>`` block or a
``document_read`` page gives away which delivery path produced the answer,
which is the one thing a blinded comparison of two delivery paths must hide,
and it is the bulk of the bytes. Other tool results are kept, capped.

Pure functions over the ``Session``-shaped dict ``session_mapper`` builds; the
input is never mutated.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, Mapping

#: Tools whose results are document text, not something a judge should weigh.
DOCUMENT_TOOLS = frozenset({"document_read", "verify_sources", "document_extract"})
DOCUMENT_RESULT_PLACEHOLDER = "[document content omitted for judging]"
MAX_TOOL_RESULT_CHARS = 2000

_DIGEST = re.compile(r"<document-digest\b.*?</document-digest>", re.DOTALL)
_PLACEHOLDER = re.compile(r"\[Document placeholder:[^\]]*\]")
_ATTACHED = re.compile(r"\s*\[Attached files: [^\]]*\]")


def scrub_text(text: str) -> str:
    text = _DIGEST.sub("[document digest omitted]", text or "")
    text = _PLACEHOLDER.sub("[document omitted]", text)
    return _ATTACHED.sub("", text)


def _cap(text: str) -> str:
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[:MAX_TOOL_RESULT_CHARS] + f" … [{len(text) - MAX_TOOL_RESULT_CHARS} chars omitted]"


def scrub_session(session: Mapping[str, Any]) -> Dict[str, Any]:
    scrubbed = copy.deepcopy(dict(session))
    for trace in scrubbed.get("traces") or []:
        for span in trace.get("spans") or []:
            if span.get("span_type") == "invoke_agent":
                span["user_prompt"] = scrub_text(span.get("user_prompt") or "")
                span["agent_response"] = scrub_text(span.get("agent_response") or "")
            elif span.get("span_type") == "execute_tool":
                result = span.get("tool_result") or {}
                name = (span.get("tool_call") or {}).get("name")
                if name in DOCUMENT_TOOLS:
                    result["content"] = DOCUMENT_RESULT_PLACEHOLDER
                else:
                    result["content"] = _cap(scrub_text(result.get("content") or ""))
    return scrubbed
