"""MCP form elicitation: turn a server's requested schema into a form we can
render, and check a user's answer against it before it goes back.

Pure and stdlib-only, so the Runtime (which pauses the turn and validates the
answer) and the stream coordinator (which emits the form) share one reading of
the schema. ``docs/specs/mcp-elicitation.md`` §4.

What a server may ask for is deliberately narrow. The MCP spec restricts a form
to a flat object of primitive properties: strings (optionally with a format),
numbers, booleans, and single- or multi-select enums. Anything outside that
subset, or over the bounds below, is refused rather than rendered half-right:
the caller answers the server ``cancel`` and the model is told the server asked
for input this app cannot show. The text comes from a third-party server and
travels on SSE and in a DynamoDB breadcrumb, so every string is bounded.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Mapping, Optional, Tuple

#: The interrupt reason's ``type``; the stream coordinator keys on it.
ELICITATION_EVENT_TYPE = "mcp_elicitation_required"

#: Prefix of every elicitation interrupt name. The full name carries the round,
#: the input-request key and the schema digest (``interrupt_name``), so a
#: replayed round finds its stored answer only when the server asks the same
#: question again.
INTERRUPT_NAME_PREFIX = "mcp_elicitation"

MAX_MESSAGE_CHARS = 2_000
MAX_FIELDS = 10
MAX_OPTIONS = 50
MAX_LABEL_CHARS = 200
MAX_TEXT_ANSWER_CHARS = 10_000
MAX_KEY_CHARS = 64

_STRING_FORMATS = frozenset({"date", "date-time", "email", "uri"})
ACTIONS = ("accept", "decline", "cancel")

FieldKind = Literal["text", "number", "integer", "boolean", "choice"]
Action = Literal["accept", "decline", "cancel"]


class UnsupportedElicitation(ValueError):
    """The server asked for a form this app does not render."""


class InvalidElicitationAnswer(ValueError):
    """The answer does not fit the form it was given for."""


@dataclass(frozen=True)
class ChoiceOption:
    value: str
    label: str

    def to_dict(self) -> Dict[str, Any]:
        return {"value": self.value, "label": self.label}


@dataclass(frozen=True)
class FormField:
    name: str
    kind: FieldKind
    title: str
    description: Optional[str] = None
    required: bool = False
    default: Any = None
    # text
    format: Optional[str] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    # number / integer
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    # choice
    options: Tuple[ChoiceOption, ...] = ()
    multi_select: bool = False
    min_items: Optional[int] = None
    max_items: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "title": self.title,
            "description": self.description,
            "required": self.required,
            "default": self.default,
        }
        if self.kind == "text":
            data.update(
                format=self.format, minLength=self.min_length, maxLength=self.max_length
            )
        elif self.kind in ("number", "integer"):
            data.update(minimum=self.minimum, maximum=self.maximum)
        elif self.kind == "choice":
            data.update(
                options=[option.to_dict() for option in self.options],
                multiSelect=self.multi_select,
                minItems=self.min_items,
                maxItems=self.max_items,
            )
        return data


@dataclass(frozen=True)
class ElicitationForm:
    message: str
    fields: Tuple[FormField, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {"message": self.message, "fields": [f.to_dict() for f in self.fields]}


@dataclass(frozen=True)
class ElicitationAnswer:
    action: Action
    content: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Reading the server's schema
# ---------------------------------------------------------------------------


def _bounded_text(value: Any, what: str, limit: int = MAX_LABEL_CHARS) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise UnsupportedElicitation(f"{what} is not a string")
    if len(value) > limit:
        raise UnsupportedElicitation(f"{what} is longer than {limit} characters")
    return value


def _optional_int(value: Any, what: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UnsupportedElicitation(f"{what} is not a non-negative integer")
    return value


def _optional_number(value: Any, what: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise UnsupportedElicitation(f"{what} is not a finite number")
    return value


def _options_from_enum(values: Any, names: Any = None) -> Tuple[ChoiceOption, ...]:
    if not isinstance(values, list) or not values:
        raise UnsupportedElicitation("enum is empty")
    if names is not None and (not isinstance(names, list) or len(names) != len(values)):
        raise UnsupportedElicitation("enumNames does not match enum")
    options = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise UnsupportedElicitation("enum values must be strings")
        label = names[index] if names is not None else value
        options.append(
            ChoiceOption(
                value=_bounded_text(value, "an enum value") or "",
                label=_bounded_text(label, "an enum label") or value,
            )
        )
    return tuple(options)


def _options_from_consts(items: Any) -> Tuple[ChoiceOption, ...]:
    if not isinstance(items, list) or not items:
        raise UnsupportedElicitation("oneOf/anyOf is empty")
    options = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("const"), str):
            raise UnsupportedElicitation("a titled option has no string const")
        value = _bounded_text(item["const"], "an option value") or ""
        options.append(
            ChoiceOption(value=value, label=_bounded_text(item.get("title"), "an option title") or value)
        )
    return tuple(options)


def _check_options(options: Tuple[ChoiceOption, ...]) -> Tuple[ChoiceOption, ...]:
    if len(options) > MAX_OPTIONS:
        raise UnsupportedElicitation(f"more than {MAX_OPTIONS} options")
    if len({option.value for option in options}) != len(options):
        raise UnsupportedElicitation("duplicate option values")
    return options


def _field(name: str, prop: Mapping[str, Any], required: bool) -> FormField:
    kind_on_wire = prop.get("type")
    common = dict(
        name=name,
        title=_bounded_text(prop.get("title"), f"{name}'s title") or name,
        description=_bounded_text(prop.get("description"), f"{name}'s description"),
        required=required,
    )
    if kind_on_wire == "string" and ("enum" in prop or "oneOf" in prop):
        options = (
            _options_from_consts(prop["oneOf"])
            if "oneOf" in prop
            else _options_from_enum(prop["enum"], prop.get("enumNames"))
        )
        options = _check_options(options)
        default = prop.get("default")
        if default is not None and default not in {o.value for o in options}:
            raise UnsupportedElicitation(f"{name}'s default is not one of its options")
        return FormField(kind="choice", options=options, default=default, **common)

    if kind_on_wire == "string":
        fmt = prop.get("format")
        if fmt is not None and fmt not in _STRING_FORMATS:
            raise UnsupportedElicitation(f"{name} has an unsupported format")
        default = prop.get("default")
        if default is not None and not isinstance(default, str):
            raise UnsupportedElicitation(f"{name}'s default is not a string")
        return FormField(
            kind="text",
            format=fmt,
            min_length=_optional_int(prop.get("minLength"), f"{name}'s minLength"),
            max_length=_optional_int(prop.get("maxLength"), f"{name}'s maxLength"),
            default=_bounded_text(default, f"{name}'s default", MAX_TEXT_ANSWER_CHARS),
            **common,
        )

    if kind_on_wire in ("number", "integer"):
        return FormField(
            kind=kind_on_wire,
            minimum=_optional_number(prop.get("minimum"), f"{name}'s minimum"),
            maximum=_optional_number(prop.get("maximum"), f"{name}'s maximum"),
            default=_optional_number(prop.get("default"), f"{name}'s default"),
            **common,
        )

    if kind_on_wire == "boolean":
        default = prop.get("default")
        if default is not None and not isinstance(default, bool):
            raise UnsupportedElicitation(f"{name}'s default is not a boolean")
        return FormField(kind="boolean", default=default, **common)

    if kind_on_wire == "array":
        items = prop.get("items")
        if not isinstance(items, Mapping):
            raise UnsupportedElicitation(f"{name} is an array without items")
        if "anyOf" in items:
            options = _options_from_consts(items["anyOf"])
        elif items.get("type") == "string" and "enum" in items:
            options = _options_from_enum(items["enum"])
        else:
            raise UnsupportedElicitation(f"{name} is an array that is not a multi-select enum")
        options = _check_options(options)
        default = prop.get("default")
        if default is not None and (
            not isinstance(default, list)
            or not set(default) <= {o.value for o in options}
        ):
            raise UnsupportedElicitation(f"{name}'s default is not a subset of its options")
        return FormField(
            kind="choice",
            multi_select=True,
            options=options,
            min_items=_optional_int(prop.get("minItems"), f"{name}'s minItems"),
            max_items=_optional_int(prop.get("maxItems"), f"{name}'s maxItems"),
            default=default,
            **common,
        )

    raise UnsupportedElicitation(f"{name} has an unsupported type")


def normalize_form(message: Any, requested_schema: Any) -> ElicitationForm:
    """Read a form-mode request into the shape the SPA renders.

    Raises:
        UnsupportedElicitation: The schema is outside the spec's restricted
            subset or over this module's bounds.
    """
    text = _bounded_text(message, "the message", MAX_MESSAGE_CHARS)
    if not text:
        raise UnsupportedElicitation("the message is empty")
    if not isinstance(requested_schema, Mapping) or requested_schema.get("type") != "object":
        raise UnsupportedElicitation("the requested schema is not an object")
    properties = requested_schema.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        raise UnsupportedElicitation("the requested schema has no properties")
    if len(properties) > MAX_FIELDS:
        raise UnsupportedElicitation(f"more than {MAX_FIELDS} fields")
    required = requested_schema.get("required") or []
    if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
        raise UnsupportedElicitation("required is not a list of names")
    unknown_required = set(required) - set(properties)
    if unknown_required:
        raise UnsupportedElicitation("required names a field that does not exist")
    fields = []
    for name, prop in properties.items():
        if not isinstance(name, str) or not name or len(name) > MAX_KEY_CHARS:
            raise UnsupportedElicitation("a field name is empty or too long")
        if not isinstance(prop, Mapping):
            raise UnsupportedElicitation(f"{name} is not an object")
        fields.append(_field(name, prop, name in required))
    return ElicitationForm(message=text, fields=tuple(fields))


def schema_digest(requested_schema: Any) -> str:
    """A short, order-independent fingerprint of a requested schema."""
    canonical = json.dumps(requested_schema, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def interrupt_name(round_index: int, request_key: str, requested_schema: Any) -> str:
    """The interrupt name for one input request of one round.

    Folding the schema digest into the name is what makes a replay safe: when
    the server asks a different question in the same slot, the name differs, no
    stored answer matches, and the user is asked again rather than having an
    answer applied to a form they never saw. The message is left out on purpose,
    because a server may stamp an id or a time into it.
    """
    key = str(request_key)[:MAX_KEY_CHARS]
    return f"{INTERRUPT_NAME_PREFIX}:{round_index}:{key}:{schema_digest(requested_schema)}"


def build_reason(
    form: ElicitationForm,
    *,
    tool_name: str,
    server_name: Optional[str],
) -> Dict[str, Any]:
    """The interrupt reason, which the stream coordinator emits as the event."""
    return {
        "type": ELICITATION_EVENT_TYPE,
        "toolName": tool_name,
        "serverName": server_name or "",
        **form.to_dict(),
    }


# ---------------------------------------------------------------------------
# Checking the user's answer
# ---------------------------------------------------------------------------


def _check_value(spec: FormField, value: Any) -> Any:
    if spec.kind == "text":
        if not isinstance(value, str):
            raise InvalidElicitationAnswer(f"{spec.name} must be text")
        if len(value) > MAX_TEXT_ANSWER_CHARS:
            raise InvalidElicitationAnswer(f"{spec.name} is too long")
        if spec.min_length is not None and len(value) < spec.min_length:
            raise InvalidElicitationAnswer(f"{spec.name} is too short")
        if spec.max_length is not None and len(value) > spec.max_length:
            raise InvalidElicitationAnswer(f"{spec.name} is too long")
        return value
    if spec.kind in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise InvalidElicitationAnswer(f"{spec.name} must be a number")
        if spec.kind == "integer":
            if isinstance(value, float) and not value.is_integer():
                raise InvalidElicitationAnswer(f"{spec.name} must be a whole number")
            value = int(value)
        if spec.minimum is not None and value < spec.minimum:
            raise InvalidElicitationAnswer(f"{spec.name} is below its minimum")
        if spec.maximum is not None and value > spec.maximum:
            raise InvalidElicitationAnswer(f"{spec.name} is above its maximum")
        return value
    if spec.kind == "boolean":
        if not isinstance(value, bool):
            raise InvalidElicitationAnswer(f"{spec.name} must be true or false")
        return value
    allowed = {option.value for option in spec.options}
    if spec.multi_select:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise InvalidElicitationAnswer(f"{spec.name} must be a list of choices")
        if len(set(value)) != len(value) or not set(value) <= allowed:
            raise InvalidElicitationAnswer(f"{spec.name} has a choice that was not offered")
        if spec.min_items is not None and len(value) < spec.min_items:
            raise InvalidElicitationAnswer(f"{spec.name} needs more choices")
        if spec.max_items is not None and len(value) > spec.max_items:
            raise InvalidElicitationAnswer(f"{spec.name} has too many choices")
        return list(value)
    if not isinstance(value, str) or value not in allowed:
        raise InvalidElicitationAnswer(f"{spec.name} is not one of the choices offered")
    return value


def validate_answer(form: ElicitationForm, response: Any) -> ElicitationAnswer:
    """Check a resume response against the form it answers.

    The response is the object the SPA posts as the interrupt's response:
    ``{"action": "accept", "content": {...}}``, ``{"action": "decline"}`` or
    ``{"action": "cancel"}``. Content is checked only for ``accept``; a field
    the form did not ask for is refused, so nothing reaches the server beyond
    what it requested.

    Raises:
        InvalidElicitationAnswer: The response is malformed or does not fit.
    """
    if not isinstance(response, Mapping):
        raise InvalidElicitationAnswer("the answer is not an object")
    action = response.get("action")
    if action not in ACTIONS:
        raise InvalidElicitationAnswer("the answer has no valid action")
    if action != "accept":
        return ElicitationAnswer(action=action)
    content = response.get("content")
    if content is None:
        content = {}
    if not isinstance(content, Mapping):
        raise InvalidElicitationAnswer("the answer's content is not an object")
    by_name = {spec.name: spec for spec in form.fields}
    extra = set(content) - set(by_name)
    if extra:
        raise InvalidElicitationAnswer("the answer has a field the form did not ask for")
    checked: Dict[str, Any] = {}
    for spec in form.fields:
        value = content.get(spec.name)
        if value is None or (spec.kind == "text" and value == "" and spec.required):
            if spec.required:
                raise InvalidElicitationAnswer(f"{spec.name} is required")
            continue
        checked[spec.name] = _check_value(spec, value)
    return ElicitationAnswer(action="accept", content=checked)


#: The reason keys the event and the breadcrumb carry, in emission order.
_EVENT_KEYS = ("toolUseId", "toolName", "serverName", "message", "fields")


def event_payload(interrupt_id: str, reason: Mapping[str, Any]) -> Dict[str, Any]:
    """The ``mcp_elicitation_required`` payload for one pending form."""
    return {
        "type": ELICITATION_EVENT_TYPE,
        "interruptId": interrupt_id,
        **{key: reason.get(key) for key in _EVENT_KEYS},
    }


def event_sse(interrupt_id: str, reason: Mapping[str, Any]) -> str:
    """The SSE frame for one pending form."""
    return (
        f"event: {ELICITATION_EVENT_TYPE}\n"
        f"data: {json.dumps(event_payload(interrupt_id, reason))}\n\n"
    )


def unsupported_form_text(server_name: Optional[str]) -> str:
    """What the model is told when a form could not be shown."""
    who = server_name or "The MCP server"
    return (
        f"{who} asked the user for input in a form this app cannot show, so the "
        "request was cancelled. Nothing was submitted."
    )


__all__: List[str] = [
    "ACTIONS",
    "ELICITATION_EVENT_TYPE",
    "INTERRUPT_NAME_PREFIX",
    "ChoiceOption",
    "ElicitationAnswer",
    "ElicitationForm",
    "FormField",
    "InvalidElicitationAnswer",
    "UnsupportedElicitation",
    "build_reason",
    "event_payload",
    "event_sse",
    "interrupt_name",
    "normalize_form",
    "schema_digest",
    "unsupported_form_text",
    "validate_answer",
]
