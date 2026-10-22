import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import { MemorySpaceSummary } from '../models/memory-space.model';
import { MemorySpaceService } from '../services/memory-space.service';
import {
  ShareSpaceDialogComponent,
  ShareSpaceDialogData,
  ShareSpaceDialogResult,
} from './share-space-dialog.component';

const SPACE: MemorySpaceSummary = {
  spaceId: 's1',
  name: 'Chief of Staff',
  template: 'blank',
  role: 'owner',
  ownerId: 'u1',
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
};

describe('ShareSpaceDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [{ provide: MemorySpaceService, useValue: { listShares: vi.fn(async () => ({ members: [] })) } }],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  it('names the dialog from its title, describes it with the space, and starts on the email field', async () => {
    const { container } = await openInCdkDialog<ShareSpaceDialogComponent, ShareSpaceDialogData, ShareSpaceDialogResult>(
      ShareSpaceDialogComponent,
      { data: { space: SPACE } },
    );
    expectNamedDialog(container, { name: 'Share memory space', description: 'Chief of Staff' });
    expect(container.querySelector('[cdkFocusInitial]')?.id).toBe('member-email');
  });
});
