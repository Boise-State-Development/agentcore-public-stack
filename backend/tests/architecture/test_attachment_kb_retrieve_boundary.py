"""Only one function may query the shared ``attachments`` knowledge base.

Every user's large attachments are in that one knowledge base, separated only by
``user_id`` and ``session_id`` filters (``docs/specs/large-document-attachments.md``
§4E, isolation layer 2). The filter is built in exactly one place,
``search_attachments`` in ``apis/shared/files/attachment_search.py``, from the
caller's identity and nothing else. A second code path that retrieved from the
knowledge base would be a second place to get the filter wrong, so this test keeps
there from being one. Same shape as ``test_conversation_index_retrieve_boundary.py``:

* **No retrieval names the knowledge base outside that module.** A call to
  ``search`` or ``retrieve`` (any receiver) that passes ``ATTACHMENTS_KB_ID``, the
  pipeline module's ``KB_ID`` or the literal ``"attachments"`` fails, anywhere else
  in ``src/`` or ``scripts/``.
* **Every module that references the id is known.** Reaching the knowledge base
  means naming it. The modules that do so today are listed below with why; a new
  one fails here until it is reviewed and added. The ones that are not the search
  module may not call ``retrieve`` or ``search`` at all.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

_BACKEND = Path(__file__).resolve().parent.parent.parent
_ROOTS = (_BACKEND / "src", _BACKEND / "scripts")

_SEARCH_MODULE = "src/apis/shared/files/attachment_search.py"
_PIPELINE_MODULE = "src/apis/shared/files/attachment_index.py"

#: Modules allowed to reference the id, and why. None but the search module may retrieve.
_KNOWN_REFERENCES = {
    _SEARCH_MODULE: "the one search function",
    _PIPELINE_MODULE: "ingests, polls, lists and deletes documents (no retrieval); defines KB_ID",
    "src/apis/shared/kb_backend/reserved.py": "defines the id",
}

_RESERVED_NAMES = {"ATTACHMENTS_KB_ID"}
_RETRIEVAL_METHODS = {"search", "retrieve"}
_RESERVED_LITERAL = "attachments"


def _python_files() -> Iterator[Path]:
    for root in _ROOTS:
        yield from sorted(root.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(_BACKEND).as_posix()


def _parse(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return None


def _imports_pipeline_kb_id(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and (node.module or "").endswith("attachment_index")
        and any(alias.name == "KB_ID" for alias in node.names)
        for node in ast.walk(tree)
    )


def _names_reserved_id(node: ast.AST, *, kb_id_is_reserved: bool) -> bool:
    if isinstance(node, ast.Name) and (node.id in _RESERVED_NAMES or (kb_id_is_reserved and node.id == "KB_ID")):
        return True
    if isinstance(node, ast.Attribute) and node.attr in _RESERVED_NAMES:
        return True
    return isinstance(node, ast.Constant) and node.value == _RESERVED_LITERAL


def _retrieval_calls(tree: ast.AST) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if name in _RETRIEVAL_METHODS:
            yield node


def _call_args(call: ast.Call) -> List[ast.AST]:
    return [*call.args, *(kw.value for kw in call.keywords)]


def _references_reserved_id(tree: ast.AST) -> bool:
    if _imports_pipeline_kb_id(tree):
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(alias.name in _RESERVED_NAMES for alias in node.names):
            return True
        if isinstance(node, (ast.Name, ast.Attribute)) and getattr(node, "id", getattr(node, "attr", None)) in _RESERVED_NAMES:
            return True
    return False


def test_only_the_search_module_retrieves_with_the_attachments_kb_id():
    violations: List[Tuple[str, int]] = []
    for path in _python_files():
        rel = _rel(path)
        if rel == _SEARCH_MODULE:
            continue
        tree = _parse(path)
        if tree is None:
            continue
        kb_id_is_reserved = rel == _PIPELINE_MODULE or _imports_pipeline_kb_id(tree)
        for call in _retrieval_calls(tree):
            if any(_names_reserved_id(arg, kb_id_is_reserved=kb_id_is_reserved) for arg in _call_args(call)):
                violations.append((rel, call.lineno))
    assert violations == [], (
        "only search_attachments may query the attachments knowledge base (it builds "
        f"the caller's user_id + session_id filter); found retrievals at {violations}"
    )


def test_the_search_module_does_retrieve_through_the_reserved_id():
    """Guards the guard: if the search module stopped naming the id, the test
    above would pass vacuously while some other path did the retrieving."""
    tree = _parse(_BACKEND / _SEARCH_MODULE)
    assert any(
        any(_names_reserved_id(arg, kb_id_is_reserved=False) for arg in _call_args(call))
        for call in _retrieval_calls(tree)
    )


def test_every_module_naming_the_id_is_known_and_only_one_retrieves():
    referencing = set()
    retrieving_elsewhere = []
    for path in _python_files():
        rel = _rel(path)
        tree = _parse(path)
        if tree is None:
            continue
        if not (rel == _PIPELINE_MODULE or _references_reserved_id(tree)):
            continue
        referencing.add(rel)
        if rel != _SEARCH_MODULE and any(True for _ in _retrieval_calls(tree)):
            retrieving_elsewhere.append(rel)

    unknown = sorted(referencing - set(_KNOWN_REFERENCES))
    assert unknown == [], (
        f"{unknown} reference the attachments KB id. Review it: only {_SEARCH_MODULE} "
        "may retrieve from that knowledge base. If it has another reason (ingest, "
        "delete, a teardown guard), add it to _KNOWN_REFERENCES with why."
    )
    assert retrieving_elsewhere == [], (
        f"{retrieving_elsewhere} reference the attachments KB id and call search/retrieve; "
        "route attachment search through search_attachments"
    )
