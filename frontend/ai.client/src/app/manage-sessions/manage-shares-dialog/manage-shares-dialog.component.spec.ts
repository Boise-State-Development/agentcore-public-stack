import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import { ShareService } from '../../session/services/share/share.service';
import { ToastService } from '../../services/toast/toast.service';
import { ManageSharesDialogComponent, ManageSharesDialogData } from './manage-shares-dialog.component';

describe('ManageSharesDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: ShareService, useValue: { listSharesForSession: vi.fn(async () => ({ shares: [] })) } },
        { provide: ToastService, useValue: { success: vi.fn(), error: vi.fn() } },
      ],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  function open(data: ManageSharesDialogData) {
    return openInCdkDialog<ManageSharesDialogComponent, ManageSharesDialogData, boolean>(ManageSharesDialogComponent, {
      data,
    });
  }

  it('names the dialog from its title and describes it with the conversation', async () => {
    const { container } = await open({ sessionId: 's1', sessionTitle: 'Budget review' });
    expectNamedDialog(container, { name: 'Manage Shared Instances', description: 'Budget review' });
    expect(container.querySelector('[cdkFocusInitial]')).toBeNull();
  });

  it('falls back to "Untitled Conversation" and closes true on Escape', async () => {
    const { ref, container } = await open({ sessionId: 's1', sessionTitle: '' });
    expectNamedDialog(container, { name: 'Manage Shared Instances', description: 'Untitled Conversation' });

    let result: boolean | undefined;
    ref.closed.subscribe(r => (result = r));
    container
      .querySelector('app-dialog-shell')!
      .dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(result).toBe(true);
  });
});
