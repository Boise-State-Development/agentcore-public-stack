"""The form module: what a server may ask for, and what may go back to it."""

from __future__ import annotations

import json

import pytest

from apis.shared.feature_flags import mcp_elicitation_enabled
from apis.shared.mcp_elicitation.forms import (
    ELICITATION_EVENT_TYPE,
    MAX_FIELDS,
    MAX_MESSAGE_CHARS,
    MAX_OPTIONS,
    InvalidElicitationAnswer,
    UnsupportedElicitation,
    build_reason,
    event_payload,
    event_sse,
    interrupt_name,
    normalize_form,
    schema_digest,
    validate_answer,
)

GITHUB_DELETE = {
    "type": "object",
    "properties": {
        "repository_name": {
            "type": "string",
            "title": "Repository name",
            "description": "Enter acme/scratch exactly to confirm deletion",
        }
    },
    "required": ["repository_name"],
}


def _form(properties, required=None, message="Fill this in"):
    schema = {"type": "object", "properties": properties}
    if required is not None:
        schema["required"] = required
    return normalize_form(message, schema)


class TestFlag:
    @pytest.mark.parametrize("value", [None, "", "false", "yes", "1", "on"])
    def test_only_true_enables_it(self, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("MCP_ELICITATION_ENABLED", raising=False)
        else:
            monkeypatch.setenv("MCP_ELICITATION_ENABLED", value)
        assert mcp_elicitation_enabled() is False

    @pytest.mark.parametrize("value", ["true", "TRUE", " True "])
    def test_true_in_any_case_enables_it(self, monkeypatch, value):
        monkeypatch.setenv("MCP_ELICITATION_ENABLED", value)
        assert mcp_elicitation_enabled() is True


class TestNormalize:
    def test_githubs_delete_confirmation(self):
        form = normalize_form('Type "acme/scratch" to confirm.', GITHUB_DELETE)

        assert form.message == 'Type "acme/scratch" to confirm.'
        (field,) = form.fields
        assert (field.name, field.kind, field.title, field.required) == (
            "repository_name",
            "text",
            "Repository name",
            True,
        )

    def test_a_title_defaults_to_the_field_name(self):
        (field,) = _form({"note": {"type": "string"}}).fields
        assert field.title == "note"
        assert field.required is False

    def test_string_formats_and_lengths(self):
        (field,) = _form(
            {"email": {"type": "string", "format": "email", "minLength": 3, "maxLength": 80}}
        ).fields
        assert (field.format, field.min_length, field.max_length) == ("email", 3, 80)

    def test_numbers_and_integers(self):
        form = _form(
            {
                "ratio": {"type": "number", "minimum": 0, "maximum": 1.5},
                "count": {"type": "integer", "default": 2},
            }
        )
        ratio, count = form.fields
        assert (ratio.kind, ratio.minimum, ratio.maximum) == ("number", 0, 1.5)
        assert (count.kind, count.default) == ("integer", 2)

    def test_boolean(self):
        (field,) = _form({"notify": {"type": "boolean", "default": True}}).fields
        assert (field.kind, field.default) == ("boolean", True)

    def test_untitled_single_select(self):
        (field,) = _form({"size": {"type": "string", "enum": ["s", "m"]}}).fields
        assert field.kind == "choice"
        assert [(o.value, o.label) for o in field.options] == [("s", "s"), ("m", "m")]
        assert field.multi_select is False

    def test_titled_single_select(self):
        (field,) = _form(
            {
                "size": {
                    "type": "string",
                    "oneOf": [{"const": "s", "title": "Small"}, {"const": "m", "title": "Medium"}],
                    "default": "m",
                }
            }
        ).fields
        assert [(o.value, o.label) for o in field.options] == [("s", "Small"), ("m", "Medium")]
        assert field.default == "m"

    def test_legacy_enum_names(self):
        (field,) = _form(
            {"size": {"type": "string", "enum": ["s", "m"], "enumNames": ["Small", "Medium"]}}
        ).fields
        assert [o.label for o in field.options] == ["Small", "Medium"]

    def test_untitled_multi_select(self):
        (field,) = _form(
            {
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["a", "b", "c"]},
                    "minItems": 1,
                    "maxItems": 2,
                }
            }
        ).fields
        assert (field.kind, field.multi_select, field.min_items, field.max_items) == (
            "choice",
            True,
            1,
            2,
        )

    def test_titled_multi_select(self):
        (field,) = _form(
            {
                "tags": {
                    "type": "array",
                    "items": {"anyOf": [{"const": "a", "title": "Alpha"}]},
                    "default": ["a"],
                }
            }
        ).fields
        assert [(o.value, o.label) for o in field.options] == [("a", "Alpha")]
        assert field.default == ["a"]

    @pytest.mark.parametrize(
        "schema",
        [
            None,
            {"type": "array"},
            {"type": "object"},
            {"type": "object", "properties": {}},
            {"type": "object", "properties": {"x": {"type": "object"}}},
            {"type": "object", "properties": {"x": {"type": "string", "format": "password"}}},
            {"type": "object", "properties": {"x": {"type": "string", "enum": []}}},
            {"type": "object", "properties": {"x": {"type": "string", "enum": [1, 2]}}},
            {"type": "object", "properties": {"x": {"type": "string", "enum": ["a", "a"]}}},
            {"type": "object", "properties": {"x": {"type": "string", "enum": ["a"], "default": "b"}}},
            {"type": "object", "properties": {"x": {"type": "array", "items": {"type": "number"}}}},
            {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["y"]},
            {"type": "object", "properties": {"x": {"type": "number", "minimum": "low"}}},
            {"type": "object", "properties": {"x": {"type": "boolean", "default": "yes"}}},
        ],
    )
    def test_anything_outside_the_subset_is_refused(self, schema):
        with pytest.raises(UnsupportedElicitation):
            normalize_form("Fill this in", schema)

    def test_an_empty_message_is_refused(self):
        with pytest.raises(UnsupportedElicitation):
            normalize_form("", GITHUB_DELETE)

    def test_bounds_are_enforced(self):
        with pytest.raises(UnsupportedElicitation):
            normalize_form("x" * (MAX_MESSAGE_CHARS + 1), GITHUB_DELETE)
        with pytest.raises(UnsupportedElicitation):
            _form({f"f{i}": {"type": "string"} for i in range(MAX_FIELDS + 1)})
        with pytest.raises(UnsupportedElicitation):
            _form({"x": {"type": "string", "enum": [str(i) for i in range(MAX_OPTIONS + 1)]}})
        with pytest.raises(UnsupportedElicitation):
            _form({"x": {"type": "string", "title": "t" * 201}})


