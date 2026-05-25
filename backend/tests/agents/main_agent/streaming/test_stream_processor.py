"""
Tests for stream processor event handler functions and _serialize_object.

Validates: Requirements 23.1–23.8
"""

from datetime import datetime, date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from agents.main_agent.streaming.stream_processor import (
    _create_event,
    _format_force_stop_message,
    _handle_citation_events,
    _handle_content_block_events,
    _handle_lifecycle_events,
    _handle_metadata_events,
    _handle_reasoning_events,
    _handle_tool_events,
    _serialize_object,
    process_agent_stream,
)


# ---------------------------------------------------------------------------
# 23.1  Lifecycle events (message_start, message_stop, etc.)
# ---------------------------------------------------------------------------

class TestHandleLifecycleEvents:
    """Validates: Requirement 23.1"""

    def test_init_event_loop(self):
        """init_event_loop: True produces an init_event_loop processed event."""
        events = _handle_lifecycle_events({"init_event_loop": True})
        assert any(e["type"] == "init_event_loop" for e in events)

    def test_start_event_loop(self):
        """start_event_loop: True produces a start_event_loop processed event."""
        events = _handle_lifecycle_events({"start_event_loop": True})
        assert any(e["type"] == "start_event_loop" for e in events)

    def test_message_event_with_role(self):
        """A message dict with 'role' produces a message processed event."""
        raw = {"message": {"role": "assistant", "content": [{"text": "hi"}]}}
        events = _handle_lifecycle_events(raw)
        msg_events = [e for e in events if e["type"] == "message"]
        assert len(msg_events) >= 1
        assert msg_events[0]["data"]["message"]["role"] == "assistant"

    def test_message_event_skipped_when_result_present(self):
        """Message inside a result event is not emitted as a standalone message."""
        raw = {
            "message": {"role": "assistant", "content": []},
            "result": {"some": "data"},
        }
        events = _handle_lifecycle_events(raw)
        msg_events = [e for e in events if e["type"] == "message"]
        assert len(msg_events) == 0

    def test_event_passthrough(self):
        """An 'event' key produces an event processed event."""
        raw = {"event": {"messageStart": {"role": "assistant"}}}
        events = _handle_lifecycle_events(raw)
        evt = [e for e in events if e["type"] == "event"]
        assert len(evt) == 1
        assert evt[0]["data"]["event"]["messageStart"]["role"] == "assistant"

    def test_result_event(self):
        """A 'result' key produces a result processed event."""
        raw = {"result": {"text": "final answer"}}
        events = _handle_lifecycle_events(raw)
        result_events = [e for e in events if e["type"] == "result"]
        assert len(result_events) == 1
        assert result_events[0]["data"]["result"]["text"] == "final answer"

    def test_empty_event_returns_empty(self):
        """An event with no lifecycle keys returns an empty list."""
        assert _handle_lifecycle_events({}) == []

    def test_tool_result_extracted_from_message_content(self):
        """toolResult in message content produces a tool_result event."""
        raw = {
            "message": {
                "role": "user",
                "content": [
                    {"toolResult": {"toolUseId": "tu-1", "content": [{"text": "ok"}]}}
                ],
            }
        }
        events = _handle_lifecycle_events(raw)
        tr_events = [e for e in events if e["type"] == "tool_result"]
        assert len(tr_events) == 1
        assert tr_events[0]["data"]["tool_result"]["toolUseId"] == "tu-1"


# ---------------------------------------------------------------------------
# 23.2  Content block start events
# ---------------------------------------------------------------------------

