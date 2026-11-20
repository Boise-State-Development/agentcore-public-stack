import { Dialog } from '@angular/cdk/dialog';
import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ImportPreview, MySkill, SkillImportSource } from '../models/my-skill.model';
import { MySkillService } from '../services/my-skill.service';
import { SkillBundleImportComponent } from './skill-bundle-import.component';

function preview(overrides: Partial<ImportPreview> = {}): ImportPreview {
  return {
    source: {
      type: 'github',
      path: 'skills/redline',
      bundleHash: 'hash-1',
      importedAt: '2026-11-18T00:00:00+00:00',
      url: 'https://github.com/acme/skills/tree/main/skills/redline',
    },
    candidates: [],
    skill: {
      name: 'contract-redline',
      storedName: 'contract-redline',
      displayName: 'Contract redline',
      description: 'Redline a contract.',
      instructionsLength: 120,
      allowedTools: [],
    },
    changes: null,
    files: [
      { path: 'references/nda.md', status: 'added', filename: 'nda.md', kind: 'reference', size: 3, reason: null },
      { path: 'assets/x.svg', status: 'skipped', filename: null, kind: null, size: 6, reason: 'file type not allowed (.svg)' },
    ],
    bundleHash: 'hash-1',
    targetUpdatedAt: null,
    ...overrides,
  };
}

const SKILL = { skillId: 'contract_redline' } as MySkill;

