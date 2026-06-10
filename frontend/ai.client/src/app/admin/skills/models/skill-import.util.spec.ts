import { describe, it, expect } from 'vitest';
import {
  parseSkillMarkdown,
  slugifySkillId,
  isValidSkillId,
} from './skill-import.util';

describe('parseSkillMarkdown', () => {
  it('extracts name, description and body from frontmatter', () => {
    const md = [
      '---',
      'name: pdf',
      'description: Use this skill whenever a PDF is involved.',
      '---',
      '',
      '# PDF Workflows',
      '',
      'Use the bound tools to manipulate PDFs.',
    ].join('\n');

    const parsed = parseSkillMarkdown(md);
    expect(parsed.name).toBe('pdf');
    expect(parsed.description).toBe('Use this skill whenever a PDF is involved.');
    expect(parsed.instructions).toBe(
      '# PDF Workflows\n\nUse the bound tools to manipulate PDFs.'
    );
  });

  it('strips surrounding quotes from frontmatter values', () => {
    const md = '---\nname: "docx"\ndescription: \'Word docs\'\n---\nBody';
    const parsed = parseSkillMarkdown(md);
    expect(parsed.name).toBe('docx');
    expect(parsed.description).toBe('Word docs');
  });

  it('treats the whole text as instructions when there is no frontmatter', () => {
    const md = '# Just a body\n\nNo frontmatter here.';
    const parsed = parseSkillMarkdown(md);
    expect(parsed.name).toBe('');
    expect(parsed.description).toBe('');
    expect(parsed.instructions).toBe('# Just a body\n\nNo frontmatter here.');
  });

  it('normalizes CRLF line endings', () => {
    const md = '---\r\nname: x\r\n---\r\n\r\nbody line';
    const parsed = parseSkillMarkdown(md);
    expect(parsed.name).toBe('x');
    expect(parsed.instructions).toBe('body line');
  });

  it('ignores unknown frontmatter keys (tools/scripts not imported)', () => {
    const md =
      '---\nname: pdf\nallowed-tools: Bash, Read\nlicense: MIT\n---\nbody';
    const parsed = parseSkillMarkdown(md);
    expect(parsed.name).toBe('pdf');
    // Only name/description are mapped; the rest are dropped.
    expect(parsed.description).toBe('');
    expect(parsed.instructions).toBe('body');
  });
});

describe('slugifySkillId', () => {
  it('keeps a valid id unchanged', () => {
    expect(slugifySkillId('pdf_workflows')).toBe('pdf_workflows');
  });

  it('converts hyphens to underscores', () => {
    expect(slugifySkillId('pdf-tools')).toBe('pdf_tools');
  });

  it('lowercases and collapses non-word runs', () => {
    expect(slugifySkillId('PDF  Tools!!')).toBe('pdf_tools');
  });

  it('prefixes when it would not start with a letter', () => {
    expect(slugifySkillId('123-skill')).toBe('s_123_skill');
  });

  it('trims surrounding underscores', () => {
    expect(slugifySkillId('--edge--')).toBe('edge');
  });

  it('clamps to 50 characters without a trailing underscore', () => {
    const slug = slugifySkillId('a'.repeat(60));
    expect(slug.length).toBeLessThanOrEqual(50);
    expect(slug.endsWith('_')).toBe(false);
  });
});

describe('isValidSkillId', () => {
  it.each(['abc', 'pdf_workflows', 'a12', 'a'.repeat(50)])(
    'accepts %s',
    (id) => expect(isValidSkillId(id)).toBe(true)
  );

  it.each(['ab', '1skill', 'Skill', 'skill-1', '', 'a'.repeat(51)])(
    'rejects %s',
    (id) => expect(isValidSkillId(id)).toBe(false)
  );
});