class TestHandleContentBlockStart:
    """Validates: Requirement 23.2"""

    def test_text_content_block_start(self):
        """contentBlockStart with text type produces content_block_start with index."""
        raw = {
            "event": {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {"text": ""},
                }
            }
        }
        idx = {"index": 0, "skipped_blocks": set()}
        events = _handle_content_block_events(raw, idx)
        starts = [e for e in events if e["type"] == "content_block_start"]
        assert len(starts) == 1
        assert starts[0]["data"]["contentBlockIndex"] == 0
        assert starts[0]["data"]["type"] == "text"

    def test_tool_use_content_block_start(self):
        """contentBlockStart with toolUse produces content_block_start with tool info."""
        raw = {
            "event": {
                "contentBlockStart": {
                    "contentBlockIndex": 1,
                    "start": {
                        "toolUse": {
                            "toolUseId": "tu-abc",
                            "name": "search",
                            "type": "tool_use",
                        }
                    },
                }
            }
        }
        idx = {"index": 0, "skipped_blocks": set()}
        events = _handle_content_block_events(raw, idx)
        starts = [e for e in events if e["type"] == "content_block_start"]
        assert len(starts) == 1
        assert starts[0]["data"]["contentBlockIndex"] == 1
        assert starts[0]["data"]["type"] == "tool_use"
        assert starts[0]["data"]["toolUse"]["toolUseId"] == "tu-abc"
        assert starts[0]["data"]["toolUse"]["name"] == "search"

    def test_content_block_start_uses_tracked_index_when_missing(self):
        """When provider omits contentBlockIndex, the tracked index is used."""
        raw = {
            "event": {
                "contentBlockStart": {
                    "start": {"text": ""},
                }
            }
        }
        idx = {"index": 3, "skipped_blocks": set()}
        events = _handle_content_block_events(raw, idx)
        starts = [e for e in events if e["type"] == "content_block_start"]
        assert starts[0]["data"]["contentBlockIndex"] == 3


# ---------------------------------------------------------------------------
# 23.3  Content block delta events
# ---------------------------------------------------------------------------

class TestHandleContentBlockDelta:
    """Validates: Requirement 23.3"""

    def test_text_delta(self):
        """contentBlockDelta with text produces content_block_delta with text."""
        raw = {
            "event": {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"text": "Hello "},
                }
            }
        }
        idx = {"index": 0, "skipped_blocks": set()}
        events = _handle_content_block_events(raw, idx)
        deltas = [e for e in events if e["type"] == "content_block_delta"]
        assert len(deltas) == 1
        assert deltas[0]["data"]["text"] == "Hello "
        assert deltas[0]["data"]["contentBlockIndex"] == 0

    def test_tool_use_delta(self):
        """contentBlockDelta with toolUse input produces content_block_delta."""
        raw = {
            "event": {
                "contentBlockDelta": {
                    "contentBlockIndex": 1,
                    "delta": {"toolUse": {"input": '{"query": "test"}'}},
                }
            }
        }
        idx = {"index": 0, "skipped_blocks": set()}
        events = _handle_content_block_events(raw, idx)
        deltas = [e for e in events if e["type"] == "content_block_delta"]
        assert len(deltas) == 1
        assert deltas[0]["data"]["type"] == "tool_use"
        assert deltas[0]["data"]["input"] == '{"query": "test"}'

    def test_reasoning_delta_skipped(self):
        """contentBlockDelta with reasoningContent is skipped (handled elsewhere)."""
        raw = {
            "event": {
                "contentBlockDelta": {
                    "contentBlockIndex": 2,
                    "delta": {"reasoningContent": {"text": "thinking..."}},
                }
            }
        }
        idx = {"index": 0, "skipped_blocks": set()}
        events = _handle_content_block_events(raw, idx)
        deltas = [e for e in events if e["type"] == "content_block_delta"]
        assert len(deltas) == 0
        # Block should be tracked as skipped
        assert 2 in idx["skipped_blocks"]

    def test_message_start_resets_block_index(self):
        """messageStart resets the tracked block index to 0."""
        raw = {"event": {"messageStart": {"role": "assistant"}}}
        idx = {"index": 5, "skipped_blocks": {1, 2}}
        events = _handle_content_block_events(raw, idx)
        assert idx["index"] == 0
        assert len(idx["skipped_blocks"]) == 0
        starts = [e for e in events if e["type"] == "message_start"]
        assert len(starts) == 1

    def test_message_stop(self):
        """messageStop produces a message_stop event with stopReason."""
        raw = {"event": {"messageStop": {"stopReason": "end_turn"}}}
        idx = {"index": 0, "skipped_blocks": set()}
        events = _handle_content_block_events(raw, idx)
        stops = [e for e in events if e["type"] == "message_stop"]
        assert len(stops) == 1
        assert stops[0]["data"]["stopReason"] == "end_turn"

    def test_content_block_stop(self):
        """contentBlockStop produces content_block_stop event."""
        raw = {"event": {"contentBlockStop": {"contentBlockIndex": 0}}}
        idx = {"index": 0, "skipped_blocks": set()}
        events = _handle_content_block_events(raw, idx)
        stops = [e for e in events if e["type"] == "content_block_stop"]
        assert len(stops) == 1
        assert stops[0]["data"]["contentBlockIndex"] == 0

    def test_content_block_stop_skipped_for_reasoning(self):
        """contentBlockStop for a skipped reasoning block is not emitted."""
        raw = {"event": {"contentBlockStop": {"contentBlockIndex": 2}}}
        idx = {"index": 0, "skipped_blocks": {2}}
        events = _handle_content_block_events(raw, idx)
        stops = [e for e in events if e["type"] == "content_block_stop"]
        assert len(stops) == 0

    def test_no_event_key_returns_empty(self):
        """Event without 'event' key returns empty list."""
        assert _handle_content_block_events({}, {"index": 0, "skipped_blocks": set()}) == []
        assert _handle_content_block_events({"event": "not_a_dict"}, {"index": 0, "skipped_blocks": set()}) == []