class TestInterruptName:
    def test_it_changes_with_the_schema_not_the_message(self):
        other = json.loads(json.dumps(GITHUB_DELETE))
        other["properties"]["repository_name"]["description"] = "something else"

        assert interrupt_name(0, "k", GITHUB_DELETE) == interrupt_name(0, "k", GITHUB_DELETE)
        assert interrupt_name(0, "k", GITHUB_DELETE) != interrupt_name(0, "k", other)
        assert interrupt_name(0, "k", GITHUB_DELETE) != interrupt_name(1, "k", GITHUB_DELETE)
        assert interrupt_name(0, "k", GITHUB_DELETE) != interrupt_name(0, "j", GITHUB_DELETE)

    def test_the_digest_ignores_key_order(self):
        reordered = {"required": GITHUB_DELETE["required"], **GITHUB_DELETE}
        assert schema_digest(reordered) == schema_digest(GITHUB_DELETE)


class TestValidateAnswer:
    def setup_method(self):
        self.form = _form(
            {
                "name": {"type": "string", "maxLength": 5},
                "count": {"type": "integer", "minimum": 1},
                "ok": {"type": "boolean"},
                "size": {"type": "string", "enum": ["s", "m"]},
                "tags": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}},
            },
            required=["name"],
        )

    def test_accept_with_valid_content(self):
        answer = validate_answer(
            self.form,
            {
                "action": "accept",
                "content": {"name": "abc", "count": 2.0, "ok": False, "size": "m", "tags": ["b"]},
            },
        )
        assert answer.action == "accept"
        assert answer.content == {"name": "abc", "count": 2, "ok": False, "size": "m", "tags": ["b"]}

    def test_optional_fields_may_be_left_out(self):
        answer = validate_answer(self.form, {"action": "accept", "content": {"name": "abc"}})
        assert answer.content == {"name": "abc"}

    @pytest.mark.parametrize("action", ["decline", "cancel"])
    def test_decline_and_cancel_carry_no_content(self, action):
        answer = validate_answer(self.form, {"action": action, "content": {"name": "ignored"}})
        assert (answer.action, answer.content) == (action, None)

    @pytest.mark.parametrize(
        "response",
        [
            None,
            "accept",
            {"action": "maybe"},
            {"action": "accept", "content": []},
            {"action": "accept", "content": {}},
            {"action": "accept", "content": {"name": ""}},
            {"action": "accept", "content": {"name": "toolong"}},
            {"action": "accept", "content": {"name": "a", "count": 0}},
            {"action": "accept", "content": {"name": "a", "count": 1.5}},
            {"action": "accept", "content": {"name": "a", "count": True}},
            {"action": "accept", "content": {"name": "a", "ok": "yes"}},
            {"action": "accept", "content": {"name": "a", "size": "xl"}},
            {"action": "accept", "content": {"name": "a", "tags": ["c"]}},
            {"action": "accept", "content": {"name": "a", "tags": ["a", "a"]}},
            {"action": "accept", "content": {"name": "a", "password": "hunter2"}},
        ],
    )
    def test_anything_that_does_not_fit_is_refused(self, response):
        with pytest.raises(InvalidElicitationAnswer):
            validate_answer(self.form, response)


class TestEvent:
    def test_the_event_carries_the_form_and_who_is_asking(self):
        form = normalize_form('Type "acme/scratch" to confirm.', GITHUB_DELETE)
        reason = build_reason(form, tool_name="github___delete_repository", server_name="GitHub")
        reason["toolUseId"] = "tu-1"

        payload = event_payload("v1:middleware_execute_tool:tu-1:x", reason)

        assert payload == {
            "type": ELICITATION_EVENT_TYPE,
            "interruptId": "v1:middleware_execute_tool:tu-1:x",
            "toolUseId": "tu-1",
            "toolName": "github___delete_repository",
            "serverName": "GitHub",
            "message": 'Type "acme/scratch" to confirm.',
            "fields": [
                {
                    "name": "repository_name",
                    "kind": "text",
                    "title": "Repository name",
                    "description": "Enter acme/scratch exactly to confirm deletion",
                    "required": True,
                    "default": None,
                    "format": None,
                    "minLength": None,
                    "maxLength": None,
                }
            ],
        }
        frame = event_sse("v1:x", reason)
        assert frame.startswith("event: mcp_elicitation_required\ndata: ")
        assert frame.endswith("\n\n")
