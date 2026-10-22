"""Contract between scripts/memory-audit/audit.py and the runtime's retrieval hook.

The audit compares AgentCore's namespace templates with the namespaces the
backend queries, and replays retrieval with the backend's top-k and relevance
cut. It carries its own copies of those values (it must not import the backend),
so this suite fails when the backend changes and the audit is not updated.
Also covers the pure helpers that turn raw ids into aggregate-only output.
"""

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT = REPO_ROOT / "scripts" / "memory-audit" / "audit.py"
SESSION_FACTORY = REPO_ROOT / "backend" / "src" / "agents" / "main_agent" / "session" / "session_factory.py"
CONSTANTS = REPO_ROOT / "backend" / "src" / "agents" / "main_agent" / "config" / "constants.py"


def _load_audit():
    spec = importlib.util.spec_from_file_location("memory_audit", AUDIT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = _load_audit()


def test_backend_namespace_templates_match_session_factory():
    source = SESSION_FACTORY.read_text()
    # session_factory writes f"/strategies/{preference_id}/actors/{{actorId}}" etc.
    written = {
        re.sub(r"\{\w+_id\}", "{memoryStrategyId}", m.replace("{{", "{").replace("}}", "}"))
        for m in re.findall(r'f"(/strategies/[^"]+)"', source)
    }
    assert set(audit.BACKEND_NAMESPACE_TEMPLATES.values()) == written


def test_retrieval_parameters_match_backend_defaults():
    source = CONSTANTS.read_text()
    relevance = float(re.search(r"MEMORY_RELEVANCE_SCORE = ([0-9.]+)", source).group(1))
    top_k = int(re.search(r"MEMORY_TOP_K = (\d+)", source).group(1))
    assert audit.RETRIEVAL_RELEVANCE == relevance
    assert audit.RETRIEVAL_TOP_K == top_k


def test_classify_actor_detects_the_session_id_fallback():
    sid = "5b1f3c2e-8f7a-4d2b-9c1e-000000000001"
    assert audit.classify_actor(sid, [sid]) == "session_fallback"
    assert audit.classify_actor("a1b2c3d4-0000-7000-8000-000000000001", ["other"]) == "uuid_v7"
    assert audit.classify_actor("preview-abc", []) == "preview"
    assert audit.classify_actor("local-dev", ["s1"]) == "other"


def test_namespace_shape_hides_ids_and_keeps_structure():
    types = {"strat-sem": "SEMANTIC", "strat-sum": "SUMMARIZATION"}
    assert (
        audit.namespace_shape("/strategies/strat-sem/actors/a1b2c3d4-0000-7000-8000-000000000001/", types)
        == "/strategies/{SEMANTIC}/actors/{actorId}/"
    )
    assert (
        audit.namespace_shape("/strategies/strat-sum/actors/u/sessions/s", types)
        == "/strategies/{SUMMARIZATION}/actors/{actorId}/sessions/{sessionId}"
    )
    assert audit.namespace_shape("/strategies/zzz/actors/u", types) == "/strategies/{unknownStrategy}/actors/{actorId}"


def test_histogram_buckets():
    assert audit.histogram([0, 1, 3, 7, 300]) == {"0": 1, "1": 1, "2-4": 1, "5-9": 1, ">250": 1}


def test_score_log_pattern_matches_the_runtime_line():
    source = (REPO_ROOT / "backend" / "src" / "agents" / "main_agent" / "session"
              / "turn_based_session_manager.py").read_text()
    assert audit.LOG_PATTERNS["retrieval_scores"].strip('"') in source
    assert "top=%s returned=%d kept=%d cut=%s" in source


def test_score_line_stats_histograms_top_score_by_strategy_type():
    prefix = "2026-09-25 04:03:56,279 INFO [agents.main_agent.session.turn_based_session_manager] - "
    lines = [
        prefix + "memory retrieval scores namespace=/strategies/sem-1/actors/{actorId} top=0.431 returned=3 kept=0 cut=0.5",
        prefix + "memory retrieval scores namespace=/strategies/sem-1/actors/{actorId} top=0.671 returned=4 kept=1 cut=0.5",
        prefix + "memory retrieval scores namespace=/strategies/pref-1/actors/{actorId} top=none returned=0 kept=0 cut=0.5",
        prefix + "memory retrieval scores namespace=/strategies/pref-1/actors/{actorId} top=1.000 returned=1 kept=1 cut=0.5",
        prefix + "Retrieved 1 customer context items",
    ]
    stats = audit.score_line_stats(lines, {"sem-1": "SEMANTIC", "pref-1": "USER_PREFERENCE"})
    assert stats == {
        "SEMANTIC": {"lines": 2, "returnedNone": 0, "keptAny": 1, "cuts": {"0.5": 2},
                     "topScoreHistogram": {"0.40-0.45": 1, "0.65-0.70": 1}},
        "USER_PREFERENCE": {"lines": 2, "returnedNone": 1, "keptAny": 1, "cuts": {"0.5": 2},
                            "topScoreHistogram": {"0.95-1.00": 1}},
    }
