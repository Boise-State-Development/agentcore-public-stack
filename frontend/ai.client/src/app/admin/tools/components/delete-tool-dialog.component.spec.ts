import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import {
  DeleteToolDialogComponent,
  DeleteToolDialogData,
  DeleteToolDialogResult,
} from './delete-tool-dialog.component';

describe('DeleteToolDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  it('is an alertdialog named from its title, described, and starts on Cancel', async () => {
    const { container } = await openInCdkDialog<DeleteToolDialogComponent, DeleteToolDialogData, DeleteToolDialogResult>(
      DeleteToolDialogComponent,
      { data: { toolId: 'web_search', displayName: 'Web Search' } },
    );
    expectNamedDialog(container, {
      name: 'Delete Tool',
      role: 'alertdialog',
      description: /^Are you sure you want to delete Web Search\?/,
    });
    expect(container.querySelector('[cdkFocusInitial]')?.textContent?.trim()).toBe('Cancel');
  });
});
