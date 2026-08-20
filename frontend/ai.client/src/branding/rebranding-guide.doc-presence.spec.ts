// rebranding-guide.doc-presence.spec.ts
//
// Doc-presence test for the Rebranding_Documentation's Non-Goals section
// (Requirement 9). Reads the actual README.md content and asserts, via
// case-insensitive substring/regex checks, that each non-goal called out
// by requirements.md 9.1-9.5 is documented. Assertions are written against
// key terms rather than exact wording so the guide's prose can evolve
// without breaking this test. See requirements.md Requirement 9.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const SPEC_DIR = dirname(fileURLToPath(import.meta.url));
const README_PATH = resolve(SPEC_DIR, './README.md');

const readmeContent = readFileSync(README_PATH, 'utf8');

describe('Rebranding_Documentation Non-Goals section (Requirement 9)', () => {
  // Feature: branding-customization
  // Validates: Requirements 9.1
  it('contains a heading with the term "Non-Goals"', () => {
    expect(readmeContent).toMatch(/^#+.*Non-Goals/im);
  });

  // Feature: branding-customization
  // Validates: Requirements 9.2
  it('states that an in-app admin UI for editing branding is out of scope, deferred to "Option 2"', () => {
    expect(readmeContent).toMatch(/admin ui/i);
    expect(readmeContent).toMatch(/option 2/i);
  });

  // Feature: branding-customization
  // Validates: Requirements 9.3
  it('states that backend persistence of branding values is out of scope', () => {
    expect(readmeContent).toMatch(/backend persistence/i);
  });

  // Feature: branding-customization
  // Validates: Requirements 9.4
  it('states that runtime logo uploads are out of scope', () => {
    expect(readmeContent).toMatch(/runtime logo uploads?/i);
  });

  // Feature: branding-customization
  // Validates: Requirements 9.5
  it('states that runtime color overrides and runtime branding overrides are out of scope', () => {
    expect(readmeContent).toMatch(/runtime color overrides?/i);
    expect(readmeContent).toMatch(/runtime branding overrides?/i);
  });
});