# ---------------------------------------------------------------------------
# 23.4  Tool events
# ---------------------------------------------------------------------------

class TestHandleToolEvents:
    """Validates: Requirement 23.4"""

    def test_current_tool_use_event(self):
        """current_tool_use with name produces a tool_use event."""
        raw = {
            "current_tool_use": {
                "toolUseId": "tu-123",
                "name": "web_search",
                "input": {"query": "test"},
            }
        }
        events = _handle_tool_events(raw)
        tu_events = [e for e in events if e["type"] == "tool_use"]
        assert len(tu_events) == 1
        data = tu_events[0]["data"]["tool_use"]
        assert data["name"] == "web_search"
        assert data["tool_use_id"] == "tu-123"
        assert data["input"] == {"query": "test"}

    def test_current_tool_use_without_name_skipped(self):
        """current_tool_use without name is skipped."""
        raw = {"current_tool_use": {"toolUseId": "tu-123"}}
        events = _handle_tool_events(raw)
        assert len([e for e in events if e["type"] == "tool_use"]) == 0

    def test_tool_result_event(self):
        """tool_result key produces a tool_result event."""
        raw = {"tool_result": {"content": [{"text": "result"}]}}
        events = _handle_tool_events(raw)
        tr_events = [e for e in events if e["type"] == "tool_result"]
        assert len(tr_events) == 1
        assert tr_events[0]["data"]["tool_result"]["content"] == [{"text": "result"}]

    def test_tool_result_with_display_content(self):
        """tool_result with display_content extracts it."""
        raw = {
            "tool_result": {
                "content": [{"text": "ok"}],
                "display_content": {"type": "json_block", "json_block": "{}"},
            }
        }
        events = _handle_tool_events(raw)
        tr_events = [e for e in events if e["type"] == "tool_result"]
        assert "display_content" in tr_events[0]["data"]

    def test_tool_error_event(self):
        """tool_error key produces a tool_error event."""
        raw = {"tool_error": "Something went wrong"}
        events = _handle_tool_events(raw)
        te_events = [e for e in events if e["type"] == "tool_error"]
        assert len(te_events) == 1
        assert te_events[0]["data"]["tool_error"] == "Something went wrong"

    def test_tool_stream_event(self):
        """tool_stream_event key produces a tool_stream_event event."""
        raw = {"tool_stream_event": {"chunk": "partial data"}}
        events = _handle_tool_events(raw)
        tse = [e for e in events if e["type"] == "tool_stream_event"]
        assert len(tse) == 1

    def test_empty_event_returns_empty(self):
        """Event with no tool keys returns empty list."""
        assert _handle_tool_events({}) == []

    def test_tool_use_with_display_content_and_message(self):
        """current_tool_use with display_content and message are included."""
        raw = {
            "current_tool_use": {
                "name": "code_interpreter",
                "toolUseId": "tu-456",
                "input": {},
                "display_content": {"type": "text", "text": "Running..."},
                "message": "Executing code",
            }
        }
        events = _handle_tool_events(raw)
        tu = [e for e in events if e["type"] == "tool_use"][0]
        assert tu["data"]["tool_use"]["display_content"]["type"] == "text"
        assert tu["data"]["tool_use"]["message"] == "Executing code"


