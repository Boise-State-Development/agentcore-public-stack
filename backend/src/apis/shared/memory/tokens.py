"""Token accounting for memory files, counted once per save (§4.3 step 5).

A save calls Bedrock ``CountTokens`` once on the whole rendered file. The call
costs ~80 ms whatever the payload, which is fine on a save and never
acceptable per turn, so nothing on the read or injection path calls this:
hydration keeps its chars/4 estimate, and the manifest carries the counted
number for anything that wants the real size.

The client is bounded the same way as the runtime's
``agents/main_agent/core/bedrock_count_tokens.py`` (which ``apis.shared``
cannot import): one attempt and a 2 s timeout, so a throttled or failing
count falls back to the chars/4 estimate at once instead of waiting out a
retry backoff. The result says which method produced it.

Configuration:

- ``MEMORY_TOKEN_COUNT_MODEL_ID``: model whose tokenizer counts memory.
  Defaults to Claude Haiku 4.5 (base foundation-model id; CountTokens rejects
  ``us.``-style inference-profile ids, so a prefix is stripped). Set it empty
  to turn counting off and always estimate.
- ``COUNT_TOKENS_TIMEOUT_SECONDS`` is shared with the runtime helper.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

from .models import TokenMethod

logger = logging.getLogger(__name__)

TOKEN_COUNT_MODEL_ENV = "MEMORY_TOKEN_COUNT_MODEL_ID"
DEFAULT_TOKEN_COUNT_MODEL_ID = "anthropic.claude-haiku-4-5-20251001-v1:0"
_TIMEOUT_ENV = "COUNT_TOKENS_TIMEOUT_SECONDS"
_TIMEOUT_DEFAULT = 2.0
_CHARS_PER_TOKEN = 4
_INFERENCE_PROFILE_PREFIX = re.compile(r"^(us|eu|apac|us-gov|global)\.")
_THROTTLE_CODES = frozenset({"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"})


@dataclass(frozen=True)
class TokenCount:
    tokens: int
    method: TokenMethod


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (ceil of chars / 4)."""
    return -(-len(text) // _CHARS_PER_TOKEN)


def token_count_model_id() -> str:
    """The base model id to count with, or ``""`` when counting is off."""
    raw = os.environ.get(TOKEN_COUNT_MODEL_ENV)
    model_id = DEFAULT_TOKEN_COUNT_MODEL_ID if raw is None else raw.strip()
    return _INFERENCE_PROFILE_PREFIX.sub("", model_id, count=1)


def _timeout_seconds() -> float:
    try:
        value = float(os.environ.get(_TIMEOUT_ENV) or _TIMEOUT_DEFAULT)
    except ValueError:
        value = _TIMEOUT_DEFAULT
    return value if value > 0 else _TIMEOUT_DEFAULT


_client: Any = None


def _get_client() -> Any:
    """Process-wide bounded ``bedrock-runtime`` client, built on first count."""
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config

        timeout = _timeout_seconds()
        _client = boto3.client(
            "bedrock-runtime",
            config=Config(
                retries={"total_max_attempts": 1, "mode": "standard"},
                read_timeout=timeout,
                connect_timeout=timeout,
                user_agent_extra="memory-spaces count-tokens",
            ),
        )
    return _client


def count_file_tokens(text: str, *, client: Any = None, model_id: Optional[str] = None) -> TokenCount:
    """Count ``text`` with CountTokens, or estimate it when that is not possible.

    Never raises: counting must not fail a save. The count includes the few
    tokens of Converse message framing around the text.
    """
    if not text:
        return TokenCount(tokens=0, method="estimate")
    model = token_count_model_id() if model_id is None else model_id
    if not model:
        return TokenCount(tokens=estimate_tokens(text), method="estimate")
    try:
        response = (client or _get_client()).count_tokens(
            modelId=model,
            input={"converse": {"messages": [{"role": "user", "content": [{"text": text}]}]}},
        )
        tokens = response.get("inputTokens")
        if tokens is None:
            raise ValueError("CountTokens returned no inputTokens")
        return TokenCount(tokens=int(tokens), method="count")
    except Exception as exc:  # noqa: BLE001 - fall back on anything; a save must not fail here
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
        if code in _THROTTLE_CODES:
            logger.info("memory-spaces: CountTokens throttled (%s); estimating this save", code)
        else:
            logger.warning("memory-spaces: CountTokens failed (%s); estimating this save", code or type(exc).__name__)
        return TokenCount(tokens=estimate_tokens(text), method="estimate")
