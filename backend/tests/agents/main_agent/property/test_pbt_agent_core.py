"""
Property-based tests for Agent Core module.

Uses Hypothesis to verify universal correctness properties across
randomly generated inputs.
"""

from hypothesis import given, settings

from agents.main_agent.core.model_config import ModelConfig

from .conftest import st_model_config


class TestModelConfigRoundTrip:
    """
    Feature: agent-core-tests, Property 1: ModelConfig round-trip

    Validates: Requirements 1.13
    """

    @given(config=st_model_config())
    @settings(max_examples=100)
    def test_to_dict_from_params_round_trip(self, config: ModelConfig):
        """
        Feature: agent-core-tests, Property 1: ModelConfig round-trip

        Validates: Requirements 1.13

        For any valid ModelConfig instance, converting to a dictionary via
        to_dict() and then reconstructing via from_params() using those
        dictionary values should produce a ModelConfig whose to_dict()
        output is identical to the original's to_dict() output.
        """
        d = config.to_dict()
        reconstructed = ModelConfig.from_params(
            model_id=d["model_id"],
            caching_enabled=d["caching_enabled"],
            provider=d["provider"],
            inference_params=d["inference_params"],
        )
        assert reconstructed.to_dict() == d


from agents.main_agent.core.model_config import RetryConfig

from .conftest import st_retry_config


class TestRetryConfigDelayInvariant:
    """
    Feature: agent-core-tests, Property 2: RetryConfig delay invariant

    Validates: Requirements 2.4
    """

    @given(config=st_retry_config())
    @settings(max_examples=100)
    def test_initial_delay_lte_max_delay(self, config: RetryConfig):
        """
        Feature: agent-core-tests, Property 2: RetryConfig delay invariant

        Validates: Requirements 2.4

        For any RetryConfig instance constructed with valid parameters,
        sdk_initial_delay should be less than or equal to sdk_max_delay.
        """
        assert config.sdk_initial_delay <= config.sdk_max_delay


from hypothesis import given, settings
import hypothesis.strategies as st

from agents.main_agent.tools.tool_registry import ToolRegistry

from .conftest import st_tool_ids


class TestToolRegistryCountInvariant:
    """
    Feature: agent-core-tests, Property 3: ToolRegistry count invariant

    Validates: Requirements 5.9
    """

    @given(tool_id_list=st_tool_ids)
    @settings(max_examples=100)
    def test_count_equals_unique_ids(self, tool_id_list: list[str]):
        """
        Feature: agent-core-tests, Property 3: ToolRegistry count invariant

        Validates: Requirements 5.9

        For any sequence of register_tool(tool_id, tool_obj) calls on a fresh
        ToolRegistry, get_tool_count() should equal the number of distinct
        tool_id values in the sequence.
        """
        registry = ToolRegistry()
        for tool_id in tool_id_list:
            registry.register_tool(tool_id, lambda: None)

        expected_count = len(set(tool_id_list))
        assert registry.get_tool_count() == expected_count


from agents.main_agent.tools.tool_filter import ToolFilter


class TestToolFilterPartitionInvariant:
    """
    Feature: agent-core-tests, Property 4: ToolFilter partition invariant

    Validates: Requirements 6.7
    """

    @given(tool_id_list=st_tool_ids)
    @settings(max_examples=100)
    def test_partition_sums_to_total_requested(self, tool_id_list: list[str]):
        """
        Feature: agent-core-tests, Property 4: ToolFilter partition invariant

        Validates: Requirements 6.7

        For any list of enabled_tool_ids and any ToolFilter (with a known
        registry and known external MCP tool set), the sum of local_tools +
        gateway_tools + external_mcp_tools + unknown_tools from
        get_statistics() should equal total_requested.
        """
        # Build a known registry with the local tools the strategy can generate
        registry = ToolRegistry()
        for name in ["calculator", "weather", "search", "code_interpreter", "browser"]:
            registry.register_tool(name, lambda: None)

        tool_filter = ToolFilter(registry)

        # Register a broad set of external MCP tool IDs so the strategy's
        # ext_mcp_* IDs are recognised.  We collect them from the input list
        # to keep the known set aligned with what Hypothesis generates.
        ext_mcp_ids = [tid for tid in tool_id_list if tid.startswith("ext_mcp_")]
        if ext_mcp_ids:
            tool_filter.set_external_mcp_tools(ext_mcp_ids)

        stats = tool_filter.get_statistics(tool_id_list)

        partition_sum = (
            stats["local_tools"]
            + stats["gateway_tools"]
            + stats["external_mcp_tools"]
            + stats["unknown_tools"]
        )
        assert partition_sum == stats["total_requested"]


