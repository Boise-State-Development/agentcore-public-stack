"""Which import source a project's skill binding reports.

A re-import moves the live skill to a new commit while a pinned project keeps
running the version it pinned, so the binding must name the pinned version's
commit (found on dev: the page showed e4bb5d9 while the project ran b9e19e6).
"""

from __future__ import annotations

from apis.app_api.projects.routes import _pinned_import_source
from apis.shared.skills.pinning import PinStatus

LIVE = {"type": "github", "owner": "anthropics", "repo": "skills", "commitSha": "b" * 40}
PINNED = {"type": "github", "owner": "anthropics", "repo": "skills", "commitSha": "a" * 40}


def test_an_unpinned_binding_runs_live_so_shows_live():
    assert _pinned_import_source(None, LIVE) == LIVE
    assert _pinned_import_source(PinStatus(None, None, False), LIVE) == LIVE


def test_a_pinned_binding_shows_the_commit_its_version_was_cut_from():
    assert _pinned_import_source(PinStatus(1, "t", True, PINNED), LIVE) == PINNED
    assert _pinned_import_source(PinStatus(1, "t", False, PINNED), LIVE) == PINNED


def test_an_old_version_shows_live_only_while_the_content_still_matches():
    """Versions cut before they recorded a source: the live one is right only if nothing moved."""
    assert _pinned_import_source(PinStatus(1, "t", False, None), LIVE) == LIVE
    assert _pinned_import_source(PinStatus(1, "t", True, None), LIVE) is None
