"""Shared ``bedrock-runtime`` OpenAI Responses model construction.

The second OpenAI-compatible Bedrock surface, alongside Bedrock Mantle
(:mod:`apis.shared.models.mantle`). Both speak the OpenAI wire protocol with a
short-term bearer token; they differ in host, IAM, model-id shape and — the
reason this module exists — **prompt caching**.

Why this transport at all
-------------------------

GPT-5.6 supports prompt caching only on the **Responses API**, on either
endpoint. Converse is the tempting path — it drops into the existing
``BedrockModel`` plumbing with SigV4 and no new auth — and it is the one path
with *zero* caching. At Sol's published rates a ~30k stable prefix costs
~$0.13/turn on Converse against ~$0.013 on a Responses cache hit.

``bedrock-runtime`` over Mantle-Responses because it adds CRIS, invocation
logging, CloudWatch metrics and Cost Explorer itemization, and Global CRIS is
cheaper for this family. We give up server-side tool use (unused) and In-Region
inference (not offered here for this model anyway).

How it differs from the Mantle builder
--------------------------------------

Strands' ``bedrock_mantle_config`` hardcodes the Mantle host
(``models/_openai_bedrock.py``) and *rejects* a caller-supplied ``base_url`` /
``api_key`` when set, so pointing at ``bedrock-runtime`` means not using that
config at all — plain ``client_args`` instead.

That trade has one consequence worth naming: ``bedrock_mantle_config`` re-mints
its bearer token per request, while a static ``api_key`` in ``client_args``
freezes at construction. Our microVMs live 18-50 minutes against a 12-hour
token cap, so a frozen token would work *by luck*. :func:`build_bedrock_responses_model`
instead returns a subclass overriding ``_resolve_client_args()`` — which Strands
calls per request — to mint fresh. A handful of lines that removes a class of
"worked in dev, expired in prod" failure.

Usage semantics are normalized here as they are on the Mantle path: OpenAI's
``input_tokens`` is inclusive of both cache buckets, which every cost path we
own treats as disjoint. See :mod:`apis.shared.models.usage_normalization`.
"""

import logging
import os
from typing import Any, Dict, Optional

# The OpenAI Responses API's native param names are a property of the *API*,
# not of the transport, so this is the Mantle map aliased rather than copied —
# the two surfaces can never drift apart.
from .mantle import MANTLE_RESPONSES_PARAM_MAP as BEDROCK_RESPONSES_PARAM_MAP
from .usage_normalization import usage_normalized

logger = logging.getLogger(__name__)

__all__ = [
    "BEDROCK_RESPONSES_PARAM_MAP",
    "BEDROCK_RUNTIME_OPENAI_PATH",
    "EXPLICIT_CACHE_ENABLED_ENV",
    "EXPLICIT_CACHE_OPTIONS",
    "EXPLICIT_CACHE_TTL",
    "apply_explicit_prompt_cache",
    "build_bedrock_responses_model",
    "build_prompt_cache_key",
    "explicit_prompt_cache_enabled",
    "get_bedrock_runtime_openai_base_url",
]

# bedrock-runtime is a regional endpoint on amazonaws.com (Mantle is api.aws).
_BEDROCK_RUNTIME_HOST_TEMPLATE = "https://bedrock-runtime.{region}.amazonaws.com"

# The OpenAI-compatible base path. Unlike Mantle — where the path varies by
# model family and the SDK derives it from the model id — bedrock-runtime
# serves every OpenAI-compatible model from this one path.
BEDROCK_RUNTIME_OPENAI_PATH = "/openai/v1"

# Cross-Region inference profile prefixes. GPT-5.6 is not offered as an
# in-Region model on this endpoint, so a bare `openai.` id is a
# misconfiguration — but this is a warning, not a gate: a future model may
# well be served in-Region and should not need a code change to run.
_INFERENCE_PROFILE_PREFIXES = ("us.", "global.", "eu.", "apac.")


