"""Modality predicates over catalog rows.

A speech-to-speech model (Nova 2 Sonic) lives in the catalog so voice
sessions price, but it answers only ``InvokeModelWithBidirectionalStream``:
offered as a chat model it would fail every turn. Every surface that lists
models *for chat* filters with this predicate; cost and admin surfaces do not.
"""

from __future__ import annotations

from typing import Iterable, Optional, Protocol


class _HasOutputModalities(Protocol):
    output_modalities: Optional[Iterable[str]]


def is_speech_model(model: _HasOutputModalities) -> bool:
    """Whether the row's output modalities include speech (case-insensitive)."""
    modalities = getattr(model, "output_modalities", None) or []
    return any(str(m).upper() == "SPEECH" for m in modalities)
