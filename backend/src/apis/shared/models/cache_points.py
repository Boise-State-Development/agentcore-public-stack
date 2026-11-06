"""Which Bedrock Converse models may be sent an explicit ``cachePoint``.

One predicate for every caller that hand-places a cachePoint: the agent's
system point (``ModelConfig.bedrock_cache_points_supported``) and the
api-converse route's system and message points. It lives in ``apis.shared``
because those callers sit on both sides of the import boundary.

A point sent to a model that cannot cache fails the whole call with
**AccessDeniedException** ("You invoked an unsupported model or your request
did not allow prompt caching"), measured live in us-west-2 on 2026-09-11 on
llama3-3-70b, mistral-large-2407 and deepseek-r1. So the test is deliberately
narrow: it tracks the Claude family, which Strands' ``"auto"`` cache strategy
also recognizes, rather than every model Bedrock will cache for. Nova Micro
accepts a system point and honors it, and this still denies it. Widen it only
after re-measuring with ``backend/scripts/probe_bedrock_cache_point_support.py``.
"""


def bedrock_cache_points_supported(model_id: str) -> bool:
    """True when an explicit Bedrock ``cachePoint`` may be sent for ``model_id``."""
    model_lower = (model_id or "").lower()
    return "claude" in model_lower or "anthropic" in model_lower
