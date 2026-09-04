"""Static policy for Python source executed in the diagram/analysis sandbox.

The diagram and spreadsheet-analysis tools accept user-shaped Python code
and forward it to AWS Bedrock Code Interpreter. The sandbox is isolated
(no AWS credentials, no outbound network), but it is still arbitrary code
execution from the application's perspective: nothing about the system
prevents a user from asking it to ``import subprocess`` or read files in
``/etc/``.

This module provides :func:`validate_diagram_code`, a pre-execution gate
that parses the source with :mod:`ast` and rejects programs that reach
outside the legitimate plotting / data-analysis surface area. The check is
prompt-independent: it runs after the LLM emits the tool call but before
the sandbox is invoked, so a user-controlled ``system_prompt`` cannot
disable it.

The policy is an allowlist of imports plus a denylist of escape hatches.
Both layers must pass for code to be accepted. Violations raise
:class:`PolicyError` with a generic message; the structural reason is
emitted via the module logger for operator visibility.

The denylist applies to **attribute names as well as bare names**. An
allowlisted module can re-export a forbidden module as one of its own
attributes — ``pandas.io.common`` does ``import os``, so
``pd.io.common.os.popen('id')`` reaches the full :mod:`os` module with no
``import`` statement anywhere in the submitted source. Checking
``Import``/``ImportFrom`` nodes and bare ``Name`` nodes alone therefore does
not bound what the source can reach; every ``Attribute`` node is checked
against the same denylist. The cost is that a data column whose name
collides with a denied name must be reached by subscript
(``df['open']`` rather than ``df.open``) and a lazily-loaded submodule by
direct import (``from scipy.signal import butter`` rather than
``sp.signal.butter``) — both already true for the bare-name form.

Out of scope, deliberately: reading and writing arbitrary paths inside the
sandbox. ``pandas.read_csv`` / ``DataFrame.to_csv`` take a path by design,
so this gate does not pretend to be a filesystem boundary. What it bounds is
escape from the interpreter into the host process — command execution,
module re-export, and deserialization sinks that run constructor code.
"""

from __future__ import annotations

import ast
import logging

logger = logging.getLogger(__name__)


# Top-level packages legitimate plotting / data-analysis code needs.
# Submodules of these packages (e.g. ``matplotlib.pyplot``,
# ``pandas.api.types``) are allowed because the check matches by package
# root.
_ALLOWED_IMPORT_ROOTS: frozenset[str] = frozenset(
    {
        "matplotlib",
        "numpy",
        "pandas",
        "seaborn",
        "scipy",
        "math",
        "statistics",
        "json",
        "datetime",
        "random",
        "collections",
        "itertools",
        "functools",
        "re",
        "decimal",
        "fractions",
        "csv",
        "io",
    }
)

# Modules that hand out host-process capability. Denied as imports, as bare
# names, and as attributes — an allowlisted package that imports one of these
# would otherwise re-export it (see module docstring).
_HOST_MODULES: frozenset[str] = frozenset(
    {
        "subprocess",
        "os",
        "posix",
        "nt",
        "sys",
        "socket",
        "shutil",
        "pickle",
        "marshal",
        "ctypes",
        "importlib",
        "pathlib",
        "tempfile",
        "urllib",
        "http",
        "requests",
        "httpx",
        "asyncio",
        "threading",
        "multiprocessing",
        "signal",
        "resource",
        "platform",
        "pty",
        "fcntl",
        # Reach the interpreter itself: builtins namespace, object graph
        # traversal, module execution, debuggers, and string-executing timers.
        "builtins",
        "gc",
        "runpy",
        "codeop",
        "pdb",
        "bdb",
        "timeit",
        "webbrowser",
    }
)

# Builtins that execute source, reach the builtins namespace, or defeat the
# static read of the program.
_FORBIDDEN_BUILTINS: frozenset[str] = frozenset(
    {
        "__import__",
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "breakpoint",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "__builtins__",
    }
)

# Names that, if referenced anywhere in the source, indicate an attempt to
# escape the allowed surface — even if no ``import`` for them appears.
# ``__import__`` is the canonical bypass; the rest are common gateways to
# the host process.
_FORBIDDEN_NAMES: frozenset[str] = _HOST_MODULES | _FORBIDDEN_BUILTINS

