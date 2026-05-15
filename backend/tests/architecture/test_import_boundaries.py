"""Architectural boundary enforcement tests.

These tests use AST-based import analysis to ensure that service boundaries
are respected. If any of these fail, it means someone introduced a
cross-service import that violates the decoupling contract.

Rules enforced:
1. inference_api must NEVER import from app_api
2. agents/ must NEVER import from app_api (use apis.shared instead)
3. apis.shared must NEVER import from app_api or inference_api
4. app_api may import from apis.shared (that's the point of shared)
5. app_api may NOT import from inference_api (except main.py wiring)
"""

import ast
import os
from pathlib import Path
from typing import List, Set, Tuple

import pytest

# Root of the backend source tree
_BACKEND_SRC = Path(__file__).resolve().parent.parent.parent / "src"


def _collect_python_files(directory: Path) -> List[Path]:
    """Recursively collect all .py files under a directory."""
    return sorted(directory.rglob("*.py"))


def _extract_imports(filepath: Path) -> List[Tuple[str, int]]:
    """Parse a Python file and return all imported module paths with line numbers.

    Returns a list of (module_path, line_number) tuples.
    Handles both `import X` and `from X import Y` forms,
    including lazy imports inside function bodies.
    """
    try:
        source = filepath.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError):
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append((node.module, node.lineno))
    return imports


def _find_violations(
    source_dir: Path,
    forbidden_prefixes: Set[str],
    label: str,
) -> List[str]:
    """Scan all .py files under source_dir for imports matching forbidden_prefixes.

    Returns a list of human-readable violation strings.
    """
    violations = []
    for pyfile in _collect_python_files(source_dir):
        rel = pyfile.relative_to(_BACKEND_SRC)
        for module, lineno in _extract_imports(pyfile):
            for prefix in forbidden_prefixes:
                if module == prefix or module.startswith(prefix + "."):
                    violations.append(
                        f"  {rel}:{lineno} imports '{module}' "
                        f"(violates: {label})"
                    )
    return violations


# ── Test cases ────────────────────────────────────────────────────────────────


class TestInferenceApiIsolation:
    """inference_api must not import from app_api."""

    def test_no_app_api_imports(self):
        source = _BACKEND_SRC / "apis" / "inference_api"
        if not source.exists():
            pytest.skip("inference_api directory not found")

        violations = _find_violations(
            source,
            {"apis.app_api"},
            "inference_api → app_api",
        )
        assert violations == [], (
            "inference_api has forbidden imports from app_api:\n"
            + "\n".join(violations)
        )


class TestAgentsIsolation:
    """The agents module must not import from app_api.

    Agents are shared infrastructure used by both services.
    They should only depend on apis.shared, not on either service.
    """

    def test_no_app_api_imports(self):
        source = _BACKEND_SRC / "agents"
        if not source.exists():
            pytest.skip("agents directory not found")

        violations = _find_violations(
            source,
            {"apis.app_api"},
            "agents → app_api",
        )
        assert violations == [], (
            "agents/ has forbidden imports from app_api:\n"
            + "\n".join(violations)
        )

    def test_no_inference_api_imports(self):
        source = _BACKEND_SRC / "agents"
        if not source.exists():
            pytest.skip("agents directory not found")

        violations = _find_violations(
            source,
            {"apis.inference_api"},
            "agents → inference_api",
        )
        assert violations == [], (
            "agents/ has forbidden imports from inference_api:\n"
            + "\n".join(violations)
        )


class TestSharedIsolation:
    """apis.shared must not import from app_api or inference_api.

    Shared is the lowest layer — it cannot depend on either service.
    """

    def test_no_app_api_imports(self):
        source = _BACKEND_SRC / "apis" / "shared"
        if not source.exists():
            pytest.skip("apis/shared directory not found")

        violations = _find_violations(
            source,
            {"apis.app_api"},
            "shared → app_api",
        )
        assert violations == [], (
            "apis.shared has forbidden imports from app_api:\n"
            + "\n".join(violations)
        )

    def test_no_inference_api_imports(self):
        source = _BACKEND_SRC / "apis" / "shared"
        if not source.exists():
            pytest.skip("apis/shared directory not found")

        violations = _find_violations(
            source,
            {"apis.inference_api"},
            "shared → inference_api",
        )
        assert violations == [], (
            "apis.shared has forbidden imports from inference_api:\n"
            + "\n".join(violations)
        )


class TestAppApiDoesNotImportInferenceApi:
    """app_api must not import from inference_api.

    The one exception is the assistants route that calls inference_api
    for test-chat functionality — this is a known coupling that should
    eventually be refactored into a shared service.
    """

    # Files that are allowed to import from inference_api (known exceptions)
    _ALLOWED_FILES = {
        # Assistants test-chat calls the inference streaming endpoint directly
        Path("apis/app_api/assistants/routes.py"),
        # Chat routes proxy to inference_api (BFF pattern) — tracked in issue #106
        Path("apis/app_api/chat/routes.py"),
    }

    def test_no_inference_api_imports_except_allowed(self):
        source = _BACKEND_SRC / "apis" / "app_api"
        if not source.exists():
            pytest.skip("apis/app_api directory not found")

        violations = []
        for pyfile in _collect_python_files(source):
            rel = pyfile.relative_to(_BACKEND_SRC)
            if rel in self._ALLOWED_FILES:
                continue
            for module, lineno in _extract_imports(pyfile):
                if module == "apis.inference_api" or module.startswith("apis.inference_api."):
                    violations.append(
                        f"  {rel}:{lineno} imports '{module}'"
                    )

        assert violations == [], (
            "app_api has forbidden imports from inference_api:\n"
            + "\n".join(violations)
            + "\n\nIf this is intentional, add the file to _ALLOWED_FILES "
            "with a comment explaining why."
        )