def get_bedrock_runtime_openai_base_url(region: Optional[str] = None) -> str:
    """OpenAI-compatible base URL for ``bedrock-runtime`` in ``region``.

    Args:
        region: AWS region. ``None`` -> ``AWS_REGION``.

    Returns:
        e.g. ``https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1``.

    Raises:
        ValueError: When no region can be resolved. Deliberately loud: this
            endpoint is regional and silently defaulting to some other region
            produces an opaque auth failure at the first turn, not a clear one.
    """
    resolved = _resolve_region(region)
    return _BEDROCK_RUNTIME_HOST_TEMPLATE.format(region=resolved) + BEDROCK_RUNTIME_OPENAI_PATH


def _resolve_region(region: Optional[str]) -> str:
    """Resolve the region for both the base URL and the token signature."""
    resolved = region or os.environ.get("AWS_REGION")
    if not resolved:
        raise ValueError(
            "No AWS region available for the bedrock-runtime OpenAI endpoint. "
            "Set AWS_REGION, or pin a region on the managed model."
        )
    return resolved


def _warn_on_missing_inference_profile(model_id: str) -> None:
    """Log when a model id names no cross-Region inference profile."""
    if not model_id.startswith(_INFERENCE_PROFILE_PREFIXES):
        logger.warning(
            "Model id %r names no inference profile (expected one of %s). "
            "bedrock-runtime does not offer in-Region inference for the "
            "GPT-5.6 family, so this will likely be rejected — record the "
            "profile-prefixed id on the managed model.",
            model_id,
            ", ".join(_INFERENCE_PROFILE_PREFIXES),
        )


# ── Explicit prompt caching (GPT-5.6) ────────────────────────────────────────
#
# ⛔ OPT-IN, DEFAULT OFF — measured as a pessimization on our workload.
#
# The plan was: mark where the reusable prefix ends, so a change in
# conversation history costs a *read* of the static prefix rather than a full
# re-write at the 1.25x premium. That reasoning had the counterfactual wrong.
# GPT-5.6's default **implicit** caching does not re-write history when it
# grows — it appends the delta — so a single breakpoint after the static
# prefix does not save a re-write. It stops the history being cached at all.
#
# Measured live on `us.openai.gpt-5.6-sol` (dev-ai, us-west-2, 8k static
# prefix, 5 turns, ~1.5k tokens of history growth per turn), priced at the
# Price List ratios (input 1x, cache read 0.1x, cache write 1.25x):
#
#                uncached input   cacheRead   cacheWrite   input-equivalents
#   explicit            22,790      23,228        5,807              32,372
#   implicit                10      38,410       13,405              20,607
#
# Explicit cost ~57% MORE. Under explicit the uncached input grew every turn
# (1,516 -> 7,600) while cacheRead stayed flat at 5,807; under implicit the
# whole growing conversation stayed cached.
#
# The code is kept because the placement, not the mechanism, is what failed —
# the API allows up to 4 breakpoints, and a scheme that also marks the end of
# history could plausibly beat implicit. Nobody should turn this on again
# without re-running `scripts/probe_gpt56_cache_rates.py --mode both
# --grow-history` and beating the implicit arm.
#
# ⛔ THE SECOND-BREAKPOINT IDEA ABOVE WAS TESTED AND LOSES. Measured on Kimi
# K3 (2026-09-21, dev-ai, unique prefix per arm, 7.7k static prefix, 5 turns
# with a tool-result blob appended each turn, US CRIS rates):
#
#                              read    write  uncached   5-turn $
#   one breakpoint (ships)    30,740    7,685    11,610    0.08016
#   + end-of-history bp       30,740   19,155       140    0.08962   +11.8%
#
# The second breakpoint re-WRITES the whole growing history every turn
# (1,147 -> 2,294 -> 3,441 -> 4,588) and never reads it back — cache reads stay
# pinned at the static prefix in both arms. Marking a boundary whose content
# changes every turn just moves history from the $3.30 input rate to the $4.125
# write rate: 1.25x worse, by construction rather than by accident.
#
# Two mechanics worth knowing before anyone tries a variant:
#   - A breakpoint is REJECTED on an assistant `output_text` block —
#     "prompt_cache_breakpoint must be attached to a content block that renders
#     cacheable prompt content". It has to ride user/developer input content.
#   - Lookup did not do longest-prefix matching. Turn N+1's history contains
#     turn N's as a prefix, yet nothing from the earlier entry was read back.
#
# Not tested: a breakpoint PINNED at a fixed history offset for several turns
# (stable content, so it could actually be read). That is the only variant of
# this idea still open, and it caches a fixed early chunk while the tail keeps
# growing uncached — so the ceiling on it is small.
#
# Opt-in: only the literal string "true" enables it.
#
# ⚠️ Deliberately NOT wired into the CDK Runtime construct.
# `AWS::BedrockAgentCore::Runtime` caps EnvironmentVariables at 50 and
# `inference-agentcore-construct.ts` is AT that cap — a 51st entry fails
# CloudFormation *changeset validation*, i.e. after synth, tsc, jest and green
# CI (it broke the dev Platform Stack deploy on 2026-08-05).
EXPLICIT_CACHE_ENABLED_ENV = "BEDROCK_RESPONSES_EXPLICIT_CACHE_ENABLED"

