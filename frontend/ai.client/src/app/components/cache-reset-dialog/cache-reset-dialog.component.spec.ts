import { afterEach, describe, expect, it } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import {
  CacheResetDialogComponent,
  CacheResetDialogData,
  CacheResetDialogResult,
} from './cache-reset-dialog.component';

const DATA: CacheResetDialogData = {
  title: 'Switch model?',
  message: 'This conversation is cached for Alpha.',
  confirmText: 'Switch model',
};

async function open() {
  return openInCdkDialog<CacheResetDialogComponent, CacheResetDialogData, CacheResetDialogResult>(
    CacheResetDialogComponent,
    { data: DATA },
  );
}

function button(container: HTMLElement, text: string): HTMLButtonElement {
  return Array.from(container.querySelectorAll('button')).find(b => b.textContent?.trim() === text)!;
}

describe('CacheResetDialogComponent', () => {
  afterEach(() => TestBed.inject(Dialog).closeAll());

  it('is an alertdialog named by its title and described by its message', async () => {
    const { container } = await open();
    expectNamedDialog(container, { name: 'Switch model?', role: 'alertdialog', description: /cached for Alpha/ });
  });

  it('confirms without opting out by default', async () => {
    const { ref, container } = await open();
    const closed = firstValueFrom(ref.closed);
    button(container, 'Switch model').click();
    expect(await closed).toEqual({ dontAskAgain: false });
  });

  it('carries "Don\'t ask again" with the confirmation', async () => {
    const { ref, container } = await open();
    const closed = firstValueFrom(ref.closed);
    const checkbox = container.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    checkbox.click();
    button(container, 'Switch model').click();
    expect(await closed).toEqual({ dontAskAgain: true });
  });

  it('closes with undefined on Cancel, even with the box ticked', async () => {
    const { ref, container } = await open();
    const closed = firstValueFrom(ref.closed);
    container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
    button(container, 'Cancel').click();
    expect(await closed).toBeUndefined();
  });
});
