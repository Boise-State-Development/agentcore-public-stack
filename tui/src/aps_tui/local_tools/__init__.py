"""Tools the terminal runs on the user's own machine.

The agent loop lives on the server. What the server cannot do is touch this
machine, so the terminal declares a small set of tools on every turn
(:func:`tool_specs`), the server pauses the turn when the model calls one, and
:class:`ToolExecutor` runs it here and hands the result back. Every execution
is preceded by an approval prompt in the transcript; nothing in this package
runs because a model asked.

Design rules, each of which has a reason:

* **No shell globbing of the user's intent.** ``run_command`` runs exactly the
  string the model produced, through the user's shell, after the user has read
  it. There is no allow-list: an allow-list that permits ``git`` permits
  ``git push --force``, so the decision belongs to the person, not a pattern.
* **Everything is bounded.** Commands time out (:data:`DEFAULT_TIMEOUT`), output
  is capped (:data:`MAX_OUTPUT_CHARS`), file reads are capped
  (:data:`MAX_FILE_BYTES`). The caps protect the prompt (every byte returned
  is a token) more than the machine.
* **Results are strings.** The wire shape is ``{"output": str, "isError": bool}``
  and the model reads it as text. Structured output would be re-serialised
  anyway.
* **Paths are resolved against the working directory the TUI started in**, and
  reported back absolute, so the model and the user agree about what was read.
"""

from __future__ import annotations

from .executor import ApprovalDecision, LocalToolCall, LocalToolResult, ToolExecutor, describe_call
from .specs import LOCAL_TOOL_NAMES, tool_specs

__all__ = [
    "LOCAL_TOOL_NAMES",
    "ApprovalDecision",
    "LocalToolCall",
    "LocalToolResult",
    "ToolExecutor",
    "describe_call",
    "tool_specs",
]
