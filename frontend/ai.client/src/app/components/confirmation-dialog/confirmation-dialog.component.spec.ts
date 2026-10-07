import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import { ConfirmationDialogComponent, ConfirmationDialogData } from './confirmation-dialog.component';

describe('ConfirmationDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  function open(data: ConfirmationDialogData) {
    return openInCdkDialog<ConfirmationDialogComponent, ConfirmationDialogData, boolean>(ConfirmationDialogComponent, {
      data,
    });
  }

  it('is an alertdialog named by its title and described by its message', async () => {
    const { container } = await open({ title: 'Archive project', message: 'Members lose access.' });
    expectNamedDialog(container, { role: 'alertdialog', name: 'Archive project', description: 'Members lose access.' });
  });

  it('marks Cancel as the initial focus, and shows the danger badge only when destructive', async () => {
    const plain = await open({ title: 'Archive project', message: 'Members lose access.', cancelText: 'Keep it' });
    expect(plain.container.querySelector('[cdkFocusInitial]')?.textContent?.trim()).toBe('Keep it');
    expect(plain.container.querySelector('[dialogIcon]')).toBeNull();
    plain.ref.close();

    const destructive = await open({ title: 'Delete', message: 'Gone for good.', destructive: true });
    const badge = destructive.container.querySelector('[dialogIcon]');
    expect(badge?.nextElementSibling?.querySelector('h2')).not.toBeNull();
  });

  it('closes true on confirm and false on cancel or Escape', async () => {
    const confirm = await open({ title: 'Archive', message: 'm', confirmText: 'Archive' });
    let result: boolean | undefined;
    confirm.ref.closed.subscribe(r => (result = r));
    const buttons = Array.from(confirm.container.querySelectorAll<HTMLButtonElement>('[dialogFooter] button'));
    buttons.find(b => b.textContent?.trim() === 'Archive')!.click();
    expect(result).toBe(true);

    const cancel = await open({ title: 'Archive', message: 'm' });
    cancel.ref.closed.subscribe(r => (result = r));
    cancel.container
      .querySelector('app-dialog-shell')!
      .dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(result).toBe(false);
  });
});
