"""One place decides how an attachment reaches the model.

``docs/specs/unified-attachment-delivery.md`` §9.1 rule 3: from phase 1 the
planner (``apis/shared/files/attachment_plan.py``) is the only selector. A second
place that chooses a delivery mode, or reads a limit constant to choose one, is a
second system to keep in step, which is how the turn path, app-api admission and
the SPA came to disagree about the same file (§1.2, §1.3). Same shape as
``test_attachment_kb_retrieve_boundary.py``:

* **The route's old selectors stay gone.** ``_apply_message_file_cap``,
  ``_apply_inline_byte_budget``, ``_partition_attachments`` and
  ``_over_budget_as_references`` are neither defined nor referenced in ``src/``.
* **Every module that reads a delivery input is known.** Choosing a mode means
  reading a limit constant (``INLINE_DOCUMENT_MAX_BYTES``, ``MAX_FILES_PER_MESSAGE``
  …), a class predicate (``is_reference_document``, ``is_tabular_file``,
  ``is_presentation_file``) or a flag the ``legacy`` profile takes as an input.
  The modules that do so today are listed below with why; a new one fails here
  until it is reviewed and added. **No turn-path module is on the list**: the chat
  route and the file resolver ask the planner.
* **The SPA's mirrored limits are known too**, for the same reason; the next wave
  replaces them with ``GET /files/limits``.

Docstrings and comments do not count: only code (names, attributes, imports).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, Iterator, Set

_BACKEND = Path(__file__).resolve().parent.parent.parent
_REPO = _BACKEND.parent
_ROOTS = (_BACKEND / "src", _BACKEND / "scripts")

_PLANNER = "src/apis/shared/files/attachment_plan.py"

#: The selectors the planner replaced (§9.1 removal table, phase 1).
_REMOVED = {
    "_apply_message_file_cap",
    "_apply_inline_byte_budget",
    "_partition_attachments",
    "_over_budget_as_references",
}

#: What choosing a delivery mode reads.
_DELIVERY_INPUTS = {
    "INLINE_DOCUMENT_MAX_BYTES",
    "INLINE_ATTACHMENTS_MAX_TOTAL_BYTES",
    "MAX_FILES_PER_MESSAGE",
    "TEXT_ONLY_DOCUMENTS_MAX_CHARS",
    "LARGE_DOCUMENT_MAX_BYTES",
    "is_reference_document",
    "is_tabular_file",
    "is_presentation_file",
    "attachment_turn_guard_enabled",
    "inline_budget_by_reference_enabled",
    "converse_documents_as_text_enabled",
}

#: Modules allowed to read a delivery input, and why. None of them chooses a mode
#: on the turn path. Entries marked "next wave" are the app-api admission rules
#: that restate the turn's; they move to the planner's limits with
#: ``GET /files/limits`` (§9.1 removal table, phase 1, second and fourth rows).
_KNOWN: Dict[str, str] = {
    _PLANNER: "the planner",
    "src/apis/shared/files/models.py": "defines the constants and the class predicates",
    "src/apis/shared/files/__init__.py": "re-exports the constants and predicates",
    "src/apis/shared/feature_flags.py": "defines the flag readers the legacy profile reads",
    # Next wave: app-api admission (max_size_for, the count check, preparation at upload).
    "src/apis/app_api/files/service.py": "next wave: upload admission and preparation (reads the planner's limits after GET /files/limits)",
    # Not a delivery decision: what a tool or a pipeline does with a file it already has.
    "src/apis/shared/files/document_read.py": "the document_read tool: which files it can read, its own batch cap",
    "src/apis/shared/files/attachment_index.py": "attachments-KB indexing eligibility (after upload, off the turn path)",
    "src/lambdas/large_document_prepare/handler.py": "preparation Lambda: which uploads to prepare (off the turn path)",
    "src/apis/shared/files/folder_manifest.py": "the folder manifest's wording about sheets and decks",
    "src/apis/shared/files/inline_persist.py": "the MIME to store an inline sheet or deck under",
    "src/apis/shared/assistants/kb_tabular.py": "a project knowledge base's spreadsheet inventory",
    "src/agents/builtin_tools/spreadsheet_analysis/analyze_tool.py": "the spreadsheet tool finding its file",
    "src/agents/builtin_tools/spreadsheet_analysis/list_spreadsheets_tool.py": "the spreadsheet tool listing its files",
    # Phase 6 (§9.1): the per-call projection's own selection retires with the flag.
    "src/agents/main_agent/core/agent_factory.py": "phase 6: installs the document-cap projection in its as-text mode",
}

#: Modules on the turn path. They may not appear in ``_KNOWN``.
_TURN_PATH_PREFIXES = (
    "src/apis/inference_api/",
    "src/apis/shared/files/file_resolver.py",
)

#: SPA files that mirror a server limit today. Next wave: read ``GET /files/limits``
#: at load, keeping one built-in fallback object (§9.1 removal table, phase 1).
_SPA_ROOT = _REPO / "frontend" / "ai.client" / "src"
_SPA_LIMIT_NAMES = re.compile(
    r"\b(MAX_FILES_PER_MESSAGE|INLINE_ATTACHMENTS_MAX_TOTAL_BYTES|INLINE_DOCUMENT_MAX_BYTES|MAX_FILE_SIZE_BYTES)\b"
)
_SPA_KNOWN = {
    "app/services/file-upload/file-upload.service.ts",
    "app/services/file-upload/inline-attachment-budget.ts",
    "app/session/components/chat-input/composer-add-menu.component.ts",
    "app/session/components/chat-input/chat-input.component.ts",
    "app/session/components/chat-input/inline-budget-notice.component.ts",
}


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


def _names_in_code(tree: ast.AST, wanted: Set[str]) -> Set[str]:
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in wanted:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in wanted:
            found.add(node.attr)
        elif isinstance(node, ast.alias) and (node.name in wanted or (node.asname or "") in wanted):
            found.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            found.add(node.name)
        elif isinstance(node, ast.Constant) and node.value in wanted:
            # getattr(module, "is_tabular_file") and monkeypatch-by-name.
            found.add(node.value)
    return found


def _references() -> Dict[str, Set[str]]:
    out: Dict[str, Set[str]] = {}
    for path in _python_files():
        tree = _parse(path)
        if tree is None:
            continue
        found = _names_in_code(tree, _DELIVERY_INPUTS)
        if found:
            out[_rel(path)] = found
    return out


def test_the_old_selectors_are_gone():
    offenders = {}
    for path in _python_files():
        tree = _parse(path)
        if tree is None:
            continue
        found = _names_in_code(tree, _REMOVED)
        if found:
            offenders[_rel(path)] = sorted(found)
    assert not offenders, f"a removed attachment selector came back: {offenders}"


def test_every_module_reading_a_delivery_input_is_known():
    unknown = {path: sorted(names) for path, names in _references().items() if path not in _KNOWN}
    assert not unknown, (
        "These modules read a limit constant, a class predicate or a selector flag. "
        "Ask the planner (apis/shared/files/attachment_plan.py) instead, or add the module "
        f"to _KNOWN with why it is not choosing a delivery mode: {unknown}"
    )


def test_no_turn_path_module_is_allowed_to():
    assert not [p for p in _KNOWN if p.startswith(_TURN_PATH_PREFIXES)]
    on_turn_path = {p: sorted(n) for p, n in _references().items() if p.startswith(_TURN_PATH_PREFIXES)}
    assert not on_turn_path, f"the turn path chooses a delivery mode outside the planner: {on_turn_path}"


def test_the_known_list_has_no_stale_entries():
    referencing = set(_references())
    stale = sorted(p for p in _KNOWN if p not in referencing)
    assert not stale, f"no longer reads a delivery input; remove from _KNOWN: {stale}"


def test_the_route_asks_the_planner():
    tree = _parse(_BACKEND / "src/apis/inference_api/chat/routes.py")
    assert tree is not None
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert {"plan_attachments", "cap_message_attachments", "refine_for_model"} <= called


def test_the_resolver_asks_the_planner():
    tree = _parse(_BACKEND / "src/apis/shared/files/file_resolver.py")
    assert tree is not None
    assert "delivers_by_reference" in _names_in_code(tree, {"delivers_by_reference"})


def test_every_spa_file_mirroring_a_limit_is_known():
    if not _SPA_ROOT.is_dir():  # backend-only checkout
        return
    mirroring = {
        path.relative_to(_SPA_ROOT).as_posix()
        for path in _SPA_ROOT.rglob("*.ts")
        if not path.name.endswith(".spec.ts") and _SPA_LIMIT_NAMES.search(path.read_text(encoding="utf-8"))
    }
    unknown = sorted(mirroring - _SPA_KNOWN)
    assert not unknown, f"a new SPA file mirrors a server attachment limit; read GET /files/limits instead: {unknown}"
