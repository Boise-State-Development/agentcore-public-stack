"""The service images must ship their code read-only to the runtime user.

Two properties, both easy to lose in a one-line Dockerfile edit:

1. No ``chown -R`` over /app. Beyond handing the runtime user write access to
   the code it executes, a recursive chown rewrites every file it touches into
   a fresh layer — /app ends up shipped twice. On app-api that was a 454MB
   duplicate.

2. The writable location is declared explicitly via RUNTIME_DATA_DIR and is
   the only path the runtime user owns. If someone adds a service that writes
   in-tree again, this plus
   backend/tests/architecture/test_source_tree_not_written.py is what catches
   it before the image stops booting.

See apis/shared/runtime_paths.py.
"""

import re
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent

# The two long-lived service images. The Lambda images are a different shape:
# they have no venv copy and AWS controls /var/task ownership.
_SERVICE_DOCKERFILES = ["Dockerfile.app-api", "Dockerfile.inference-api"]


def _dockerfile(name: str) -> str:
    return (_BACKEND / name).read_text(encoding="utf-8")


def _instructions(text: str):
    """Yield logical Dockerfile instructions, joining backslash continuations
    and dropping comments."""
    joined = re.sub(r"\\\s*\n", " ", text)
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield stripped


@pytest.mark.parametrize("name", _SERVICE_DOCKERFILES)
def test_no_recursive_chown_of_app(name):
    """A `chown -R` over /app duplicates the whole tree into a new layer."""
    offenders = [
        instr
        for instr in _instructions(_dockerfile(name))
        if re.search(r"\bchown\b", instr)
        and re.search(r"-R|--recursive", instr)
        and "/app" in instr
    ]
    assert not offenders, (
        f"{name} recursively chowns /app, which ships the tree twice and hands the "
        f"runtime user write access to its own code:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("name", _SERVICE_DOCKERFILES)
def test_code_is_not_copied_with_chown(name):
    """/app/src and /app/.venv must land root-owned.

    `COPY --chown=<runtime user>` is cheaper than a recursive chown but still
    grants write access to the code; the point of RUNTIME_DATA_DIR is that it
    is no longer needed.
    """
    offenders = [
        instr
        for instr in _instructions(_dockerfile(name))
        if instr.upper().startswith("COPY")
        and "--chown" in instr
        and ("/app/src" in instr or "/app/.venv" in instr)
    ]
    assert not offenders, (
        f"{name} copies code with --chown, so the runtime user can rewrite it:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("name", _SERVICE_DOCKERFILES)
def test_declares_runtime_data_dir(name):
    """The image must point the services at a writable dir off the source tree."""
    instrs = list(_instructions(_dockerfile(name)))

    declared = [i for i in instrs if i.upper().startswith("ENV") and "RUNTIME_DATA_DIR" in i]
    assert declared, (
        f"{name} does not set RUNTIME_DATA_DIR. Without it the services fall back to "
        "their legacy in-tree paths and will fail on the read-only source tree."
    )

    match = re.search(r"RUNTIME_DATA_DIR[= ]+(\S+)", declared[0])
    assert match, f"could not parse RUNTIME_DATA_DIR out of: {declared[0]}"
    data_dir = match.group(1).strip('"').strip("'")

    assert not data_dir.startswith("/app/src"), (
        f"{name} points RUNTIME_DATA_DIR at {data_dir}, which is inside the source tree"
    )

    # ...and it must actually be created and handed to the runtime user.
    created = [i for i in instrs if data_dir in i and ("install -d" in i or "mkdir" in i)]
    assert created, (
        f"{name} sets RUNTIME_DATA_DIR={data_dir} but never creates it as a "
        "runtime-user-owned directory; the service will fail on first write."
    )
    assert any("-o " in i or "--owner" in i for i in created), (
        f"{name} creates {data_dir} but does not give it to the runtime user:\n  "
        + "\n  ".join(created)
    )


@pytest.mark.parametrize("name", _SERVICE_DOCKERFILES)
def test_no_dead_toplevel_asset_dirs(name):
    """/app/output, /app/uploads, /app/generated_images were never used.

    base_dir has always resolved under /app/src, so nothing read or wrote
    these. They are gone; this keeps them gone.
    """
    offenders = [
        instr
        for instr in _instructions(_dockerfile(name))
        if re.search(r"/app/(output|uploads|generated_images)\b", instr)
    ]
    assert not offenders, (
        f"{name} recreates the dead top-level asset dirs; runtime paths resolve under "
        f"RUNTIME_DATA_DIR, not /app:\n  " + "\n  ".join(offenders)
    )