import re

from agents.main_agent.multimodal.file_sanitizer import FileSanitizer

from .conftest import st_filename

# Allowed-character regex from the design document
_SANITIZED_PATTERN = re.compile(r"^[a-zA-Z0-9\s\-\(\)\[\]_]*$")


class TestFileSanitizerOutputInvariantAndIdempotence:
    """
    Feature: agent-core-tests, Property 5: FileSanitizer output invariant and idempotence

    Validates: Requirements 10.4, 10.5
    """

    @given(filename=st_filename)
    @settings(max_examples=100)
    def test_output_matches_allowed_pattern_and_is_idempotent(self, filename: str):
        """
        Feature: agent-core-tests, Property 5: FileSanitizer output invariant and idempotence

        Validates: Requirements 10.4, 10.5

        For any input string, sanitize_filename(x) should (a) match the regex
        ^[a-zA-Z0-9\\s\\-\\(\\)\\[\\]_]*$ and (b) satisfy
        sanitize_filename(sanitize_filename(x)) == sanitize_filename(x).
        """
        result = FileSanitizer.sanitize_filename(filename)

        # (a) Output invariant — only allowed characters
        assert _SANITIZED_PATTERN.match(result) is not None, (
            f"Sanitized output contains disallowed characters: {result!r}"
        )

        # (b) Idempotence
        assert FileSanitizer.sanitize_filename(result) == result, (
            f"sanitize_filename is not idempotent for input: {filename!r}"
        )


import json

from agents.main_agent.streaming.event_formatter import StreamEventFormatter

from .conftest import st_sse_event_dict


class TestSSEFormatInvariantAndJSONRoundTrip:
    """
    Feature: agent-core-tests, Property 6: SSE format invariant and JSON round-trip

    Validates: Requirements 12.9, 12.10
    """

    @given(event_dict=st_sse_event_dict)
    @settings(max_examples=100)
    def test_sse_format_and_json_round_trip(self, event_dict: dict):
        """
        Feature: agent-core-tests, Property 6: SSE format invariant and JSON round-trip

        Validates: Requirements 12.9, 12.10

        For any dictionary with string keys and JSON-serializable values,
        format_sse_event(d) should (a) start with "data: " and end with
        "\\n\\n", and (b) the JSON payload extracted from between the prefix
        and suffix should parse back to the original dictionary.
        """
        result = StreamEventFormatter.format_sse_event(event_dict)

        # (a) SSE format invariant
        assert result.startswith("data: "), (
            f"SSE event does not start with 'data: ': {result!r}"
        )
        assert result.endswith("\n\n"), (
            f"SSE event does not end with '\\n\\n': {result!r}"
        )

        # (b) JSON round-trip — extract payload between prefix and suffix
        payload_str = result[len("data: "):-len("\n\n")]
        parsed = json.loads(payload_str)
        assert parsed == event_dict, (
            f"JSON round-trip failed: expected {event_dict!r}, got {parsed!r}"
        )


import hypothesis.strategies as st
from hypothesis import given, settings

from agents.main_agent.session.preview_session_manager import PreviewSessionManager
from strands.types.session import SessionMessage


# Strategy: sequences of "add" and "clear" operations
_preview_ops = st.lists(
    st.sampled_from(["add", "clear"]),
    min_size=0,
    max_size=50,
)


