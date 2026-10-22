"""Diagnosis rules — one test per classification, pure in, pure out.

Each rule encodes a finding a prior quota investigation reached by hand. The
tests pin the *threshold* the rule fires at and the *evidence* it carries, so a
future tweak to a number is a visible diff here rather than a silent change in
what an admin is told.
"""

from __future__ import annotations


from apis.app_api.admin.costs import diagnoses as dg
from apis.app_api.admin.costs.diagnoses import ProfileFacts, run_diagnoses


def _codes(facts: ProfileFacts) -> list[str]:
    return [d.code for d in run_diagnoses(facts)]


def _known(**overrides) -> ProfileFacts:
    base = dict(cost_known=True, total_cost=1.0, call_count=10, cache_read_tokens=100_000, cache_write_tokens=10_000)
    base.update(overrides)
    return ProfileFacts(**base)


# ── threshold resolution ─────────────────────────────────────────────────────


def test_compaction_threshold_defaults_when_unset(monkeypatch):
    monkeypatch.delenv(dg.EnvVars.COMPACTION_TOKEN_THRESHOLD, raising=False)
    assert dg.compaction_token_threshold() == dg.Defaults.COMPACTION_TOKEN_THRESHOLD


def test_compaction_threshold_reads_env_and_tolerates_garbage(monkeypatch):
    monkeypatch.setenv(dg.EnvVars.COMPACTION_TOKEN_THRESHOLD, "50000")
    assert dg.compaction_token_threshold() == 50_000
    monkeypatch.setenv(dg.EnvVars.COMPACTION_TOKEN_THRESHOLD, "not-a-number")
    assert dg.compaction_token_threshold() == dg.Defaults.COMPACTION_TOKEN_THRESHOLD
    monkeypatch.setenv(dg.EnvVars.COMPACTION_TOKEN_THRESHOLD, "   ")
    assert dg.compaction_token_threshold() == dg.Defaults.COMPACTION_TOKEN_THRESHOLD


# ── individual rules ─────────────────────────────────────────────────────────


def test_cost_unknown_fires_only_when_cost_is_unrecorded():
    assert "COST_UNKNOWN" in _codes(ProfileFacts(cost_known=False))
    assert "COST_UNKNOWN" not in _codes(_known())


def test_over_threshold_uses_the_facts_threshold_not_a_constant():
    facts = _known(peak_context_tokens=60_000, compaction_threshold=50_000)
    d = dg.over_compaction_threshold(facts)
    assert d is not None and d.severity == "warn"
    assert d.evidence["compactionThreshold"] == 50_000
    assert dg.over_compaction_threshold(_known(peak_context_tokens=50_000, compaction_threshold=50_000)) is None
    assert dg.over_compaction_threshold(_known(peak_context_tokens=None)) is None


def test_prefix_spiral_requires_over_threshold_and_write_heavy_ratio():
    over = dict(peak_context_tokens=150_000)
    # Over threshold but healthy 1:10 → not a spiral.
    assert dg.prefix_spiral(_known(**over, cache_read_tokens=100_000, cache_write_tokens=10_000)) is None
    # Over threshold and writes 4× reads → spiral.
    d = dg.prefix_spiral(_known(**over, cache_read_tokens=10_000, cache_write_tokens=40_000))
    assert d is not None and d.severity == "high"
    assert d.evidence["writeReadRatio"] == 4.0
    # Writes with no reads at all is the degenerate spiral.
    d = dg.prefix_spiral(_known(**over, cache_read_tokens=0, cache_write_tokens=40_000))
    assert d is not None and d.evidence["writeReadRatio"] is None
    # Under threshold, the same ratio is not a spiral (it is a partial-miss question).
    assert dg.prefix_spiral(_known(peak_context_tokens=50_000, cache_read_tokens=10_000, cache_write_tokens=40_000)) is None


def test_partial_miss_heavy_is_a_share_of_known_cost():
    assert dg.partial_miss_heavy(_known(total_cost=10.0, partial_miss_usd=4.9)) is None
    d = dg.partial_miss_heavy(_known(total_cost=10.0, partial_miss_usd=5.0, partial_miss_count=7))
    assert d is not None and d.severity == "high"
    assert d.evidence["shareOfCost"] == 0.5 and d.evidence["partialMissCount"] == 7
    # Unknown cost cannot fire it — there is no denominator.
    assert dg.partial_miss_heavy(ProfileFacts(cost_known=False, partial_miss_usd=99.0)) is None


def test_summary_over_budget_fires_above_the_proposed_budget():
    assert dg.summary_over_budget(_known(summary_approx_tokens=dg.SUMMARY_TOKEN_BUDGET)) is None
    d = dg.summary_over_budget(_known(summary_approx_tokens=dg.SUMMARY_TOKEN_BUDGET + 1))
    assert d is not None and d.evidence["budgetTokens"] == dg.SUMMARY_TOKEN_BUDGET
    assert dg.summary_over_budget(_known(summary_approx_tokens=None)) is None


def test_anchor_mismatch_needs_both_coordinates():
    assert dg.anchor_mismatch(_known(checkpoint=3, truncation_anchor=None)) is None
    assert dg.anchor_mismatch(_known(checkpoint=3, truncation_anchor=3)) is None
    d = dg.anchor_mismatch(_known(checkpoint=34, truncation_anchor=68))
    assert d is not None and d.evidence == {"checkpoint": 34, "truncationAnchor": 68}


