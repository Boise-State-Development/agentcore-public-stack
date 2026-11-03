"""Match managed-catalog models to Bedrock Service Quotas by name.

Bedrock quota names are unstructured English, for example::

    Global cross-region model inference tokens per minute for Anthropic Claude Sonnet 4.6
    On-demand model inference requests per minute for Meta Llama 3.1 70B Instruct
    [bedrock-mantle endpoint] Output tokens per minute for GPT-5.4

and nothing in Service Quotas links a quota to a model id. So both sides are
reduced to word tokens and a model matches a quota when the model's tokens
appear, in order and contiguously, in the part of the quota name after "for".
Letters and digits are split apart ("llama3" -> "llama 3", "70B" -> "70 b")
so a model id and a display name tokenize the same way.

One rule keeps versions apart: the match may not be followed by another
number, so "Claude Sonnet 4" does not claim "Claude Sonnet 4.5" quotas.

Pure functions, no AWS calls, so the behaviour is pinned by unit tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

# The inference throughput quotas the default view shows. Everything else in
# the bedrock service (batch, customization, agents, guardrails…) is behind
# "Show all quotas".
INFERENCE_QUOTA_PATTERN = re.compile(
    r"^(?:"
    r"On-demand model inference (?:requests|tokens) per minute"
    r"|(?:Global )?[Cc]ross-[Rr]egion model inference (?:requests|tokens) per minute"
    r"|\[bedrock-mantle endpoint\] (?:Input|Output) tokens per minute"
    r") for (?P<model>.+)$"
)

# Managed-model providers served by Bedrock. OpenAI and Gemini limits are
# not AWS quotas and are out of scope.
AWS_MODEL_PROVIDERS = frozenset({"bedrock", "mantle", "bedrock-responses"})

# Bedrock id prefixes that are not part of the model's name.
_REGION_PREFIXES = frozenset({"us", "eu", "apac", "ap", "global", "us-gov", "jp", "au", "ca"})
_PROVIDER_PREFIXES = frozenset(
    {
        "anthropic",
        "amazon",
        "meta",
        "mistral",
        "cohere",
        "ai21",
        "deepseek",
        "openai",
        "qwen",
        "writer",
        "twelvelabs",
        "stability",
        "google",
        "moonshot",
        "minimax",
        "nvidia",
    }
)

# Tokens that carry no identity on the model side.
_NOISE_TOKENS = frozenset({"v"})


@dataclass(frozen=True)
class ModelRef:
    """The parts of a managed model the matcher reads."""

    model_id: str
    model_name: str = ""
    provider: str = "bedrock"


def tokenize(text: str) -> List[str]:
    """Lower-case word tokens with letters and digits split apart."""
    spaced = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", text)
    return [t for t in re.split(r"[^a-z0-9]+", spaced.lower()) if t]


def model_id_tokens(model_id: str) -> List[str]:
    """Tokens naming the model in a Bedrock id, without region, vendor or version.

    ``us.anthropic.claude-sonnet-4-5-20250929-v1:0`` -> ``claude sonnet 4 5``.
    """
    ident = model_id.strip()
    # An inference-profile or foundation-model ARN ends with the id.
    if ident.startswith("arn:"):
        ident = ident.rsplit("/", 1)[-1]
    parts = ident.split(".")
    while len(parts) > 1 and parts[0].lower() in _REGION_PREFIXES:
        parts = parts[1:]
    if len(parts) > 1 and parts[0].lower() in _PROVIDER_PREFIXES:
        parts = parts[1:]
    ident = ".".join(parts)
    ident = re.sub(r":\d+$", "", ident)  # ":0" throughput suffix
    ident = re.sub(r"-v\d+$", "", ident)  # "-v1" version suffix
    ident = re.sub(r"-\d{8}$", "", ident)  # "-20250929" release date
    return [t for t in tokenize(ident) if t not in _NOISE_TOKENS]


def model_name_tokens(model_name: str) -> List[str]:
    tokens = tokenize(model_name)
    # A display name may lead with the vendor ("Anthropic Claude…"); the quota
    # name usually does too, but not always, so drop it and match the rest.
    if tokens and tokens[0] in _PROVIDER_PREFIXES:
        tokens = tokens[1:]
    return tokens


def _contains(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """``needle`` appears contiguously in ``haystack``, not followed by a number."""
    n = len(needle)
    if n == 0 or n > len(haystack):
        return False
    for start in range(len(haystack) - n + 1):
        if list(haystack[start : start + n]) != list(needle):
            continue
        following = haystack[start + n] if start + n < len(haystack) else None
        if following is not None and following.isdigit():
            continue
        return True
    return False


def quota_model_part(quota_name: str) -> Optional[str]:
    """The model named by an inference quota, or None for any other quota."""
    match = INFERENCE_QUOTA_PATTERN.match(quota_name.strip())
    return match.group("model") if match else None


def is_inference_quota(quota_name: str) -> bool:
    return quota_model_part(quota_name) is not None


def model_matches_quota(model: ModelRef, quota_name: str) -> bool:
    """Whether ``quota_name`` is an inference quota for ``model``."""
    if model.provider.lower() not in AWS_MODEL_PROVIDERS:
        return False
    part = quota_model_part(quota_name)
    if part is None:
        return False
    quota_tokens = tokenize(part)
    candidates: Tuple[List[str], ...] = (
        model_id_tokens(model.model_id),
        model_name_tokens(model.model_name) if model.model_name else [],
    )
    # A single generic token ("pro", "large") would match too much.
    return any(len(c) >= 2 and _contains(quota_tokens, c) for c in candidates)


def filter_quotas_for_models(quota_names: Iterable[str], models: Iterable[ModelRef]) -> List[str]:
    """The inference quota names that match at least one of ``models``."""
    refs = [m for m in models if m.provider.lower() in AWS_MODEL_PROVIDERS]
    return [name for name in quota_names if any(model_matches_quota(m, name) for m in refs)]
