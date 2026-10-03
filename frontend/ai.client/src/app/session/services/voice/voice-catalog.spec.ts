import { describe, expect, it } from 'vitest';
import {
  DEFAULT_VOICE_ID,
  NOVA_SONIC_VOICES,
  VOICE_GROUPS,
  findVoice,
  isVoiceId,
  voiceDescription,
} from './voice-catalog';

// Every voiceId the Nova 2 user guide lists for `audioOutputConfiguration`
// (sonic-input-events, verified 2026-09-30). The backend validates against
// the same list (`apis/shared/voice_catalog.py`); the two must agree.
const PUBLISHED_VOICE_IDS = [
  'matthew', 'tiffany', 'amy', 'olivia', 'lupe', 'carlos', 'ambre', 'florian',
  'lennart', 'beatrice', 'lorenzo', 'tina', 'carolina', 'leo', 'kiara', 'arjun',
].sort();

describe('voice catalog', () => {
  it('matches the published voice ids exactly', () => {
    expect(NOVA_SONIC_VOICES.map((v) => v.id).sort()).toEqual(PUBLISHED_VOICE_IDS);
  });

  it('defaults to a polyglot voice, so a language switch keeps the voice', () => {
    expect(findVoice(DEFAULT_VOICE_ID)?.polyglot).toBe(true);
    expect(NOVA_SONIC_VOICES.filter((v) => v.polyglot).map((v) => v.id).sort()).toEqual(['matthew', 'tiffany']);
  });

  it('groups by language in catalog order, English first', () => {
    expect(VOICE_GROUPS[0].language).toBe('English (US)');
    expect(VOICE_GROUPS[0].voices.map((v) => v.id)).toEqual(['tiffany', 'matthew']);
    // Every voice lands in exactly one group.
    const grouped = VOICE_GROUPS.flatMap((g) => g.voices.map((v) => v.id));
    expect(grouped.sort()).toEqual(PUBLISHED_VOICE_IDS);
  });

  it('looks voices up case- and whitespace-insensitively, and refuses the rest', () => {
    expect(findVoice(' Matthew ')?.id).toBe('matthew');
    expect(isVoiceId('AMY')).toBe(true);
    expect(findVoice('siri')).toBeUndefined();
    expect(isVoiceId('')).toBe(false);
    expect(isVoiceId(null)).toBe(false);
  });

  it('describes a polyglot voice as speaking every language', () => {
    expect(voiceDescription(findVoice('tiffany')!)).toBe('Feminine · Speaks every language');
    expect(voiceDescription(findVoice('leo')!)).toBe('Masculine');
  });
});