class TestPreviewSessionManagerCountInvariant:
    """
    Feature: agent-core-tests, Property 7: PreviewSessionManager count invariant

    Validates: Requirements 14.8
    """

    @given(ops=_preview_ops)
    @settings(max_examples=100)
    def test_message_count_tracks_adds_since_last_clear(self, ops: list[str]):
        """
        Feature: agent-core-tests, Property 7: PreviewSessionManager count invariant

        Validates: Requirements 14.8

        For any sequence of create_message and clear_session operations on a
        PreviewSessionManager, message_count should equal the number of
        create_message calls since the last clear_session (or since
        initialization if no clear has occurred).
        """
        manager = PreviewSessionManager(session_id="preview-test", user_id="user-1")
        expected_count = 0

        for op in ops:
            if op == "add":
                msg = SessionMessage(
                    message_id=str(expected_count),
                    message={"role": "user", "content": [{"text": "hello"}]},
                )
                manager.create_message("preview-test", "default", msg)
                expected_count += 1
            else:  # clear
                manager.clear_session()
                expected_count = 0

        assert manager.message_count == expected_count


from agents.main_agent.session.compaction_models import CompactionState

from .conftest import st_compaction_state


class TestCompactionStateRoundTrip:
    """
    Feature: agent-core-tests, Property 8: CompactionState round-trip

    Validates: Requirements 16.5
    """

    @given(state=st_compaction_state())
    @settings(max_examples=100)
    def test_to_dict_from_dict_round_trip(self, state: CompactionState):
        """
        Feature: agent-core-tests, Property 8: CompactionState round-trip

        Validates: Requirements 16.5

        For any valid CompactionState instance (with any checkpoint, summary,
        last_input_tokens, and updated_at), CompactionState.from_dict(state.to_dict())
        should produce a CompactionState whose to_dict() output is identical
        to the original's to_dict() output.
        """
        d = state.to_dict()
        reconstructed = CompactionState.from_dict(d)
        assert reconstructed.to_dict() == d


import hypothesis.strategies as st
from hypothesis import given, settings
from typing import List, Dict, Any

from agents.main_agent.streaming.stream_processor import (
    _handle_lifecycle_events,
    _handle_content_block_events,
    _handle_tool_events,
    _handle_reasoning_events,
    _handle_citation_events,
    _handle_metadata_events,
)


# ---------------------------------------------------------------------------
# Strategy: generate raw event dicts that exercise the various handlers
# ---------------------------------------------------------------------------

_json_leaf = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1000, max_value=1000),
    st.text(min_size=0, max_size=30),
)

_json_value = st.recursive(
    _json_leaf,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(st.text(max_size=10), children, max_size=3),
    ),
    max_leaves=8,
)

# Lifecycle-relevant keys
_lifecycle_event = st.fixed_dictionaries(
    {},
    optional={
        "init_event_loop": st.booleans(),
        "start_event_loop": st.booleans(),
        "message": st.one_of(
            st.fixed_dictionaries(
                {"role": st.sampled_from(["user", "assistant"])},
                optional={
                    "content": st.lists(
                        st.fixed_dictionaries(
                            {},
                            optional={
                                "text": st.text(max_size=20),
                                "toolResult": st.fixed_dictionaries(
                                    {},
                                    optional={"content": st.text(max_size=10)},
                                ),
                            },
                        ),
                        max_size=2,
                    )
                },
            ),
            st.text(max_size=10),  # non-dict message (should be skipped)
        ),
        "result": _json_value,
    },
)

