"""Which catalog models a workflow prompt step can run on (``docs/specs/workflow-designer.md`` §21.3).

One rule for both sides, so the run sheet blocks exactly what the Runtime would refuse:

- **The sheet** (``apis.shared.workflows.sheet``) shows a step on an unsupported model as a blocker.
- **The Runtime** (``inference_api/chat/workflow_prompt.py``) stops the step ``model_refused``.

A prompt step is one Bedrock Converse call carrying the run's documents, a PDF as a native
``document`` block. So the model's catalog row must name the Converse provider (``bedrock``: the
other providers speak another API), must take more than text, since a TEXT-only model refuses
a document block, and must answer in text alone (a speech or embedding model does not).
Retirement, the catalog row's existence and the caller's access are checked beside this, by the
rules every model call already uses (``resolve_effective_model``, ``grants_model_access``).
"""

from __future__ import annotations

from typing import Any, Optional

#: The catalog provider that speaks Bedrock Converse, the only API a prompt step calls.
PROMPT_STEP_PROVIDER = "bedrock"


def prompt_model_unsupported_reason(record: Any) -> Optional[str]:
    """Why a catalog row's model can't run a prompt step, or ``None`` when it can.

    ``record`` is a ``ManagedModel`` (or anything with ``provider`` and ``input_modalities``).
    A missing row is the caller's to refuse: it has no price, so nothing could meter the call.
    """
    if record is None:
        return "uncataloged"
    if str(getattr(record, "provider", "") or "").strip().lower() != PROMPT_STEP_PROVIDER:
        return "not_converse"
    inputs = _upper(getattr(record, "input_modalities", None))
    outputs = _upper(getattr(record, "output_modalities", None))
    if inputs == {"TEXT"}:
        return "text_only"
    if "TEXT" not in inputs:
        return "no_text_input"
    if outputs != {"TEXT"}:
        # A speech model (Nova Sonic's SPEECH output) or an embedding model writes no text answer.
        return "no_text_output"
    return None


def _upper(values: Any) -> set:
    return {str(v).strip().upper() for v in (values or []) if str(v).strip()}


def prompt_model_supported(record: Any) -> bool:
    """Whether a catalog row's model can run a prompt step (see the module docstring)."""
    return prompt_model_unsupported_reason(record) is None


__all__ = ["PROMPT_STEP_PROVIDER", "prompt_model_supported", "prompt_model_unsupported_reason"]
