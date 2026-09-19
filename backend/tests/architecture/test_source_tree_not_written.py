"""The services must not write into their own source tree.

The container images ship ``/app/src`` and ``/app/.venv`` root-owned and
read-only, so the runtime user cannot rewrite the code it executes. That only
holds as long as nothing creates directories or files inside the source tree at
runtime. It did once: both API lifespans and the agents package derived their
writable directories from ``__file__``, which is why the images had to hand the
whole of ``/app`` to the runtime user (and pay for a duplicate layer to do it).

These tests are the regression guard for that property. If one fails, the
container will fail to boot — or, worse, will boot and then fail the first time
a tool tries to save a file — so failing here is the cheap version.

See apis/shared/runtime_paths.py.
"""

import ast
import os
from pathlib import Path

import pytest

_BACKEND_SRC = Path(__file__).resolve().parent.parent.parent / "src"

# Modules that legitimately still reference the legacy in-tree location: the
# helper that DEFINES the legacy fallback, and .env loading (read-only).
_ALLOWED_FILE_DERIVED_PATHS = {
    _BACKEND_SRC / "apis" / "shared" / "runtime_paths.py",
}

# Runs on SageMaker / in the code-interpreter sandbox, not in our containers.
_OUT_OF_CONTAINER = ("fine_tuning/sagemaker_scripts", "documents/ingestion/generate_random_doc.py")


def _container_python_files():
    for path in sorted(_BACKEND_SRC.rglob("*.py")):
        rel = path.relative_to(_BACKEND_SRC).as_posix()
        if any(marker in rel for marker in _OUT_OF_CONTAINER):
            continue
        yield path


def test_runtime_data_dir_relocates_both_roots(tmp_path, monkeypatch):
    """RUNTIME_DATA_DIR must move BOTH roots off the source tree."""
    from apis.shared import runtime_paths

    monkeypatch.setenv(runtime_paths.RUNTIME_DATA_DIR_ENV, str(tmp_path))

    static_dir = runtime_paths.get_static_assets_dir()
    agent_dir = runtime_paths.get_agent_workspace_dir()

    for resolved in (static_dir, agent_dir):
        assert tmp_path in resolved.parents or resolved == tmp_path, (
            f"{resolved} is not under the configured RUNTIME_DATA_DIR"
        )
        assert _BACKEND_SRC not in resolved.parents, (
            f"{resolved} still resolves inside the source tree"
        )

    # The two roots stay distinct: collapsing them would change which files the
    # unauthenticated static mount serves. See runtime_paths.__doc__.
    assert static_dir != agent_dir


@pytest.mark.parametrize("unset_value", ["", "   "])
def test_empty_runtime_data_dir_falls_back_to_legacy(monkeypatch, unset_value):
    """An exported-but-empty variable must not resolve paths against ""."""
    from apis.shared import runtime_paths

    monkeypatch.setenv(runtime_paths.RUNTIME_DATA_DIR_ENV, unset_value)

    assert runtime_paths.get_static_assets_dir() == _BACKEND_SRC / "apis"
    assert runtime_paths.get_agent_workspace_dir() == _BACKEND_SRC


def test_agents_config_honours_runtime_data_dir(tmp_path, monkeypatch):
    """Config is the agents-side entry point; it must not write in-tree."""
    from agents.utils.config import Config
    from apis.shared import runtime_paths

    monkeypatch.setenv(runtime_paths.RUNTIME_DATA_DIR_ENV, str(tmp_path))

    # These three CREATE the directory as a side effect — that is the whole
    # reason the source tree had to be writable.
    for resolved in (
        Config.get_output_dir(),
        Config.get_uploads_dir(),
        Config.get_generated_images_dir(),
        Config.get_session_output_dir("session-abc"),
    ):
        assert _BACKEND_SRC not in resolved.parents, f"{resolved} is inside the source tree"
        assert resolved.is_dir()

    # Nothing appeared in the source tree as a side effect.
    for leaked in ("output", "uploads", "generated_images"):
        assert not (_BACKEND_SRC / leaked).exists(), (
            f"Config created {leaked}/ inside the source tree despite RUNTIME_DATA_DIR"
        )


def test_no_new_writable_paths_derived_from_file():
    """Catch a NEW writable directory built from ``__file__``.

    This is the exact pattern that created the problem:

        base_dir = Path(__file__).parent.parent      # lands in the source tree
        output_dir = os.path.join(base_dir, "output")

    Reading a file relative to ``__file__`` stays fine (templates, .env). What
    this flags is joining one of the writable directory names onto a path that
    was derived from ``__file__`` -- precise enough that it does not fire on
    every module that merely mentions both.
    """
    writable_names = {"output", "uploads", "generated_images"}
    offenders = []

    for path in _container_python_files():
        if path in _ALLOWED_FILE_DERIVED_PATHS:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        if "__file__" not in source:
            continue

        # Names bound to something derived from __file__.
        file_derived: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                try:
                    value_src = ast.get_source_segment(source, node.value) or ""
                except Exception:  # pragma: no cover - defensive
                    value_src = ""
                if "__file__" in value_src:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            file_derived.add(target.id)
        if not file_derived:
            continue

        def _root_name(expr):
            while isinstance(expr, ast.Attribute):
                expr = expr.value
            return expr.id if isinstance(expr, ast.Name) else None

        def _is_writable_const(expr):
            return isinstance(expr, ast.Constant) and expr.value in writable_names

        for node in ast.walk(tree):
            # os.path.join(base_dir, "output")
            if isinstance(node, ast.Call) and len(node.args) >= 2:
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "join" and _root_name(node.args[0]) in file_derived:
                    if any(_is_writable_const(a) for a in node.args[1:]):
                        offenders.append(f"{path.relative_to(_BACKEND_SRC)}:{node.lineno}")
            # base_dir / "output"
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                if _root_name(node.left) in file_derived and _is_writable_const(node.right):
                    offenders.append(f"{path.relative_to(_BACKEND_SRC)}:{node.lineno}")

    assert not offenders, (
        "These build a writable directory from __file__, which puts it inside the "
        "source tree. Use apis.shared.runtime_paths instead:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


def test_container_code_does_not_makedirs_under_source_tree():
    """No module may makedirs/mkdir a path rooted at the source tree.

    Complements the __file__ check above: this one catches a hard-coded
    '/app/src/...' or a Config-style helper that resolves in-tree.
    """
    offenders = []
    for path in _container_python_files():
        source = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "/app/src" in stripped and ("makedirs" in stripped or "mkdir" in stripped):
                offenders.append(f"{path.relative_to(_BACKEND_SRC)}:{lineno}")

    assert not offenders, (
        "Hard-coded writes under /app/src (the read-only source tree):\n  "
        + "\n  ".join(offenders)
    )
