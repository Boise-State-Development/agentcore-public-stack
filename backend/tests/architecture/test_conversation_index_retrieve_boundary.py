"""Only one function may query the shared ``conversations`` knowledge base.

Every user's archived turns are in that one knowledge base, separated only by a
``user_id`` filter (``docs/specs/conversation-search.md`` §4). The filter is
built in exactly one place, ``search_conversation_index`` in
``apis/shared/conversation_search/index_search.py``, from the caller's user id
and nothing else (§7, PR-4 isolation requirements 1 and 2). A second code path
that retrieved from the knowledge base would be a second place to get the
filter wrong, so this test keeps there from being one:

* **No retrieval names the knowledge base outside that module.** A call to
  ``search`` or ``retrieve`` (any receiver) that passes ``CONVERSATIONS_KB_ID``
  or the literal ``"conversations"`` fails, anywhere else in ``src/`` or
  ``scripts/``.
* **Every module that references the reserved id is known.** Reaching the
  knowledge base means resolving its AWS id from its KB_Record, which means
  naming the reserved id. The modules that do so today are listed below with
  why; a new one fails here until it is reviewed and added. The ones that are
  not the search module may not call ``retrieve`` or ``search`` at all.

What this cannot see: an agent route passing a URL's assistant id to the RAG
facade. That path is closed by the reserved-id guards (spec §4,
"Provisioning"): every route that takes an assistant id 404s for
``conversations``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

_BACKEND = Path(__file__).resolve().parent.parent.parent
_ROOTS = (_BACKEND / "src", _BACKEND / "scripts")

_SEARCH_MODULE = "src/apis/shared/conversation_search/index_search.py"

#: Modules allowed to reference the reserved id, and why. None but the search
#: module may retrieve.
_KNOWN_REFERENCES = {
    _SEARCH_MODULE: "the one search function",
    "src/apis/shared/kb_backend/reserved.py": "defines the id",
    "src/apis/shared/kb_backend/records.py": "refuses to tear the knowledge base down",
    "src/apis/app_api/conversation_index/consumer.py": "ingests and deletes documents (no retrieval)",
    "src/apis/app_api/conversation_index/reconciler.py": "lists and deletes documents (no retrieval)",
    "scripts/cleanup_orphaned_agent_rows.py": "does not count the partition as an orphan",
}

_RESERVED_NAMES = {"CONVERSATIONS_KB_ID", "RESERVED_KB_IDS", "is_reserved_kb_id"}
_RETRIEVAL_METHODS = {"search", "retrieve"}
_RESERVED_LITERAL = "conversations"


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


def _names_reserved_id(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id == "CONVERSATIONS_KB_ID":
        return True
    if isinstance(node, ast.Attribute) and node.attr == "CONVERSATIONS_KB_ID":
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


def _references_reserved_id(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(alias.name in _RESERVED_NAMES for alias in node.names):
            return True
        if isinstance(node, (ast.Name, ast.Attribute)) and getattr(node, "id", getattr(node, "attr", None)) in _RESERVED_NAMES:
            return True
    return False


def test_only_the_search_module_retrieves_with_the_conversations_kb_id():
    violations: List[Tuple[str, int]] = []
    for path in _python_files():
        rel = _rel(path)
        if rel == _SEARCH_MODULE:
            continue
        tree = _parse(path)
        if tree is None:
            continue
        for call in _retrieval_calls(tree):
            args = [*call.args, *(kw.value for kw in call.keywords)]
            if any(_names_reserved_id(arg) for arg in args):
                violations.append((rel, call.lineno))
    assert violations == [], (
        "only search_conversation_index may query the conversations knowledge base "
        f"(it builds the caller's user_id filter); found retrievals at {violations}"
    )


def test_the_search_module_does_retrieve_through_the_reserved_id():
    """Guards the guard: if the search module stopped naming the id, the test
    above would pass vacuously while some other path did the retrieving."""
    tree = _parse(_BACKEND / _SEARCH_MODULE)
    assert any(
        any(_names_reserved_id(arg) for arg in [*call.args, *(kw.value for kw in call.keywords)])
        for call in _retrieval_calls(tree)
    )


def test_every_module_naming_the_reserved_id_is_known_and_only_one_retrieves():
    referencing = set()
    retrieving_elsewhere = []
    for path in _python_files():
        rel = _rel(path)
        tree = _parse(path)
        if tree is None or not _references_reserved_id(tree):
            continue
        referencing.add(rel)
        if rel != _SEARCH_MODULE and any(True for _ in _retrieval_calls(tree)):
            retrieving_elsewhere.append(rel)

    unknown = sorted(referencing - set(_KNOWN_REFERENCES))
    assert unknown == [], (
        f"{unknown} reference the reserved conversations KB id. Review it: only "
        f"{_SEARCH_MODULE} may retrieve from that knowledge base. If it has another "
        "reason (ingest, delete, a teardown guard), add it to _KNOWN_REFERENCES with why."
    )
    assert retrieving_elsewhere == [], (
        f"{retrieving_elsewhere} reference the reserved conversations KB id and call "
        "search/retrieve; route conversation search through search_conversation_index"
    )
