import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import {
  RequestChangesDialogComponent,
  RequestChangesDialogData,
  RequestChangesDialogResult,
} from './request-changes-dialog.component';

describe('RequestChangesDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  it('names the dialog from its title, describes it, and starts on the reason field', async () => {
    const { container } = await openInCdkDialog<
      RequestChangesDialogComponent,
      RequestChangesDialogData,
      RequestChangesDialogResult
    >(RequestChangesDialogComponent, { data: { listing: { name: 'Grader', ownerName: 'Sam' } } });
    expectNamedDialog(container, { name: 'Request changes', description: /^Returns Grader to Sam\./ });
    expect(container.querySelector('[cdkFocusInitial]')?.id).toBe('change-reason');
  });
});
