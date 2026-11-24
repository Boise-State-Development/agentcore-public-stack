import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { HttpErrorResponse } from '@angular/common/http';
import { of } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import { ScriptReviewSheetComponent, ScriptReviewSheetData } from './components/script-review-sheet.component';
import { resourcePathBelowKind, saveWithScriptReview } from './script-review';
import {
  SkillScriptStatus,
  UnreviewedScriptSkill,
  bindingScriptsNote,
  scriptsProvenanceLine,
  unreviewedScriptsFrom,
} from './script-review.model';

const HASH = '3f2a9c1e' + 'a'.repeat(56);

const SKILL: UnreviewedScriptSkill = {
  skillId: 'pdf',
  displayName: 'PDF',
  scriptsHash: HASH,
  version: 3,
  fromVersion: null,
  reviewedAt: null,
  provenance: { type: 'github', owner: 'anthropics', repo: 'skills', commitSha: 'e4bb5d9aa' },
  author: 'Pat',
  scripts: [
    { path: 'scripts/fill_fillable_fields.py', size: 4096, change: 'new' },
    { path: 'scripts/extract_form_field_info.py', size: 3000, change: 'new' },
  ],
  files: [{ path: 'references/forms.md', size: 900 }],
};

function conflict(skills: UnreviewedScriptSkill[] = [SKILL]): HttpErrorResponse {
  return new HttpErrorResponse({
    status: 409,
    error: { detail: { code: 'skill_scripts_unreviewed', message: 'Review the scripts', skills } },
  });
}

describe('unreviewedScriptsFrom', () => {
  it('reads the skills from the 409 and nothing else', () => {
    expect(unreviewedScriptsFrom(conflict())).toEqual([SKILL]);
    expect(unreviewedScriptsFrom(new HttpErrorResponse({ status: 409, error: { detail: 'Busy' } }))).toBeNull();
    expect(
      unreviewedScriptsFrom(new HttpErrorResponse({ status: 409, error: { detail: { code: 'skill_reach_unconfirmed' } } })),
    ).toBeNull();
    expect(unreviewedScriptsFrom(new HttpErrorResponse({ status: 400 }))).toBeNull();
    expect(unreviewedScriptsFrom(new Error('x'))).toBeNull();
  });
});

describe('the copy', () => {
  const status = (over: Partial<SkillScriptStatus>): SkillScriptStatus => ({
    skillId: 'pdf',
    displayName: 'PDF',
    version: 3,
    scriptsHash: HASH,
    state: 'allowed',
    method: 'review',
    author: 'Pat',
    reviewer: 'Pat',
    reviewedAt: '2026-11-23T10:00:00Z',
    provenance: { type: 'zip' },
    ...over,
  });
  const scope = 'everyone who uses Forms';

  it('says on each binding row whether its scripts run for other people (§8.5)', () => {
    expect(bindingScriptsNote(status({}), scope)?.text).toBe('Scripts allowed · version 3f2a9c1e · Nov 23, 2026');
    expect(bindingScriptsNote(status({ state: 'authored', provenance: null }), scope)?.text).toBe(
      'Scripts run for everyone who uses Forms · version 3f2a9c1e',
    );
    expect(bindingScriptsNote(status({ scriptsChanged: true }), scope)).toEqual({
      text: 'Scripts changed since your review',
      tone: 'warning',
      review: false,
    });
    expect(bindingScriptsNote(status({ state: 'not_allowed' }), scope)).toEqual({
      text: 'Scripts not allowed',
      tone: 'warning',
      review: true,
    });
    expect(bindingScriptsNote(status({ state: 'off_by_admin' }), scope)?.text).toBe(
      'Scripts turned off by an administrator',
    );
    expect(bindingScriptsNote(status({ state: 'admin' }), scope)).toBeNull();
  });

  it('names where the scripts came from', () => {
    expect(scriptsProvenanceLine(SKILL)).toBe('Imported from anthropics/skills @ e4bb5d9 by Pat');
    expect(scriptsProvenanceLine({ provenance: { type: 'zip' }, author: 'Pat' })).toBe('Imported from a zip file by Pat');
    expect(scriptsProvenanceLine({ provenance: null, author: 'Pat' })).toBe('Written by Pat');
  });

  it('turns a bundle path into the My Skills resource path', () => {
    expect(resourcePathBelowKind('scripts/fill.py')).toBe('fill.py');
    expect(resourcePathBelowKind('references/legal/nda.md')).toBe('legal/nda.md');
  });
});

