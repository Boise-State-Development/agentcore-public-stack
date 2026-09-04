"""Every module a Lambda handler can import must actually be in its image.

The app-api / inference-api images COPY all of `backend/src`, so their
import graph is trivially satisfied. The three Lambda images do NOT — each
COPYs a hand-maintained subset of `apis.shared` to keep the image small.
That subset was a manual list with nothing keeping it in sync with the
code, and it drifted twice over:

* `apis/shared/timestamps.py` was introduced in 8544d87a and imported by
  `documents/ingestion/status.py`; neither Dockerfile.rag-ingestion nor
  Dockerfile.kb-sync copied it. Document ingestion and KB sync ran at a
  100% error rate in dev and prod for days. Nothing surfaced it — the SPA
  just showed crawled documents sitting at `uploading` forever.
* `document_service.soft_delete_document` imports `apis.shared.assistants`
  inside a `try/except ImportError` that logs "boto3 is required" and
  returns None. In the kb-sync image that except branch was always taken,
  so the worker's miss-eviction path silently no-opped.

Those two failure modes are why this test does NOT distinguish
module-level imports from function-local ones. A function-local import
still executes when its function is called — `handler.py` reaches
`status.py` exactly that way — and a *swallowed* one is worse than a
crash, because it degrades quietly. The invariant is simply: if a module
is reachable from an entrypoint's import graph, the image must contain
it.

The cost of being complete is bounded — these are leaf-ish helper modules
whose own dependencies (stdlib, boto3, pydantic) are already pinned in
each image's requirements. If a genuinely heavy module ever shows up
here, the right fix is to stop importing it from a Lambda path, not to
weaken this test.
"""

import ast
import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "backend/src"

# image -> (dockerfile, handler entrypoints, flattened roots)
#
# `flattened roots` are directories whose *contents* land directly at
# LAMBDA_TASK_ROOT (`COPY <dir>/ ${LAMBDA_TASK_ROOT}`), so a bare
# `import status` inside them resolves as a top-level module.
IMAGES: Dict[str, Tuple[str, List[str], List[str]]] = {
    "rag-ingestion": (
        "backend/Dockerfile.rag-ingestion",
        ["apis/app_api/documents/ingestion/handler.py"],
        ["apis/app_api/documents/ingestion"],
    ),
    "kb-sync": (
        "backend/Dockerfile.kb-sync",
        [
            "apis/app_api/kb_sync/dispatcher.py",
            "apis/app_api/kb_sync/worker.py",
        ],
        [],
    ),
    # Four functions, one image, handler selected per function by
    # ImageConfig.Command — so all four entrypoints must be walked, not just
    # the Dockerfile's default CMD.
    "kb-migration": (
        "backend/Dockerfile.kb-migration",
        [
            "apis/app_api/kb_migration/dispatcher.py",
            "apis/app_api/kb_migration/worker.py",
            "apis/app_api/kb_migration/reconciler.py",
            "apis/app_api/kb_migration/ingestion_consumer.py",
        ],
        [],
    ),
    "scheduled-runs": (
        "backend/Dockerfile.scheduled-runs",
        [
            "lambdas/scheduled_runs_dispatcher/dispatcher.py",
            "lambdas/scheduled_runs_worker/worker.py",
        ],
        ["lambdas/scheduled_runs_dispatcher", "lambdas/scheduled_runs_worker"],
    ),
}

# The kb-sync image deliberately ships these alongside their packages but
# never imports them — they pull FastAPI, which the image lacks (see the
# note in Dockerfile.kb-sync). Excluding them keeps the walk honest about
# what a Lambda actually reaches.
NOT_AN_ENTRYPOINT = (
    "apis/app_api/documents/routes.py",
    "apis/app_api/web_sources/routes.py",
    "apis/app_api/file_sources/service.py",
)

_COPY = re.compile(r"^COPY\s+(?:--from=\S+\s+)?(backend/src/\S+)\s")


def _copied_paths(dockerfile: str) -> List[str]:
    """Paths (relative to backend/src) that the Dockerfile COPYs in."""
    out: List[str] = []
    for line in (REPO_ROOT / dockerfile).read_text().splitlines():
        match = _COPY.match(line.strip())
        if match:
            out.append(match.group(1)[len("backend/src/") :].rstrip("/"))
    return out