# ---------------------------------------------------------------------------
# 23.5  Reasoning events
# ---------------------------------------------------------------------------

class TestHandleReasoningEvents:
    """Validates: Requirement 23.5"""

    def test_reasoning_flag(self):
        """reasoning: True produces a reasoning event."""
        events = _handle_reasoning_events({"reasoning": True})
        assert any(e["type"] == "reasoning" for e in events)

    def test_reasoning_text_top_level(self):
        """Top-level reasoningText produces a reasoning event with text."""
        events = _handle_reasoning_events({"reasoningText": "Let me think..."})
        r_events = [e for e in events if e["type"] == "reasoning"]
        assert len(r_events) == 1
        assert r_events[0]["data"]["reasoningText"] == "Let me think..."

    def test_reasoning_content_nested_text(self):
        """reasoningContent with nested reasoningText.text is extracted."""
        raw = {
            "reasoningContent": {
                "reasoningText": {"text": "Step 1: analyze", "signature": "sig123"}
            }
        }
        events = _handle_reasoning_events(raw)
        r_events = [e for e in events if e["type"] == "reasoning"]
        assert len(r_events) == 1
        assert r_events[0]["data"]["reasoningText"] == "Step 1: analyze"
        assert r_events[0]["data"]["reasoning_signature"] == "sig123"

    def test_reasoning_content_string_text(self):
        """reasoningContent with string reasoningText is handled."""
        raw = {"reasoningContent": {"reasoningText": "just a string"}}
        events = _handle_reasoning_events(raw)
        r_events = [e for e in events if e["type"] == "reasoning"]
        assert len(r_events) == 1
        assert r_events[0]["data"]["reasoningText"] == "just a string"

    def test_reasoning_signature_top_level(self):
        """Top-level reasoning_signature produces a reasoning event."""
        events = _handle_reasoning_events({"reasoning_signature": "abc"})
        r_events = [e for e in events if e["type"] == "reasoning"]
        assert len(r_events) == 1
        assert r_events[0]["data"]["reasoning_signature"] == "abc"

    def test_redacted_content_top_level(self):
        """Top-level redactedContent produces a reasoning event."""
        events = _handle_reasoning_events({"redactedContent": b"redacted"})
        r_events = [e for e in events if e["type"] == "reasoning"]
        assert len(r_events) == 1

    def test_reasoning_content_with_redacted(self):
        """reasoningContent with redactedContent is extracted."""
        raw = {"reasoningContent": {"redactedContent": b"secret"}}
        events = _handle_reasoning_events(raw)
        r_events = [e for e in events if e["type"] == "reasoning"]
        assert len(r_events) == 1

    def test_empty_event_returns_empty(self):
        """Event with no reasoning keys returns empty list."""
        assert _handle_reasoning_events({}) == []

    def test_reasoning_text_not_duplicated_when_content_present(self):
        """Top-level reasoningText is skipped when reasoningContent is present."""
        raw = {
            "reasoningContent": {"reasoningText": {"text": "from content"}},
            "reasoningText": "should be ignored",
        }
        events = _handle_reasoning_events(raw)
        texts = [e["data"].get("reasoningText") for e in events if e["type"] == "reasoning"]
        assert "from content" in texts
        assert "should be ignored" not in texts


# ---------------------------------------------------------------------------
# 23.6  Citation events
# ---------------------------------------------------------------------------

