import { describe, expect, it } from 'vitest';
import { encodeSkillFilePath, isNestedSkillFile, skillFilePath } from './skill-resource-path';

describe('skill file paths', () => {
  it('reads a top-level file, with or without a path, as its filename', () => {
    expect(skillFilePath({ filename: 'forms.md' })).toBe('forms.md');
    expect(skillFilePath({ filename: 'forms.md', path: 'forms.md' })).toBe('forms.md');
    expect(isNestedSkillFile({ filename: 'forms.md' })).toBe(false);
  });

  it('reads a nested file as its path', () => {
    const file = { filename: 'nda.md', path: 'legal/nda.md' };
    expect(skillFilePath(file)).toBe('legal/nda.md');
    expect(isNestedSkillFile(file)).toBe(true);
  });

  it('encodes each segment and keeps the slashes', () => {
    expect(encodeSkillFilePath('forms.md')).toBe(encodeURIComponent('forms.md'));
    expect(encodeSkillFilePath('legal/v1 2/nda.md')).toBe('legal/v1%202/nda.md');
  });
});