describe('saveWithScriptReview', () => {
  const scope = { name: 'Forms', kind: 'agent' as const };

  function dialogAnswering(...answers: Array<'allow' | 'decline' | undefined>) {
    const open = vi.fn();
    for (const answer of answers) open.mockReturnValueOnce({ closed: of(answer) });
    return { open } as unknown as Dialog & { open: ReturnType<typeof vi.fn> };
  }

  it('sends the save once when nothing needs a review', async () => {
    const attempt = vi.fn().mockResolvedValue('saved');
    const dialog = dialogAnswering();
    expect(await saveWithScriptReview(dialog, scope, attempt)).toBe('saved');
    expect(attempt).toHaveBeenCalledWith({});
    expect(dialog.open).not.toHaveBeenCalled();
  });

  it('Allow resends with the exact hash; Add without scripts resends it declined', async () => {
    const attempt = vi.fn().mockRejectedValueOnce(conflict()).mockResolvedValue('saved');
    expect(await saveWithScriptReview(dialogAnswering('allow'), scope, attempt)).toBe('saved');
    expect(attempt).toHaveBeenLastCalledWith({ scriptsReviewed: { pdf: HASH } });

    const declined = vi.fn().mockRejectedValueOnce(conflict()).mockResolvedValue('saved');
    expect(await saveWithScriptReview(dialogAnswering('decline'), scope, declined)).toBe('saved');
    expect(declined).toHaveBeenLastCalledWith({ scriptsDeclined: ['pdf'] });
  });

  it('a raced hash asks again with the new set', async () => {
    const raced = { ...SKILL, scriptsHash: 'b'.repeat(64) };
    const attempt = vi
      .fn()
      .mockRejectedValueOnce(conflict())
      .mockRejectedValueOnce(conflict([raced]))
      .mockResolvedValue('saved');
    expect(await saveWithScriptReview(dialogAnswering('allow', 'allow'), scope, attempt)).toBe('saved');
    expect(attempt).toHaveBeenLastCalledWith({ scriptsReviewed: { pdf: 'b'.repeat(64) } });
  });

  it('Cancel saves nothing; on an Update, keeping the reviewed version saves nothing either', async () => {
    const attempt = vi.fn().mockRejectedValue(conflict());
    expect(await saveWithScriptReview(dialogAnswering(undefined), scope, attempt)).toBeNull();
    expect(await saveWithScriptReview(dialogAnswering('decline'), scope, attempt, { isUpdate: true })).toBeNull();
    expect(attempt).toHaveBeenCalledTimes(2);
  });

  it('rethrows anything that is not the review 409', async () => {
    const err = new HttpErrorResponse({ status: 403, error: { detail: 'No' } });
    await expect(saveWithScriptReview(dialogAnswering(), scope, vi.fn().mockRejectedValue(err))).rejects.toBe(err);
  });

  it('carries reviews allowed earlier (a row Review) on the first send', async () => {
    const attempt = vi.fn().mockResolvedValue('saved');
    await saveWithScriptReview(dialogAnswering(), scope, attempt, { initial: { scriptsReviewed: { pdf: HASH } } });
    expect(attempt).toHaveBeenCalledWith({ scriptsReviewed: { pdf: HASH } });
  });
});

describe('ScriptReviewSheetComponent', () => {
  afterEach(() => TestBed.inject(Dialog).closeAll());

  async function open(data: Partial<ScriptReviewSheetData> = {}) {
    return openInCdkDialog<ScriptReviewSheetComponent, ScriptReviewSheetData, string>(ScriptReviewSheetComponent, {
      data: { skill: SKILL, scopeName: 'Forms', scope: 'agent', ...data },
    });
  }

  const text = (el: HTMLElement) => (el.textContent ?? '').replace(/\s+/g, ' ');
  const button = (el: HTMLElement, name: string) =>
    [...el.querySelectorAll('button')].find((b) => b.textContent!.trim() === name)!;

  it('names the skill, says who can run the scripts, and lists them', async () => {
    const { container } = await open();
    expectNamedDialog(container, {
      name: 'Review the scripts in PDF',
      description: /Anyone who uses Forms can run these scripts without being asked first/,
    });
    expect(text(container)).toContain('Imported from anthropics/skills @ e4bb5d9 by Pat');
    expect(text(container)).toContain('Version 3f2a9c1e');
    expect(text(container)).toContain('scripts/fill_fillable_fields.py');
    expect(text(container)).toContain('references/forms.md');
    expect(button(container, 'Add without scripts')).toBeTruthy();
  });

  it('in a project, speaks of its members', async () => {
    const { container } = await open({ scope: 'project', scopeName: 'Grants' });
    expect(text(container)).toContain('Anyone in Grants can run these scripts');
  });

  it('closes with what was chosen', async () => {
    const { ref, container } = await open();
    const closed = vi.fn();
    ref.closed.subscribe(closed);
    button(container, 'Allow scripts').click();
    expect(closed).toHaveBeenCalledWith('allow');
  });

  it('on an Update, heads with what changed, marks each script and collapses the unchanged', async () => {
    const skill: UnreviewedScriptSkill = {
      ...SKILL,
      fromVersion: 2,
      reviewedAt: '2026-11-01T10:00:00Z',
      scripts: [
        { path: 'scripts/a.py', size: 10, change: 'new' },
        { path: 'scripts/b.py', size: 10, change: 'changed' },
        { path: 'scripts/c.py', size: 10, change: 'same' },
        { path: 'scripts/d.py', size: 10, change: 'removed' },
      ],
    };
    const { container } = await open({ skill, isUpdate: true });
    expectNamedDialog(container, { name: '3 scripts changed since you reviewed them on Nov 1, 2026' });
    expect(text(container)).toContain('everyone keeps running the version you reviewed');
    expect(text(container)).toContain('New');
    expect(text(container)).toContain('Changed');
    expect(text(container)).toContain('Removed');
    expect(text(container)).not.toContain('scripts/c.py');
    expect(text(container)).toContain('1 unchanged');
    expect(button(container, 'Keep the reviewed version')).toBeTruthy();
  });

  it('View reads the file and shows its source', async () => {
    const readFile = vi.fn().mockResolvedValue('print("hello")');
    const { container } = await open({ readFile });
    container.querySelector<HTMLButtonElement>('button[aria-label="View scripts/fill_fillable_fields.py"]')!.click();
    await new Promise((r) => setTimeout(r, 0));
    TestBed.tick();
    expect(readFile).toHaveBeenCalledWith('pdf', 'scripts/fill_fillable_fields.py');
    expect(container.querySelector('pre')?.textContent).toBe('print("hello")');
  });
});
