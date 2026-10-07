import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import { AdminListingRow } from '../models/marketplace.model';
import {
  WithdrawalDecisionDialogComponent,
  WithdrawalDecisionDialogData,
  WithdrawalDecisionDialogResult,
} from './withdrawal-decision-dialog.component';

describe('WithdrawalDecisionDialogComponent', () => {
  const listing = { name: 'Grader', ownerName: 'Sam' } as AdminListingRow;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  function open(decision: WithdrawalDecisionDialogData['decision']) {
    return openInCdkDialog<
      WithdrawalDecisionDialogComponent,
      WithdrawalDecisionDialogData,
      WithdrawalDecisionDialogResult
    >(WithdrawalDecisionDialogComponent, { data: { listing, decision } });
  }

  it('names a grant from its title, describes it, and starts on the note field', async () => {
    const { container } = await open('grant');
    expectNamedDialog(container, { name: 'Grant this withdrawal?', description: /^Grader leaves the store/ });
    expect(container.querySelector('[cdkFocusInitial]')?.id).toBe('withdrawal-note');
  });

  it('names a decline from its title and describes it', async () => {
    const { container } = await open('decline');
    expectNamedDialog(container, { name: 'Decline this withdrawal?', description: /^Grader stays in the store/ });
  });
});
