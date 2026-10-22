"""The services must not write into their own source tree.

The container images ship ``/app/src`` and ``/app/.venv`` root-owned, so the
runtime user cannot rewrite the code it executes. That only holds as long as
nothing creates directories or files inside the source tree at runtime.

Something did, until it was deleted: both API lifespans ran ``os.makedirs``
against a ``base_dir`` derived from ``__file__`` (landing in ``<src>/apis``),
and ``agents/utils/config.py`` did the same one level up to hold local tool
output. That local-output mechanism was dead — its only writer could fire for
two tool names that no longer exist anywhere in the repo, and the static mounts
that served it were never referenced by the frontend. Output goes to S3.

These tests keep it from coming back. If one fails, the container is about to
fail at boot — or, worse, boot and then fail the first time it tries to write —
so failing here is the cheap version.
"""

import ast
from pathlib import Path

_BACKEND_SRC = Path(__file__).resolve().parent.parent.parent / "src"

# Runs on SageMaker / in the code-interpreter sandbox, not in our containers.
_OUT_OF_CONTAINER = (
    "fine_tuning/sagemaker_scripts",
    "documents/ingestion/generate_random_doc.py",
)

# Directory names the deleted mechanism used. A new one of these joined onto a
# path derived from __file__ is the regression we are guarding against.
_WRITABLE_NAMES = {"output", "uploads", "generated_images"}


def _container_python_files():
    for path in sorted(_BACKEND_SRC.rglob("*.py")):
        rel = path.relative_to(_BACKEND_SRC).as_posix()
        if any(marker in rel for marker in _OUT_OF_CONTAINER):
            continue
        yield path


def _root_name(expr):
    """Walk `a.b.c` back to `a`, returning the bare name or None."""
    while isinstance(expr, ast.Attribute):
        expr = expr.value
    return expr.id if isinstance(expr, ast.Name) else None


def test_no_writable_dir_is_derived_from_file():
    """Catch a writable directory built from ``__file__``.

    This is the exact pattern that made the source tree writable:

        base_dir = Path(__file__).parent.parent      # lands in the source tree
        output_dir = os.path.join(base_dir, "output")

    Reading a file relative to ``__file__`` stays fine (templates, .env). What
    this flags is joining one of the writable directory names onto a path
    derived from ``__file__`` — precise enough that it does not fire on every
    module that merely mentions both.
    """
    offenders = []

    for path in _container_python_files():
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

        def _is_writable_const(expr):
            return isinstance(expr, ast.Constant) and expr.value in _WRITABLE_NAMES

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
        "source tree the image ships read-only. Generated output belongs in S3:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


def test_no_makedirs_under_the_source_tree():
    """Catch a hard-coded write under /app/src (the read-only source tree)."""
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


def test_the_deleted_local_output_mechanism_stays_deleted():
    """The static mounts and their directories are gone; keep them gone.

    ``app.mount("/output", StaticFiles(...))`` served directories that nothing
    wrote to and nothing linked to, backed by a saver that could not fire.
    Reintroducing a StaticFiles mount over a source-tree path would make the
    image need a writable /app again — and would expose whatever it serves on
    an unauthenticated route.
    """
    offenders = []
    for path in _container_python_files():
        source = path.read_text(encoding="utf-8")
        if "StaticFiles" not in source:
            continue
        for lineno, line in enumerate(source.splitlines(), start=1):
            if "StaticFiles" in line and not line.strip().startswith("#"):
                offenders.append(f"{path.relative_to(_BACKEND_SRC)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "StaticFiles mounts are back. They serve from the source tree the image "
        "ships read-only, on an unauthenticated route. Generated output goes to "
        "S3:\n  " + "\n  ".join(offenders)
    )
