"""
Shared Hypothesis strategies for property-based tests.

Each strategy generates valid instances of core dataclasses or representative
input data used across the property test suite.
"""

import string

import hypothesis.strategies as st

from agents.main_agent.core.model_config import ModelConfig, ModelProvider, RetryConfig
from agents.main_agent.session.compaction_models import CompactionState


# ---------------------------------------------------------------------------
# st_model_config — valid ModelConfig instances
# ---------------------------------------------------------------------------
@st.composite
def st_model_config(draw):
    """Generate valid ModelConfig instances with random providers, temperatures,
    model IDs, caching flags, and optional max_tokens."""
    provider = draw(st.sampled_from(list(ModelProvider)))

    # Build a model_id that is consistent with the chosen provider so that
    # get_provider() auto-detection agrees with the explicit provider field.
    if provider == ModelProvider.OPENAI:
        model_id = draw(
            st.sampled_from(["gpt-4", "gpt-4o", "gpt-3.5-turbo", "o1-preview"])
        )
    elif provider == ModelProvider.GEMINI:
        model_id = draw(
            st.sampled_from(["gemini-pro", "gemini-1.5-flash", "gemini-2.0"])
        )
    else:
        # BEDROCK — must contain "anthropic" or "claude" (or any non-matching
        # string, which also defaults to BEDROCK).
        model_id = draw(
            st.sampled_from([
                "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "anthropic.claude-3-sonnet",
                "us.anthropic.claude-v2",
            ])
        )

    temperature = draw(st.floats(min_value=0.0, max_value=1.0))
    caching_enabled = draw(st.booleans())
    max_tokens = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=8192)))

    inference_params: dict = {"temperature": temperature}
    if max_tokens is not None:
        inference_params["max_tokens"] = max_tokens

    return ModelConfig(
        model_id=model_id,
        caching_enabled=caching_enabled,
        provider=provider,
        inference_params=inference_params,
        retry_config=None,  # retry_config is not part of to_dict round-trip
    )


# ---------------------------------------------------------------------------
# st_retry_config — RetryConfig with initial_delay <= max_delay
# ---------------------------------------------------------------------------
@st.composite
def st_retry_config(draw):
    """Generate RetryConfig instances where sdk_initial_delay <= sdk_max_delay."""
    boto_max_attempts = draw(st.integers(min_value=1, max_value=10))
    boto_retry_mode = draw(st.sampled_from(["legacy", "standard", "adaptive"]))
    connect_timeout = draw(st.integers(min_value=1, max_value=60))
    read_timeout = draw(st.integers(min_value=1, max_value=300))
    sdk_max_attempts = draw(st.integers(min_value=1, max_value=10))

    # Ensure initial_delay <= max_delay by drawing max first, then initial
    sdk_max_delay = draw(st.floats(min_value=0.1, max_value=120.0))
    sdk_initial_delay = draw(
        st.floats(min_value=0.1, max_value=max(0.1, sdk_max_delay))
    )

    return RetryConfig(
        boto_max_attempts=boto_max_attempts,
        boto_retry_mode=boto_retry_mode,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        sdk_max_attempts=sdk_max_attempts,
        sdk_initial_delay=sdk_initial_delay,
        sdk_max_delay=sdk_max_delay,
    )


# ---------------------------------------------------------------------------
# st_compaction_state — CompactionState with random fields
# ---------------------------------------------------------------------------
@st.composite
def st_compaction_state(draw):
    """Generate CompactionState instances with random checkpoint, summary,
    token counts, and optional updated_at timestamps."""
    checkpoint = draw(st.integers(min_value=0, max_value=500))
    summary = draw(st.one_of(st.none(), st.text(min_size=1, max_size=200)))
    last_input_tokens = draw(st.integers(min_value=0, max_value=500_000))
    updated_at = draw(
        st.one_of(
            st.none(),
            st.text(
                alphabet=string.digits + "-T:Z",
                min_size=10,
                max_size=30,
            ),
        )
    )

    return CompactionState(
        checkpoint=checkpoint,
        summary=summary,
        last_input_tokens=last_input_tokens,
        updated_at=updated_at,
    )


# ---------------------------------------------------------------------------
# st_filename — arbitrary text for sanitizer testing
# ---------------------------------------------------------------------------
st_filename = st.text(
    alphabet=st.characters(codec="utf-8", categories=("L", "M", "N", "P", "S", "Z")),
    min_size=0,
    max_size=300,
)


# ---------------------------------------------------------------------------
# st_tool_ids — lists mixing local, gateway, external, and unknown IDs
# ---------------------------------------------------------------------------
_local_tool_names = st.sampled_from([
    "calculator", "weather", "search", "code_interpreter", "browser",
])
_gateway_ids = st.from_regex(r"gateway_[a-z_]{3,15}", fullmatch=True)
_external_mcp_ids = st.from_regex(r"ext_mcp_[a-z_]{3,15}", fullmatch=True)
_unknown_ids = st.from_regex(r"unknown_[a-z0-9]{3,10}", fullmatch=True)

st_tool_ids = st.lists(
    st.one_of(_local_tool_names, _gateway_ids, _external_mcp_ids, _unknown_ids),
    min_size=0,
    max_size=20,
)


# ---------------------------------------------------------------------------
# st_sse_event_dict — dicts with string keys and JSON-serializable values
# ---------------------------------------------------------------------------
_json_primitives = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**31), max_value=2**31),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=50),
)

# Recursive strategy: primitives, lists of primitives, or nested dicts
_json_values = st.recursive(
    _json_primitives,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(max_size=10), children, max_size=5),
    ),
    max_leaves=15,
)

st_sse_event_dict = st.dictionaries(
    keys=st.text(min_size=1, max_size=20),
    values=_json_values,
    min_size=1,
    max_size=8,
)
