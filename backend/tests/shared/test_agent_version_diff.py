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
        "responseStrictness": "balanced",
        "voiceEnabled": False,
        "voiceInstructionsOnly": True,
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
        "response_strictness": "strict",
        "voice_enabled": True,
        "voice_instructions_only": False,
        "name": "Renamed",
        "description": "A different summary",
        "tagline": "A different subtitle",
        "starters": ["A different starter"],
        "emoji": "🧭",
        "icon_key": "icons/other.png",
        "category": "Teaching",
        "publisher_id": "pub-other",
    }
    alias = {
        "model_settings": "modelConfig",
        "icon_key": "iconKey",
        "publisher_id": "publisherId",
        "response_strictness": "responseStrictness",
        "voice_enabled": "voiceEnabled",
        "voice_instructions_only": "voiceInstructionsOnly",
    }
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
        ("responseStrictness", "flexible"),
        ("voiceEnabled", True),
        ("voiceInstructionsOnly", False),
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


# ── response strictness (#112) ───────────────────────────────────────────────────────
def test_a_strictness_change_is_reported_as_a_behavior_change():
    after = version(responseStrictness="flexible")
    assert [name for name, _b, _a in changed_fields(version(), after)] == ["response_strictness"]
    assert behavior_changed(version(), after) is True


def test_strictness_snapshot_and_apply_round_trip():
    from apis.shared.assistants.models import Assistant
    from apis.shared.assistants.versions import apply_version, snapshot_of

    live = Assistant(
        assistantId="ast-001", ownerId="u1", ownerName="U", name="n", description="d",
        instructions="i", vectorIndexId="v", visibility="PRIVATE", status="COMPLETE",
        createdAt="2026-01-01T00:00:00Z", updatedAt="2026-01-01T00:00:00Z",
        responseStrictness="strict",
    )
    snap = snapshot_of(live, created_at="2026-01-01T00:00:00Z", created_by="u1")
    assert snap.response_strictness == "strict"

    live.response_strictness = "flexible"
    assert apply_version(live, snap).response_strictness == "strict"


def test_a_version_without_strictness_runs_as_balanced():
    """A version approved before #112 was approved as balanced; the live value must not leak through."""
    from apis.shared.assistants.models import Assistant
    from apis.shared.assistants.versions import apply_version

    live = Assistant(
        assistantId="ast-001", ownerId="u1", ownerName="U", name="n", description="d",
        instructions="i", vectorIndexId="v", visibility="PRIVATE", status="COMPLETE",
        createdAt="2026-01-01T00:00:00Z", updatedAt="2026-01-01T00:00:00Z",
        responseStrictness="strict",
    )
    old = AgentVersion(agentId="ast-001", name="n", description="d", instructions="i")
    assert apply_version(live, old).response_strictness == "balanced"


def test_an_unchanged_resubmission_of_a_legacy_version_reports_no_strictness_change():
    legacy = version()
    legacy_dump = legacy.model_dump(by_alias=True)
    legacy_dump.pop("responseStrictness")
    legacy = AgentVersion(**legacy_dump)
    assert changed_fields(legacy, version()) == []
    assert behavior_changed(legacy, version()) is False


def test_the_listing_edit_model_rejects_strictness():
    from pydantic import ValidationError

    from apis.shared.assistants.models import AdminListingPatchRequest

    with pytest.raises(ValidationError):
        AdminListingPatchRequest(responseStrictness="flexible")


# ── voice mode (#625) ────────────────────────────────────────────────────────────────
def _live(**fields):
    from apis.shared.assistants.models import Assistant

    return Assistant(
        assistantId="ast-001", ownerId="u1", ownerName="U", name="n", description="d",
        instructions="i", vectorIndexId="v", visibility="PRIVATE", status="COMPLETE",
        createdAt="2026-01-01T00:00:00Z", updatedAt="2026-01-01T00:00:00Z", **fields,
    )


def test_voice_settings_snapshot_and_apply_round_trip():
    from apis.shared.assistants.versions import apply_version, snapshot_of

    live = _live(voiceEnabled=True, voiceInstructionsOnly=False)
    snap = snapshot_of(live, created_at="2026-01-01T00:00:00Z", created_by="u1")
    assert snap.voice_enabled is True
    assert snap.voice_instructions_only is False

    live.voice_enabled = False
    live.voice_instructions_only = True
    ran = apply_version(live, snap)
    assert ran.voice_enabled is True
    assert ran.voice_instructions_only is False


def test_a_version_from_before_voice_runs_with_voice_off():
    """Voice never ran an Agent before #625, so an owner cannot turn it on for a
    published Agent without review by editing the live record."""
    from apis.shared.assistants.versions import apply_version

    live = _live(voiceEnabled=True, voiceInstructionsOnly=False)
    old = AgentVersion(agentId="ast-001", name="n", description="d", instructions="i")
    ran = apply_version(live, old)
    assert ran.voice_enabled is False
    assert ran.voice_instructions_only is True


def test_an_unchanged_resubmission_of_a_pre_voice_version_reports_no_change():
    legacy_dump = version().model_dump(by_alias=True)
    legacy_dump.pop("voiceEnabled")
    legacy_dump.pop("voiceInstructionsOnly")
    legacy = AgentVersion(**legacy_dump)
    assert changed_fields(legacy, version()) == []
    assert behavior_changed(legacy, version()) is False


def test_the_listing_edit_model_rejects_voice_settings():
    from pydantic import ValidationError

    from apis.shared.assistants.models import AdminListingPatchRequest

    with pytest.raises(ValidationError):
        AdminListingPatchRequest(voiceEnabled=True)