describe('SkillBundleImportComponent', () => {
  afterEach(() => TestBed.resetTestingModule());

  async function render(
    service: Partial<Record<keyof MySkillService, unknown>>,
    inputs: { skillId?: string | null; importSource?: SkillImportSource | null } = {},
    extraProviders: unknown[] = [],
  ) {
    TestBed.configureTestingModule({
      imports: [SkillBundleImportComponent],
      providers: [
        {
          provide: MySkillService,
          useValue: { describeError: (_: unknown, fallback: string) => fallback, ...service },
        },
        ...(extraProviders as never[]),
      ],
    });
    const fixture = TestBed.createComponent(SkillBundleImportComponent);
    fixture.componentRef.setInput('skillId', inputs.skillId ?? null);
    fixture.componentRef.setInput('importSource', inputs.importSource ?? null);
    const emitted: MySkill[] = [];
    fixture.componentInstance.imported.subscribe((s) => emitted.push(s));
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const settle = async () => {
      for (let i = 0; i < 3; i++) {
        await new Promise((resolve) => setTimeout(resolve));
        await fixture.whenStable();
        fixture.detectChanges();
      }
    };
    const button = (label: string) =>
      [...el.querySelectorAll('button')].find((b) => b.textContent?.trim().startsWith(label)) as
        | HTMLButtonElement
        | undefined;
    const typeUrl = (url: string) => {
      const input = el.querySelector('#bundle-url') as HTMLInputElement;
      input.value = url;
      input.dispatchEvent(new Event('input'));
      fixture.detectChanges();
    };
    return { el, settle, button, typeUrl, emitted };
  }

  it('previews a GitHub URL, shows each file and why one was skipped, then imports with the hash', async () => {
    const previewImport = vi.fn().mockResolvedValue(preview());
    const importSkill = vi.fn().mockResolvedValue({ skill: SKILL, files: [] });
    const r = await render({ previewImport, importSkill });

    r.typeUrl('https://github.com/acme/skills/tree/main/skills/redline');
    r.button('Preview')!.click();
    await r.settle();

    expect(previewImport).toHaveBeenCalledWith(
      { url: 'https://github.com/acme/skills/tree/main/skills/redline', path: null },
      null,
    );
    expect(r.el.textContent).toContain('references/nda.md');
    expect(r.el.textContent).toContain('file type not allowed (.svg)');
    expect((r.el.querySelector('#bundle-name') as HTMLInputElement).value).toBe('Contract redline');

    r.button('Import skill')!.click();
    await r.settle();

    expect(importSkill).toHaveBeenCalledWith(
      { url: 'https://github.com/acme/skills/tree/main/skills/redline', path: 'skills/redline' },
      { displayName: 'Contract redline', expectedBundleHash: 'hash-1' },
    );
    expect(r.emitted).toEqual([SKILL]);
  });

  it('asks which skill when the archive holds several, and previews the one picked', async () => {
    const previewImport = vi
      .fn()
      .mockResolvedValueOnce(preview({ candidates: ['skills/docx', 'skills/pdf'], skill: null, files: [] }))
      .mockResolvedValueOnce(preview());
    const r = await render({ previewImport });

    r.typeUrl('https://github.com/acme/skills');
    r.button('Preview')!.click();
    await r.settle();
    expect(r.el.textContent).toContain('This holds 2 skills');

    r.button('skills/pdf')!.click();
    await r.settle();
    expect(previewImport).toHaveBeenLastCalledWith(
      { url: 'https://github.com/acme/skills', path: 'skills/pdf' },
      null,
    );
  });

  it('on a re-import, warns about deletions and sends the preview hash and updatedAt', async () => {
    const source: SkillImportSource = preview().source;
    const previewImport = vi.fn().mockResolvedValue(
      preview({
        changes: { description: false, instructions: true, allowedTools: false, skillMetadata: false },
        files: [
          { path: 'references/nda.md', status: 'changed', filename: 'nda.md', kind: 'reference', size: 3, reason: null },
          { path: 'scripts/old.py', status: 'removed', filename: 'old.py', kind: 'script', size: 3, reason: null },
        ],
        targetUpdatedAt: '2026-11-18T01:00:00+00:00',
      }),
    );
    const reimportSkill = vi.fn().mockResolvedValue({ skill: SKILL, files: [] });
    const r = await render({ previewImport, reimportSkill }, { skillId: 'contract_redline', importSource: source });

    r.button('Check GitHub for changes')!.click();
    await r.settle();

    expect(previewImport).toHaveBeenCalledWith({ url: source.url, path: 'skills/redline' }, 'contract_redline');
    expect(r.el.textContent).toContain('Changes: instructions.');
    expect(r.el.textContent).toContain('1 file is not in the bundle and will be deleted');
    expect(r.el.textContent).toContain('keep their current version until someone clicks Update');

    r.button('Re-import')!.click();
    await r.settle();
    expect(reimportSkill).toHaveBeenCalledWith(
      'contract_redline',
      { url: source.url, path: 'skills/redline' },
      { expectedBundleHash: 'hash-1', expectedUpdatedAt: '2026-11-18T01:00:00+00:00' },
    );
    expect(r.emitted).toEqual([SKILL]);
  });

  it('says a re-import is already up to date and offers nothing to apply', async () => {
    const previewImport = vi.fn().mockResolvedValue(
      preview({
        changes: { description: false, instructions: false, allowedTools: false, skillMetadata: false },
        files: [{ path: 'references/nda.md', status: 'unchanged', filename: 'nda.md', kind: 'reference', size: 3, reason: null }],
      }),
    );
    const r = await render({ previewImport }, { skillId: 'contract_redline', importSource: preview().source });

    r.button('Check GitHub for changes')!.click();
    await r.settle();

    expect(r.el.textContent).toContain('Already up to date.');
    expect(r.button('Re-import')!.disabled).toBe(true);
  });

  it("shows the backend's message when the preview fails", async () => {
    const previewImport = vi.fn().mockRejectedValue({ error: { detail: 'not public' } });
    const r = await render({
      previewImport,
      describeError: (err: { error?: { detail?: string } }) => err.error?.detail ?? 'x',
    });

    r.typeUrl('https://github.com/acme/private');
    r.button('Preview')!.click();
    await r.settle();

    expect(r.el.querySelector('[role="alert"]')?.textContent).toContain('not public');
  });

  it('shows a refresh diff as text with markers, and the commit it moves between', async () => {
    const previewImport = vi.fn().mockResolvedValue(
      preview({
        changes: { description: false, instructions: true, allowedTools: false, skillMetadata: false },
        source: { ...preview().source, commitSha: 'b'.repeat(40) },
        previousCommitSha: 'a'.repeat(40),
        descriptionDiff: { diff: '', truncated: false },
        instructionsDiff: {
          diff: '--- a/SKILL.md\n+++ b/SKILL.md\n@@ -1,3 +1,3 @@\n Line one.\n-Line two.\n+Line <b>2</b>.\n Line three.',
          truncated: false,
        },
      }),
    );
    const r = await render({ previewImport }, { skillId: 'contract_redline', importSource: preview().source });
    r.button('Check GitHub for changes')!.click();
    await r.settle();

    const diffs = r.el.querySelectorAll<HTMLDetailsElement>('[data-testid="import-diff"]');
    expect(diffs.length).toBe(1); // the unchanged description shows nothing
    expect(diffs[0].open).toBe(true);
    expect(diffs[0].querySelector('summary')!.textContent).toContain('1 added, 1 removed');
    const added = diffs[0].querySelector('[data-kind="added"]')!;
    const removed = diffs[0].querySelector('[data-kind="removed"]')!;
    expect(added.textContent).toBe('Added: +Line <b>2</b>.');
    expect(added.querySelector('b')).toBeNull();
    expect(removed.textContent).toBe('Removed: -Line two.');
    expect(r.el.textContent).toContain('aaaaaaa');
    expect(r.el.textContent).toContain('bbbbbbb');
  });

  it('collapses a long diff and says when it was cut', async () => {
    const long = ['--- a/SKILL.md', '+++ b/SKILL.md', '@@ -1,40 +1,40 @@']
      .concat(Array.from({ length: 40 }, (_, i) => `+line ${i}`))
      .join('\n');
    const previewImport = vi.fn().mockResolvedValue(
      preview({
        changes: { description: false, instructions: true, allowedTools: false, skillMetadata: false },
        instructionsDiff: { diff: long, truncated: true },
      }),
    );
    const r = await render({ previewImport }, { skillId: 'contract_redline', importSource: preview().source });
    r.button('Check GitHub for changes')!.click();
    await r.settle();

    const diff = r.el.querySelector<HTMLDetailsElement>('[data-testid="import-diff"]')!;
    expect(diff.open).toBe(false);
    expect(r.el.querySelector('[data-testid="import-diff-truncated"]')).not.toBeNull();
  });

  it('shows the stored name and the diagnostics the preview returned', async () => {
    const previewImport = vi.fn().mockResolvedValue(
      preview({
        skill: { ...preview().skill!, name: 'mcp-builder', storedName: 'mcp-builder-2' },
        diagnostics: [
          { level: 'warning', field: 'name', code: 'stored_name_differs', message: 'Stored as mcp-builder-2.' },
        ],
      }),
    );
    const r = await render({ previewImport });
    r.typeUrl('https://github.com/acme/skills/tree/main/skills/mcp-builder');
    r.button('Preview')!.click();
    await r.settle();

    expect(r.el.textContent).toContain('Activation name: mcp-builder-2');
    const row = r.el.querySelector('[data-testid="skill-md-diagnostics"] li')!;
    expect(row.textContent).toContain('Warning: Stored as mcp-builder-2.');
  });

  describe('a re-import that reaches other people', () => {
    const reach = 'This skill runs unpinned in 1 project ("Legal").';
    const conflict = new HttpErrorResponse({
      status: 409,
      error: { detail: { code: 'skill_reach_unconfirmed', message: 'Confirm', reach } },
    });

    async function reimportWith(confirmed: boolean) {
      const source: SkillImportSource = preview().source;
      const previewImport = vi.fn().mockResolvedValue(
        preview({ changes: { description: false, instructions: true, allowedTools: false, skillMetadata: false } }),
      );
      const reimportSkill = vi
        .fn()
        .mockRejectedValueOnce(conflict)
        .mockResolvedValue({ skill: SKILL, files: [] });
      const dialog = {
        open: vi.fn((_component: unknown, _config: { data: { message: string } }) => ({ closed: of(confirmed) })),
      };
      const r = await render(
        { previewImport, reimportSkill },
        { skillId: 'contract_redline', importSource: source },
        [{ provide: Dialog, useValue: dialog }],
      );
      r.button('Check GitHub for changes')!.click();
      await r.settle();
      r.button('Re-import')!.click();
      await r.settle();
      return { r, reimportSkill, dialog };
    }

    it('shows the reach summary and resubmits with confirmReach on confirm', async () => {
      const { r, reimportSkill, dialog } = await reimportWith(true);
      expect(dialog.open.mock.calls[0][1].data.message).toBe(reach);
      expect(reimportSkill).toHaveBeenCalledTimes(2);
      expect(reimportSkill.mock.calls[1][2]).toEqual(expect.objectContaining({ confirmReach: true }));
      expect(r.emitted).toEqual([SKILL]);
    });

    it('writes nothing and shows no error when the user keeps the current version', async () => {
      const { r, reimportSkill } = await reimportWith(false);
      expect(reimportSkill).toHaveBeenCalledTimes(1);
      expect(r.emitted).toEqual([]);
      expect(r.el.querySelector('[role="alert"]')).toBeNull();
    });
  });
});
