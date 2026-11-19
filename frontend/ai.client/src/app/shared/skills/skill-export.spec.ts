import { describe, it, expect, vi, afterEach } from 'vitest';
import { HttpErrorResponse } from '@angular/common/http';
import {
  SAVE_BLOB_REVOKE_DELAY_MS,
  saveBlob,
  skillExportErrorMessage,
  skillExportFilename,
  skillExportSlug,
} from './skill-export';

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

  describe('saveBlob', () => {
    const original = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };

    afterEach(() => {
      vi.useRealTimers();
      vi.restoreAllMocks();
      URL.createObjectURL = original.create;
      URL.revokeObjectURL = original.revoke;
    });

    it('clicks a link in the document and revokes the URL only after a delay', () => {
      vi.useFakeTimers();
      URL.createObjectURL = vi.fn(() => 'blob:skill');
      const revoke = vi.fn();
      URL.revokeObjectURL = revoke;
      const seen: { download: string; href: string; attached: boolean }[] = [];
      vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
        this: HTMLAnchorElement,
      ) {
        seen.push({ download: this.download, href: this.href, attached: this.isConnected });
      });

      saveBlob(new Blob(['zip']), 'my-skill.zip');

      expect(seen).toEqual([{ download: 'my-skill.zip', href: 'blob:skill', attached: true }]);
      expect(document.querySelector('a[download]')).toBeNull();
      expect(revoke).not.toHaveBeenCalled();
      vi.advanceTimersByTime(SAVE_BLOB_REVOKE_DELAY_MS - 1);
      expect(revoke).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1);
      expect(revoke).toHaveBeenCalledWith('blob:skill');
    });
  });
});
