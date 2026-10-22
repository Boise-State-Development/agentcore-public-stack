"""`scripts/memory-audit/audit.py calibrate`: the eval set, the labelling and
the analysis that turn retrieval scores into recall/precision per policy.

Pure helpers only; the subcommand itself writes to a deployed memory.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT = REPO_ROOT / "scripts" / "memory-audit" / "audit.py"


def _load_audit():
    spec = importlib.util.spec_from_file_location("memory_audit_calibration", AUDIT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = _load_audit()
CALIBRATION = json.loads(audit.CALIBRATION_SET.read_text())

CALIBRATION = json.loads(audit.CALIBRATION_SET.read_text())


def test_calibration_set_is_well_formed():
    facts = {f["id"]: f for f in CALIBRATION["facts"]}
    assert len(facts) == len(CALIBRATION["facts"]) >= 15
    assert sorted(f for group in CALIBRATION["sessions"] for f in group) == sorted(facts)
    assert len(CALIBRATION["negatives"]) >= 20
    for f in facts.values():
        assert set(f["queries"]) == {"direct", "indirect", "filler"}
        assert f["keys"] and set(f["namespaces"]) <= set(audit.RETRIEVED_TYPES)


def test_fact_keys_do_not_label_other_facts():
    """A key that matches another fact's statement mislabels that fact's
    records. Merge candidates name the fact they extend, on purpose."""
    facts = CALIBRATION["facts"]
    for f in facts:
        assert audit.record_matches(f["say"], f["keys"]), f["id"]
        for g in facts:
            if g is not f and g["shape"] != "merge_candidate":
                assert not audit.record_matches(g["say"], f["keys"]), (f["id"], g["id"])
    pasted = CALIBRATION["fillers"]["pasted"]
    for q in CALIBRATION["negatives"] + [pasted]:
        assert not any(audit.record_matches(q, f["keys"]) for f in facts), q


@pytest.mark.parametrize("text,expected", [
    ("Different question: what is my capstone research project about?",
     "What is my capstone research project about?"),
    ("ok thanks, that helps a lot. hmm, one more thing before I forget: who is my advisor?", "Who is my advisor?"),
    ("cool, thanks! also, random, what does my sister do?", "What does my sister do?"),
    ("Lecture notes here.\nSecond line.\n\nAnyway, where did I say I work?", "Where did I say I work?"),
    ("Solar panels: how do they work?", "Solar panels: how do they work?"),
    ("Okra recipes please", "Okra recipes please"),
    ("I want to send my sister a gift. Any ideas?", "I want to send my sister a gift. Any ideas?"),
    ("ok", "ok"),
])
def test_strip_query_filler(text, expected):
    assert audit.strip_query_filler(text) == expected


def test_record_matches_at_word_start_only():
    assert audit.record_matches("Prefers Rust over Python", ["rust"])
    assert not audit.record_matches("The user trusts the process", ["rust"])
    assert audit.record_matches("works at the bike co-op", ["co-op"])


def _row(query, fact, ns, scores, variant="raw", style="direct"):
    """``scores``: (score, correct) pairs, any order."""
    hits = [{"score": s, "correct": c, "chars": 100} for s, c in sorted(scores, key=lambda x: -x[0])]
    return {"query": query, "fact": fact, "style": style if fact else "negative", "variant": variant,
            "namespace": ns, "hits": hits}


def test_analyze_calibration_policy_metrics():
    rows = [
        # Fact a: right record ranks first but under 0.5 (the 2026-09-25 case).
        _row("a", "a", "SEMANTIC", [(0.43, True), (0.38, False), (0.30, False)]),
        _row("a", "a", "USER_PREFERENCE", [(0.49, True), (0.40, False)]),
        # Fact b: clears 0.5 comfortably.
        _row("b", "b", "SEMANTIC", [(0.66, True), (0.39, False)], style="indirect"),
        _row("b", "b", "USER_PREFERENCE", [], style="indirect"),
        # A negative whose best unrelated record scores 0.41.
        _row("n", None, "SEMANTIC", [(0.41, False), (0.33, False)]),
        _row("n", None, "USER_PREFERENCE", [(0.36, False)]),
    ]
    out = audit.analyze_calibration(rows)
    p = out["policies"]["raw"]
    assert out["turns"] == {"fact": 2, "negative": 1}

    assert p["cut>=0.50"]["recall"] == 0.5
    assert p["cut>=0.50"]["precision"] == 1.0
    assert p["cut>=0.50"]["negativeTurnsWithInjection"] == 0.0
    assert p["cut>=0.50"]["recallByStyle"] == {"direct": 0.0, "indirect": 1.0}

    assert p["cut>=0.40"]["recall"] == 1.0
    assert p["cut>=0.40"]["wrongItemsPerFactTurn"] == 0.5  # the 0.40 preference runner-up
    assert p["cut>=0.40"]["negativeTurnsWithInjection"] == 1.0
    assert p["cut>=0.40"]["itemsPerNegativeTurn"] == 1.0

    # Margin: fact a's semantic top beats its runner-up by 0.05, preference by 0.09.
    assert p["margin0.05>=0.40|>=0.50"]["recall"] == 1.0
    assert p["margin0.05>=0.40|>=0.50"]["wrongItemsPerFactTurn"] == 0.0
    # The negative's 0.41 beats its runner-up by 0.08: margin alone cannot reject it.
    assert p["margin0.05>=0.40|>=0.50"]["negativeTurnsWithInjection"] == 1.0

    d = out["scoreDistributions"]["raw"]["SEMANTIC"]["direct"]
    assert d["bestCorrect"]["max"] == 0.43 and d["bestIncorrect"]["max"] == 0.38
    assert d["rank1OnTopic"] == 1 and d["queriesWithCorrect"] == 1


def test_margin_policy_keeps_a_lone_hit_and_everything_above_high():
    policy = audit._margin(0.40, 0.05, 0.50)
    lone = [{"score": 0.42}]
    assert policy(lone) == lone
    close = [{"score": 0.45}, {"score": 0.43}]
    assert policy(close) == []
    high = [{"score": 0.62}, {"score": 0.60}, {"score": 0.44}]
    assert policy(high) == high[:2]


def test_related_records_are_neither_matches_nor_noise():
    rows = [
        _row("a", "a", "SEMANTIC", [(0.46, True), (0.45, False), (0.36, False)]),
        _row("n", None, "SEMANTIC", [(0.37, False)]),
    ]
    rows[0]["hits"][1]["related"] = True  # e.g. the capstone update for a capstone question
    out = audit.analyze_calibration(rows)
    p = out["policies"]["raw"]["cut>=0.40"]
    assert p["recall"] == 1.0 and p["precision"] == 1.0 and p["wrongItemsPerFactTurn"] == 0.0
    d = out["scoreDistributions"]["raw"]["SEMANTIC"]["direct"]
    assert d["bestIncorrect"]["max"] == 0.36 and d["related"] == 1


def test_label_marks_related_facts():
    facts = {"a": {"keys": ["alpha"], "related": ["b"], "relatedKeys": ["topic"]}, "b": {"keys": ["beta"]}}
    rows = [{"fact": "a", "hits": [{"text": "alpha"}, {"text": "beta"}, {"text": "gamma"}, {"text": "on topic"}]},
            {"fact": None, "hits": [{"text": "beta"}]}]
    audit.Audit._label(rows, facts)
    assert [(h["correct"], h["related"]) for h in rows[0]["hits"]] == [(True, False), (False, True), (False, False), (False, True)]
    assert (rows[1]["hits"][0]["correct"], rows[1]["hits"][0]["related"]) == (False, False)