# ── …but the measurement above is about GPT-5.6, and it does not generalize ──
#
# Models on this transport that REQUIRE explicit mode to cache at all. For
# these the env flag above is irrelevant: without explicit controls they do not
# merely fail to save, they bill a full cache WRITE every turn and never read
# one back — strictly worse than not caching.
#
# Measured clean-room on 2026-09-21 (dev-ai, us-west-2, unique prefix per arm
# so no arm could read another's entry; 10.5k stable prefix, 4 turns, priced at
# US CRIS $3.30 / $0.33 / $4.125):
#
#                                        read    write  uncached     4-turn $
#   kimi-k3  stock (no options)             0   42,008        28      0.17338
#   kimi-k3  developer-message placement    0   42,024        28      0.17344
#   kimi-k3  options, no breakpoint    23,040        0    19,008      0.07033
#   kimi-k3  FULL explicit (opts+bp)  31,458   10,486       112      0.05401
#   gpt-5.6-luna  stock (control)     31,263   10,421         8      0.05333
#
# Three things that read backwards from the GPT-5.6 result:
#
#   1. Kimi K3's model card says it "supports implicit (automatic) prompt
#      caching" by default. On this endpoint that is a write every turn and a
#      read never. The same 4 turns UNCACHED would be 42,036 tok * $3.30 =
#      $0.1387 — so stock "caching" costs **25% MORE than no caching at all**,
#      for the life of every session.
#   2. `prompt_cache_options` is the deciding variable, not breakpoint
#      placement: moving the prefix into a developer message changed nothing
#      ($0.17344 vs $0.17338), while adding the options alone recovered most of
#      the saving.
#   3. Full explicit lands the model on the control's efficiency ($0.05401 vs
#      $0.05333) — a **69% saving against stock**, which is the opposite sign
#      to the 57% pessimization the env flag exists to prevent on GPT-5.6.
#
# So the decision has to be PER MODEL. This is a code-level tuple rather than a
# second env var on purpose: `AWS::BedrockAgentCore::Runtime` caps
# EnvironmentVariables at 50, `inference-agentcore-construct.ts` is AT that cap,
# and a 51st entry fails changeset validation after synth, tsc, jest and green
# CI — which is why the flag above was never wired into CDK either.
#
# Matched as a SUBSTRING so both `us.` and `global.` inference profiles hit.
_EXPLICIT_CACHE_REQUIRED_MODELS = ("moonshotai.kimi-k3",)

# Request-level cache controls. `ttl` is the string form of the same window
# the cache-status classifier measures gaps against
# (``OPENAI_RESPONSES_CACHE_TTL_SECONDS``); a test asserts the two agree so a
# change to one cannot silently diverge from the other.
EXPLICIT_CACHE_TTL = "30m"
EXPLICIT_CACHE_OPTIONS: Dict[str, str] = {"mode": "explicit", "ttl": EXPLICIT_CACHE_TTL}

# Marks the end of the reusable prefix. Goes on a *content block*, not on the
# request — see the AWS explicit-prompt-caching guidance for GPT-5.6.
_CACHE_BREAKPOINT = {"mode": "explicit"}