class TestHandleCitationEvents:
    """Validates: Requirement 23.6"""

    def test_citation_start_delta(self):
        """citation_start_delta produces a citation_start event with metadata."""
        raw = {
            "citation_start_delta": {
                "citation": {
                    "uuid": "cit-1",
                    "title": "Source Doc",
                    "url": "https://example.com",
                    "sources": [{"title": "s1"}],
                }
            }
        }
        events = _handle_citation_events(raw)
        cs = [e for e in events if e["type"] == "citation_start"]
        assert len(cs) == 1
        assert cs[0]["data"]["citation_uuid"] == "cit-1"
        assert cs[0]["data"]["title"] == "Source Doc"
        assert cs[0]["data"]["url"] == "https://example.com"
        assert cs[0]["data"]["sources"] == [{"title": "s1"}]

    def test_citation_end_delta(self):
        """citation_end_delta produces a citation_end event with uuid."""
        raw = {"citation_end_delta": {"citation_uuid": "cit-1"}}
        events = _handle_citation_events(raw)
        ce = [e for e in events if e["type"] == "citation_end"]
        assert len(ce) == 1
        assert ce[0]["data"]["citation_uuid"] == "cit-1"

    def test_citation_top_level(self):
        """Top-level citation key produces a citation event."""
        raw = {"citation": {"source": "wiki", "text": "cited text"}}
        events = _handle_citation_events(raw)
        c = [e for e in events if e["type"] == "citation"]
        assert len(c) == 1
        assert c[0]["data"]["citation"]["source"] == "wiki"

    def test_citations_content_array(self):
        """citationsContent array produces multiple citation events."""
        raw = {
            "citationsContent": [
                {"source": "a"},
                {"source": "b"},
            ]
        }
        events = _handle_citation_events(raw)
        c = [e for e in events if e["type"] == "citation"]
        assert len(c) == 2

    def test_citation_start_with_metadata_and_origin(self):
        """citation_start_delta with metadata and origin_tool_name are extracted."""
        raw = {
            "citation_start_delta": {
                "citation": {
                    "uuid": "cit-2",
                    "metadata": {"site_domain": "example.com"},
                    "origin_tool_name": "web_search",
                }
            }
        }
        events = _handle_citation_events(raw)
        cs = [e for e in events if e["type"] == "citation_start"]
        assert cs[0]["data"]["metadata"]["site_domain"] == "example.com"
        assert cs[0]["data"]["origin_tool_name"] == "web_search"

    def test_empty_event_returns_empty(self):
        """Event with no citation keys returns empty list."""
        assert _handle_citation_events({}) == []

    def test_citation_top_level_skipped_when_start_delta_present(self):
        """Top-level citation is skipped when citation_start_delta is present."""
        raw = {
            "citation_start_delta": {"citation": {"uuid": "cit-3"}},
            "citation": {"source": "should be skipped"},
        }
        events = _handle_citation_events(raw)
        # Should only have citation_start, not a separate citation event
        types = [e["type"] for e in events]
        assert "citation_start" in types
        assert "citation" not in types


# ---------------------------------------------------------------------------
# 23.7  Metadata events
# ---------------------------------------------------------------------------

