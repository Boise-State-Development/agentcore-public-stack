import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import {
  DeclineSubmissionDialogComponent,
  DeclineSubmissionDialogData,
  DeclineSubmissionDialogResult,
} from './decline-submission-dialog.component';

describe('DeclineSubmissionDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  it('names the dialog from its title, describes it, and starts on the reason field', async () => {
    const { container } = await openInCdkDialog<
      DeclineSubmissionDialogComponent,
      DeclineSubmissionDialogData,
      DeclineSubmissionDialogResult
    >(DeclineSubmissionDialogComponent, { data: { name: 'Grader', ownerName: 'Sam' } });
    expectNamedDialog(container, { name: 'Decline this submission', description: /^Grader will not go into the store/ });
    expect(container.querySelector('[cdkFocusInitial]')?.id).toBe('decline-reason');
  });
});
