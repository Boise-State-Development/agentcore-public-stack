import { Dialog } from '@angular/cdk/dialog';
import { HttpErrorResponse } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SkillVersionHistoryComponent } from './skill-version-history.component';
import {
  SkillVersionDetail,
  SkillVersionHistoryApi,
  SkillVersionSummary,
} from './skill-version-history.model';

interface FakeSkill {
  skillId: string;
}

function row(version: number, overrides: Partial<SkillVersionSummary> = {}): SkillVersionSummary {
  return {
    version,
    createdAt: `2026-11-23T10:${String(version).padStart(2, '0')}:00+00:00`,
    trigger: 'save',
    displayName: 'Grant Review',
    fileCount: 0,
    isCurrent: false,
    createdByYou: true,
    ...overrides,
  };
}

function detail(version: number, overrides: Partial<SkillVersionDetail> = {}): SkillVersionDetail {
  return {
    ...row(version),
    description: 'Review a grant.',
    instructions: 'Read it closely.',
    files: [],
    descriptionDiff: { diff: '', truncated: false },
    instructionsDiff: {
      diff: '--- a/SKILL.md\n+++ b/SKILL.md\n@@ -1 +1 @@\n-Skim it.\n+Read it closely.',
      truncated: false,
    },
    fileChanges: [
      { path: 'references/rubric.md', status: 'changed', filename: 'rubric.md', kind: 'reference', size: 3, reason: null },
      { path: 'references/keep.md', status: 'unchanged', filename: 'keep.md', kind: 'reference', size: 3, reason: null },
    ],
    ...overrides,
  };
}

const SKILL: FakeSkill = { skillId: 'grant_review' };

