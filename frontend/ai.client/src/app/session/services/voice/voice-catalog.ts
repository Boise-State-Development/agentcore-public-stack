/**
 * The Nova 2 Sonic voice catalog.
 *
 * Mirrors `apis/shared/voice_catalog.py` — the backend validates whatever id
 * the SPA sends against the same list, so the two must agree. Source: the
 * Nova 2 user guide, "Language support and multilingual capabilities"
 * (verified 2026-09-30).
 *
 * `tiffany` and `matthew` are the polyglot voices: they speak every supported
 * language, which is why `tiffany` is the default — a user who switches
 * language mid-conversation keeps the same voice. Every other voice is tuned
 * to one language and answers in that accent whatever the user speaks.
 */

export type VoiceGender = 'feminine' | 'masculine';

export interface NovaSonicVoice {
  /** The `voiceId` Bedrock accepts in `promptStart`. */
  readonly id: string;
  /** Display name. */
  readonly name: string;
  /** The language the voice is tuned to. */
  readonly language: string;
  /** BCP-47 locale, for the group header. */
  readonly locale: string;
  /** How the voice sounds; presentation only. */
  readonly gender: VoiceGender;
  /** Speaks every supported language, not just its own. */
  readonly polyglot?: boolean;
}

export const DEFAULT_VOICE_ID = 'tiffany';

export const NOVA_SONIC_VOICES: readonly NovaSonicVoice[] = [
  { id: 'tiffany', name: 'Tiffany', language: 'English (US)', locale: 'en-US', gender: 'feminine', polyglot: true },
  { id: 'matthew', name: 'Matthew', language: 'English (US)', locale: 'en-US', gender: 'masculine', polyglot: true },
  { id: 'amy', name: 'Amy', language: 'English (UK)', locale: 'en-GB', gender: 'feminine' },
  { id: 'olivia', name: 'Olivia', language: 'English (Australia)', locale: 'en-AU', gender: 'feminine' },
  { id: 'kiara', name: 'Kiara', language: 'English (India) / Hindi', locale: 'en-IN', gender: 'feminine' },
  { id: 'arjun', name: 'Arjun', language: 'English (India) / Hindi', locale: 'en-IN', gender: 'masculine' },
  { id: 'ambre', name: 'Ambre', language: 'French', locale: 'fr-FR', gender: 'feminine' },
  { id: 'florian', name: 'Florian', language: 'French', locale: 'fr-FR', gender: 'masculine' },
  { id: 'beatrice', name: 'Beatrice', language: 'Italian', locale: 'it-IT', gender: 'feminine' },
  { id: 'lorenzo', name: 'Lorenzo', language: 'Italian', locale: 'it-IT', gender: 'masculine' },
  { id: 'tina', name: 'Tina', language: 'German', locale: 'de-DE', gender: 'feminine' },
  { id: 'lennart', name: 'Lennart', language: 'German', locale: 'de-DE', gender: 'masculine' },
  { id: 'lupe', name: 'Lupe', language: 'Spanish (US)', locale: 'es-US', gender: 'feminine' },
  { id: 'carlos', name: 'Carlos', language: 'Spanish (US)', locale: 'es-US', gender: 'masculine' },
  { id: 'carolina', name: 'Carolina', language: 'Portuguese (Brazil)', locale: 'pt-BR', gender: 'feminine' },
  { id: 'leo', name: 'Leo', language: 'Portuguese (Brazil)', locale: 'pt-BR', gender: 'masculine' },
];

/** One language's voices, in catalog order, for a grouped picker. */
export interface VoiceGroup {
  readonly language: string;
  readonly voices: readonly NovaSonicVoice[];
}

/** The catalog grouped by language, in catalog order (English first). */
export const VOICE_GROUPS: readonly VoiceGroup[] = (() => {
  const groups: VoiceGroup[] = [];
  for (const voice of NOVA_SONIC_VOICES) {
    const group = groups.find((g) => g.language === voice.language);
    if (group) {
      (group.voices as NovaSonicVoice[]).push(voice);
    } else {
      groups.push({ language: voice.language, voices: [voice] });
    }
  }
  return groups;
})();

/** Look a voice up by id; `undefined` for anything not in the catalog. */
export function findVoice(id: string | null | undefined): NovaSonicVoice | undefined {
  if (!id) return undefined;
  const key = id.trim().toLowerCase();
  return NOVA_SONIC_VOICES.find((v) => v.id === key);
}

/** Whether `id` names a voice Bedrock will accept. */
export function isVoiceId(id: string | null | undefined): boolean {
  return findVoice(id) !== undefined;
}

/** A short line for the picker: "Feminine · Speaks every language" / "Masculine". */
export function voiceDescription(voice: NovaSonicVoice): string {
  const gender = voice.gender === 'feminine' ? 'Feminine' : 'Masculine';
  return voice.polyglot ? `${gender} · Speaks every language` : gender;
}
