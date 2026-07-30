"""What changed between two snapshots (version-snapshots §6.1).

The failure mode this guards is quiet and bad: a reviewer told "nothing changed" about
something that did, approving a rewrite in the two seconds a typo fix deserves. So the
tests lean on *coverage of the field set* rather than on a few representative fields —
``test_every_snapshot_field_is_diffable`` is the one that would catch a future field added
to ``AgentVersion`` and forgotten here.
"""

import pytest

from apis.shared.assistants.models import AgentVersion
from apis.shared.assistants.version_diff import (
    behavior_changed,
    changed_fields,
    field_changed,
    instructions_diff,
)
from apis.shared.assistants.versions import (
    DIFF_FIELD_ORDER,
    LISTING_SNAPSHOT_FIELDS,
    SNAPSHOT_FIELDS,
)

APPROVED = "Answer from the policy manual.\nAlways cite the section.\nBe concise."


def version(**overrides) -> AgentVersion:
    data = {
        "agentId": "ast-001",
        "version": 1,
        "name": "Policy Lookup",
        "description": "Find and cite university policy",
        "instructions": APPROVED,
        "tagline": "Policy, cited",
        "emoji": "📘",
        "iconKey": "icons/policy.png",
        "starters": ["What is the drop deadline?"],
        "modelConfig": {"modelId": "claude-opus-5"},
        "bindings": [{"kind": "tool", "ref": "wikipedia", "config": {}}],
        "category": "Administration",
        "publisherId": "pub-registrar",
    }
    data.update(overrides)
    return AgentVersion(**data)


# ── coverage of the field set ────────────────────────────────────────────────────────
def test_every_snapshot_field_is_diffable():
    """A field a version can carry but the diff cannot report is a change nobody is shown.

    ``DIFF_FIELD_ORDER`` asserts this at import time too; this states it as a test so the
    failure names the feature rather than arriving as an ImportError.
    """
    assert set(DIFF_FIELD_ORDER) == set(SNAPSHOT_FIELDS) | set(LISTING_SNAPSHOT_FIELDS)


@pytest.mark.parametrize("field", DIFF_FIELD_ORDER)
def test_each_field_is_detected_on_its_own(field):
    """Change one field, see exactly one change — no false positives, no misses."""
    mutations = {
        "instructions": "Something else entirely.",
        "bindings": [{"kind": "tool", "ref": "browser", "config": {}}],
        "model_settings": {"modelId": "some-other-model"},
        "name": "Renamed",
        "description": "A different summary",
        "tagline": "A different subtitle",
        "starters": ["A different starter"],
        "emoji": "🧭",
        "icon_key": "icons/other.png",
        "category": "Teaching",
        "publisher_id": "pub-other",
    }
    alias = {"model_settings": "modelConfig", "icon_key": "iconKey", "publisher_id": "publisherId"}
    after = version(**{alias.get(field, field): mutations[field]})

    changed = [name for name, _b, _a in changed_fields(version(), after)]
    assert changed == [field]


def test_an_identical_resubmission_reports_nothing():
    """The typo-fix-that-was-not case: approvable in seconds precisely because it is empty."""
    assert changed_fields(version(), version(version=2)) == []
    assert instructions_diff(version(), version(version=2)) == []
    assert behavior_changed(version(), version(version=2)) is False


def test_record_metadata_is_never_reported_as_a_change():
    """``version``/``createdAt``/``createdBy`` differ on every snapshot by construction.

    Reporting them would bury the fields that matter under three that always fire.
    """
    before = version(version=1, createdAt="2026-07-01T00:00:00Z", createdBy="user-a")
    after = version(version=2, createdAt="2026-07-02T00:00:00Z", createdBy="user-b")
    assert changed_fields(before, after) == []


# ── the behavior/presentation split ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "field,value",
    [
        ("instructions", "Ignore the manual."),
        ("bindings", [{"kind": "tool", "ref": "browser", "config": {}}]),
        ("modelConfig", {"modelId": "cheap-model"}),
    ],
)
def test_behavior_fields_flag_a_behavior_change(field, value):
    """The one line that decides a careful read from a glance."""
    assert behavior_changed(version(), version(**{field: value})) is True