def test_prefix_mutation_rules_count_distinct_hashes_beyond_one():
    assert dg.system_prompt_mutated(_known(distinct_system_prompt_hashes=1)) is None
    assert dg.system_prompt_mutated(_known(distinct_system_prompt_hashes=2)) is not None
    assert dg.toolconfig_mutated(_known(distinct_tool_config_hashes=1)) is None
    assert dg.toolconfig_mutated(_known(distinct_tool_config_hashes=3)).evidence["distinctToolConfigHashes"] == 3


def test_agent_switch_churn_threshold():
    assert dg.agent_switch_churn(_known(agent_switch_count=2)) is None
    assert dg.agent_switch_churn(_known(agent_switch_count=3)).severity == "info"


def test_agent_cache_bypass_names_only_the_non_key_described_injected_ids(monkeypatch):
    # The rule's mechanics, pinned against fixed sets so the test does not
    # re-litigate which families are promoted today (that is
    # tests/shared/test_injected_tool_cache_eligibility.py's job).
    monkeypatch.setattr(dg, "INJECTED_TOOL_IDS", frozenset({"a_tool", "b_tool", "c_tool"}))
    monkeypatch.setattr(dg, "KEY_DESCRIBED_INJECTED_TOOL_IDS", frozenset({"a_tool"}))
    facts = _known(enabled_tools=["calculator", "a_tool", "c_tool", "b_tool"])
    d = dg.agent_cache_bypass(facts)
    assert d is not None
    assert d.evidence["bypassingToolIds"] == ["b_tool", "c_tool"]  # sorted, registry ids excluded
    assert dg.agent_cache_bypass(_known(enabled_tools=["calculator", "a_tool"])) is None


def test_agent_cache_bypass_tracks_the_live_promotion_state():
    # Every enabled_tools-gated injected family is promoted (spreadsheet
    # analysis last, once assistant_id joined the cache key), so the rule is
    # silent for any toolset today. It stays wired for the next family that
    # closes over something the key does not carry.
    promoted = ["create_artifact", "create_word_document", "create_excel_spreadsheet",
                "create_powerpoint_presentation", "workspace_files",
                "analyze_spreadsheet", "list_spreadsheets"]
    assert dg.agent_cache_bypass(_known(enabled_tools=["calculator", *promoted])) is None


def test_large_toolset_counts_catalog_ids():
    assert dg.large_toolset(_known(enabled_tools=[f"t{i}" for i in range(dg.LARGE_TOOLSET_MIN_IDS - 1)])) is None
    d = dg.large_toolset(_known(enabled_tools=[f"t{i}" for i in range(dg.LARGE_TOOLSET_MIN_IDS)]))
    assert d is not None and d.evidence["enabledToolCount"] == dg.LARGE_TOOLSET_MIN_IDS


def test_attachment_heavy_fires_on_count_or_bytes():
    assert dg.attachment_heavy(_known(attachment_count=4, attachment_bytes=1)) is None
    assert dg.attachment_heavy(_known(attachment_count=5)) is not None
    assert dg.attachment_heavy(_known(attachment_count=1, attachment_bytes=dg.ATTACHMENT_HEAVY_BYTES)) is not None


def test_dominant_session_share():
    assert dg.dominant_session(_known(share_of_user_period=49.9)) is None
    assert dg.dominant_session(_known(share_of_user_period=50.0)).evidence["shareOfUserPeriodPct"] == 50.0
    assert dg.dominant_session(_known(share_of_user_period=None)) is None


def test_tool_rules_stay_silent_when_the_census_was_never_recorded():
    assert dg.tool_error_rate(_known(tool_call_count=None, tool_error_count=None)) is None
    assert dg.tool_heavy(_known(tool_call_count=None)) is None


def test_tool_error_rate_needs_a_minimum_sample():
    assert dg.tool_error_rate(_known(tool_call_count=3, tool_error_count=3)) is None
    assert dg.tool_error_rate(_known(tool_call_count=4, tool_error_count=0)) is None
    d = dg.tool_error_rate(_known(tool_call_count=4, tool_error_count=1))
    assert d is not None and d.evidence["errorRate"] == 0.25


def test_tool_heavy_threshold():
    assert dg.tool_heavy(_known(tool_call_count=dg.TOOL_HEAVY_CALLS - 1)) is None
    assert dg.tool_heavy(_known(tool_call_count=dg.TOOL_HEAVY_CALLS)) is not None


# ── composition ──────────────────────────────────────────────────────────────


def test_run_diagnoses_orders_high_then_warn_then_info_then_code():
    facts = _known(
        peak_context_tokens=150_000,
        cache_read_tokens=10_000,
        cache_write_tokens=40_000,   # spiral (high) + over-threshold (warn)
        agent_switch_count=5,        # info
        share_of_user_period=90.0,   # info
    )
    findings = run_diagnoses(facts)
    severities = [d.severity for d in findings]
    assert severities == sorted(severities, key=lambda s: dg.SEVERITY_ORDER[s])
    infos = [d.code for d in findings if d.severity == "info"]
    assert infos == sorted(infos)
    assert dg.top_severity(findings) == "high"
    assert dg.top_severity([]) is None


def test_a_healthy_short_conversation_has_no_findings():
    assert run_diagnoses(_known(peak_context_tokens=20_000, enabled_tools=["calculator"])) == []


def test_to_dict_is_the_wire_shape():
    d = dg.cost_unknown(ProfileFacts())
    assert set(d.to_dict()) == {"code", "severity", "headline", "evidence", "suggestion", "ref"}
    assert d.to_dict()["evidence"] is not d.evidence  # a copy, not the frozen instance's dict
