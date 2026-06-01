"""Tests for the streamed partial-JSON healer (SEP-1865 tool-input-partial).

The host must close unterminated strings/brackets so a streamed prefix of a
tool call's arguments parses as a JSON object before it is delivered to the
App. These cover the common shapes Bedrock produces while streaming
`toolUse.input`: mid-string, mid-array, dangling comma/colon, nested
containers, and the degenerate cases that must yield ``None``.
"""

from __future__ import annotations

from apis.shared.mcp_apps.partial_json import heal_partial_json


class TestHealPartialJson:
    def test_none_and_empty_return_none(self) -> None:
        assert heal_partial_json(None) is None
        assert heal_partial_json("") is None
        assert heal_partial_json("   ") is None

    def test_complete_object_passthrough(self) -> None:
        assert heal_partial_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}

    def test_non_object_returns_none(self) -> None:
        # Tool arguments are always key/value; a bare array/scalar is not valid.
        assert heal_partial_json("[1, 2, 3]") is None
        assert heal_partial_json('"just a string"') is None
        assert heal_partial_json("42") is None

    def test_open_object_is_closed(self) -> None:
        assert heal_partial_json('{"a": 1') == {"a": 1}
        assert heal_partial_json('{"a": 1,') == {"a": 1}

    def test_mid_string_value_is_terminated(self) -> None:
        out = heal_partial_json('{"title": "Hello wor')
        assert out == {"title": "Hello wor"}

    def test_dangling_key_is_dropped(self) -> None:
        # A key with no value yet must not corrupt the object.
        assert heal_partial_json('{"a": 1, "b":') == {"a": 1}
        assert heal_partial_json('{"a": 1, "b"') == {"a": 1}

    def test_nested_array_of_objects_partial(self) -> None:
        raw = '{"elements": [{"type": "rect", "id": "1"}, {"type": "ell'
        out = heal_partial_json(raw)
        assert isinstance(out, dict)
        assert "elements" in out
        assert isinstance(out["elements"], list)
        # First element survives intact; the truncated tail is dropped/closed.
        assert {"type": "rect", "id": "1"} in out["elements"]

    def test_elements_as_embedded_json_string(self) -> None:
        # Some tools pass `elements` as a JSON *string*; the outer object must
        # still heal to a dict (the App re-parses the inner string itself).
        raw = '{"elements": "[{\\"type\\":\\"rect\\"},{\\"type\\":\\"ell'
        out = heal_partial_json(raw)
        assert isinstance(out, dict)
        assert isinstance(out.get("elements"), str)

    def test_escaped_quote_inside_string(self) -> None:
        out = heal_partial_json('{"label": "she said \\"hi')
        assert isinstance(out, dict)
        assert "label" in out

    def test_trailing_backslash_does_not_escape_closer(self) -> None:
        # A lone trailing backslash would escape our closing quote → must drop.
        out = heal_partial_json('{"path": "C:\\\\temp\\\\')
        assert isinstance(out, dict)
        assert "path" in out

    def test_camera_update_progressive_shape(self) -> None:
        # Mirrors the Excalidraw guided-tour payload: elements array with a
        # cameraUpdate pseudo-element, truncated mid-stream.
        raw = (
            '{"elements": [{"type": "rectangle", "x": 0, "y": 0}, '
            '{"type": "cameraUpdate", "x": 100, "y": 50, "width": 40'
        )
        out = heal_partial_json(raw)
        assert isinstance(out, dict)
        assert isinstance(out["elements"], list)
        assert out["elements"][0]["type"] == "rectangle"

    def test_deeply_nested_partial(self) -> None:
        raw = '{"a": {"b": {"c": [1, 2, {"d": " value'
        out = heal_partial_json(raw)
        assert isinstance(out, dict)
        assert "a" in out
