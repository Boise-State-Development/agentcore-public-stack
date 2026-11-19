import { describe, it, expect } from 'vitest';
import { HttpErrorResponse } from '@angular/common/http';
import { skillExportErrorMessage, skillExportFilename, skillExportSlug } from './skill-export';

describe('skill export helpers', () => {
  it('slugs a skill id the way the backend names the bundle', () => {
    expect(skillExportSlug('pdf_workflows')).toBe('pdf-workflows');
    expect(skillExportSlug('__A__b__')).toBe('a-b');
    expect(skillExportSlug('___')).toBe('skill');
    expect(skillExportSlug('x'.repeat(80))).toHaveLength(64);
    expect(skillExportFilename('contract_redline')).toBe('contract-redline.zip');
  });

  it('reads the detail out of a Blob error body', async () => {
    const err = new HttpErrorResponse({
      status: 409,
      error: new Blob([JSON.stringify({ detail: "The skill's file 'a.md' could not be read" })]),
    });
    expect(await skillExportErrorMessage(err)).toBe("The skill's file 'a.md' could not be read");
  });

  it('falls back when there is no readable detail', async () => {
    const err = new HttpErrorResponse({ status: 500, error: new Blob(['oops']) });
    expect(await skillExportErrorMessage(err)).toContain("Couldn't download the skill");
    expect(await skillExportErrorMessage(new Error('x'))).toContain("Couldn't download the skill");
  });
});
