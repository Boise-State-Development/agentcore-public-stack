"""The Nova 2 Sonic voice catalog — the one list every voice consumer agrees on."""

from apis.shared.voice_catalog import (
    DEFAULT_VOICE_ID,
    NOVA_SONIC_VOICES,
    VOICE_IDS,
    get_voice,
    is_valid_voice_id,
    resolve_voice_id,
)

# Every voiceId the Nova 2 user guide lists for `audioOutputConfiguration`
# (sonic-input-events, verified 2026-09-30). Sending anything else fails the
# session at promptStart.
PUBLISHED_VOICE_IDS = {
    "matthew", "tiffany", "amy", "olivia", "lupe", "carlos", "ambre", "florian",
    "lennart", "beatrice", "lorenzo", "tina", "carolina", "leo", "kiara", "arjun",
}


def test_catalog_matches_the_published_voice_ids_exactly():
    assert VOICE_IDS == PUBLISHED_VOICE_IDS


def test_ids_are_unique():
    ids = [v.voice_id for v in NOVA_SONIC_VOICES]
    assert len(ids) == len(set(ids))


def test_default_is_a_polyglot_voice():
    # The default has to survive a user switching language mid-conversation,
    # which only the two polyglot voices do.
    assert get_voice(DEFAULT_VOICE_ID).polyglot is True


def test_exactly_two_polyglot_voices():
    assert {v.voice_id for v in NOVA_SONIC_VOICES if v.polyglot} == {"tiffany", "matthew"}


def test_every_voice_declares_a_presentation_gender():
    assert {v.gender for v in NOVA_SONIC_VOICES} == {"feminine", "masculine"}


def test_lookup_is_case_and_whitespace_insensitive():
    assert get_voice("  Matthew ").voice_id == "matthew"
    assert is_valid_voice_id("AMY")


def test_unknown_blank_and_non_string_are_invalid():
    assert get_voice("siri") is None
    assert get_voice("") is None
    assert get_voice(None) is None
    assert get_voice(42) is None  # type: ignore[arg-type]
    assert not is_valid_voice_id("siri")


def test_resolve_returns_the_catalog_id_for_a_valid_request():
    assert resolve_voice_id("Carlos") == "carlos"


def test_resolve_falls_back_for_an_unknown_request():
    assert resolve_voice_id("siri") == DEFAULT_VOICE_ID
    assert resolve_voice_id(None) == DEFAULT_VOICE_ID
    assert resolve_voice_id("siri", fallback="matthew") == "matthew"


def test_resolve_never_trusts_a_bad_fallback():
    # A misconfigured NOVA_SONIC_VOICE env var must not reach promptStart.
    assert resolve_voice_id("siri", fallback="alexa") == DEFAULT_VOICE_ID
