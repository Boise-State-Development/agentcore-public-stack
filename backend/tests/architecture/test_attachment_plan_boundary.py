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
* **App-api admission asks the planner.** ``FileUploadService.max_size_for``
  calls ``upload_size_limit``; the service reads no limit constant and no class
  predicate except ``is_reference_document``, which decides *preparation* at
  upload, not admission.
* **The SPA mirrors no limit.** It reads ``GET /files/limits`` at load and keeps
  one built-in fallback object (``attachment-limits.ts``) that names none of the
  server's constants.

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
    "attachment_plan_enabled",
}

#: Modules allowed to read a delivery input, and why. None of them chooses a mode
#: on the turn path, and none restates an upload limit (§9.1 removal table,
#: phase 1: admission and the SPA read the planner's limits).
_KNOWN: Dict[str, str] = {
    _PLANNER: "the planner",
    "src/apis/shared/files/models.py": "defines the constants and the class predicates",
    "src/apis/shared/files/__init__.py": "re-exports the constants and predicates",
    "src/apis/shared/feature_flags.py": "defines the flag readers the legacy profile reads",
    # Admission reads the planner (upload_size_limit); this is preparation only.
    "src/apis/app_api/files/service.py": "which ready uploads to prepare (is_reference_document only; see _ONLY)",
    # Not a delivery decision: what a tool or a pipeline does with a file it already has.
    "src/apis/shared/files/document_read.py": "the document_read tool: which files it can read, its own batch cap",
    "src/apis/shared/files/attachment_index.py": "attachments-KB indexing eligibility (after upload, off the turn path)",
    "src/lambdas/large_document_prepare/handler.py": "preparation Lambda: which uploads to prepare (off the turn path)",
    "src/apis/shared/files/folder_manifest.py": "the folder manifest's wording about sheets and decks",
    "src/apis/shared/assistants/kb_tabular.py": "a project knowledge base's spreadsheet inventory",
    "src/apis/shared/files/attachment_preview.py": "which card preview to write (a display choice, written at upload off the turn path; never a delivery mode)",
    "src/agents/builtin_tools/spreadsheet_analysis/analyze_tool.py": "the spreadsheet tool finding its file",
    "src/agents/builtin_tools/spreadsheet_analysis/list_spreadsheets_tool.py": "the spreadsheet tool listing its files",
}

#: Modules on ``_KNOWN`` held to a narrower set of names than ``_DELIVERY_INPUTS``.
_ONLY: Dict[str, Set[str]] = {
    "src/apis/app_api/files/service.py": {"is_reference_document"},
}

#: Environment variables that set an upload limit. Read in the planner
#: (``upload_limits``) or in ``models.py``'s constants, nowhere else.
_LIMIT_ENV = {
    "FILE_UPLOAD_MAX_SIZE_BYTES",
    "FILE_UPLOAD_MAX_SIZE_BYTES_PRESENTATION",
    "FILE_UPLOAD_MAX_SIZE_BYTES_SPREADSHEET",
    "FILE_UPLOAD_MAX_FILES_PER_MESSAGE",
    "LARGE_DOCUMENT_MAX_BYTES",
    "INLINE_DOCUMENT_MAX_BYTES",
    "INLINE_ATTACHMENTS_MAX_TOTAL_BYTES",
}
_LIMIT_ENV_READERS = {
    _PLANNER,
    "src/apis/shared/files/models.py",
    "src/apis/shared/files/__init__.py",  # ``__all__`` names the constants
}

#: Modules on the turn path. They may not appear in ``_KNOWN``.
_TURN_PATH_PREFIXES = (
    "src/apis/inference_api/",
    "src/apis/shared/files/file_resolver.py",
)

#: SPA files that may mirror a server limit by name. None: the SPA reads
#: ``GET /files/limits`` at load (§9.1 removal table, phase 1).
_SPA_ROOT = _REPO / "frontend" / "ai.client" / "src"
_SPA_LIMIT_NAMES = re.compile(
    r"\b(MAX_FILES_PER_MESSAGE|INLINE_ATTACHMENTS_MAX_TOTAL_BYTES|INLINE_DOCUMENT_MAX_BYTES|MAX_FILE_SIZE_BYTES)\b"
)
_SPA_KNOWN: Set[str] = set()


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


def test_narrowed_modules_read_only_their_names():
    refs = _references()
    wider = {p: sorted(refs.get(p, set()) - allowed) for p, allowed in _ONLY.items() if refs.get(p, set()) - allowed}
    assert not wider, f"reads a limit or class predicate it should ask the planner for: {wider}"


def test_upload_limit_env_is_read_in_one_place():
    offenders = {}
    for path in _python_files():
        rel = _rel(path)
        if rel in _LIMIT_ENV_READERS:
            continue
        tree = _parse(path)
        if tree is None:
            continue
        found = sorted(
            node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and node.value in _LIMIT_ENV
        )
        if found:
            offenders[rel] = found
    assert not offenders, f"an upload limit is restated outside the planner: {offenders}"


def test_admission_asks_the_planner():
    tree = _parse(_BACKEND / "src/apis/app_api/files/service.py")
    assert tree is not None
    assert {"upload_size_limit", "upload_limits", "upload_download_ceiling"} <= _names_in_code(
        tree, {"upload_size_limit", "upload_limits", "upload_download_ceiling"}
    )


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