def _is_copied(rel: str, copied: List[str]) -> bool:
    return any(rel == c or rel.startswith(c + "/") for c in copied)


def _resolve(module: str, flattened_roots: List[str]) -> Optional[str]:
    """Map a dotted module name to its path under backend/src, or None.

    Returning None *is* the third-party filter: `json`, `boto3` and friends
    have no file under backend/src. Note the flattened-root candidates —
    rag-ingestion's handler reaches `status.py` as a bare `import status`,
    with no `apis.` prefix to key off. An earlier version of this test
    filtered on that prefix and so never walked into `status.py` at all,
    missing the very import that broke the image.
    """
    bases = [module.replace(".", "/")]
    bases += [f"{root}/{module.replace('.', '/')}" for root in flattened_roots]
    for base in bases:
        for candidate in (f"{base}.py", f"{base}/__init__.py"):
            if (SRC / candidate).exists():
                return candidate
    return None


def _imports(path: Path) -> Iterator[str]:
    """Yield every dotted module name imported anywhere in a file.

    `from x import y` yields both `x` and `x.y` — the latter matters when
    `y` is a submodule rather than a name. Relative imports are skipped:
    they resolve inside a package that is copied as a unit.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module
            for alias in node.names:
                yield f"{node.module}.{alias.name}"


def missing_modules(image: str) -> Dict[str, Set[str]]:
    """Return {module path not in the image -> {files that import it}}.

    Walks *through* missing modules as well, so one absent package does
    not hide the rest of the closure behind it.
    """
    dockerfile, entrypoints, flattened_roots = IMAGES[image]
    copied = _copied_paths(dockerfile)

    missing: Dict[str, Set[str]] = {}
    seen: Set[str] = set()
    queue: List[str] = list(entrypoints)

    while queue:
        rel = queue.pop()
        if rel in seen or rel in NOT_AN_ENTRYPOINT:
            continue
        seen.add(rel)
        path = SRC / rel
        if not path.exists():
            continue
        try:
            found = list(_imports(path))
        except SyntaxError:  # pragma: no cover - the linter catches this first
            continue
        for module in found:
            resolved = _resolve(module, flattened_roots)
            if resolved is None:
                continue
            if not _is_copied(resolved, copied):
                missing.setdefault(resolved, set()).add(rel)
            if resolved not in seen:
                queue.append(resolved)

    return missing


@pytest.mark.parametrize("image", sorted(IMAGES))
def test_lambda_image_copies_its_full_import_closure(image: str) -> None:
    missing = missing_modules(image)
    if missing:
        detail = "\n".join(
            f"  {path}\n      imported by: {', '.join(sorted(importers))}"
            for path, importers in sorted(missing.items())
        )
        dockerfile = IMAGES[image][0]
        pytest.fail(
            f"{image}: these modules are reachable from the handler's import "
            f"graph but are not COPYed into the image. At runtime they raise "
            f"ModuleNotFoundError — at cold start if imported at module "
            f"level, or mid-invocation (possibly swallowed by an "
            f"`except ImportError`) if imported inside a function:\n{detail}\n\n"
            f"Fix: add a COPY for each to {dockerfile}, AND add the path to "
            f"the {image} SOURCE_DIRS/MANIFESTS in scripts/build/build-one.sh "
            f"so the content-hash tag notices future changes."
        )


def test_entrypoints_exist() -> None:
    """Guards the test itself: a renamed handler would silently pass above."""
    for image, (_dockerfile, entrypoints, _flattened) in IMAGES.items():
        for entry in entrypoints:
            assert (SRC / entry).exists(), f"{image}: missing entrypoint {entry}"


@pytest.mark.parametrize("image", sorted(IMAGES))
def test_walk_reaches_beyond_the_entrypoint(image: str) -> None:
    """Guards the test itself: a resolver bug would make every image 'pass'.

    If `_resolve` or the COPY parser broke, `missing_modules` would return
    {} for everything and the suite would go quietly green. Asserting the
    walk visits real first-party modules keeps that failure visible.
    """
    _dockerfile, entrypoints, flattened_roots = IMAGES[image]
    reached = {
        _resolve(module, flattened_roots)
        for entry in entrypoints
        for module in _imports(SRC / entry)
    }
    assert [r for r in reached if r], f"{image}: walk resolved no first-party imports"
