"""Agent Designer Phase 3 (PR-B) — Memory-Space hydration helper.

Resolves alwaysLoad specs against a fake MemorySpaceService: MEMORY.md, the
latest:<type>/<prefix> scheme, bare slugs, missing-entry skip, default, budget cap.
"""

from dataclasses import dataclass

import pytest

from apis.shared.memory.hydration import (
    DEFAULT_ALWAYS_LOAD,
    render_memory_block,
    resolve_always_load,
)
from apis.shared.memory.service import MemorySpaceNotFoundError


@dataclass
class _Ref:
    slug: str
    entry_type: str
    updated: str


class _FakeService:
    """Minimal MemorySpaceService stand-in; filters like the real list_entries."""

    def __init__(self, index="", entries=None, bodies=None):
        self._index = index
        self._entries = entries or []
        self._bodies = bodies or {}

    def read_index(self, space_id, user_id, user_email=None):
        return self._index

    def list_entries(self, space_id, user_id, user_email=None, *, entry_type=None, where=None):
        return [e for e in self._entries if entry_type is None or e.entry_type == entry_type]

    def read_entry(self, space_id, user_id, user_email, slug):
        if slug not in self._bodies:
            raise MemorySpaceNotFoundError(f"entry '{slug}' not found")
        return self._bodies[slug]


def _resolve(service, always_load, **kw):
    return resolve_always_load(service, "spc_1", "u1", "u1@x.edu", always_load, **kw)


class TestHydration:
    def test_default_is_memory_md(self):
        frags = _resolve(_FakeService(index="# Index"), None)
        assert DEFAULT_ALWAYS_LOAD == ["MEMORY.md"]
        assert [f.label for f in frags] == ["MEMORY.md"]
        assert frags[0].text == "# Index"

    def test_empty_index_skipped(self):
        assert _resolve(_FakeService(index=""), ["MEMORY.md"]) == []

    def test_latest_picks_most_recent_matching_type_and_prefix(self):
        svc = _FakeService(
            entries=[
                _Ref("daily-2026-07-05", "episodic", "2026-07-05"),
                _Ref("daily-2026-07-07", "episodic", "2026-07-07"),
                _Ref("weekly-2026-07-01", "episodic", "2026-07-01"),
                _Ref("daily-note", "fact", "2026-07-09"),  # wrong type, ignored
            ],
            bodies={"daily-2026-07-07": "latest daily"},
        )
        frags = _resolve(svc, ["latest:episodic/daily"])
        assert len(frags) == 1
        assert frags[0].label == "latest:episodic/daily → daily-2026-07-07"
        assert frags[0].text == "latest daily"

    def test_latest_with_invalid_type_treats_whole_as_slug_prefix(self):
        svc = _FakeService(
            entries=[_Ref("proj-x", "fact", "2026-07-07")],
            bodies={"proj-x": "project x"},
        )
        frags = _resolve(svc, ["latest:proj"])
        assert frags[0].text == "project x"

    def test_latest_no_match_skipped(self):
        assert _resolve(_FakeService(entries=[]), ["latest:episodic/daily"]) == []

    def test_bare_slug_reads_entry(self):
        svc = _FakeService(bodies={"jane-doe": "Jane's profile"})
        frags = _resolve(svc, ["jane-doe"])
        assert frags[0].label == "jane-doe" and frags[0].text == "Jane's profile"

    def test_missing_slug_is_skipped_not_raised(self):
        assert _resolve(_FakeService(bodies={}), ["ghost"]) == []

    def test_budget_truncates_with_marker(self):
        svc = _FakeService(index="A" * 500)
        frags = _resolve(svc, ["MEMORY.md"], max_total_bytes=100)
        assert frags[0].text.startswith("A" * 100)
        assert "truncated" in frags[0].text

    def test_budget_stops_further_fragments(self):
        svc = _FakeService(index="A" * 100, bodies={"e": "B" * 100})
        frags = _resolve(svc, ["MEMORY.md", "e"], max_total_bytes=100)
        # First fragment exhausts the budget; the second is not loaded.
        assert [f.label for f in frags] == ["MEMORY.md"]


class TestRenderBlock:
    def test_empty_when_no_fragments(self):
        assert render_memory_block("Oliver's Brain", []) == ""

    def test_renders_labeled_sections(self):
        svc = _FakeService(index="# Index", bodies={"jane": "profile"})
        frags = _resolve(svc, ["MEMORY.md", "jane"])
        block = render_memory_block("Oliver's Brain", frags)
        assert 'Bound Memory — "Oliver\'s Brain"' in block
        assert "### MEMORY.md" in block and "# Index" in block
        assert "### jane" in block and "profile" in block
