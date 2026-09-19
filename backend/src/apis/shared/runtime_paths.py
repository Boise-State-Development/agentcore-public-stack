"""Where the services are allowed to write at runtime.

Historically both API entrypoints and the agents package derived their writable
directories from ``__file__``, which put them *inside the source tree*:

    apis/app_api/main.py        Path(__file__).parent.parent -> <src>/apis
    apis/inference_api/main.py  Path(__file__).parent.parent -> <src>/apis
    agents/utils/config.py      Path(__file__).parent.parent.parent -> <src>

That is what forced the container images to hand the whole of ``/app`` to the
runtime user: a service that creates directories inside its own source tree
cannot run from a read-only one. Owning the code you execute is exactly the
property an attacker who lands RCE wants, so the images paid for it twice —
once in security posture, and once in image size, because granting it needed a
recursive ``chown`` that rewrote every file into a duplicate layer.

Setting ``RUNTIME_DATA_DIR`` moves both roots outside the source tree, which
lets ``/app/src`` and ``/app/.venv`` ship root-owned and read-only. The
container images set it; nothing else has to.

Unset (local dev, tests, any environment that has not opted in) the legacy
in-tree paths are returned unchanged, so this is behaviour-preserving by
default and the migration is a deployment concern rather than a code one.

The two roots are deliberately kept SEPARATE rather than unified. They are
different directories today and they disagree by one level: the static mounts
serve ``<src>/apis/output`` while the agents package writes tool output to
``<src>/output``. Collapsing them here would silently turn a mount that has
always served an empty directory into one that serves real session files, from
an unauthenticated route — a security change smuggled in under a path refactor.
See the module docstring note in ``agents/utils/config.py``.
"""

import os
from pathlib import Path

#: Environment variable that relocates both roots out of the source tree.
RUNTIME_DATA_DIR_ENV = "RUNTIME_DATA_DIR"

# <src>/apis/shared/runtime_paths.py -> parents[1] == <src>/apis, parents[2] == <src>
_LEGACY_STATIC_ASSETS_DIR = Path(__file__).resolve().parents[1]
_LEGACY_AGENT_WORKSPACE_DIR = Path(__file__).resolve().parents[2]


def _configured_root() -> Path | None:
    """Return the configured runtime data root, or None when unset.

    An empty or whitespace-only value counts as unset: a deployment that
    exports the variable without a value should fall back to the legacy
    behaviour rather than resolve paths against "".
    """
    raw = os.getenv(RUNTIME_DATA_DIR_ENV, "").strip()
    return Path(raw) if raw else None


def get_static_assets_dir() -> Path:
    """Root of the directories the FastAPI apps create and mount as static files.

    The apps join ``output``/``uploads``/``generated_images`` onto this.
    """
    root = _configured_root()
    return root / "static" if root else _LEGACY_STATIC_ASSETS_DIR


def get_agent_workspace_dir() -> Path:
    """Root of the directories the agents package writes tool output into.

    ``agents.utils.config.Config`` joins ``output``/``uploads``/
    ``generated_images`` onto this, plus per-session subdirectories.
    """
    root = _configured_root()
    return root / "agent" if root else _LEGACY_AGENT_WORKSPACE_DIR