# Attribute-only additions. These are method/function names rather than
# modules, so they cannot appear as a bare name that matters, but they are
# reachable on allowlisted objects:
#
# * process-spawn entry points — a second net in case a host module is
#   reachable under a name this policy failed to enumerate;
# * deserialization sinks that run constructor code during load. A pickle
#   payload can be supplied as a bytes literal through an allowlisted buffer
#   (``pd.read_pickle(io.BytesIO(b'...'))``), so the sink is reachable with
#   no forbidden import and no file of the attacker's making.
_ESCAPE_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "popen",
        "fork",
        "forkpty",
        "execv",
        "execve",
        "execvp",
        "execl",
        "spawnv",
        "spawnl",
        "spawnve",
        "read_pickle",
        "to_pickle",
        "read_hdf",
        "to_hdf",
        "load",
    }
)

_FORBIDDEN_ATTRIBUTES: frozenset[str] = _FORBIDDEN_NAMES | _ESCAPE_ATTRIBUTES


# Maximum source length accepted (defense in depth — the sandbox itself
# limits execution time, but rejecting absurdly large inputs early avoids
# wasting resources and makes the AST walk bounded).
_MAX_SOURCE_LENGTH = 32 * 1024  # 32 KiB


class PolicyError(ValueError):
    """Raised when ``python_code`` violates the diagram-tool policy."""


def _root_package(dotted_name: str) -> str:
    """Return the leftmost segment of a dotted import name."""
    return dotted_name.split(".", 1)[0]


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__") and len(name) >= 4


class _PolicyVisitor(ast.NodeVisitor):
    """AST walker enforcing the diagram-tool policy.

    Raises :class:`PolicyError` on the first violation encountered. The
    message is a short structural label (``"forbidden import"``,
    ``"dunder attribute access"``, etc.); callers translate to a generic
    user-facing string.
    """

    # Imports ---------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 (ast API)
        for alias in node.names:
            root = _root_package(alias.name)
            if root not in _ALLOWED_IMPORT_ROOTS:
                raise PolicyError(f"forbidden import: {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        # Reject relative imports outright — the sandbox has no project tree.
        if node.level and node.level > 0:
            raise PolicyError("relative import")
        if not node.module:
            raise PolicyError("forbidden import")
        root = _root_package(node.module)
        if root not in _ALLOWED_IMPORT_ROOTS:
            raise PolicyError(f"forbidden import: {node.module}")
        # An allowlisted module re-exports forbidden ones, so the *imported
        # member* needs the same denylist as an attribute would:
        # ``from pandas.io.common import os as o`` binds the real os module
        # under a name this policy would otherwise consider innocuous.
        for alias in node.names:
            if alias.name in _FORBIDDEN_ATTRIBUTES or _is_dunder(alias.name):
                raise PolicyError(f"forbidden import member: {alias.name}")
        self.generic_visit(node)

    # Names and attributes --------------------------------------------------

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if node.id in _FORBIDDEN_NAMES or _is_dunder(node.id):
            raise PolicyError(f"forbidden name: {node.id}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if _is_dunder(node.attr):
            raise PolicyError(f"dunder attribute access: {node.attr}")
        # Attribute chains are checked with the same denylist as bare names:
        # an allowlisted module that itself imports ``os`` re-exports it as an
        # attribute, so ``pd.io.common.os.popen(...)`` would otherwise reach
        # the host with no import statement in the source at all.
        if node.attr in _FORBIDDEN_ATTRIBUTES:
            raise PolicyError(f"forbidden attribute: {node.attr}")
        self.generic_visit(node)

    # Constructs that subvert static analysis ------------------------------

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        # Lambdas themselves are fine; we still walk their body.
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:  # noqa: N802
        # ``global`` and ``nonlocal`` aren't escapes on their own, but they
        # frequently appear in obfuscation; the names they bind are checked
        # by visit_Name when used.
        self.generic_visit(node)


def validate_diagram_code(source: str) -> None:
    """Validate that ``source`` is permitted by the diagram-tool policy.

    Args:
        source: Python source code as supplied to the tool's
            ``python_code`` parameter.

    Raises:
        PolicyError: when the source is empty, exceeds the size limit,
            fails to parse, or contains a construct the policy forbids.
    """
    if not isinstance(source, str) or not source.strip():
        raise PolicyError("empty source")
    if len(source) > _MAX_SOURCE_LENGTH:
        raise PolicyError("source too large")

    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        logger.warning("Diagram code rejected: SyntaxError %s", exc.msg)
        raise PolicyError("syntax error") from exc

    try:
        _PolicyVisitor().visit(tree)
    except PolicyError as exc:
        logger.warning("Diagram code rejected: %s", exc)
        raise
