"""The service images must ship their code read-only to the runtime user.

Two properties, both easy to lose in a one-line Dockerfile edit:

1. No ``chown -R`` over /app. Beyond handing the runtime user write access to
   the code it executes, a recursive chown rewrites every file it touches into
   a fresh layer — /app ends up shipped twice. On app-api that was a 454MB
   duplicate.

2. No `COPY --chown` of the code either. The runtime user reads and executes
   /app/src and /app/.venv; it does not own them, so a process compromised at
   runtime cannot rewrite its own source or drop a backdoor into site-packages.

There is deliberately no writable directory under /app at all. The services
write nothing to local disk — generated output goes to S3 — so the image does
not need one. backend/tests/architecture/test_source_tree_not_written.py is the
code-side half of the same guarantee.
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
    grants write access to the code, and nothing needs it: the services write
    nothing to local disk.
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
        f"and nothing writes to local disk any more:\n  " + "\n  ".join(offenders)
    )
