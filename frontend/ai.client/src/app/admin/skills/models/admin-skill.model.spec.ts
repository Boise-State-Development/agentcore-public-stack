import { describe, expect, it } from 'vitest';
import { SKILL_ID_PATTERN, slugifySkillId } from './admin-skill.model';

describe('slugifySkillId', () => {
  it.each([
    ['pdf_workflows', 'pdf_workflows'],
    ['pdf-tools', 'pdf_tools'],
    ['PDF  Tools!!', 'pdf_tools'],
    ['123-skill', 's_123_skill'],
    ['--edge--', 'edge'],
  ])('%s → %s', (name, id) => expect(slugifySkillId(name)).toBe(id));

  it('clamps to 50 characters and still matches the pattern', () => {
    const slug = slugifySkillId('a'.repeat(60));
    expect(slug.length).toBe(50);
    expect(SKILL_ID_PATTERN.test(slug)).toBe(true);
  });
});
