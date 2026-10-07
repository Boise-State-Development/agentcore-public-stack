import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import { AdminListingRow } from '../models/marketplace.model';
import {
  TakedownDialogComponent,
  TakedownDialogData,
  TakedownDialogResult,
} from './takedown-dialog.component';

describe('TakedownDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  it('names the dialog from its title, describes it, and starts on the reason field', async () => {
    const listing = { name: 'Grader', ownerName: 'Sam' } as AdminListingRow;
    const { container } = await openInCdkDialog<TakedownDialogComponent, TakedownDialogData, TakedownDialogResult>(
      TakedownDialogComponent,
      { data: { listing } },
    );
    expectNamedDialog(container, { name: 'Take down “Grader”?', description: /^It leaves the store/ });
    expect(container.querySelector('[cdkFocusInitial]')?.id).toBe('takedown-reason');
  });
});
