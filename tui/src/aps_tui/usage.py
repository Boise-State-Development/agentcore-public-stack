"""Token counts for one turn.

Lives at the package root rather than in ``client/`` because three layers need
it and none of them should depend on the others: the wire parser produces it,
the conversation store keeps it, and the status bar renders it. It used to live
in ``client/events.py``, which made ``widgets/status.py`` import a wire module
to draw a number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts for one turn. Cache fields are absent on some models."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Usage:
        """Build from the Bedrock Converse ``usage`` shape (camelCase keys).

        A wire constructor on a domain type is a small impurity, accepted
        because every producer so far speaks this one shape. A second dialect
        with different key names should get its own mapping function in that
        dialect's module rather than another classmethod here.
        """

        def as_int(value: Any) -> int:
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        def as_opt_int(value: Any) -> int | None:
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        return cls(
            input_tokens=as_int(payload.get("inputTokens")),
            output_tokens=as_int(payload.get("outputTokens")),
            cache_read_input_tokens=as_opt_int(payload.get("cacheReadInputTokens")),
            cache_write_input_tokens=as_opt_int(payload.get("cacheWriteInputTokens")),
        )