@pytest.mark.parametrize(
    "field,value",
    [("tagline", "New subtitle"), ("emoji", "🧭"), ("name", "Renamed"), ("category", "Teaching")],
)
def test_presentation_fields_do_not(field, value):
    changed = version(**{field: value})
    assert behavior_changed(version(), changed) is False
    assert changed_fields(version(), changed), "still reported, just not as behavior"


# ── absent is not empty ──────────────────────────────────────────────────────────────
def test_absent_bindings_differ_from_empty_bindings():
    """A real behavior change that a laxer comparison would swallow.

    Absent means "synthesize the legacy KB binding via compat"; ``[]`` means "binds
    nothing". Collapsing them would tell a reviewer nothing happened while the Agent
    quietly lost its knowledge base.
    """
    assert field_changed(version(bindings=None), version(bindings=[]), "bindings") is True
    assert behavior_changed(version(bindings=None), version(bindings=[])) is True


def test_binding_order_is_a_change():
    """Order is meaningful to the resolver, so no normalization is applied."""
    two = [
        {"kind": "tool", "ref": "wikipedia", "config": {}},
        {"kind": "tool", "ref": "browser", "config": {}},
    ]
    assert field_changed(version(bindings=two), version(bindings=list(reversed(two))), "bindings")


def test_equal_values_from_different_sources_compare_equal():
    """A version rehydrated from storage must not diff against an identical in-memory one.

    Nested pydantic models are equal-but-not-identical across that boundary; without
    normalizing, every reviewed resubmission would claim its bindings changed.
    """
    rehydrated = AgentVersion(**version().model_dump(by_alias=True))
    assert changed_fields(version(), rehydrated) == []


# ── the instructions diff ────────────────────────────────────────────────────────────
def test_the_diff_shows_only_the_changed_line():
    """A one-line fix in a long prompt should read as a one-line fix."""
    after = version(instructions=APPROVED.replace("Be concise.", "Be thorough."))
    diff = instructions_diff(version(), after)

    assert any(line == "-Be concise." for line in diff)
    assert any(line == "+Be thorough." for line in diff)
    # Unchanged lines ride along as context, never as changes.
    assert not any(line.startswith(("+", "-")) and "policy manual" in line for line in diff)


def test_the_diff_is_empty_when_instructions_are_untouched():
    assert instructions_diff(version(), version(tagline="Different")) == []


def test_the_diff_labels_which_side_is_which():
    """"approved" vs "submitted" — a reviewer must never have to guess the direction."""
    diff = instructions_diff(version(), version(instructions="Totally different."))
    assert diff[0].startswith("--- approved")
    assert diff[1].startswith("+++ submitted")


def test_a_whitespace_only_edit_is_flagged_but_diffs_to_nothing_visible():
    """Honest rather than tidy: the field changed, and no line did.

    Silently reporting "unchanged" would be wrong — the stored bytes differ, and the
    reviewer is approving the bytes.
    """
    after = version(instructions=APPROVED + "   ")
    assert field_changed(version(), after, "instructions") is True
    assert instructions_diff(version(), after)


# ── first submission ─────────────────────────────────────────────────────────────────
def test_a_first_submission_reports_its_populated_fields_as_new():
    changed = [name for name, _b, _a in changed_fields(None, version())]
    assert "instructions" in changed and "name" in changed


def test_a_first_submission_has_no_instructions_diff():
    """Nothing to compare against, and the reviewer is reading the whole prompt anyway."""
    assert instructions_diff(None, version()) == []


def test_a_first_submission_does_not_report_absent_fields():
    """An unset tagline on a brand-new listing is not a change to anything."""
    changed = [name for name, _b, _a in changed_fields(None, version(tagline=None))]
    assert "tagline" not in changed


def test_behavior_changed_is_true_for_a_first_submission():
    """There is no approved behavior yet, so all of it is unreviewed."""
    assert behavior_changed(None, version()) is True