def explicit_prompt_cache_enabled(model_id: Optional[str] = None) -> bool:
    """Whether to send explicit cache breakpoints for ``model_id``.

    Two independent ways to be on, because the right answer is model-specific:

    - **Per model, always on.** ``model_id`` naming a member of
      :data:`_EXPLICIT_CACHE_REQUIRED_MODELS` returns ``True`` regardless of
      the env flag. Those models do not cache at all without explicit
      controls — they bill a write every turn and never read one back, which
      is measurably worse than not caching. The env flag must not be able to
      turn that back off.
    - **Globally, default OFF.** For everything else, only the literal string
      ``"true"`` opts in — explicit mode cost ~57% more than default implicit
      caching on GPT-5.6 with a growing history.

    Args:
        model_id: The inference profile id for this request. ``None`` (the
            pre-2026-09-21 signature) consults the env flag only, which keeps
            every existing caller behaving exactly as before.

    Returns:
        Whether to stamp explicit cache controls onto the request.

    Read per call (no module-level caching) so tests and live config changes
    behave predictably; the env read is negligible next to request assembly.
    """
    if model_id and any(name in model_id for name in _EXPLICIT_CACHE_REQUIRED_MODELS):
        return True
    return os.environ.get(EXPLICIT_CACHE_ENABLED_ENV, "").lower() == "true"


def build_prompt_cache_key(
    system_prompt: Optional[str],
    tool_specs: Optional[Any],
) -> str:
    """Cache key for requests that share a prefix.

    Derived from the same fingerprints the prompt-cache observability layer
    records on each call, so requests with an identical static prefix route to
    one cache entry and any config change rotates the key *by construction* —
    there is no separate list to keep in sync.

    Deliberately covers only the static prefix (system prompt + tool
    definitions). Including conversation history would rotate the key every
    turn, which is precisely the cache-busting this exists to prevent.
    """
    from apis.shared.observability import fingerprint_canonical_json, fingerprint_text

    return f"{fingerprint_text(system_prompt)}:{fingerprint_canonical_json(tool_specs or [])}"


def apply_explicit_prompt_cache(
    request: Dict[str, Any],
    system_prompt: Optional[str],
    tool_specs: Optional[Any],
) -> Dict[str, Any]:
    """Stamp explicit cache controls onto a formatted Responses request.

    Strands emits the system prompt as the top-level ``instructions`` string,
    but a breakpoint has to sit on a *content block*. So the instructions are
    re-expressed as the ``developer`` message the AWS guidance shows, placed at
    the head of ``input`` and carrying the breakpoint — which puts the cache
    boundary exactly at the end of the static prefix (tools + system), before
    any conversation history.

    Args:
        request: The request dict from ``OpenAIResponsesModel._format_request``.
            Mutated in place and returned.
        system_prompt: This turn's system prompt.
        tool_specs: This turn's tool specifications.

    Returns:
        ``request``.

    Note:
        With no system prompt there is no content block marking the end of a
        static prefix, so this returns the request untouched and the model
        keeps its default **implicit** caching. Switching to explicit mode with
        a badly placed boundary would be worse than not switching at all.
    """
    instructions = request.get("instructions")
    if not instructions:
        return request

    developer_message = {
        "type": "message",
        "role": "developer",
        "content": [
            {
                "type": "input_text",
                "text": instructions,
                "prompt_cache_breakpoint": dict(_CACHE_BREAKPOINT),
            }
        ],
    }
    request.pop("instructions", None)
    existing_input = request.get("input")
    request["input"] = [developer_message, *(existing_input or [])]

    # `prompt_cache_key` is a first-class SDK parameter; `prompt_cache_options`
    # is not, so it rides `extra_body`. Merge rather than assign — a caller's
    # `params` may already carry an extra_body.
    request.setdefault("prompt_cache_key", build_prompt_cache_key(system_prompt, tool_specs))
    extra_body = dict(request.get("extra_body") or {})
    extra_body.setdefault("prompt_cache_options", dict(EXPLICIT_CACHE_OPTIONS))
    request["extra_body"] = extra_body

    return request


_model_cls: Optional[type] = None


