"""Tier 1's coverage property over the triage corpus (docs/specs/quiet-watcher-triage.md §6.4).

Deterministic, so it runs in CI with no model calls. For every positive variant whose planted
change is inside a replayed result (P1-P3, P5, P6), some call's fingerprint must differ from the
baseline's. A variant labelled ``unchanged`` (identical results, or only what version 1
normalizes: whitespace runs, object key order) must match on every call. P4 is the case tier 1
cannot see, so it is pinned as unchanged here and its guards (``wakeAt``, the staleness floor)
are tested with T.2's worker logic.

The corpus is synthetic (eval-harness D6) and lives beside, not inside, ``evals/cases``, whose
every file the eval harness loads as eval cases. This is a first slice; §6.2's full corpus
(24 prompts, 120 positive and 80 negative variants, a held-out third) is PR T.4's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from apis.shared.scheduled_prompts.watch_probe import fingerprint_result

CORPUS = Path(__file__).resolve().parents[2] / "evals" / "watch_triage" / "corpus.json"
TIER1_VISIBLE = {"P1", "P2", "P3", "P5", "P6"}
CLASSES = TIER1_VISIBLE | {"P4", "N1", "N2", "N3", "N4"}
SHAPES = {
    "notifications_list",
    "issue_pr_list_with_detail",
    "email_inbox",
    "folder_listing",
    "web_page_text",
    "announcements_feed",
}


def _corpus() -> dict:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def _variants() -> List[tuple]:
    return [
        pytest.param(prompt, variant, id=f"{prompt['id']}::{variant['id']}")
        for prompt in _corpus()["prompts"]
        for variant in prompt["variants"]
    ]


def _fingerprints(prompt: dict, variant: dict) -> tuple:
    results = [call["result"] for call in prompt["calls"]]
    for change in variant["changes"]:
        results[change["call"]] = change["result"]
    return tuple(fingerprint_result(r) for r in results)


def _baseline(prompt: dict) -> tuple:
    return tuple(fingerprint_result(call["result"]) for call in prompt["calls"])


class TestCorpusShape:
    def test_covers_every_source_shape(self):
        assert {p["shape"] for p in _corpus()["prompts"]} == SHAPES

    def test_ids_are_unique(self):
        prompts = _corpus()["prompts"]
        assert len({p["id"] for p in prompts}) == len(prompts)
        for prompt in prompts:
            assert len({v["id"] for v in prompt["variants"]}) == len(prompt["variants"]), prompt["id"]

    @pytest.mark.parametrize("prompt,variant", _variants())
    def test_every_variant_is_labelled_consistently(self, prompt, variant):
        assert variant["class"] in CLASSES
        assert variant["label"] == ("escalate" if variant["class"].startswith("P") else "quiet_ok")
        assert variant["tier1"] in {"changed", "unchanged", "either"}
        if variant["class"] in TIER1_VISIBLE:
            assert variant["tier1"] == "changed", "a planted change inside a result must be visible to tier 1"
        if variant["class"] in {"P4", "N1", "N4"}:
            assert variant["changes"] == [] and variant["tier1"] == "unchanged"
        for change in variant["changes"]:
            assert 0 <= change["call"] < len(prompt["calls"])

    def test_every_tier1_visible_class_is_present(self):
        classes = {v["class"] for p in _corpus()["prompts"] for v in p["variants"]}
        assert TIER1_VISIBLE | {"P4", "N1", "N2", "N3"} <= classes

    def test_every_probe_input_is_within_the_bound(self):
        from apis.shared.scheduled_prompts.watch_probe import MAX_PROBE_CALLS, MAX_PROBE_INPUT_CHARS, probe_input_json

        for prompt in _corpus()["prompts"]:
            assert 1 <= len(prompt["calls"]) <= MAX_PROBE_CALLS
            for call in prompt["calls"]:
                assert len(probe_input_json(call["input"])) <= MAX_PROBE_INPUT_CHARS


@pytest.mark.parametrize("prompt,variant", _variants())
def test_tier1_sees_what_it_must(prompt, variant):
    baseline, replayed = _baseline(prompt), _fingerprints(prompt, variant)
    if variant["tier1"] == "changed":
        assert replayed != baseline, f"{variant['class']} planted change is invisible to the fingerprint"
    elif variant["tier1"] == "unchanged":
        assert replayed == baseline