describe('SkillVersionHistoryComponent', () => {
  afterEach(() => TestBed.resetTestingModule());

  async function render(
    api: Partial<Record<keyof SkillVersionHistoryApi<FakeSkill>, unknown>>,
    dialogAnswers: boolean[] = [],
  ) {
    const dialog = {
      open: vi.fn((_component: unknown, _config: { data: { title: string; message: string } }) => ({
        closed: of(dialogAnswers.shift()),
      })),
    };
    TestBed.configureTestingModule({
      imports: [SkillVersionHistoryComponent],
      providers: [{ provide: Dialog, useValue: dialog }],
    });
    const fixture = TestBed.createComponent(SkillVersionHistoryComponent<FakeSkill>);
    fixture.componentRef.setInput('api', {
      describeError: (_: unknown, fallback: string) => fallback,
      ...api,
    });
    fixture.componentRef.setInput('skillId', 'grant_review');
    fixture.componentRef.setInput('updatedAt', '2026-11-23T10:05:00+00:00');
    fixture.componentRef.setInput('currentName', 'Grant Review');
    const emitted: FakeSkill[] = [];
    fixture.componentInstance.restored.subscribe((s) => emitted.push(s));
    const el = fixture.nativeElement as HTMLElement;
    const settle = async () => {
      for (let i = 0; i < 3; i++) {
        await new Promise((resolve) => setTimeout(resolve));
        await fixture.whenStable();
        fixture.detectChanges();
      }
    };
    fixture.detectChanges();
    await settle();
    const button = (label: string) =>
      [...el.querySelectorAll('button')].find((b) => b.textContent?.trim().startsWith(label)) as
        | HTMLButtonElement
        | undefined;
    return { el, settle, button, emitted, dialog, fixture };
  }

  it('lists versions newest first with what cut them, who, and the Current badge', async () => {
    const listVersions = vi.fn().mockResolvedValue({
      versions: [row(3, { isCurrent: true }), row(2, { trigger: 'restore', restoredFrom: 1 }), row(1, { trigger: 'create' })],
    });
    const r = await render({ listVersions });

    expect(listVersions).toHaveBeenCalledWith('grant_review', { limit: 20, before: null });
    const items = [...r.el.querySelectorAll('li')].map((li) => li.textContent?.replace(/\s+/g, ' ').trim());
    expect(items[0]).toContain('Version 3 Current');
    expect(items[0]).toContain('Saved · You');
    expect(items[1]).toContain('Restored from version 1');
    expect(items[2]).toContain('Created');
  });

  it('collapses a run of file changes until it is expanded', async () => {
    const listVersions = vi.fn().mockResolvedValue({
      versions: [row(4, { trigger: 'file' }), row(3, { trigger: 'file' }), row(2, { trigger: 'file' }), row(1)],
    });
    const r = await render({ listVersions });
    const collapsed = r.button('3 file changes')!;
    expect(collapsed.textContent).toContain('versions 2–4');
    collapsed.click();
    await r.settle();
    expect(r.button('Version 4')).toBeTruthy();
    expect(r.button('Version 2')).toBeTruthy();
  });

  it('shows what restoring would change, without unchanged files, and no Restore on Current', async () => {
    const listVersions = vi.fn().mockResolvedValue({ versions: [row(2, { isCurrent: true }), row(1)] });
    const getVersion = vi
      .fn()
      .mockImplementation((_id: string, n: number) =>
        Promise.resolve(n === 2 ? detail(2, { isCurrent: true }) : detail(1)),
      );
    const r = await render({ listVersions, getVersion });

    r.button('Version 1')!.click();
    await r.settle();
    expect(getVersion).toHaveBeenCalledWith('grant_review', 1);
    expect(r.button('Version 1')!.getAttribute('aria-expanded')).toBe('true');
    expect(r.el.textContent).toContain('Restoring version 1 would change:');
    expect(r.el.querySelector('[data-testid="import-diff"]')).not.toBeNull();
    expect(r.el.textContent).toContain('references/rubric.md');
    expect(r.el.textContent).not.toContain('references/keep.md');
    expect(r.button('Restore this version')).toBeTruthy();

    r.button('Version 2')!.click();
    await r.settle();
    expect(r.el.textContent).toContain('This is the skill as it is now.');
    expect(r.button('Restore this version')).toBeUndefined();
    expect(r.button('Download this version')).toBeTruthy();
  });

  it('restores after the confirmation, naming the new number, and emits the skill', async () => {
    const listVersions = vi.fn().mockResolvedValue({ versions: [row(5, { isCurrent: true }), row(4), row(3)] });
    const getVersion = vi.fn().mockResolvedValue(detail(3));
    const restoreVersion = vi.fn().mockResolvedValue({
      skill: SKILL,
      version: row(6, { trigger: 'restore', restoredFrom: 3, isCurrent: true }),
      importSourceRestored: true,
    });
    const r = await render({ listVersions, getVersion, restoreVersion }, [true]);

    r.button('Version 3')!.click();
    await r.settle();
    r.button('Restore this version')!.click();
    await r.settle();

    const config = r.dialog.open.mock.calls[0][1];
    expect(config.data.title).toBe('Restore version 3?');
    expect(config.data.message).toContain('saved as version 6');
    expect(restoreVersion).toHaveBeenCalledWith('grant_review', 3, {
      expectedUpdatedAt: '2026-11-23T10:05:00+00:00',
    });
    expect(r.el.querySelector('[role="status"]')?.textContent).toContain(
      'Restored version 3 as version 6.',
    );
    expect(r.emitted).toEqual([SKILL]);
  });

  it('writes nothing when the confirmation is cancelled', async () => {
    const listVersions = vi.fn().mockResolvedValue({ versions: [row(2, { isCurrent: true }), row(1)] });
    const restoreVersion = vi.fn();
    const r = await render(
      { listVersions, getVersion: vi.fn().mockResolvedValue(detail(1)), restoreVersion },
      [false],
    );
    r.button('Version 1')!.click();
    await r.settle();
    r.button('Restore this version')!.click();
    await r.settle();
    expect(restoreVersion).not.toHaveBeenCalled();
    expect(r.emitted).toEqual([]);
  });

  it('asks for the reach confirmation on a 409 and resubmits with confirmReach', async () => {
    const conflict = new HttpErrorResponse({
      status: 409,
      error: { detail: { code: 'skill_reach_unconfirmed', message: 'Confirm', reach: 'Runs in 1 project.' } },
    });
    const restoreVersion = vi
      .fn()
      .mockRejectedValueOnce(conflict)
      .mockResolvedValue({ skill: SKILL, version: null, importSourceRestored: false });
    const r = await render(
      {
        listVersions: vi.fn().mockResolvedValue({ versions: [row(2, { isCurrent: true }), row(1)] }),
        getVersion: vi.fn().mockResolvedValue(detail(1)),
        restoreVersion,
      },
      [true, true],
    );
    r.button('Version 1')!.click();
    await r.settle();
    r.button('Restore this version')!.click();
    await r.settle();

    expect(r.dialog.open.mock.calls[1][1].data.message).toBe('Runs in 1 project.');
    expect(restoreVersion.mock.calls[1][2]).toEqual(expect.objectContaining({ confirmReach: true }));
    expect(r.el.querySelector('[role="status"]')?.textContent).toContain(
      'Its import source was left as it is now.',
    );
    expect(r.emitted).toEqual([SKILL]);
  });

  it("shows the backend's message when a restore fails", async () => {
    const r = await render(
      {
        listVersions: vi.fn().mockResolvedValue({ versions: [row(2, { isCurrent: true }), row(1)] }),
        getVersion: vi.fn().mockResolvedValue(detail(1)),
        restoreVersion: vi.fn().mockRejectedValue({ error: { detail: 'changed since you opened' } }),
        describeError: (err: { error?: { detail?: string } }) => err.error?.detail ?? 'x',
      },
      [true],
    );
    r.button('Version 1')!.click();
    await r.settle();
    r.button('Restore this version')!.click();
    await r.settle();
    expect(r.el.querySelector('[role="alert"]')?.textContent).toContain('changed since you opened');
  });

  it('loads older versions from the cursor', async () => {
    const listVersions = vi
      .fn()
      .mockResolvedValueOnce({ versions: [row(3, { isCurrent: true }), row(2)], nextCursor: 2 })
      .mockResolvedValueOnce({ versions: [row(1)] });
    const r = await render({ listVersions });
    r.button('Show older versions')!.click();
    await r.settle();
    expect(listVersions).toHaveBeenLastCalledWith('grant_review', { limit: 20, before: 2 });
    expect(r.button('Version 1')).toBeTruthy();
    expect(r.button('Show older versions')).toBeUndefined();
  });

  it('reloads when the live skill changes', async () => {
    const listVersions = vi.fn().mockResolvedValue({ versions: [row(1, { isCurrent: true })] });
    const r = await render({ listVersions });
    r.fixture.componentRef.setInput('updatedAt', '2026-11-23T11:00:00+00:00');
    await r.settle();
    expect(listVersions).toHaveBeenCalledTimes(2);
  });

  it('shows skeleton rows with a status line while the first page loads', async () => {
    const r = await render({ listVersions: vi.fn().mockReturnValue(new Promise(() => undefined)) });
    expect(r.el.querySelector('[role="status"]')?.textContent).toContain('Loading history');
    const skeleton = r.el.querySelector('ul[aria-hidden="true"]')!;
    expect(skeleton.querySelectorAll('li').length).toBe(3);
    expect(skeleton.querySelector('.motion-reduce\\:animate-none')).not.toBeNull();
    expect(r.el.textContent).not.toContain('No versions yet.');
  });

  it('says when there is no history yet', async () => {
    const r = await render({ listVersions: vi.fn().mockResolvedValue({ versions: [] }) });
    expect(r.el.textContent).toContain('No versions yet.');
  });
});
