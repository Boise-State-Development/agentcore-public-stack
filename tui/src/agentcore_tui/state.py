"""Local, non-authoritative state that persists between runs.

This is deliberately separate from :mod:`config`. Config is *input* the user
owns and hand-edits; state is *output* the client writes about itself. Mixing
them would mean rewriting the user's config file on every launch, and would put
machine bookkeeping in a file they are invited to edit.

Nothing here is essential to operation, so every failure degrades rather than
raises. A read-only home directory, a corrupt file, or a missing state
directory must never stop the client from starting — the worst outcome is that
the startup banner is shown again.

Location, courtesy of ``platformdirs``:

========= ==============================================================
Linux     ``~/.local/state/agentcore-tui/state.json``
macOS     ``~/Library/Application Support/agentcore-tui/state.json``
Windows   ``%LOCALAPPDATA%\\agentcore-tui\\state.json``
========= ==============================================================
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from platformdirs import user_state_path

from .keyring_store import APP_NAME

logger = logging.getLogger(__name__)

#: Key under which the last version whose banner was shown is recorded.
BANNER_VERSION_KEY = "banner_shown_version"


def state_path() -> Path:
    """Return the platform-correct state file path (may not exist)."""
    return user_state_path(APP_NAME, appauthor=False, ensure_exists=False) / "state.json"


def read_state(path: Path | None = None) -> dict[str, object]:
    """Load the state file. Returns ``{}`` when absent, unreadable, or corrupt.

    A corrupt file is treated as empty rather than repaired or reported: the
    only consequence is re-showing the banner, and that is a far better outcome
    than an error on startup about a file the user has never heard of.
    """
    target = path or state_path()
    try:
        with target.open("rb") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.debug("ignoring unreadable state file %s: %s", target, exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_state(values: dict[str, object], path: Path | None = None) -> bool:
    """Merge ``values`` into the state file. Returns False if it could not be written.

    Written via a temporary file and :func:`os.replace` so an interrupted write
    cannot truncate an existing file into invalid JSON — cheap here, and the
    alternative is a corrupt-state path that only ever shows up as a confusing
    bug report.
    """
    target = path or state_path()
    merged = {**read_state(target), **values}

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Same directory as the target, because os.replace is only atomic
        # within a filesystem.
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                json.dump(merged, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temp_path, target)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    except OSError as exc:
        logger.debug("could not persist state to %s: %s", target, exc)
        return False
    return True


def banner_shown_version(path: Path | None = None) -> str | None:
    """The version whose startup banner was last displayed, if any."""
    recorded = read_state(path).get(BANNER_VERSION_KEY)
    return recorded if isinstance(recorded, str) and recorded else None


def should_show_banner(version: str, path: Path | None = None) -> bool:
    """True when the banner has not yet been shown for ``version``.

    Compares for inequality rather than ordering so a downgrade also shows the
    banner. Version strings are not reliably comparable without a parser, and
    "show it again after any version change" is the intended behaviour anyway.
    """
    return banner_shown_version(path) != version


def record_banner_shown(version: str, path: Path | None = None) -> bool:
    """Remember that ``version``'s banner has been displayed."""
    return write_state({BANNER_VERSION_KEY: version}, path)
