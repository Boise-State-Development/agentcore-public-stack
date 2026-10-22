import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import { AdminReportRow } from '../models/marketplace.model';
import {
  ResolveReportDialogComponent,
  ResolveReportDialogData,
  ResolveReportDialogResult,
} from './resolve-report-dialog.component';

describe('ResolveReportDialogComponent', () => {
  const report = { agentName: 'Grader' } as AdminReportRow;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  function open(decision: ResolveReportDialogData['decision']) {
    return openInCdkDialog<ResolveReportDialogComponent, ResolveReportDialogData, ResolveReportDialogResult>(
      ResolveReportDialogComponent,
      { data: { report, decision } },
    );
  }

  it('names the dialog for the decision, describes it, and starts on the note', async () => {
    const resolve = await open('resolve');
    expectNamedDialog(resolve.container, {
      name: 'Resolve this report?',
      description: /^Resolving takes it off the queue/,
    });
    expect(resolve.container.querySelector('[cdkFocusInitial]')?.id).toBe('resolution-note');
    resolve.ref.close();

    const dismiss = await open('dismiss');
    expectNamedDialog(dismiss.container, {
      name: 'Dismiss this report?',
      description: /^Dismissing takes it off the queue/,
    });
  });
});