# Content-block-relevant keys (nested under "event")
_inner_content_event = st.fixed_dictionaries(
    {},
    optional={
        "messageStart": st.fixed_dictionaries(
            {}, optional={"role": st.sampled_from(["assistant", "user"])}
        ),
        "contentBlockStart": st.fixed_dictionaries(
            {},
            optional={
                "contentBlockIndex": st.integers(min_value=0, max_value=5),
                "start": st.one_of(
                    st.fixed_dictionaries(
                        {"toolUse": st.fixed_dictionaries(
                            {},
                            optional={
                                "toolUseId": st.text(min_size=1, max_size=10),
                                "name": st.text(min_size=1, max_size=10),
                            },
                        )}
                    ),
                    st.fixed_dictionaries({"text": st.text(max_size=10)}),
                ),
            },
        ),
        "contentBlockDelta": st.fixed_dictionaries(
            {},
            optional={
                "contentBlockIndex": st.integers(min_value=0, max_value=5),
                "delta": st.one_of(
                    st.fixed_dictionaries({"text": st.text(max_size=20)}),
                    st.fixed_dictionaries(
                        {"toolUse": st.fixed_dictionaries(
                            {}, optional={"input": st.text(max_size=10)}
                        )}
                    ),
                    st.fixed_dictionaries({"reasoningContent": _json_value}),
                ),
            },
        ),
        "contentBlockStop": st.fixed_dictionaries(
            {},
            optional={"contentBlockIndex": st.integers(min_value=0, max_value=5)},
        ),
        "messageStop": st.fixed_dictionaries(
            {},
            optional={
                "stopReason": st.sampled_from(["end_turn", "tool_use", "max_tokens"])
            },
        ),
    },
)

_content_block_event = st.fixed_dictionaries({"event": _inner_content_event})

# Tool-relevant keys
_tool_event = st.fixed_dictionaries(
    {},
    optional={
        "current_tool_use": st.fixed_dictionaries(
            {"name": st.text(min_size=1, max_size=15)},
            optional={
                "toolUseId": st.text(min_size=1, max_size=10),
                "input": _json_value,
                "display_content": _json_value,
                "message": st.text(max_size=20),
            },
        ),
        "tool_result": _json_value,
        "tool_error": _json_value,
        "tool_stream_event": _json_value,
    },
)

# Reasoning-relevant keys
_reasoning_event = st.fixed_dictionaries(
    {},
    optional={
        "reasoning": st.booleans(),
        "reasoningText": st.text(max_size=30),
        "reasoning_signature": st.text(max_size=30),
        "redactedContent": st.binary(max_size=20),
        "reasoningContent": st.one_of(
            st.fixed_dictionaries(
                {},
                optional={
                    "reasoningText": st.one_of(
                        st.text(max_size=20),
                        st.fixed_dictionaries(
                            {},
                            optional={
                                "text": st.text(max_size=20),
                                "signature": st.text(max_size=20),
                            },
                        ),
                    ),
                    "redactedContent": st.binary(max_size=10),
                    "signature": st.text(max_size=20),
                },
            ),
        ),
    },
)

# Citation-relevant keys
_citation_event = st.fixed_dictionaries(
    {},
    optional={
        "citation": _json_value,
        "citationsContent": st.one_of(
            st.lists(_json_value, max_size=3),
            _json_value,
        ),
        "citation_start_delta": st.fixed_dictionaries(
            {},
            optional={
                "citation": st.fixed_dictionaries(
                    {},
                    optional={
                        "uuid": st.text(min_size=1, max_size=10),
                        "title": st.text(max_size=20),
                        "url": st.text(max_size=30),
                        "sources": st.lists(_json_value, max_size=2),
                    },
                ),
            },
        ),
        "citation_end_delta": st.fixed_dictionaries(
            {},
            optional={"citation_uuid": st.text(min_size=1, max_size=10)},
        ),
    },
)

# Metadata-relevant keys
_usage_dict = st.fixed_dictionaries(
    {},
    optional={
        "inputTokens": st.integers(min_value=0, max_value=10000),
        "outputTokens": st.integers(min_value=0, max_value=10000),
        "totalTokens": st.integers(min_value=0, max_value=20000),
        "cacheReadInputTokens": st.integers(min_value=0, max_value=5000),
    },
)

_metadata_event = st.fixed_dictionaries(
    {},
    optional={
        "metadata": st.fixed_dictionaries(
            {},
            optional={
                "usage": _usage_dict,
                "metrics": st.fixed_dictionaries(
                    {}, optional={"latencyMs": st.integers(min_value=0, max_value=5000)}
                ),
            },
        ),
        "usage": _usage_dict,
        "event": st.fixed_dictionaries(
            {},
            optional={
                "modelMetadataEvent": st.fixed_dictionaries(
                    {}, optional={"usage": _usage_dict}
                ),
            },
        ),
    },
)


