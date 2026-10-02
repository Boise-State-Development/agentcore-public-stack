"""The Nova 2 Sonic voice catalog.

One list, shared by every consumer that has to agree on what a valid voice is:
the voice agent (which sends the id to Bedrock), the voice WebSocket route
(which accepts one from the SPA's config frame) and the user-settings route
(which persists a preference). Lives in ``apis.shared`` because ``app_api``
and ``agents`` may not import from each other.

Source: the Nova 2 user guide, "Language support and multilingual
capabilities" (verified 2026-09-30). Every language has a feminine-sounding
and, where Amazon ships one, a masculine-sounding voice. ``tiffany`` and
``matthew`` are the two **polyglot** voices: they speak every supported
language, which is why ``tiffany`` is the default — a user who switches
language mid-conversation keeps the same voice instead of getting a reply in
an accent the language does not have.

The ``gender`` field is *presentation*, not identity: some languages (Hindi,
Portuguese, French, Italian, Spanish) conjugate the speaker's own gender, and
Amazon's prompt guidance is to tell the model which form matches the voice so
"I am tired" comes out as ``cansada`` for ``carolina`` and ``cansado`` for
``leo``. The voice agent folds that into its system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Literal, Optional

VoiceGender = Literal["feminine", "masculine"]


@dataclass(frozen=True)
class NovaSonicVoice:
    """One selectable Nova 2 Sonic voice."""

    voice_id: str
    name: str
    language: str
    locale: str
    gender: VoiceGender
    polyglot: bool = False


NOVA_SONIC_VOICES: List[NovaSonicVoice] = [
    NovaSonicVoice("tiffany", "Tiffany", "English (US)", "en-US", "feminine", polyglot=True),
    NovaSonicVoice("matthew", "Matthew", "English (US)", "en-US", "masculine", polyglot=True),
    NovaSonicVoice("amy", "Amy", "English (UK)", "en-GB", "feminine"),
    NovaSonicVoice("olivia", "Olivia", "English (Australia)", "en-AU", "feminine"),
    NovaSonicVoice("kiara", "Kiara", "English (India) / Hindi", "en-IN", "feminine"),
    NovaSonicVoice("arjun", "Arjun", "English (India) / Hindi", "en-IN", "masculine"),
    NovaSonicVoice("ambre", "Ambre", "French", "fr-FR", "feminine"),
    NovaSonicVoice("florian", "Florian", "French", "fr-FR", "masculine"),
    NovaSonicVoice("beatrice", "Beatrice", "Italian", "it-IT", "feminine"),
    NovaSonicVoice("lorenzo", "Lorenzo", "Italian", "it-IT", "masculine"),
    NovaSonicVoice("tina", "Tina", "German", "de-DE", "feminine"),
    NovaSonicVoice("lennart", "Lennart", "German", "de-DE", "masculine"),
    NovaSonicVoice("lupe", "Lupe", "Spanish (US)", "es-US", "feminine"),
    NovaSonicVoice("carlos", "Carlos", "Spanish (US)", "es-US", "masculine"),
    NovaSonicVoice("carolina", "Carolina", "Portuguese (Brazil)", "pt-BR", "feminine"),
    NovaSonicVoice("leo", "Leo", "Portuguese (Brazil)", "pt-BR", "masculine"),
]

DEFAULT_VOICE_ID = "tiffany"

VOICES_BY_ID: Dict[str, NovaSonicVoice] = {v.voice_id: v for v in NOVA_SONIC_VOICES}
VOICE_IDS = frozenset(VOICES_BY_ID)


def get_voice(voice_id: Optional[str]) -> Optional[NovaSonicVoice]:
    """Look a voice up by id, case-insensitively. ``None`` for anything unknown."""
    if not voice_id or not isinstance(voice_id, str):
        return None
    return VOICES_BY_ID.get(voice_id.strip().lower())


def is_valid_voice_id(voice_id: Optional[str]) -> bool:
    """Whether ``voice_id`` names a voice Nova 2 Sonic will accept."""
    return get_voice(voice_id) is not None


def resolve_voice_id(requested: Optional[str], fallback: str = DEFAULT_VOICE_ID) -> str:
    """Normalise a requested voice id, falling back to a known-good one.

    A bad id must never reach ``promptStart``: Bedrock rejects the session and
    the user gets a dead voice button with no explanation. Unknown or blank
    input resolves to ``fallback``, which is itself validated so a misconfigured
    ``NOVA_SONIC_VOICE`` env var cannot smuggle a bad id in through the back.
    """
    voice = get_voice(requested)
    if voice is not None:
        return voice.voice_id
    fallback_voice = get_voice(fallback)
    return fallback_voice.voice_id if fallback_voice else DEFAULT_VOICE_ID
