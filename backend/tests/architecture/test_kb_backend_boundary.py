"""Import-boundary enforcement for ``apis.shared.kb_backend``.

``apis/shared/assistants/__init__.py`` imports ``rag_service``, which imports the
embeddings stack at module scope. So importing anything from the assistants
package pulls in that whole tree — and ``kb_backend`` is bundled into
size-constrained Lambda images (the migration dispatcher, worker, reconciler and
ingestion consumer) that deliberately do not carry it. The same constraint is why
``apis/app_api/kb_sync/records.py`` reaches DynamoDB through the raw table
resource instead of the assistants package.

The dependency is also the wrong way round architecturally: the facade in
``rag_service`` sits *above* the seam and depends on ``kb_backend``. An import in
the other direction would make the two mutually dependent and the seam
meaningless.

This is checked two ways, because either alone is insufficient:

* **Statically**, so a *lazy* import inside a function body is caught. A deferred
  import does not fail at module load; it fails at call time, in production, in a
  Lambda that has been running fine for a week.
* **At runtime in a fresh interpreter**, so a transitive import through some
  innocuous-looking third module is caught too. Static analysis cannot see
  through an import chain; a subprocess with an empty ``sys.modules`` can.

Feature: managed-kb-migration
Requirements: 24.15
"""

import ast
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND_SRC = _BACKEND_ROOT / "src"
_KB_BACKEND = _BACKEND_SRC / "apis" / "shared" / "kb_backend"

#: Modules whose absence from a fresh import is asserted. ``boto3`` is here
#: because it is the single largest dependency these Lambdas would otherwise
#: pay for, and keeping it function-local is the convention this package follows.
_FORBIDDEN_AT_IMPORT_TIME = ("apis.shared.assistants", "boto3")


def _extract_imports(filepath: Path) -> List[Tuple[str, int]]:
    """Every imported module path in a file, including imports inside functions."""
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"), filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError):
        return []

    imports: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.module, node.lineno))
    return imports


def _kb_backend_files() -> List[Path]:
    return sorted(_KB_BACKEND.rglob("*.py"))


class TestKbBackendDoesNotImportAssistants:
    """No file in kb_backend may import apis.shared.assistants, at any depth."""

    def test_no_assistants_imports_anywhere(self):
        if not _KB_BACKEND.exists():
            pytest.skip("kb_backend package not found")

        violations = []
        for pyfile in _kb_backend_files():
            rel = pyfile.relative_to(_BACKEND_SRC)
            for module, lineno in _extract_imports(pyfile):
                if module == "apis.shared.assistants" or module.startswith("apis.shared.assistants."):
                    violations.append(f"  {rel}:{lineno} imports '{module}'")

        assert violations == [], (
            "apis.shared.kb_backend must not import apis.shared.assistants "
            "(its __init__ pulls in rag_service and the whole embeddings stack, "
            "which the migration Lambda images do not carry):\n"
            + "\n".join(violations)
            + "\n\nNote that a lazy, function-local import does not fix this — it "
            "moves the failure from image build to production call time."
        )

    def test_package_init_stays_empty(self):
        """An empty ``__init__`` is what makes importing one submodule cheap.

        Re-exporting anything here would mean importing ``kb_backend.records``
        also imports every sibling — including, eventually, the managed backend
        and its boto3 client.
        """
        init = _KB_BACKEND / "__init__.py"
        assert init.exists(), "kb_backend/__init__.py must exist"
        assert init.read_text(encoding="utf-8").strip() == "", (
            "kb_backend/__init__.py must stay empty: it is imported by every "
            "submodule import, so anything placed here is paid for by all of them"
        )


class TestKbBackendFreshImportIsLean:
    """Importing a kb_backend submodule must not pull the heavy tree in.

    Each case runs in a fresh interpreter, because by the time this test file
    executes, the rest of the suite has already imported both forbidden modules
    into ``sys.modules`` — an in-process check would pass no matter what.
    """

    @staticmethod
    def _import_and_report(module: str) -> List[str]:
        """Import *module* in a subprocess; return which forbidden modules loaded."""
        program = (
            "import sys\n"
            f"import {module}\n"
            "loaded = [name for name in sys.modules\n"
            f"          if any(name == f or name.startswith(f + '.') for f in {_FORBIDDEN_AT_IMPORT_TIME!r})]\n"
            "print(','.join(sorted(set(loaded))))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            cwd=str(_BACKEND_ROOT),
            env={"PYTHONPATH": str(_BACKEND_SRC), "PATH": "/usr/bin:/bin"},
        )
        assert result.returncode == 0, (
            f"importing {module} in a clean interpreter failed:\n{result.stderr}"
        )
        return [name for name in result.stdout.strip().split(",") if name]

    def test_records_import_is_stdlib_only(self):
        """The constraint as written in task 4.8: records pulls in neither."""
        loaded = self._import_and_report("apis.shared.kb_backend.records")
        assert loaded == [], (
            "importing apis.shared.kb_backend.records loaded "
            f"{loaded}. Module-level imports in this package must be stdlib "
            "only; move boto3 and anything from apis.shared.assistants into the "
            "functions that need them."
        )

    @pytest.mark.parametrize(
        "module",
        [
            "apis.shared.kb_backend.protocol",
            "apis.shared.kb_backend.resolver",
            "apis.shared.kb_backend.s3vectors_backend",
            "apis.shared.kb_backend.managed_backend",
            "apis.shared.kb_backend.dual_read",
        ],
    )
    def test_seam_modules_import_lean(self, module):
        """The resolver and both adapters obey the same rule as records.

        The resolver is the one that matters most: the facade imports it on every
        retrieval, and it in turn imports every registered backend. If it were
        not lean, no submodule of this package could be.

        ``managed_backend`` is on this list because the resolver **registers** it at
        import (see the resolver's docstring). That registration is only free while
        the adapter's module body stays stdlib-only and its clients stay lazy; the
        day someone hoists a ``boto3.client(...)`` to module scope, every Lambda
        image carrying any part of this package pays for it.
        """
        loaded = self._import_and_report(module)
        assert loaded == [], (
            f"importing {module} loaded {loaded}; keep these imports "
            "function-local"
        )


class TestFacadeDependencyDirectionIsOneWay:
    """rag_service depends on kb_backend, never the reverse."""

    def test_facade_imports_the_seam(self):
        """A guard against the facade quietly regrowing its own retrieval path.

        If ``rag_service`` stopped importing the resolver, it would mean the
        delegation had been inlined again and the managed backend would be
        unreachable — with every legacy test still green.
        """
        rag_service = _BACKEND_SRC / "apis" / "shared" / "assistants" / "rag_service.py"
        modules = {module for module, _ in _extract_imports(rag_service)}
        assert "apis.shared.kb_backend.resolver" in modules, (
            "rag_service must resolve its backend through "
            "apis.shared.kb_backend.resolver"
        )
        assert "apis.shared.kb_backend.protocol" in modules, (
            "rag_service must use the protocol's canonical chunk shape"
        )
