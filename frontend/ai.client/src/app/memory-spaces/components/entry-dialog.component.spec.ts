import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import { MemorySpaceService } from '../services/memory-space.service';
import { EntryDialogComponent, EntryDialogData, EntryDialogResult } from './entry-dialog.component';

describe('EntryDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        {
          provide: MemorySpaceService,
          useValue: { readEntry: vi.fn(async () => ({ content: '# Jane' })), upsertEntry: vi.fn() },
        },
      ],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  function open(data: EntryDialogData) {
    return openInCdkDialog<EntryDialogComponent, EntryDialogData, EntryDialogResult>(EntryDialogComponent, { data });
  }

  it('creating: is named "New entry" and starts on the slug field', async () => {
    const { container } = await open({ spaceId: 's1', canEdit: true });
    expectNamedDialog(container, { name: 'New entry' });
    expect(container.getAttribute('aria-describedby')).toBeNull();
    expect(container.querySelector('[cdkFocusInitial]')?.id).toBe('entry-slug');
  });

  it('editing: is named "Edit entry" and described by the slug', async () => {
    const { container } = await open({ spaceId: 's1', slug: 'jane-doe', canEdit: true });
    expectNamedDialog(container, { name: 'Edit entry', description: 'jane-doe' });
  });

  it('viewing: is named "Entry" and leaves focus on the close button', async () => {
    const { container } = await open({ spaceId: 's1', slug: 'jane-doe', canEdit: false });
    expectNamedDialog(container, { name: 'Entry', description: 'jane-doe' });
    expect(container.querySelector('[cdkFocusInitial]')).toBeNull();
  });
});
