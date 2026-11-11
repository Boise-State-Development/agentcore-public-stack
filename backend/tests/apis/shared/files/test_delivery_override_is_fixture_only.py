"""The file row's ``delivery_override`` is eval surface, never user surface.

``docs/specs/unified-attachment-delivery.md`` §8.2: a fixture-only value on the
file row forces the planner's mode, so an eval can hold a question fixed and vary
only how the file is delivered. "Only a direct row write can set it; no request
field, no user surface." This pins that:

* the row round-trips the value, and reads anything else as unset;
* no request model on either API has a field that could carry it;
* only the modules listed below name it in code: the row model, the planner, and
  the chat route that hands the row's value to the planner. No app-api module
  does, so no route, service method or import path can write it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from apis.shared.files.models import DELIVERY_OVERRIDE_VALUES, FileMetadata, FileStatus

_BACKEND = Path(__file__).resolve().parents[4]
_SRC = _BACKEND / "src"

_NAMES = {"delivery_override", "deliveryOverride"}

#: The only modules that may name the field in code.
_KNOWN = {
    "src/apis/shared/files/models.py": "defines and round-trips it",
    "src/apis/shared/files/attachment_plan.py": "reads it as a planner input",
    "src/apis/inference_api/chat/routes.py": "hands the row's value to the planner (read only)",
    "src/apis/shared/evals/fixtures.py": "the eval registrar: its direct row write is the one writer",
}


def _row(**kw) -> FileMetadata:
    return FileMetadata(
        upload_id="up-1",
        user_id="user-1",
        session_id="sess-1",
        filename="policy.pdf",
        mime_type="application/pdf",
        size_bytes=1_000,
        s3_key="k",
        s3_bucket="b",
        status=FileStatus.READY,
        **kw,
    )


@pytest.mark.parametrize("value", sorted(DELIVERY_OVERRIDE_VALUES))
def test_the_row_round_trips_a_forced_mode(value):
    item = _row(delivery_override=value).to_dynamo_item()
    assert item["deliveryOverride"] == value
    assert FileMetadata.from_dynamo_item(item).delivery_override == value


def test_a_user_row_carries_no_override():
    item = _row().to_dynamo_item()
    assert "deliveryOverride" not in item
    assert FileMetadata.from_dynamo_item(item).delivery_override is None


@pytest.mark.parametrize("value", ["tool", "dropped", "SEARCH", "NATIVE", "", 1, None])
def test_anything_else_reads_as_unset(value):
    item = _row().to_dynamo_item()
    item["deliveryOverride"] = value
    assert FileMetadata.from_dynamo_item(item).delivery_override is None


def test_the_override_is_separate_from_the_folder_delivery():
    """``delivery`` is set for real users at folder import; the override is not it."""
    item = _row(delivery="reference").to_dynamo_item()
    assert item["delivery"] == "reference"
    assert "deliveryOverride" not in item


def test_no_request_model_can_carry_it():
    from apis.inference_api.chat.models import FileContent, InvocationRequest

    for model in (InvocationRequest, FileContent):
        fields = set(model.model_fields)
        aliases = {f.alias for f in model.model_fields.values() if f.alias}
        assert not (fields | aliases) & _NAMES, model.__name__


def _modules_naming_it() -> dict:
    out = {}
    for path in sorted(_SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            hit = (
                (isinstance(node, ast.Name) and node.id in _NAMES)
                or (isinstance(node, ast.Attribute) and node.attr in _NAMES)
                or (isinstance(node, ast.Constant) and node.value in _NAMES)
                or (isinstance(node, ast.keyword) and node.arg in _NAMES)
                or (isinstance(node, ast.arg) and node.arg in _NAMES)
                or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in _NAMES)
            )
            if hit:
                out[path.relative_to(_BACKEND).as_posix()] = True
                break
    return out


def test_only_the_known_modules_name_it():
    unknown = sorted(set(_modules_naming_it()) - set(_KNOWN))
    assert not unknown, (
        "delivery_override is fixture-only: no request field, route or service may set "
        f"it (unified-attachment-delivery §8.2). Named in: {unknown}"
    )


def test_no_app_api_module_names_it():
    assert not [p for p in _modules_naming_it() if p.startswith("src/apis/app_api/")]


def test_the_chat_route_only_reads_it():
    """The route reads the row's attribute; it never assigns one."""
    tree = ast.parse((_SRC / "apis/inference_api/chat/routes.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                assert not (isinstance(target, ast.Attribute) and target.attr in _NAMES)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in {"model_copy", "update_file"}:
            for kw in node.keywords:
                assert kw.arg not in _NAMES
