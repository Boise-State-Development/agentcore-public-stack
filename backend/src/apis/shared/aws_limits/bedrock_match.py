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


# ---------------------------------------------------------------------------
# Usage: which Bedrock metric series count against an inference quota
#
# Bedrock publishes per-model metrics in AWS/Bedrock keyed by the ``ModelId``
# the caller invoked: the base id for on-demand ("anthropic.claude-…"), a
# geographic inference profile for cross-Region ("us.anthropic.claude-…") and
# ``global.`` for global inference. A quota's inference type is in its name, so
# the ModelIds that count against it are the ones of the same type whose tokens
# match the quota's model.
# ---------------------------------------------------------------------------

ON_DEMAND = "on_demand"
CROSS_REGION = "cross_region"
GLOBAL = "global"

# Tokens-per-minute quotas compare against EstimatedTPMQuotaUsage, which counts
# output tokens at their quota burndown rate; requests-per-minute quotas
# against Invocations. Both publish one datapoint per request, so the
# per-minute Sum is the minute's usage.
TPM_METRIC = "EstimatedTPMQuotaUsage"
RPM_METRIC = "Invocations"

# Tokens a quota name may carry beyond the model's own without naming a
# different variant: the vendor, and "V1"-style version suffixes. Anything
# else ("1M Context Length") is a separate quota the base model's traffic
# cannot be attributed to.
_USAGE_REMAINDER_OK = _PROVIDER_PREFIXES | {"v"}


def quota_inference_type(quota_name: str) -> Optional[str]:
    """``on_demand`` / ``cross_region`` / ``global``, or None (mantle and non-inference quotas)."""
    name = quota_name.strip().lower()
    if not is_inference_quota(quota_name):
        return None
    if name.startswith("on-demand"):
        return ON_DEMAND
    if name.startswith("global cross-region"):
        return GLOBAL
    if name.startswith("cross-region"):
        return CROSS_REGION
    return None  # [bedrock-mantle endpoint]: metrics are not in AWS/Bedrock


def quota_usage_metric(quota_name: str) -> Optional[str]:
    """The AWS/Bedrock metric a quota is measured with, or None."""
    if quota_inference_type(quota_name) is None:
        return None
    name = quota_name.lower()
    if " tokens per minute " in name:
        return TPM_METRIC
    if " requests per minute " in name:
        return RPM_METRIC
    return None


def model_id_inference_type(model_id: str) -> Optional[str]:
    """How a ``ModelId`` dimension value was invoked. None for application profiles."""
    ident = model_id.strip()
    if ident.startswith("arn:"):
        if ":application-inference-profile/" in ident:
            return None  # an opaque id; its model is not in the name
        ident = ident.rsplit("/", 1)[-1]
    first = ident.split(".", 1)[0].lower()
    if "." not in ident:
        return None
    if first == "global":
        return GLOBAL
    if first in _REGION_PREFIXES:
        return CROSS_REGION
    return ON_DEMAND


def _base_model_id(model_id: str) -> Optional[str]:
    ident = model_id.strip()
    if ident.startswith("arn:"):
        if ":application-inference-profile/" in ident:
            return None
        ident = ident.rsplit("/", 1)[-1]
    parts = ident.split(".")
    while len(parts) > 2 and parts[0].lower() in _REGION_PREFIXES:
        parts = parts[1:]
    return ".".join(parts) if len(parts) >= 2 else None


def geo_prefix(region: str) -> Optional[str]:
    """The cross-Region inference profile prefix for a Region ("us-west-2" -> "us")."""
    region = region.lower()
    if region.startswith("us-gov-"):
        return "us-gov"
    for start, prefix in (("us-", "us"), ("eu-", "eu"), ("ap-", "apac"), ("ca-", "ca")):
        if region.startswith(start):
            return prefix
    return None


def model_id_variants(model_id: str, region: str) -> List[str]:
    """The ModelIds one catalog model can be invoked as: on-demand, geographic and global.

    A variant nobody has invoked has no metric, which reads as zero use: true
    for a model the platform could call that way but has not this week.
    """
    base = _base_model_id(model_id)
    if base is None:
        return [model_id]
    variants = [model_id, base, f"global.{base}"]
    geo = geo_prefix(region)
    if geo:
        variants.append(f"{geo}.{base}")
    return list(dict.fromkeys(variants))


def _usage_tokens_match(candidate: Sequence[str], quota_tokens: Sequence[str]) -> bool:
    n = len(candidate)
    if n < 2 or not _contains(quota_tokens, candidate):
        return False
    for start in range(len(quota_tokens) - n + 1):
        if list(quota_tokens[start : start + n]) != list(candidate):
            continue
        rest = list(quota_tokens[:start]) + list(quota_tokens[start + n :])
        if all(t in _USAGE_REMAINDER_OK or t.isdigit() for t in rest):
            return True
    return False


def usage_model_ids(quota_name: str, model_ids: Iterable[str], models: Iterable[ModelRef] = ()) -> List[str]:
    """The ``ModelId`` values whose AWS/Bedrock metrics count against ``quota_name``.

    ``model_ids`` are candidates (from the managed catalog and from the metrics
    Bedrock has published); ``models`` lend their display names to ids that
    tokenize differently from the quota name. Stricter than
    :func:`model_matches_quota`: the quota may not name a variant the id does not.
    """
    kind = quota_inference_type(quota_name)
    part = quota_model_part(quota_name)
    if kind is None or part is None:
        return []
    quota_tokens = tokenize(part)
    names = {m.model_id: m.model_name for m in models if m.model_name}
    found: List[str] = []
    for model_id in dict.fromkeys(model_ids):
        if model_id_inference_type(model_id) != kind:
            continue
        candidates = [model_id_tokens(model_id)]
        if model_id in names:
            candidates.append(model_name_tokens(names[model_id]))
        if any(_usage_tokens_match(c, quota_tokens) for c in candidates):
            found.append(model_id)
    return found