def _assert_processed_events(events: List[Dict[str, Any]]) -> None:
    """Assert every ProcessedEvent has exactly 'type' (str) and 'data' (dict)."""
    for evt in events:
        assert isinstance(evt, dict), f"ProcessedEvent is not a dict: {type(evt)}"
        assert set(evt.keys()) == {"type", "data"}, (
            f"ProcessedEvent keys should be exactly {{'type', 'data'}}, got {set(evt.keys())}"
        )
        assert isinstance(evt["type"], str), (
            f"ProcessedEvent 'type' should be str, got {type(evt['type'])}"
        )
        assert isinstance(evt["data"], dict), (
            f"ProcessedEvent 'data' should be dict, got {type(evt['data'])}"
        )


class TestProcessedEventStructuralInvariant:
    """
    Feature: agent-core-tests, Property 9: ProcessedEvent structural invariant

    Validates: Requirements 23.9
    """

    @given(event=_lifecycle_event)
    @settings(max_examples=100)
    def test_lifecycle_events_structural_invariant(self, event: dict):
        """
        Feature: agent-core-tests, Property 9: ProcessedEvent structural invariant

        Validates: Requirements 23.9

        Every ProcessedEvent returned by _handle_lifecycle_events contains
        exactly the keys "type" (str) and "data" (dict).
        """
        results = _handle_lifecycle_events(event)
        _assert_processed_events(results)

    @given(event=_content_block_event)
    @settings(max_examples=100)
    def test_content_block_events_structural_invariant(self, event: dict):
        """
        Feature: agent-core-tests, Property 9: ProcessedEvent structural invariant

        Validates: Requirements 23.9

        Every ProcessedEvent returned by _handle_content_block_events contains
        exactly the keys "type" (str) and "data" (dict).
        """
        block_index_state = {"index": 0, "skipped_blocks": set()}
        results = _handle_content_block_events(event, block_index_state)
        _assert_processed_events(results)

    @given(event=_tool_event)
    @settings(max_examples=100)
    def test_tool_events_structural_invariant(self, event: dict):
        """
        Feature: agent-core-tests, Property 9: ProcessedEvent structural invariant

        Validates: Requirements 23.9

        Every ProcessedEvent returned by _handle_tool_events contains
        exactly the keys "type" (str) and "data" (dict).
        """
        results = _handle_tool_events(event)
        _assert_processed_events(results)

    @given(event=_reasoning_event)
    @settings(max_examples=100)
    def test_reasoning_events_structural_invariant(self, event: dict):
        """
        Feature: agent-core-tests, Property 9: ProcessedEvent structural invariant

        Validates: Requirements 23.9

        Every ProcessedEvent returned by _handle_reasoning_events contains
        exactly the keys "type" (str) and "data" (dict).
        """
        results = _handle_reasoning_events(event)
        _assert_processed_events(results)

    @given(event=_citation_event)
    @settings(max_examples=100)
    def test_citation_events_structural_invariant(self, event: dict):
        """
        Feature: agent-core-tests, Property 9: ProcessedEvent structural invariant

        Validates: Requirements 23.9

        Every ProcessedEvent returned by _handle_citation_events contains
        exactly the keys "type" (str) and "data" (dict).
        """
        results = _handle_citation_events(event)
        _assert_processed_events(results)

    @given(event=_metadata_event)
    @settings(max_examples=100)
    def test_metadata_events_structural_invariant(self, event: dict):
        """
        Feature: agent-core-tests, Property 9: ProcessedEvent structural invariant

        Validates: Requirements 23.9

        Every ProcessedEvent returned by _handle_metadata_events contains
        exactly the keys "type" (str) and "data" (dict).
        """
        results = _handle_metadata_events(event)
        _assert_processed_events(results)
