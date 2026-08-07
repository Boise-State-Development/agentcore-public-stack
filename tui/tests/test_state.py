"""Local state tests.

Every failure mode here must degrade rather than raise: this file is cosmetic
bookkeeping, and no part of it is worth failing a startup over.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcore_tui.state import (
    BANNER_VERSION_KEY,
    banner_shown_version,
    read_state,
    record_banner_shown,
    should_show_banner,
    state_path,
    write_state,
)


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


class TestStatePath:
    def test_is_under_a_state_directory_not_the_config_one(self) -> None:
        """State is written by the client; config is owned by the user. Keeping
        them apart means launching never rewrites a hand-edited file."""
        path = state_path()
        assert path.name == "state.json"
        assert "agentcore-tui" in path.parts


class TestReadState:
    def test_missing_file_is_empty(self, state_file: Path) -> None:
        assert read_state(state_file) == {}

    def test_corrupt_json_is_treated_as_empty(self, state_file: Path) -> None:
        state_file.write_text("{not json at all", encoding="utf-8")
        assert read_state(state_file) == {}

    def test_non_object_json_is_treated_as_empty(self, state_file: Path) -> None:
        """A top-level list would otherwise crash the first `.get`."""
        state_file.write_text('["unexpected"]', encoding="utf-8")
        assert read_state(state_file) == {}

    def test_a_directory_in_the_files_place_is_treated_as_empty(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        target.mkdir()
        assert read_state(target) == {}


class TestWriteState:
    def test_roundtrips(self, state_file: Path) -> None:
        assert write_state({"key": "value"}, state_file) is True
        assert read_state(state_file) == {"key": "value"}

    def test_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "deeper" / "state.json"
        assert write_state({"key": "value"}, nested) is True
        assert read_state(nested) == {"key": "value"}

    def test_merges_rather_than_replaces(self, state_file: Path) -> None:
        write_state({"first": 1}, state_file)
        write_state({"second": 2}, state_file)
        assert read_state(state_file) == {"first": 1, "second": 2}

    def test_leaves_no_temporary_files_behind(self, tmp_path: Path) -> None:
        target = tmp_path / "state.json"
        write_state({"key": "value"}, target)
        assert [path.name for path in tmp_path.iterdir()] == ["state.json"]

    def test_unwritable_location_returns_false(self, tmp_path: Path) -> None:
        """A read-only home must not stop the client from starting."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        # `blocker` is a file, so mkdir of `blocker/state.json`'s parent fails.
        assert write_state({"key": "value"}, blocker / "state.json") is False

    def test_corrupt_existing_file_is_overwritten_not_propagated(self, state_file: Path) -> None:
        state_file.write_text("{{{", encoding="utf-8")
        assert write_state({"key": "value"}, state_file) is True
        assert read_state(state_file) == {"key": "value"}

    def test_written_json_is_human_readable(self, state_file: Path) -> None:
        write_state({BANNER_VERSION_KEY: "1.2.3"}, state_file)
        assert json.loads(state_file.read_text(encoding="utf-8")) == {BANNER_VERSION_KEY: "1.2.3"}


class TestBannerGate:
    def test_shows_when_nothing_recorded(self, state_file: Path) -> None:
        assert banner_shown_version(state_file) is None
        assert should_show_banner("1.13.0", state_file) is True

    def test_does_not_show_again_for_the_same_version(self, state_file: Path) -> None:
        assert record_banner_shown("1.13.0", state_file) is True
        assert should_show_banner("1.13.0", state_file) is False

    def test_shows_again_after_an_upgrade(self, state_file: Path) -> None:
        record_banner_shown("1.13.0", state_file)
        assert should_show_banner("1.14.0", state_file) is True

    def test_shows_again_after_a_downgrade(self, state_file: Path) -> None:
        """Compared for inequality, not ordering — version strings are not
        reliably comparable without a parser, and any change is worth showing."""
        record_banner_shown("1.13.0", state_file)
        assert should_show_banner("1.12.0", state_file) is True

    def test_an_empty_recorded_version_counts_as_unshown(self, state_file: Path) -> None:
        write_state({BANNER_VERSION_KEY: ""}, state_file)
        assert should_show_banner("1.13.0", state_file) is True

    def test_a_non_string_recorded_version_counts_as_unshown(self, state_file: Path) -> None:
        write_state({BANNER_VERSION_KEY: 113}, state_file)
        assert banner_shown_version(state_file) is None
        assert should_show_banner("1.13.0", state_file) is True

    def test_recording_preserves_unrelated_keys(self, state_file: Path) -> None:
        write_state({"unrelated": "keep me"}, state_file)
        record_banner_shown("1.13.0", state_file)
        assert read_state(state_file) == {"unrelated": "keep me", BANNER_VERSION_KEY: "1.13.0"}