class TestHandleMetadataEvents:
    """Validates: Requirement 23.7"""

    def test_top_level_metadata_with_usage(self):
        """metadata key with usage produces a metadata event with token counts."""
        raw = {
            "metadata": {
                "usage": {
                    "inputTokens": 100,
                    "outputTokens": 50,
                    "totalTokens": 150,
                }
            }
        }
        events = _handle_metadata_events(raw)
        m = [e for e in events if e["type"] == "metadata"]
        assert len(m) >= 1
        usage = m[0]["data"]["usage"]
        assert usage["inputTokens"] == 100
        assert usage["outputTokens"] == 50
        assert usage["totalTokens"] == 150

    def test_top_level_metadata_with_metrics(self):
        """metadata key with metrics produces a metadata event with latency."""
        raw = {
            "metadata": {
                "metrics": {"latencyMs": 250, "timeToFirstByteMs": 80}
            }
        }
        events = _handle_metadata_events(raw)
        m = [e for e in events if e["type"] == "metadata"]
        assert len(m) >= 1
        assert m[0]["data"]["metrics"]["latencyMs"] == 250
        assert m[0]["data"]["metrics"]["timeToFirstByteMs"] == 80

    def test_standalone_usage(self):
        """Top-level usage (without metadata key) produces a metadata event."""
        raw = {"usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}}
        events = _handle_metadata_events(raw)
        m = [e for e in events if e["type"] == "metadata"]
        assert len(m) >= 1
        assert m[0]["data"]["usage"]["inputTokens"] == 10

    def test_standalone_metrics(self):
        """Top-level metrics (without metadata key) produces a metadata event."""
        raw = {"metrics": {"latencyMs": 100}}
        events = _handle_metadata_events(raw)
        m = [e for e in events if e["type"] == "metadata"]
        assert len(m) >= 1
        assert m[0]["data"]["metrics"]["latencyMs"] == 100

    def test_cache_tokens_included(self):
        """Cache token fields are included when present."""
        raw = {
            "metadata": {
                "usage": {
                    "inputTokens": 100,
                    "outputTokens": 50,
                    "totalTokens": 150,
                    "cacheReadInputTokens": 80,
                    "cacheWriteInputTokens": 20,
                }
            }
        }
        events = _handle_metadata_events(raw)
        m = [e for e in events if e["type"] == "metadata"]
        usage = m[0]["data"]["usage"]
        assert usage["cacheReadInputTokens"] == 80
        assert usage["cacheWriteInputTokens"] == 20

    def test_nested_event_model_metadata(self):
        """Nested event.modelMetadataEvent produces a metadata event."""
        raw = {
            "event": {
                "modelMetadataEvent": {
                    "usage": {"inputTokens": 200, "outputTokens": 100, "totalTokens": 300}
                }
            }
        }
        events = _handle_metadata_events(raw)
        m = [e for e in events if e["type"] == "metadata"]
        assert len(m) >= 1
        assert m[0]["data"]["usage"]["inputTokens"] == 200

    def test_snake_case_usage_keys(self):
        """Snake_case usage keys (input_tokens) are normalized."""
        raw = {"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
        events = _handle_metadata_events(raw)
        m = [e for e in events if e["type"] == "metadata"]
        assert m[0]["data"]["usage"]["inputTokens"] == 10
        assert m[0]["data"]["usage"]["outputTokens"] == 5

    def test_empty_event_returns_empty(self):
        """Event with no metadata keys returns empty list."""
        assert _handle_metadata_events({}) == []

    def test_result_with_accumulated_usage(self):
        """result.metrics.accumulated_usage rides the metadata_summary track.

        It must NOT be emitted as a `metadata` event — those land in
        per_message_metadata in the stream coordinator and would clobber
        the last assistant message's per-call usage with a turn-cumulative
        value, double-counting earlier messages at pricing time.
        """
        raw = {
            "result": {
                "metrics": {
                    "accumulated_usage": {
                        "inputTokens": 500,
                        "outputTokens": 200,
                        "totalTokens": 700,
                    }
                }
            }
        }
        events = _handle_metadata_events(raw)
        per_message_typed = [e for e in events if e["type"] == "metadata"]
        summary_typed = [e for e in events if e["type"] == "metadata_summary"]
        assert per_message_typed == []
        assert len(summary_typed) == 1
        assert summary_typed[0]["data"]["usage"]["inputTokens"] == 500


# ---------------------------------------------------------------------------
# 23.8  _serialize_object
# ---------------------------------------------------------------------------

class TestSerializeObject:
    """Validates: Requirement 23.8"""

    # -- Primitives --

    def test_none(self):
        assert _serialize_object(None) is None

    def test_string(self):
        assert _serialize_object("hello") == "hello"

    def test_int(self):
        assert _serialize_object(42) == 42

    def test_float(self):
        assert _serialize_object(3.14) == 3.14

    def test_bool(self):
        assert _serialize_object(True) is True

    # -- Common types --

    def test_datetime(self):
        dt = datetime(2025, 1, 15, 10, 30, 0)
        assert _serialize_object(dt) == dt.isoformat()

    def test_date(self):
        d = date(2025, 6, 15)
        assert _serialize_object(d) == d.isoformat()

    def test_uuid(self):
        u = uuid4()
        assert _serialize_object(u) == str(u)

    def test_decimal(self):
        d = Decimal("3.14159")
        assert _serialize_object(d) == "3.14159"

    # -- Containers --

    def test_dict_recursive(self):
        result = _serialize_object({"dt": datetime(2025, 1, 1), "n": 5})
        assert result["dt"] == "2025-01-01T00:00:00"
        assert result["n"] == 5

    def test_list_recursive(self):
        result = _serialize_object([1, "two", Decimal("3")])
        assert result == [1, "two", "3"]

    def test_tuple_serialized_as_list(self):
        result = _serialize_object((1, 2, 3))
        assert result == [1, 2, 3]

    def test_nested_dict_and_list(self):
        obj = {"items": [{"id": UUID("12345678-1234-5678-1234-567812345678")}]}
        result = _serialize_object(obj)
        assert result["items"][0]["id"] == "12345678-1234-5678-1234-567812345678"

    # -- Objects with __dict__ --

    def test_object_with_dict_attr(self):
        class Foo:
            def __init__(self):
                self.x = 10
                self.y = "bar"

        result = _serialize_object(Foo())
        assert result == {"x": 10, "y": "bar"}

    def test_object_with_nested_complex_attrs(self):
        class Inner:
            def __init__(self):
                self.val = Decimal("1.5")

        class Outer:
            def __init__(self):
                self.inner = Inner()
                self.name = "test"

        result = _serialize_object(Outer())
        assert result["name"] == "test"
        assert result["inner"]["val"] == "1.5"

    # -- Bytes --

    def test_bytes_base64_encoded(self):
        import base64
        data = b"binary data"
        result = _serialize_object(data)
        assert result == base64.b64encode(data).decode("utf-8")

    # -- Fallback --

    def test_fallback_to_string(self):
        """Unrecognized types fall back to str()."""
        result = _serialize_object(set([1, 2, 3]))
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Structural invariant: all handler outputs have "type" and "data" keys
# ---------------------------------------------------------------------------

class TestProcessedEventStructure:
    """Cross-cutting: every event from every handler has type + data."""

    def _assert_structure(self, events):
        for e in events:
            assert "type" in e, f"Missing 'type' in {e}"
            assert "data" in e, f"Missing 'data' in {e}"
            assert isinstance(e["type"], str)
            assert isinstance(e["data"], dict)

    def test_lifecycle_structure(self):
        events = _handle_lifecycle_events({"init_event_loop": True, "start_event_loop": True})
        self._assert_structure(events)

    def test_content_block_structure(self):
        raw = {
            "event": {
                "contentBlockStart": {"contentBlockIndex": 0, "start": {"text": ""}},
                "contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "hi"}},
                "contentBlockStop": {"contentBlockIndex": 0},
            }
        }
        events = _handle_content_block_events(raw, {"index": 0, "skipped_blocks": set()})
        self._assert_structure(events)

    def test_tool_structure(self):
        events = _handle_tool_events({
            "current_tool_use": {"name": "test", "toolUseId": "t1", "input": {}},
        })
        self._assert_structure(events)

    def test_reasoning_structure(self):
        events = _handle_reasoning_events({"reasoningText": "thinking"})
        self._assert_structure(events)

    def test_citation_structure(self):
        events = _handle_citation_events({
            "citation_start_delta": {"citation": {"uuid": "c1"}},
        })
        self._assert_structure(events)

    def test_metadata_structure(self):
        events = _handle_metadata_events({
            "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
        })
        self._assert_structure(events)


class TestProcessAgentStreamMaxTokens:
    """MaxTokensReachedException is classified as a recoverable max_tokens
    error event (not the generic stream_error) and never leaks the raw SDK
    message/URL."""

    @pytest.mark.asyncio
    async def test_max_tokens_emits_recoverable_error_event(self):
        from strands.types.exceptions import MaxTokensReachedException

        async def mock_stream():
            yield {"start_event_loop": True}
            raise MaxTokensReachedException(
                "Agent has reached an unrecoverable state due to max_tokens "
                "limit. For more information see: https://strandsagents.com/x"
            )

        events = []
        async for ev in process_agent_stream(mock_stream()):
            events.append(ev)

        error_events = [e for e in events if e.get("type") == "error"]
        assert len(error_events) == 1
        data = error_events[0]["data"]
        assert data["code"] == "max_tokens"
        assert data["recoverable"] is True
        # detail is None and excluded — no leaked SDK URL/raw exception text.
        assert "strandsagents.com" not in str(data)
        assert "unrecoverable" not in str(data).lower()

    @pytest.mark.asyncio
    async def test_generic_exception_still_stream_error(self):
        """Regression: a non-max_tokens exception still maps to the
        non-recoverable generic stream_error."""

        async def mock_stream():
            yield {"start_event_loop": True}
            raise RuntimeError("totally unrelated boom")

        events = []
        async for ev in process_agent_stream(mock_stream()):
            events.append(ev)

        error_events = [e for e in events if e.get("type") == "error"]
        assert len(error_events) == 1
        data = error_events[0]["data"]
        assert data["code"] == "stream_error"
        assert data["recoverable"] is False


class TestFormatForceStopMessage:
    """Classifier for raw Bedrock force-stop reasons → user-facing markdown.

    Regression coverage for the gpt-oss-120b case where the model rejects
    documents outright and the message was being misclassified as a size
    overflow because the substring `"document"` matched both branches.
    """

    def test_model_does_not_support_documents_is_not_size_error(self):
        reason = (
            "An error occurred (ValidationException) when calling the "
            "ConverseStream operation: This model doesn't support documents."
        )
        message, recoverable = _format_force_stop_message(reason)

        assert "can't read attached files" in message
        assert "switch to a model that supports documents" in message
        # Must NOT surface the 4.5 MB size message — that's a different problem
        assert "4.5 MB" not in message
        assert "too large" not in message
        # No brand names (model lineups change) and no deployment-specific
        # tool references (Spreadsheet Analysis may not be enabled).
        assert "Claude" not in message
        assert "Nova" not in message
        assert "Spreadsheet Analysis" not in message
        # No suggestions for actions the user can't actually take.
        assert "remove the attachment" not in message
        assert recoverable is True

    def test_model_does_not_support_documents_alternate_phrasing(self):
        """AWS sometimes phrases this as 'does not support' (no contraction)."""
        reason = (
            "ValidationException: This model does not support documents in "
            "the request."
        )
        message, recoverable = _format_force_stop_message(reason)

        assert "can't read attached files" in message
        assert "4.5 MB" not in message
        assert recoverable is True

    def test_model_does_not_support_images(self):
        reason = (
            "An error occurred (ValidationException) when calling the "
            "ConverseStream operation: This model doesn't support images."
        )
        message, recoverable = _format_force_stop_message(reason)

        assert "can't read attached images" in message
        assert "switch to a model that supports images" in message
        assert "Claude" not in message
        assert "Nova" not in message
        assert "remove the image" not in message
        assert recoverable is True

    def test_document_size_limit_classic_message(self):
        reason = (
            "ValidationException: The provided document exceeds the maximum "
            "document size."
        )
        message, recoverable = _format_force_stop_message(reason)

        assert "too large" in message
        assert "4.5 MB" in message
        # Guidance is deployment-agnostic — no references to optional tools
        # or UI affordances that might not exist in every deployment.
        assert "Spreadsheet Analysis" not in message
        assert "gear icon" not in message
        assert recoverable is True

    def test_document_size_limit_too_large_phrasing(self):
        reason = "ValidationException: document content is too large to process"
        message, recoverable = _format_force_stop_message(reason)

        assert "too large" in message
        assert "4.5 MB" in message
        assert recoverable is True

    def test_throttling(self):
        reason = "ThrottlingException: Too many requests, please slow down."
        message, recoverable = _format_force_stop_message(reason)

        assert "too many requests" in message.lower()
        assert recoverable is True

    def test_access_denied(self):
        reason = "AccessDeniedException: User is not authorized to invoke this model."
        message, recoverable = _format_force_stop_message(reason)

        assert "don't have access" in message
        assert recoverable is False

    def test_unknown_reason_falls_through(self):
        reason = "Some unexpected transient error from upstream"
        message, recoverable = _format_force_stop_message(reason)

        assert "force-stopped" in message
        assert reason in message
        assert recoverable is False
