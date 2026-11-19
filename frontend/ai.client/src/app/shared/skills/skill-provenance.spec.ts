import { HttpErrorResponse } from '@angular/common/http';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import { confirmSkillReach } from './confirm-skill-reach';
import {
  REACH_CONFIRMATION_CODE,
  bindingPinVersion,
  pinnedLabel,
  provenanceLine,
  reachConfirmationFrom,
} from './skill-provenance';

describe('provenanceLine', () => {
  it('names the GitHub repository and the short commit', () => {
    expect(
      provenanceLine({ type: 'github', owner: 'acme', repo: 'skills', commitSha: '0123456789abcdef' }),
    ).toBe('Imported from acme/skills @ 0123456');
  });

  it('leaves the commit out when there is none', () => {
    expect(provenanceLine({ type: 'github', owner: 'acme', repo: 'skills' })).toBe('Imported from acme/skills');
  });

  it('says a zip file for a zip import', () => {
    expect(provenanceLine({ type: 'zip' })).toBe('Imported from a zip file');
  });

  it('is null for a hand-written skill', () => {
    expect(provenanceLine(null)).toBeNull();
    expect(provenanceLine(undefined)).toBeNull();
  });

  it('keeps markup in a third-party name as text', () => {
    expect(provenanceLine({ type: 'github', owner: '<b>x</b>', repo: 'r' })).toBe('Imported from <b>x</b>/r');
  });
});

describe('pins', () => {
  it('reads a whole positive version from a binding config', () => {
    expect(bindingPinVersion({ version: 3 })).toBe(3);
    expect(bindingPinVersion({ version: 0 })).toBeNull();
    expect(bindingPinVersion({ version: '3' })).toBeNull();
    expect(bindingPinVersion(undefined)).toBeNull();
  });

  it('labels a pin', () => {
    expect(pinnedLabel(4)).toBe('Pinned to v4');
  });
});

describe('reachConfirmationFrom', () => {
  const reach = 'This skill runs unpinned in 1 shared agent ("Desk").';

  it('returns the summary of a reach 409', () => {
    const err = new HttpErrorResponse({ status: 409, error: { detail: { code: REACH_CONFIRMATION_CODE, reach } } });
    expect(reachConfirmationFrom(err)).toBe(reach);
  });

  it('ignores any other 409 or error', () => {
    expect(reachConfirmationFrom(new HttpErrorResponse({ status: 409, error: { detail: 'stale' } }))).toBeNull();
    expect(
      reachConfirmationFrom(new HttpErrorResponse({ status: 400, error: { detail: { code: REACH_CONFIRMATION_CODE, reach } } })),
    ).toBeNull();
    expect(reachConfirmationFrom(new Error('x'))).toBeNull();
  });
});

describe('confirmSkillReach', () => {
  afterEach(() => {
    TestBed.inject(Dialog).closeAll();
    TestBed.resetTestingModule();
  });

  async function opened(reach: string) {
    const result = confirmSkillReach(TestBed.inject(Dialog), reach);
    await new Promise((r) => setTimeout(r, 0));
    const container = document.querySelector('.cdk-dialog-container') as HTMLElement;
    return { result, container };
  }

  it('shows the reach summary as text in a named alert dialog and resolves true on Save anyway', async () => {
    const { result, container } = await opened('Runs in <i>"Desk"</i>.');
    expect(container.getAttribute('role')).toBe('alertdialog');
    expect(container.getAttribute('aria-labelledby')).toBeTruthy();
    expect(container.textContent).toContain('Runs in <i>"Desk"</i>.');
    expect(container.querySelector('i')).toBeNull();
    const save = Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Save anyway')!;
    save.click();
    expect(await result).toBe(true);
  });

  it('resolves false when the user keeps editing', async () => {
    const { result, container } = await opened('reach');
    const keep = Array.from(container.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Keep editing')!;
    keep.click();
    expect(await result).toBe(false);
  });
});
