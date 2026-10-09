import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import {
  DeleteModelDialogComponent,
  DeleteModelDialogData,
  DeleteModelDialogResult,
} from './delete-model-dialog.component';

describe('DeleteModelDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  function open() {
    return openInCdkDialog<DeleteModelDialogComponent, DeleteModelDialogData, DeleteModelDialogResult>(
      DeleteModelDialogComponent,
      { data: { modelId: 'us.anthropic.claude-opus-5-5', modelName: 'Claude Opus 5.5' } },
    );
  }

  it('is an alertdialog named by the model, described by the consequence, starting on Cancel', async () => {
    const { container } = await open();
    expectNamedDialog(container, {
      role: 'alertdialog',
      name: 'Delete Claude Opus 5.5?',
      description: /^This removes the model from the catalog/,
    });
    expect(container.querySelector('[cdkFocusInitial]')?.textContent?.trim()).toBe('Cancel');
    expect(container.querySelector('[dialogIcon]')).not.toBeNull();
  });

  it('closes true on Delete and undefined on Cancel', async () => {
    const confirm = await open();
    let result: DeleteModelDialogResult | 'open' = 'open';
    confirm.ref.closed.subscribe(r => (result = r));
    const buttons = Array.from(confirm.container.querySelectorAll<HTMLButtonElement>('[dialogFooter] button'));
    buttons.find(b => b.textContent?.trim() === 'Delete model')!.click();
    expect(result).toBe(true);

    const cancel = await open();
    result = 'open';
    cancel.ref.closed.subscribe(r => (result = r));
    cancel.container.querySelector<HTMLButtonElement>('[cdkFocusInitial]')!.click();
    expect(result).toBeUndefined();
  });
});
