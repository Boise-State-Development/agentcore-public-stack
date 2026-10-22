import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import {
  RolePinSaveDialogComponent,
  RolePinSaveDialogData,
  RolePinSaveDialogResult,
} from './role-pin-save-dialog.component';

describe('RolePinSaveDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  function open(removed: string[]) {
    return openInCdkDialog<RolePinSaveDialogComponent, RolePinSaveDialogData, RolePinSaveDialogResult>(
      RolePinSaveDialogComponent,
      { data: { roleLabel: 'Staff', removed } },
    );
  }

  it('is an alertdialog named by what it removes, described by who it reaches, starting on Cancel', async () => {
    const one = await open(['Grader']);
    expectNamedDialog(one.container, {
      role: 'alertdialog',
      name: 'Remove a default pin from Staff?',
      description: /^This unpins for everyone in this role/,
    });
    expect(one.container.querySelector('[cdkFocusInitial]')?.textContent?.trim()).toBe('Cancel');
    one.ref.close();

    const two = await open(['Grader', 'Tutor']);
    expectNamedDialog(two.container, { role: 'alertdialog', name: 'Remove 2 default pins from Staff?' });
  });

  it('closes true on Save and undefined on Cancel', async () => {
    const save = await open(['Grader']);
    let result: RolePinSaveDialogResult | 'open' = 'open';
    save.ref.closed.subscribe(r => (result = r));
    const buttons = Array.from(save.container.querySelectorAll<HTMLButtonElement>('[dialogFooter] button'));
    buttons.find(b => b.textContent?.trim() === 'Save changes')!.click();
    expect(result).toBe(true);

    const cancel = await open(['Grader']);
    result = 'open';
    cancel.ref.closed.subscribe(r => (result = r));
    cancel.container.querySelector<HTMLButtonElement>('[cdkFocusInitial]')!.click();
    expect(result).toBeUndefined();
  });
});