def _bedrock_responses_model_cls() -> type:
    """Build (once) the model class used for this transport.

    Two layers over Strands' ``OpenAIResponsesModel``:

    1. a per-request bearer-token mint, because ``client_args`` is resolved
       once at construction and our token is short-term;
    2. the OpenAI usage normalization every OpenAI-family model needs.

    Memoized so repeated agent builds reuse one type — keeps ``isinstance``
    stable and avoids leaking a class per turn.
    """
    global _model_cls
    if _model_cls is not None:
        return _model_cls

    # Lazy import: strands is heavy, and apis.shared is imported broadly.
    from strands.models import OpenAIResponsesModel

    class BedrockRuntimeResponsesModel(OpenAIResponsesModel):
        """``OpenAIResponsesModel`` that re-mints its Bedrock bearer token per request."""

        def __init__(self, bedrock_region: str, **kwargs: Any) -> None:
            # Set before super().__init__ so a base-class call into
            # _resolve_client_args() during construction still resolves.
            self._bedrock_region = bedrock_region
            # Captured here rather than read back off the SDK's config so the
            # per-model cache gate cannot break on an SDK config-shape change.
            self._bedrock_model_id = str(kwargs.get("model_id") or "")
            super().__init__(**kwargs)

        def _resolve_client_args(self) -> Dict[str, Any]:
            """Return client kwargs with a freshly minted bearer token.

            Strands calls this per request. The token is a presigned SigV4
            request that expires with the signing credentials (12h cap), so
            minting here rather than at construction is what keeps a
            long-lived model instance working.
            """
            # Lazy import keeps boto3 off this module's import path.
            from apis.shared.bedrock.bearer_token import generate_bedrock_bearer_token

            args = dict(super()._resolve_client_args())
            args["api_key"] = generate_bedrock_bearer_token(self._bedrock_region)
            return args

        def _format_request(
            self,
            messages: Any,
            tool_specs: Optional[Any] = None,
            system_prompt: Optional[str] = None,
            *args: Any,
            **kwargs: Any,
        ) -> Dict[str, Any]:
            """Format the request, then mark where the reusable prefix ends.

            The three leading parameters are named because this override needs
            two of them; ``tool_choice`` / ``model_state`` (and anything a
            future SDK adds) ride ``*args`` / ``**kwargs`` untouched. Strands
            calls this both positionally with five arguments and by keyword,
            so both forms have to work.
            """
            request = super()._format_request(messages, tool_specs, system_prompt, *args, **kwargs)
            if not explicit_prompt_cache_enabled(self._bedrock_model_id):
                return request
            return apply_explicit_prompt_cache(
                request, system_prompt=system_prompt, tool_specs=tool_specs
            )

    _model_cls = usage_normalized(BedrockRuntimeResponsesModel)
    return _model_cls


def build_bedrock_responses_model(
    model_id: str,
    region: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
):
    """Build a Strands Responses model targeting ``bedrock-runtime``.

    Args:
        model_id: A cross-Region inference profile id, e.g.
            ``us.openai.gpt-5.6-sol`` or ``global.openai.gpt-5.6-sol``.
        region: AWS region for the endpoint and the token signature. ``None``
            -> ``AWS_REGION``. One resolved value drives both, so the URL and
            the signature can never disagree.
        params: Already-native Responses params (canonical names must be
            pre-translated via :data:`BEDROCK_RESPONSES_PARAM_MAP`). Spread
            verbatim into ``responses.create()`` by the SDK.

    Returns:
        A configured Responses model reporting disjoint token usage.

    Raises:
        ValueError: When no region can be resolved.
    """
    resolved_region = _resolve_region(region)
    _warn_on_missing_inference_profile(model_id)

    # base_url is fixed for the life of the model; only api_key is re-minted
    # per request (see BedrockRuntimeResponsesModel._resolve_client_args).
    # A placeholder api_key is supplied because the OpenAI client requires one
    # at construction; it is replaced before any request goes out.
    client_args: Dict[str, Any] = {
        "base_url": _BEDROCK_RUNTIME_HOST_TEMPLATE.format(region=resolved_region)
        + BEDROCK_RUNTIME_OPENAI_PATH,
        "api_key": "placeholder-replaced-per-request",
    }

    config: Dict[str, Any] = {"model_id": model_id}
    if params:
        config["params"] = params

    return _bedrock_responses_model_cls()(
        bedrock_region=resolved_region,
        client_args=client_args,
        **config,
    )
